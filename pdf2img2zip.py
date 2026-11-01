"""PDF a JPG + ZIP: conversión aislada, archivos en disco y descargas persistentes."""
from __future__ import annotations

import gc
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import quote
import uuid
import zipfile

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DOWNLOADS_DIR = STATIC_DIR / "descargas"
MAX_ZIP_BYTES = 195 * 1024 * 1024
MAX_PAGE_PIXELS = 24_000_000
MAX_WORKER_RSS = 512 * 1024 * 1024
WORKER_STALL_SECONDS = 300
SESSION_SCHEMA = 2
PROFILES = {
    "Alta": (300, 92),
    "Media alta": (200, 88),
    "Media": (150, 85),
    "Media baja": (120, 78),
    "Baja": (96, 70),
}
LOGGER = logging.getLogger("pdf2img2zip")


class ConversionError(Exception):
    """Error que se puede mostrar sin confundirlo con una conversión completa."""


class ZipLimitExceeded(ConversionError):
    def __init__(self, size_bytes: int):
        self.size_bytes = size_bytes
        super().__init__("El ZIP necesita el ajuste de tamaño.")


def limpiar_nombre_archivo(nombre: str) -> str:
    base = str(nombre).replace("\\", "/").rsplit("/", 1)[-1]
    base = Path(base).stem
    base = unicodedata.normalize("NFC", base)
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", base)
    base = base.strip(" .")[:110].rstrip(" .") or "documento"
    if base.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        base = "documento_" + base
    return base


def nombres_unicos(files) -> list[str]:
    usados: set[str] = set()
    nombres = []
    for archivo in files:
        base = limpiar_nombre_archivo(archivo.name)
        candidato = base
        numero = 2
        while candidato.casefold() in usados:
            candidato = f"{base}_{numero}"
            numero += 1
        usados.add(candidato.casefold())
        nombres.append(candidato)
    return nombres


def obtener_parametros_calidad(perfil: str) -> tuple[int, int]:
    if perfil not in PROFILES:
        raise ConversionError("La calidad seleccionada no es válida.")
    return PROFILES[perfil]


def obtener_id_archivo(archivo) -> str:
    # Streamlit asigna un ID diferente a cada carga, incluso con nombre/tamaño iguales.
    file_id = getattr(archivo, "file_id", None)
    if file_id:
        return str(file_id)
    posicion = archivo.tell()
    digest = hashlib.sha256()
    try:
        archivo.seek(0)
        for bloque in iter(lambda: archivo.read(1024 * 1024), b""):
            digest.update(bloque)
    finally:
        archivo.seek(posicion)
    return digest.hexdigest()


def obtener_firma_lote(files, perfil: str) -> str | None:
    if not files:
        return None
    contenido = [perfil, [(obtener_id_archivo(f), f.name, f.size) for f in files]]
    return hashlib.sha256(json.dumps(contenido, ensure_ascii=False).encode()).hexdigest()


def dpi_seguro(width: float, height: float, requested: int, max_pixels: int) -> int:
    if not all(math.isfinite(x) and x > 0 for x in (width, height, requested, max_pixels)):
        raise ConversionError("Una página tiene dimensiones no válidas.")
    dpi = min(requested, max(1, int(math.sqrt(max_pixels / (width * height)) * 72)))
    # get_pixmap redondea el rectángulo hacia afuera; reservamos dos píxeles.
    while dpi > 1 and (math.ceil(width * dpi / 72) + 2) * (math.ceil(height * dpi / 72) + 2) > max_pixels:
        dpi -= 1
    if (math.ceil(width * dpi / 72) + 2) * (math.ceil(height * dpi / 72) + 2) > max_pixels:
        raise ConversionError("La página es demasiado grande incluso a 1 DPI.")
    return dpi


