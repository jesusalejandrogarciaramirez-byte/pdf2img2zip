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
import uuid
import zipfile

DOWNLOADS_DIR = Path(tempfile.gettempdir()) / "pdfzip_descargas"
MAX_ZIP_BYTES = 195 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
MAX_PAGE_PIXELS = 24_000_000
MAX_WORKER_RSS = 512 * 1024 * 1024
WORKER_STALL_SECONDS = 300
SESSION_SCHEMA = 3
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


def construir_mensaje_descarga(parts, batch: str, request=None, enabled: bool = True) -> bytes:
    """Entrega como máximo un bloque del ZIP, identificado por sesión/lote/archivo.

    El cliente sólo puede pedir IDs publicados en este lote, nunca una ruta. Cada
    petición confirma el bloque anterior; no se conserva el ZIP completo en RAM.
    """
    metadata = {"protocol": 3, "batch": batch, "enabled": bool(enabled),
                "files": [{"id": p["id"], "name": p["file_name"], "size": p["size_bytes"]} for p in parts],
                "response": None, "error": None}
    payload = b""
    if enabled and isinstance(request, dict) and request.get("batch") == batch:
        file_id, token, offset = request.get("id"), request.get("token"), request.get("offset")
        try:
            if (not isinstance(file_id, str) or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token)
                    or type(offset) is not int or offset < 0 or offset % DOWNLOAD_CHUNK_BYTES):
                raise ConversionError("Solicitud de descarga no válida. Pulsa Reintentar descarga.")
            part = next((p for p in parts if p["id"] == file_id), None)
            if part is None:
                raise ConversionError("El archivo ya no pertenece a este lote. Vuelve a procesarlo.")
            path, total = Path(part["path"]), part["size_bytes"]
            if offset >= total or path.stat().st_size != total:
                raise ConversionError("El ZIP cambió o quedó incompleto. Vuelve a procesar este PDF.")
            with path.open("rb") as source:
                if offset == 0:
                    if source.read(4) != b"PK\x03\x04" or not zipfile.is_zipfile(source):
                        raise ConversionError("El resultado no es un ZIP válido. Vuelve a procesar este PDF.")
                source.seek(offset)
                payload = source.read(min(DOWNLOAD_CHUNK_BYTES, total - offset))
            if len(payload) != min(DOWNLOAD_CHUNK_BYTES, total - offset):
                raise ConversionError("No se pudo leer el ZIP completo. Vuelve a procesar este PDF.")
            metadata["response"] = {"id": file_id, "token": token, "offset": offset,
                                    "size": len(payload), "total": total,
                                    "last": offset + len(payload) == total}
        except (ConversionError, OSError) as exc:
            payload = b""
            metadata["error"] = {"id": file_id, "token": token,
                                 "message": str(exc) if isinstance(exc, ConversionError) else
                                 "El archivo temporal no está disponible. Vuelve a procesar este PDF."}
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + payload


DOWNLOAD_CSS = """
.downloads {font: inherit; color: inherit;}
.summary {font-size: .9rem; margin: .5rem 0 1rem;}
.file {border: 1px solid #8885; border-radius: .6rem; padding: .8rem; margin: .7rem 0;}
.name {font-weight: 600; overflow-wrap: anywhere;}
.status {font-size: .85rem; opacity: .8; margin: .35rem 0 .6rem;}
.action {display: inline-flex; font: inherit; font-size: .95rem; padding: .4rem .7rem;
  border: 1px solid #8888; border-radius: .45rem; background: transparent; color: inherit;
  text-decoration: none; cursor: pointer; overflow-wrap: anywhere;}
.action:hover {border-color: #f36; color: #f36;}
.action:focus-visible {outline: 2px solid #f36; outline-offset: 2px;}
[hidden] {display: none !important;}
progress {width: 100%; height: .5rem; accent-color: #f36;}
"""