def _render_attempt(input_path: Path, output_dir: Path, nombre_base: str,
                    dpi: int, quality: int, *, split: bool, max_zip_bytes: int,
                    max_pixels: int, progress=None) -> dict:
    # Este import sólo ocurre en el proceso trabajador, nunca en el hilo de Streamlit.
    import pymupdf

    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    parts = []
    archive = None
    current_pages: list[int] = []
    current_path = None
    central_size = 0
    dpi_min = dpi
    adjusted = 0

    def start_part():
        nonlocal archive, current_pages, current_path, central_size
        current_path = output_dir / f"{uuid.uuid4().hex}.zip.part"
        created.append(current_path)
        archive = zipfile.ZipFile(current_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
        current_pages = []
        central_size = 0

    def finish_part():
        nonlocal archive
        archive.close()
        archive = None
        size = current_path.stat().st_size
        if size > max_zip_bytes:
            raise ConversionError("No se pudo preparar una parte ZIP dentro del tamaño permitido.")
        parts.append({"path": str(current_path), "size_bytes": size, "pages": list(current_pages)})

    try:
        with pymupdf.open(input_path) as doc, tempfile.TemporaryDirectory(prefix="pdfzip_pag_") as work:
            if not doc.is_pdf:
                raise ConversionError("El archivo no es un PDF válido.")
            if doc.needs_pass:
                raise ConversionError("El PDF está protegido con contraseña. Sube una copia desbloqueada.")
            total = len(doc)
            if not total:
                raise ConversionError("El PDF no contiene páginas.")
            start_part()
            jpg_path = Path(work) / "pagina.jpg"
            for index in range(total):
                if progress:
                    progress({"page": index, "total": total, "dpi": dpi, "emergency": split})
                page = doc[index]
                actual_dpi = dpi_seguro(page.rect.width, page.rect.height, dpi, max_pixels)
                dpi_min = min(dpi_min, actual_dpi)
                adjusted += actual_dpi != dpi
                pix = None
                try:
                    pix = page.get_pixmap(dpi=actual_dpi, colorspace=pymupdf.csRGB, alpha=False)
                    pix.save(jpg_path, output="jpeg", jpg_quality=quality)
                finally:
                    del pix
                    del page
                entry_name = f"{nombre_base}_pag_{index + 1:03d}.jpg"
                encoded_name_size = len(entry_name.encode("utf-8"))
                # Cabecera local, directorio central y margen para el cierre ZIP64.
                projected = archive.fp.tell() + jpg_path.stat().st_size + 2 * encoded_name_size + 76 + central_size + 128
                if projected > max_zip_bytes:
                    if not split:
                        raise ZipLimitExceeded(projected)
                    if current_pages:
                        finish_part()
                        start_part()
                    alone = jpg_path.stat().st_size + 2 * encoded_name_size + 76 + 128
                    if alone > max_zip_bytes:
                        raise ConversionError("Una sola página excede el tamaño de una parte ZIP.")
                archive.write(jpg_path, arcname=entry_name)
                central_size += 46 + encoded_name_size
                current_pages.append(index + 1)
                jpg_path.unlink()
                # La caché nativa no tiene que conservar imágenes de páginas anteriores.
                pymupdf.TOOLS.store_shrink(100)
                if progress:
                    progress({"page": index + 1, "total": total, "dpi": actual_dpi, "emergency": split})
            finish_part()

        for index, part in enumerate(parts, 1):
            partial = Path(part["path"])
            final = partial.with_suffix("")
            partial.replace(final)
            created.append(final)
            part["path"] = str(final)
            part["file_name"] = f"{nombre_base}.zip" if len(parts) == 1 else f"{nombre_base}_parte_{index:03d}.zip"
        return {"parts": parts, "total_paginas": total, "dpi_min": dpi_min, "paginas_ajustadas": adjusted}
    except BaseException:
        if archive is not None:
            try:
                archive.close()
            except Exception:
                LOGGER.exception("No se pudo cerrar el ZIP parcial durante la limpieza")
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("No se pudo eliminar el archivo parcial %s", path)
        raise


def convertir_pdf(input_path: Path, output_dir: Path, nombre_base: str, perfil: str,
                  progress=None, max_zip_bytes: int = MAX_ZIP_BYTES,
                  max_pixels: int = MAX_PAGE_PIXELS) -> dict:
    """Produce ZIP completos en disco. Nunca retorna el PDF o el ZIP como bytes."""
    dpi, quality = obtener_parametros_calidad(perfil)
    nombre_base = limpiar_nombre_archivo(nombre_base + ".pdf")
    history = []
    try:
        result = _render_attempt(Path(input_path), Path(output_dir), nombre_base, dpi, quality,
                                 split=False, max_zip_bytes=max_zip_bytes,
                                 max_pixels=max_pixels, progress=progress)
        used_profile = perfil
    except ZipLimitExceeded as exc:
        history.append({"perfil": perfil, "dpi": dpi, "jpg_quality": quality,
                        "limite_superado": True, "size_bytes": exc.size_bytes})
        dpi, quality = 72, 55
        used_profile = "Baja de emergencia"
        result = _render_attempt(Path(input_path), Path(output_dir), nombre_base, dpi, quality,
                                 split=True, max_zip_bytes=max_zip_bytes,
                                 max_pixels=max_pixels, progress=progress)
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"No se pudo convertir el PDF: {exc}") from exc
    history.append({"perfil": used_profile, "dpi": dpi, "jpg_quality": quality,
                    "limite_superado": False, "size_bytes": sum(p["size_bytes"] for p in result["parts"])})
    result.update(perfil_usado=used_profile, dpi=dpi, jpg_quality=quality, historial=history)
    return result


def _write_json(path: Path, data: dict):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _worker_main(config_path: str) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    progress_path = Path(config["progress_path"])
    result_path = Path(config["result_path"])
    try:
        result = convertir_pdf(Path(config["input_path"]), Path(config["output_dir"]),
                               config["nombre_base"], config["perfil"],
                               progress=lambda event: _write_json(progress_path, event))
        _write_json(result_path, {"ok": True, "result": result})
        return 0
    except Exception as exc:
        LOGGER.exception("No se pudo convertir el PDF en el proceso aislado")
        message = str(exc) if isinstance(exc, ConversionError) else "No se pudo completar el PDF. Revisa si está dañado o vuelve a intentar con calidad Baja."
        _write_json(result_path, {"ok": False, "error": message})
        return 1


def _worker_rss(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _stop_worker(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def ejecutar_conversion(archivo, output_dir: Path, nombre_base: str, perfil: str,
                        progress=None, heartbeat=None) -> dict:
    """Una caída de MuPDF no derriba el servidor ni detiene los otros PDF."""
    process = None
    completed = False
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="pdfzip_entrada_") as temp:
            work = Path(temp)
            input_path = work / "entrada.pdf"
            position = archivo.tell()
            try:
                archivo.seek(0)
                with input_path.open("wb") as target:
                    for chunk in iter(lambda: archivo.read(1024 * 1024), b""):
                        target.write(chunk)
            finally:
                archivo.seek(position)
            config = {"input_path": str(input_path), "output_dir": str(output_dir),
                      "nombre_base": nombre_base, "perfil": perfil,
                      "progress_path": str(work / "progress.json"),
                      "result_path": str(work / "result.json")}
            _write_json(work / "config.json", config)
            env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            with (work / "worker.log").open("w+", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(work / "config.json")],
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
                last_event = None
                last_progress = time.monotonic()
                while process.poll() is None:
                    if heartbeat:
                        heartbeat()
                    if _worker_rss(process.pid) > MAX_WORKER_RSS:
                        raise ConversionError("Este PDF requiere demasiada memoria. Prueba con calidad Baja o divide el PDF en menos páginas.")
                    if time.monotonic() - last_progress > WORKER_STALL_SECONDS:
                        raise ConversionError("Una página tardó más de 5 minutos sin avanzar. Prueba con calidad Baja o revisa ese PDF.")
                    try:
                        event = json.loads((work / "progress.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        event = None
                    if event is not None and event != last_event:
                        last_event = event
                        last_progress = time.monotonic()
                        if progress:
                            progress(event)
                    time.sleep(0.1)
                try:
                    payload = json.loads((work / "result.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    payload = None
                if process.returncode != 0 or not payload or not payload.get("ok"):
                    log.seek(0)
                    LOGGER.error("Proceso PDF finalizó con código %s: %s", process.returncode, log.read(12000))
                    raise ConversionError((payload or {}).get("error", "El motor de PDF se cerró inesperadamente. Los demás archivos pueden continuar."))
                completed = True
                return payload["result"]
    finally:
        _stop_worker(process)
        if not completed:
            shutil.rmtree(output_dir, ignore_errors=True)


def _static_url(path: Path, base_path: str = "") -> str:
    relative = path.resolve().relative_to(STATIC_DIR.resolve())
    # La ruta pertenece al backend, no a la raíz del portal de Community Cloud.
    # El navegador conserva su prefijo real (/~/+/ en Cloud) al resolverla.
    return "app/static/" + "/".join(quote(part, safe="") for part in relative.parts)


def leer_zip_validado(path: Path, expected_size: int) -> bytes:
    """Respaldo diferido nativo: nunca presentar HTML o un archivo parcial como ZIP."""
    if path.stat().st_size != expected_size:
        raise ConversionError("El ZIP cambió o quedó incompleto. Vuelve a procesar este PDF.")
    with path.open("rb") as source:
        if source.read(4) != b"PK\x03\x04":
            raise ConversionError("El resultado no es un ZIP válido. Vuelve a procesar este PDF.")
    if not zipfile.is_zipfile(path):
        raise ConversionError("No se pudo validar el directorio del ZIP.")
    return path.read_bytes()


# La cola vive en la ventana principal: un rerun no cancela una descarga en curso.
# Sólo recibe metadatos; ningún ZIP se codifica en base64 ni viaja por el websocket.
DOWNLOAD_JS = r"""
(() => {
  if (window.__pdfzipDownloads?.version === 2) return;
  window.__pdfzipDownloads?.cancel?.();
  const states = new Map(), sent = new Set(), pending = [];
  let wanted = new Set(), active = null, running = false;
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  function backendUrl(relative) {
    // La configuración la inyecta Streamlit. Si no existe, usamos la ventana
    // de la propia app: en Cloud es /~/+/, no el portal que la contiene.
    const configured = window.__streamlit?.BACKEND_BASE_URL;
    const base = new URL(configured || window.location.href, window.location.href);
    if (!['http:', 'https:'].includes(base.protocol)) throw new Error('Backend no válido');
    base.search = ''; base.hash = '';
    if (!base.pathname.endsWith('/')) base.pathname += '/';
    return new URL(relative, base);
  }
  async function nativeDownload(file, signal) {
    const selector = '.st-key-native_' + file.id;
    const deadline = Date.now() + 90000;
    let started = false, busySeen = false, observed = null, observer = null;
    try {
      while (Date.now() < deadline) {
        if (signal.aborted || !wanted.has(file.id)) throw new DOMException('Cancelado', 'AbortError');
        const wrapper = document.querySelector(selector);
        const button = wrapper?.querySelector('button');
        if (wrapper?.querySelector('[data-testid="stDownloadButtonError"]')) throw new Error('Falló la descarga nativa');
        if (button && observed !== button) {
          observer?.disconnect(); observed = button;
          observer = new MutationObserver(records => {
            if (records.some(record => record.attributeName === 'disabled' && record.oldValue === null)) busySeen = true;
          });
          observer.observe(button, {attributes: true, attributeFilter: ['disabled'], attributeOldValue: true});
        }
        if (button && !started && !button.disabled) {
          button.click(); started = true;
          states.set(file.id, 'native');
        } else if (started && button) {
          if (button.disabled) busySeen = true;
          if (busySeen && !button.disabled) return;
        }
        await wait(100);
      }
      throw new Error('La descarga nativa no confirmó su preparación');
    } finally { observer?.disconnect(); }
  }
  const alreadySent = id => {
    try { return sent.has(id) || sessionStorage.getItem('pdfzip:' + id) === 'sent'; }
    catch (_) { return sent.has(id); }
  };
  async function run() {
    if (running) return;
    running = true;
    try {
      while (pending.length) {
        const file = pending.shift();
        if (!wanted.has(file.id) || alreadySent(file.id)) continue;
        const controller = new AbortController();
        active = {id: file.id, controller};
        let objectUrl = null;
        try {
          states.set(file.id, 'loading');
          try {
            if (!file.useStatic) throw new Error('Usar transporte nativo');
            const url = backendUrl(file.url);
            const response = await fetch(url.href, {signal: controller.signal, cache: 'no-store', credentials: 'same-origin'});
            if (!response.ok) throw new Error('HTTP ' + response.status);
            if ((response.headers.get('content-type') || '').toLowerCase().includes('text/html')) throw new Error('El servidor devolvió HTML');
            const blob = await response.blob();
            if (blob.size !== file.size) throw new Error('Tamaño incompleto');
            const magic = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
            if (magic[0] !== 80 || magic[1] !== 75 || magic[2] !== 3 || magic[3] !== 4) throw new Error('El archivo no es un ZIP');
            if (!wanted.has(file.id) || controller.signal.aborted) continue;
            objectUrl = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = objectUrl;
            link.download = file.name;
            link.style.display = 'none';
            document.body.appendChild(link);
            link.click();
            link.remove();
          } catch (error) {
            if (error.name === 'AbortError' || controller.signal.aborted || !wanted.has(file.id)) throw error;
            // Streamlit resuelve su /media y el origen de descargas. Este botón
            // permanece renderizado y también sirve como respaldo manual.
            await nativeDownload(file, controller.signal);
          }
          sent.add(file.id);
          try { sessionStorage.setItem('pdfzip:' + file.id, 'sent'); } catch (_) {}
          states.set(file.id, 'sent');
          await wait(1000);
        } catch (error) {
          states.set(file.id, error.name === 'AbortError' ? 'cancelled' : 'error');
        } finally {
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          active = null;
        }
      }
    } finally { running = false; }
  }
  window.__pdfzipDownloads = {
    version: 2,
    cancel() { wanted = new Set(); pending.length = 0; active?.controller.abort(); },
    enqueue(files) {
      wanted = new Set(files.map(file => file.id));
      for (let i = pending.length - 1; i >= 0; i--) if (!wanted.has(pending[i].id)) pending.splice(i, 1);
      if (active && !wanted.has(active.id)) active.controller.abort();
      for (const file of files) {
        if (alreadySent(file.id)) { states.set(file.id, 'sent'); continue; }
        if (active && active.id === file.id || pending.some(item => item.id === file.id)) continue;
        pending.push(file);
        states.set(file.id, 'queued');
      }
      run();
    },
    status(ids) { return ids.map(id => states.get(id) || 'queued'); }
  };
})();
"""


def emitir_descargas(st, parts):
    files = [{"id": p["id"], "url": _static_url(Path(p["path"])),
              "name": p["file_name"], "size": p["size_bytes"],
              "useStatic": bool(st.get_option("server.enableStaticServing"))} for p in parts]
    encoded = json.dumps(files, ensure_ascii=True).replace("<", "\\u003c")
    script = json.dumps(DOWNLOAD_JS).replace("<", "\\u003c")
    frame = """<!doctype html><html lang="es"><meta charset="utf-8"><body style="margin:0;font:13px system-ui;color:#666">
<div id="estado" role="status" aria-live="polite"></div><script>
const files = __FILES__;
try {
  const owner = window.parent;
  if (owner.__pdfzipDownloads?.version !== 2) {
    const script = owner.document.createElement('script');
    script.textContent = __SCRIPT__;
    owner.document.head.appendChild(script);
    script.remove();
  }
  owner.__pdfzipDownloads.enqueue(files);
  function update() {
    const states = owner.__pdfzipDownloads.status(files.map(f => f.id));
    const sent = states.filter(s => s === 'sent').length;
    const failed = states.filter(s => s === 'error').length;
    document.getElementById('estado').textContent = !files.length ? '' : failed
      ? 'No se pudo iniciar alguna descarga. Usa los botones de descarga de abajo.'
      : sent < files.length ? 'Preparando descargas automáticas: ' + sent + ' de ' + files.length
      : 'Descargas solicitadas al navegador: ' + sent + '. Los botones de respaldo siguen disponibles.';
  }
  update(); const timer = setInterval(update, 500);
  window.addEventListener('pagehide', () => clearInterval(timer));
} catch (_) { document.getElementById('estado').textContent = 'Usa los botones de descarga de abajo.'; }
</script></body></html>""".replace("__FILES__", encoded).replace("__SCRIPT__", script)
    st.iframe(frame, height=48 if parts else 1)


def _new_batch(st, signature):
    old_temp = st.session_state.get("pdfzip_temp")
    if old_temp is not None:
        old_temp.cleanup()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state.pdfzip_temp = tempfile.TemporaryDirectory(prefix="s_", dir=DOWNLOADS_DIR)
    st.session_state.pdfzip_signature = signature
    st.session_state.pdfzip_results = {}
    st.session_state.pdfzip_schema = SESSION_SCHEMA
    gc.collect()


def _render_result(st, result):
    with st.container(border=True):
        if result["status"] == "error":
            st.error(f"{result['pdf_name']}: {result['error']}")
            return
        st.success(f"Conversión completada: {result['pdf_name']}")
        dpi_text = str(result["dpi"]) if result["dpi_min"] == result["dpi"] else f"{result['dpi_min']}–{result['dpi']}"
        size_mb = sum(p["size_bytes"] for p in result["parts"]) / (1024 * 1024)
        st.caption(f"{result['total_paginas']} páginas · {result['perfil_usado']} · {dpi_text} DPI · JPG {result['jpg_quality']} · {size_mb:.2f} MiB")
        if result["paginas_ajustadas"]:
            st.info(f"Se ajustó la resolución de {result['paginas_ajustadas']} páginas de gran tamaño para completar la conversión.")
        if len(result["historial"]) > 1:
            st.info("Se usó el ajuste de emergencia (72 DPI, JPG 55) porque el primer ZIP superaba 195 MiB.")
        if len(result["parts"]) > 1:
            st.info(f"El resultado se dividió en {len(result['parts'])} ZIP independientes. Cada uno contiene páginas completas.")
        for part in result["parts"]:
            path = Path(part["path"])
            if not path.is_file():
                st.warning("Este archivo temporal ya no está disponible. Vuelve a procesar el lote.")
            else:
                expected_size = part["size_bytes"]
                st.download_button(f"Descargar {part['file_name']}",
                                   data=lambda path=path, size=expected_size: leer_zip_validado(path, size),
                                   file_name=part["file_name"], mime="application/zip",
                                   key="native_" + part["id"], on_click="ignore")


def main():
    import streamlit as st

    st.set_page_config(page_title="PDF a JPG + ZIP", layout="centered")
    st.title("Convertidor de PDF a JPG")
    st.caption("Convierte cada página en JPG y descarga un ZIP por PDF automáticamente.")
    if "pdfzip_upload_key" not in st.session_state:
        st.session_state.pdfzip_upload_key = 0
    if st.session_state.get("pdfzip_schema") != SESSION_SCHEMA:
        _new_batch(st, None)

    # Un trabajador a la vez para todo el servidor, no uno por cada sesión simultánea.
    @st.cache_resource
    def conversion_slot():
        return threading.BoundedSemaphore(1)

    profile = st.selectbox("Selecciona la calidad", list(PROFILES), index=4)
    uploaded = st.file_uploader("Sube uno o varios archivos PDF", type=["pdf"],
                                accept_multiple_files=True,
                                key=f"pdfzip_upload_{st.session_state.pdfzip_upload_key}")
    auto_area = st.empty()
    with auto_area.container():
        if not uploaded:
            emitir_descargas(st, [])

    signature = obtener_firma_lote(uploaded, profile)
    if signature != st.session_state.pdfzip_signature:
        with auto_area.container():
            emitir_descargas(st, [])
        _new_batch(st, signature)
    if not uploaded:
        st.caption("Selecciona la calidad y sube tus PDF para comenzar. Los ZIP estarán disponibles mientras conserves este lote en la sesión.")
        return

    cols = st.columns(3)
    if cols[0].button("Vaciar lote", width="stretch"):
        with auto_area.container():
            emitir_descargas(st, [])
        _new_batch(st, None)
        st.session_state.pdfzip_upload_key += 1
        st.rerun()
    if cols[1].button("Volver a procesar", width="stretch"):
        with auto_area.container():
            emitir_descargas(st, [])
        _new_batch(st, signature)
        st.rerun()
    results = st.session_state.pdfzip_results
    if cols[2].button("Reintentar fallidos", disabled=not any(r["status"] == "error" for r in results.values()), width="stretch"):
        for key in [key for key, value in results.items() if value["status"] == "error"]:
            del results[key]
        st.rerun()

    names = nombres_unicos(uploaded)
    all_parts = [part for result in results.values() if result["status"] == "ok" for part in result["parts"]]
    with auto_area.container():
        emitir_descargas(st, all_parts)
    st.caption("Si tu navegador solicita permiso para varias descargas, permite las descargas de este sitio. También puedes usar los botones que se conservan debajo.")
    st.subheader("Estado del procesamiento")
    for index in range(len(uploaded)):
        if str(index) in results:
            _render_result(st, results[str(index)])
    pending = next((i for i in range(len(uploaded)) if str(i) not in results), None)
    st.progress(len(results) / len(uploaded), text=f"Archivos procesados: {len(results)} de {len(uploaded)}")
    if pending is None:
        errors = sum(result["status"] == "error" for result in results.values())
        if errors:
            st.warning(f"Lote terminado: {len(results) - errors} PDF convertidos y {errors} con error. Puedes descargar los resultados válidos y reintentar los fallidos.")
        else:
            st.success("Todos los archivos fueron convertidos. Los botones de descarga siguen disponibles.")
        return

    archivo = uploaded[pending]
    status = st.empty()
    bar = st.progress(0, text=f"Preparando {archivo.name}...")
    output_dir = Path(st.session_state.pdfzip_temp.name) / uuid.uuid4().hex
    acquired = False
    slot = conversion_slot()

    def heartbeat():
        # Una llamada Streamlit permite atender cancelaciones/reruns mientras el motor trabaja.
        status.caption(f"Procesando archivo {pending + 1} de {len(uploaded)}: {archivo.name}")

    def progress(event):
        stage = "Ajustando tamaño" if event["emergency"] else "Convirtiendo"
        bar.progress(event["page"] / event["total"], text=f"{stage} {archivo.name}: página {event['page']} de {event['total']}")

    try:
        while not slot.acquire(timeout=0.2):
            status.caption("Esperando turno de conversión. Tus archivos siguen en la cola.")
        acquired = True
        converted = ejecutar_conversion(archivo, output_dir, names[pending], profile, progress, heartbeat)
        for part in converted["parts"]:
            part["id"] = uuid.uuid4().hex
        results[str(pending)] = dict(converted, status="ok", pdf_name=archivo.name)
    except Exception as exc:
        LOGGER.exception("Error al convertir %s", archivo.name)
        results[str(pending)] = {"status": "error", "pdf_name": archivo.name,
                                 "error": str(exc) if isinstance(exc, ConversionError) else "No se pudo completar este PDF. Revisa el log del servidor para más detalle."}
    finally:
        if acquired:
            slot.release()
        if str(pending) not in results:
            shutil.rmtree(output_dir, ignore_errors=True)
        bar.empty()
        status.empty()
    st.rerun()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2]))
    main()