# Componente oficial v2: los bytes viajan por bloques por la conexión de la app.
# El navegador conserva un Blob y un enlace real; no hay rutas /media, estáticos,
# base64, clics sobre widgets ajenos ni acceso al DOM de la ventana superior.
DOWNLOAD_JS = r"""
const CACHE_BYTES = 256 * 1024 * 1024;
const MAX_CHUNK = 4 * 1024 * 1024;
const WAIT_MS = 60000;

function decode(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  if (bytes.byteLength < 4) throw new Error('Mensaje de descarga incompleto.');
  const length = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(0, false);
  if (length < 2 || length > bytes.byteLength - 4) throw new Error('Cabecera de descarga incompleta.');
  const meta = JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length)));
  if (meta.protocol !== 3 || typeof meta.batch !== 'string' || !Array.isArray(meta.files))
    throw new Error('Formato de descarga no compatible. Recarga la página.');
  return {meta, chunk: bytes.subarray(4 + length)};
}

function createState(root, batch) {
  root.querySelector('[data-pdfzip-error]')?.remove();
  const area = document.createElement('section');
  area.className = 'downloads';
  area.setAttribute('aria-label', 'Descargas');
  const summary = document.createElement('div');
  summary.className = 'summary';
  summary.setAttribute('role', 'status');
  summary.setAttribute('aria-live', 'polite');
  area.appendChild(summary);
  root.appendChild(area);
  const s = {root, batch, area, summary, records: new Map(), order: [], enabled: false,
    active: null, inflight: null, timer: null, counter: 0, disposed: false,
    emit: null, lastRequest: null};

  function sentBefore(id) {
    try { return sessionStorage.getItem('pdfzip3:' + batch + ':' + id) === 'sent'; }
    catch (_) { return false; }
  }
  function remember(rec) {
    rec.sent = true;
    try { sessionStorage.setItem('pdfzip3:' + batch + ':' + rec.id, 'sent'); } catch (_) {}
  }
  function release(rec) {
    if (rec.url) {
      const url = rec.url;
      // Una descarga recién solicitada necesita conservar su URL al iniciarse.
      if (Date.now() - rec.lastUsed < 2000) setTimeout(() => URL.revokeObjectURL(url), 2000);
      else URL.revokeObjectURL(url);
      rec.url = null;
    }
    rec.chunks = [];
  }
  function clearWait() { clearTimeout(s.timer); s.timer = null; }
  function emit(value) { s.lastRequest = value; s.emit(value); }
  function render(rec) {
    const ready = rec.phase === 'ready';
    rec.link.hidden = !ready;
    if (ready) rec.link.href = rec.url;
    else rec.link.removeAttribute('href');
    rec.retry.hidden = !['error', 'evicted'].includes(rec.phase);
    rec.retry.textContent = rec.phase === 'error' ? 'Reintentar descarga' : 'Descargar ' + rec.name;
    rec.bar.hidden = rec.phase !== 'loading';
    rec.bar.max = rec.size;
    rec.bar.value = rec.offset;
    let text;
    if (rec.phase === 'error') text = rec.error;
    else if (rec.phase === 'evicted') text = 'Pulsa Descargar para volver a preparar este archivo.';
    else if (ready) text = rec.sent ? 'Descarga solicitada al navegador. Puedes descargarlo de nuevo.' : 'Archivo listo para descargar.';
    else if (!s.enabled) text = 'Se preparará al terminar la conversión del lote.';
    else if (rec.phase === 'loading') text = 'Preparando descarga: ' + Math.floor(100 * rec.offset / rec.size) + '%';
    else text = 'En espera para preparar la descarga.';
    rec.status.textContent = (rec.size / 1048576).toFixed(2) + ' MiB · ' + text;
  }
  function renderSummary() {
    const records = [...s.records.values()];
    s.area.hidden = records.length === 0;
    const failed = records.filter(rec => rec.phase === 'error').length;
    const sent = records.filter(rec => rec.sent).length;
    s.summary.textContent = !s.enabled ? 'Los ZIP se prepararán al terminar la conversión del lote.'
      : failed ? 'No se pudo preparar ' + failed + ' archivo(s). Puedes reintentar su descarga.'
      : sent < records.length ? 'Preparando las descargas automáticas: ' + sent + ' de ' + records.length
      : 'Descargas solicitadas al navegador: ' + sent + '. Los archivos siguen disponibles debajo.';
  }
  function fail(rec, message) {
    clearWait();
    release(rec);
    rec.phase = 'error'; rec.error = message; rec.offset = 0;
    s.inflight = null; s.active = null;
    render(rec); renderSummary();
  }
  function reserve(size) {
    let cached = [...s.records.values()].filter(rec => rec.phase === 'ready');
    let total = cached.reduce((sum, rec) => sum + rec.size, 0);
    cached.sort((a, b) => a.lastUsed - b.lastUsed);
    for (const rec of cached) {
      if (total + size <= CACHE_BYTES) break;
      total -= rec.size;
      release(rec); rec.phase = 'evicted'; rec.offset = 0;
      render(rec);
    }
  }
  s.pump = () => {
    if (s.disposed || !s.enabled || s.inflight) return;
    let rec = s.active ? s.records.get(s.active) : null;
    if (!rec) rec = s.order.map(id => s.records.get(id)).find(item => item.phase === 'waiting');
    if (!rec) {
      if (s.lastRequest !== null) emit(null); // Libera también el último bloque del mensaje de Streamlit.
      renderSummary();
      return;
    }
    if (rec.phase === 'waiting') {
      reserve(rec.size);
      rec.chunks = []; rec.offset = 0; rec.phase = 'loading';
    }
    s.active = rec.id;
    const request = {batch, id: rec.id, offset: rec.offset, token: Date.now() + '-' + (++s.counter)};
    s.inflight = request;
    render(rec); renderSummary();
    emit(request);
    s.timer = setTimeout(() => {
      if (s.disposed || s.inflight?.token !== request.token) return;
      fail(rec, 'La transferencia no respondió. Comprueba la conexión y pulsa Reintentar descarga.');
      s.pump();
    }, WAIT_MS);
  };
  s.update = meta => {
    const ids = new Set(meta.files.map(file => file.id));
    for (const [id, rec] of s.records) {
      if (ids.has(id)) continue;
      if (s.active === id) { clearWait(); s.inflight = null; s.active = null; }
      release(rec); rec.row.remove(); s.records.delete(id);
    }
    s.order = meta.files.map(file => file.id);
    s.enabled = !!meta.enabled;
    if (!s.enabled) { clearWait(); s.inflight = null; }
    for (const file of meta.files) {
      if (!s.records.has(file.id)) {
        const row = document.createElement('div'); row.className = 'file';
        const name = document.createElement('div'); name.className = 'name'; name.textContent = file.name;
        const status = document.createElement('div'); status.className = 'status';
        const bar = document.createElement('progress'); bar.setAttribute('aria-label', 'Preparación de ' + file.name);
        const link = document.createElement('a'); link.className = 'action';
        link.download = file.name; link.textContent = 'Descargar ' + file.name;
        const retry = document.createElement('button'); retry.className = 'action'; retry.type = 'button';
        row.append(name, status, bar, link, retry); area.appendChild(row);
        const rec = {id: file.id, name: file.name, size: file.size, row, status, bar, link, retry,
          phase: 'waiting', chunks: [], offset: 0, url: null, lastUsed: 0,
          sent: sentBefore(file.id), manual: false, error: ''};
        link.addEventListener('click', () => { rec.lastUsed = Date.now(); remember(rec); render(rec); renderSummary(); });
        retry.addEventListener('click', () => {
          rec.phase = 'waiting'; rec.error = ''; rec.manual = true;
          render(rec); s.pump();
        });
        s.records.set(file.id, rec);
      }
      render(s.records.get(file.id));
    }
    renderSummary();
  };
  s.consume = (meta, chunk) => {
    if (!s.enabled || !s.inflight) return;
    const answer = meta.error || meta.response;
    const request = s.inflight;
    if (!answer || answer.id !== request.id || answer.token !== request.token) return;
    const rec = s.records.get(request.id);
    if (!rec) return;
    if (meta.error) { fail(rec, meta.error.message); return; }
    if (answer.offset !== request.offset || answer.offset !== rec.offset) {
      fail(rec, 'Se recibió un bloque fuera de orden. Pulsa Reintentar descarga.'); return;
    }
    if (answer.total !== rec.size || answer.size !== chunk.byteLength || chunk.byteLength === 0
        || chunk.byteLength > MAX_CHUNK || rec.offset + chunk.byteLength > rec.size
        || chunk.byteLength !== Math.min(MAX_CHUNK, rec.size - rec.offset)
        || answer.last !== (rec.offset + chunk.byteLength === rec.size)) {
      fail(rec, 'Se recibió una descarga incompleta. Pulsa Reintentar descarga.'); return;
    }
    if (rec.offset === 0 && (chunk[0] !== 80 || chunk[1] !== 75 || chunk[2] !== 3 || chunk[3] !== 4)) {
      fail(rec, 'El resultado recibido no es un ZIP válido. Vuelve a procesar el PDF.'); return;
    }
    clearWait(); s.inflight = null;
    rec.chunks.push(chunk.slice()); // Evita retener el buffer Protobuf completo.
    rec.offset += chunk.byteLength;
    if (rec.offset === rec.size) {
      try {
        const blob = new Blob(rec.chunks, {type: 'application/zip'});
        rec.chunks = [];
        rec.url = URL.createObjectURL(blob); rec.phase = 'ready'; rec.lastUsed = Date.now();
        s.active = null;
        render(rec);
        if (!rec.sent || rec.manual) { rec.manual = false; rec.link.click(); }
      } catch (_) { fail(rec, 'El navegador no pudo preparar el archivo. Pulsa Reintentar descarga.'); }
    }
    render(rec); renderSummary();
  };
  s.dispose = () => {
    if (s.disposed) return;
    s.disposed = true; clearWait();
    for (const rec of s.records.values()) release(rec);
    s.records.clear();
    s.area.remove();
    window.removeEventListener('pagehide', s.pagehide);
  };
  s.pagehide = event => { if (!event.persisted) s.dispose(); };
  window.addEventListener('pagehide', s.pagehide);
  return s;
}

export default function ({data, parentElement, setStateValue}) {
  let decoded;
  try { decoded = decode(data); }
  catch (error) {
    parentElement.__pdfzipDelivery?.dispose();
    parentElement.querySelector('[data-pdfzip-error]')?.remove();
    const message = document.createElement('p');
    message.dataset.pdfzipError = 'true'; message.setAttribute('role', 'alert');
    message.textContent = error.message;
    parentElement.appendChild(message);
    return;
  }
  const {meta, chunk} = decoded;
  const retired = parentElement.__pdfzipRetiredBatches ||= new Set();
  if (retired.has(meta.batch)) return;
  let state = parentElement.__pdfzipDelivery;
  if (!state || state.disposed || state.batch !== meta.batch) {
    if (state && state.batch !== meta.batch) retired.add(state.batch);
    state?.dispose();
    state = createState(parentElement, meta.batch);
    parentElement.__pdfzipDelivery = state;
  }
  state.emit = value => setStateValue('request', value);
  state.update(meta);
  state.consume(meta, chunk);
  state.pump();
  return () => {
    // Los datos cambian con cada bloque. Sólo limpiar al desmontar realmente,
    // sin revocar los enlaces listos durante una actualización de la pantalla.
    setTimeout(() => {
      if (parentElement.__pdfzipDelivery === state && !parentElement.isConnected) state.dispose();
    }, 0);
  };
}
"""


def emitir_descargas(st, parts, enabled=True):
    import streamlit.components.v2 as components
    component = components.component("pdfzip_delivery_v3", html="", css=DOWNLOAD_CSS, js=DOWNLOAD_JS)
    value = st.session_state.get("pdfzip_delivery")
    request = value.get("request") if value is not None else None
    message = construir_mensaje_descarga(parts, st.session_state.pdfzip_delivery_batch, request, enabled)
    component(data=message, key="pdfzip_delivery", on_request_change=lambda: None)


def _new_batch(st, signature):
    old_temp = st.session_state.get("pdfzip_temp")
    if old_temp is not None:
        old_temp.cleanup()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    st.session_state.pdfzip_temp = tempfile.TemporaryDirectory(prefix="s_", dir=DOWNLOADS_DIR)
    st.session_state.pdfzip_signature = signature
    st.session_state.pdfzip_delivery_batch = uuid.uuid4().hex
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
    # Un contenedor estable conserva el receptor entre ACK/reruns; st.empty()
    # enviaría un vaciado intermedio que puede desmontarlo y perder sus Blob.
    auto_area = st.container(key="pdfzip_download_area")

    signature = obtener_firma_lote(uploaded, profile)
    if signature != st.session_state.pdfzip_signature:
        _new_batch(st, signature)
    if not uploaded:
        with auto_area:
            emitir_descargas(st, [], enabled=False)
        st.caption("Selecciona la calidad y sube tus PDF para comenzar. Los ZIP estarán disponibles mientras conserves este lote en la sesión.")
        return

    cols = st.columns(3)
    if cols[0].button("Vaciar lote", width="stretch"):
        _new_batch(st, None)
        st.session_state.pdfzip_upload_key += 1
        st.rerun()
    if cols[1].button("Volver a procesar", width="stretch"):
        _new_batch(st, signature)
        st.rerun()
    results = st.session_state.pdfzip_results
    if cols[2].button("Reintentar fallidos", disabled=not any(r["status"] == "error" for r in results.values()), width="stretch"):
        for key in [key for key, value in results.items() if value["status"] == "error"]:
            del results[key]
        st.rerun()

    names = nombres_unicos(uploaded)
    all_parts = [part for result in results.values() if result["status"] == "ok" for part in result["parts"]]
    st.caption("Si tu navegador solicita permiso para varias descargas, permite las descargas de este sitio. También puedes usar los botones Descargar cuando los archivos estén listos.")
    st.subheader("Estado del procesamiento")
    for index in range(len(uploaded)):
        if str(index) in results:
            _render_result(st, results[str(index)])
    pending = next((i for i in range(len(uploaded)) if str(i) not in results), None)
    st.progress(len(results) / len(uploaded), text=f"Archivos procesados: {len(results)} de {len(uploaded)}")
    # Los ACK del componente producen reruns. Esperamos a terminar el lote para
    # que esas actualizaciones no cancelen un trabajador del siguiente PDF.
    with auto_area:
        emitir_descargas(st, all_parts, enabled=pending is None)
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
