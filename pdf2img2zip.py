import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import io
import zipfile
import os
import gc
import time
import re
import json
import streamlit.components.v1 as components
from typing import Tuple, Optional, Dict, Any

# ----------------------------
# CONFIGURACIÓN DE LA APP
# ----------------------------
st.set_page_config(page_title="PDF a JPG + ZIP", layout="centered")

MAX_STREAMLIT_MB = 200
SAFE_THRESHOLD_MB = 195
WAIT_SECONDS = 4  # cámbialo aquí si después quieres otro valor

# Un solo ajuste extra si el primero no alcanza
EMERGENCY_DPI = 72
EMERGENCY_JPG_QUALITY = 55
EMERGENCY_LABEL = "Baja emergencia"

# ----------------------------
# ENCABEZADO SIMPLE
# ----------------------------
st.title("Convertidor de PDF a JPG")
st.caption("Extrae cada página como JPG y genera un ZIP descargable")

# ----------------------------
# ESTADO DE SESIÓN
# ----------------------------
if "proceso_activo" not in st.session_state:
    st.session_state.proceso_activo = False

if "indice_actual" not in st.session_state:
    st.session_state.indice_actual = 0

if "mensaje_final" not in st.session_state:
    st.session_state.mensaje_final = ""

if "ultima_calidad_usada" not in st.session_state:
    st.session_state.ultima_calidad_usada = {}

if "lote_actual" not in st.session_state:
    st.session_state.lote_actual = None

if "ultimo_autoclick_id" not in st.session_state:
    st.session_state.ultimo_autoclick_id = None

if "procesados" not in st.session_state:
    st.session_state.procesados = set()

if "pending_download" not in st.session_state:
    st.session_state.pending_download = None

# ----------------------------
# OPCIONES DE EXPORTACIÓN
# ----------------------------
perfil_calidad = st.selectbox(
    "Selecciona la calidad",
    options=["Alta", "Media alta", "Media", "Media baja", "Baja"],
    index=4  # Baja por default
)

# ----------------------------
# CARGA DE ARCHIVOS
# ----------------------------
uploaded_files = st.file_uploader(
    "Sube uno o varios archivos PDF",
    type=["pdf"],
    accept_multiple_files=True
)

# ----------------------------
# FUNCIONES
# ----------------------------
def sanitizar_key_css(texto: str) -> str:
    """
    Convierte una key en algo seguro para usar en selector CSS.
    Streamlit usa la key como clase CSS con prefijo st-key-.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "-", texto)


def limpiar_nombre_archivo(nombre: str) -> str:
    base = os.path.splitext(nombre)[0]
    return base.strip()


def obtener_id_archivo(pdf_file) -> str:
    return f"{pdf_file.name}_{pdf_file.size}"


def obtener_parametros_calidad(perfil: str) -> Tuple[int, int]:
    mapa = {
        "Alta": (300, 92),
        "Media alta": (200, 88),
        "Media": (150, 85),
        "Media baja": (120, 78),
        "Baja": (96, 70),
    }
    return mapa.get(perfil, (96, 70))


def dpi_a_zoom(dpi: int) -> float:
    return dpi / 72.0


def renderizar_pagina_como_jpg(page, dpi=96, jpg_quality=70) -> bytes:
    zoom = dpi_a_zoom(dpi)
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)

    mode = "RGB" if pix.n < 4 else "RGBA"
    img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)

    if img.mode != "RGB":
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=jpg_quality, optimize=True)
    jpg_bytes = buffer.getvalue()
    buffer.close()

    del img
    del pix
    return jpg_bytes


def convertir_pdf_a_zip(
    pdf_file,
    perfil_calidad="Baja",
    mostrar_progreso=False,
    dpi_override: Optional[int] = None,
    jpg_quality_override: Optional[int] = None,
    etiqueta_progreso: Optional[str] = None,
):
    nombre_base = limpiar_nombre_archivo(pdf_file.name)
    pdf_bytes = pdf_file.getvalue()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    total_paginas = len(doc)

    if dpi_override is not None and jpg_quality_override is not None:
        dpi = dpi_override
        jpg_quality = jpg_quality_override
    else:
        dpi, jpg_quality = obtener_parametros_calidad(perfil_calidad)

    zip_buffer = io.BytesIO()

    barra = None
    estado = None

    if mostrar_progreso:
        texto = etiqueta_progreso or f"Procesando {pdf_file.name}..."
        barra = st.progress(0, text=texto)
        estado = st.empty()

    try:
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for i, page in enumerate(doc, start=1):
                imagen_bytes = renderizar_pagina_como_jpg(page, dpi=dpi, jpg_quality=jpg_quality)
                nombre_imagen = f"{nombre_base}_pag_{i:03d}.jpg"
                zip_file.writestr(nombre_imagen, imagen_bytes)

                del imagen_bytes

                if mostrar_progreso:
                    progreso = i / total_paginas
                    texto = etiqueta_progreso or f"Procesando {pdf_file.name}..."
                    barra.progress(progreso, text=texto)
                    estado.caption(f"Página {i} de {total_paginas}")

        zip_bytes = zip_buffer.getvalue()

    finally:
        if barra is not None:
            barra.empty()
        if estado is not None:
            estado.empty()

        doc.close()
        zip_buffer.close()
        del pdf_bytes
        gc.collect()

    return zip_bytes, f"{nombre_base}.zip", total_paginas, dpi, jpg_quality


def convertir_pdf_con_ajuste_automatico(pdf_file, perfil_inicial) -> Dict[str, Any]:
    """
    Estrategia rápida:
    1. Hace un solo intento con la calidad elegida.
    2. Solo si supera el umbral, hace un único reintento de emergencia.
    """
    historial = []

    # Primer intento normal
    zip_bytes, zip_name, total_paginas, dpi, jpg_quality = convertir_pdf_a_zip(
        pdf_file,
        perfil_calidad=perfil_inicial,
        mostrar_progreso=True,
        etiqueta_progreso=f"Procesando {pdf_file.name}..."
    )

    tamano_mb = len(zip_bytes) / (1024 * 1024)
    historial.append({
        "modo": "normal",
        "etiqueta": perfil_inicial,
        "dpi": dpi,
        "jpg_quality": jpg_quality,
        "tamano_mb": tamano_mb
    })

    if tamano_mb < SAFE_THRESHOLD_MB:
        return {
            "ok": True,
            "zip_bytes": zip_bytes,
            "zip_name": zip_name,
            "total_paginas": total_paginas,
            "dpi": dpi,
            "jpg_quality": jpg_quality,
            "perfil_usado": perfil_inicial,
            "tamano_mb": tamano_mb,
            "historial": historial
        }

    st.warning(
        f"El ZIP de {pdf_file.name} quedó en {tamano_mb:.2f} MB. "
        f"Se intentará un ajuste extra para evitar rebasar el límite."
    )

    del zip_bytes
    gc.collect()

    zip_bytes, zip_name, total_paginas, dpi2, jpg_quality2 = convertir_pdf_a_zip(
        pdf_file,
        perfil_calidad=perfil_inicial,
        mostrar_progreso=True,
        dpi_override=EMERGENCY_DPI,
        jpg_quality_override=EMERGENCY_JPG_QUALITY,
        etiqueta_progreso=f"Reprocesando {pdf_file.name} con ajuste de emergencia..."
    )

    tamano_mb_2 = len(zip_bytes) / (1024 * 1024)
    historial.append({
        "modo": "emergencia",
        "etiqueta": EMERGENCY_LABEL,
        "dpi": dpi2,
        "jpg_quality": jpg_quality2,
        "tamano_mb": tamano_mb_2
    })

    return {
        "ok": tamano_mb_2 < MAX_STREAMLIT_MB,
        "zip_bytes": zip_bytes,
        "zip_name": zip_name,
        "total_paginas": total_paginas,
        "dpi": dpi2,
        "jpg_quality": jpg_quality2,
        "perfil_usado": EMERGENCY_LABEL,
        "tamano_mb": tamano_mb_2,
        "historial": historial
    }


def render_descarga_nativa_y_autoclick(zip_bytes: bytes, zip_name: str, file_index: int):
    """
    1) Renderiza un botón nativo visible.
    2) Lanza clic automático por JS sobre ese mismo botón.
    3) on_click='ignore' evita rerun por el clic de descarga.
    """
    button_key_raw = f"download_btn_{file_index}_{zip_name}"
    button_key = sanitizar_key_css(button_key_raw)

    # Botón VISIBLE como respaldo
    st.download_button(
        label=f"Descargar {zip_name}",
        data=zip_bytes,
        file_name=zip_name,
        mime="application/zip",
        key=button_key,
        on_click="ignore",
    )

    autoclick_id = f"autoclick_{button_key}"

    if st.session_state.ultimo_autoclick_id != autoclick_id:
        st.session_state.ultimo_autoclick_id = autoclick_id

        selector = f".st-key-{button_key} button"
        selector_js = json.dumps(selector)

        html = f"""
        <html>
        <body>
        <script>
        const selector = {selector_js};

        function clickWhenReady() {{
            try {{
                const doc = window.parent.document;
                const btn = doc.querySelector(selector);

                if (btn) {{
                    btn.click();
                }} else {{
                    setTimeout(clickWhenReady, 250);
                }}
            }} catch (e) {{
                setTimeout(clickWhenReady, 400);
            }}
        }}

        setTimeout(clickWhenReady, 400);
        </script>
        </body>
        </html>
        """

        components.html(html, height=0, width=0)


def limpiar_memoria_objetos(*objetos):
    for obj in objetos:
        try:
            del obj
        except Exception:
            pass
    gc.collect()


def obtener_firma_lote(files) -> Optional[str]:
    if not files:
        return None
    return "|".join([f"{f.name}-{f.size}" for f in files])


def reiniciar_lote():
    st.session_state.proceso_activo = True
    st.session_state.indice_actual = 0
    st.session_state.mensaje_final = ""
    st.session_state.ultima_calidad_usada = {}
    st.session_state.ultimo_autoclick_id = None
    st.session_state.procesados = set()
    st.session_state.pending_download = None


# ----------------------------
# INICIO AUTOMÁTICO DEL PROCESO
# ----------------------------
if uploaded_files:
    firma_lote = obtener_firma_lote(uploaded_files)

    if st.session_state.lote_actual != firma_lote:
        st.session_state.lote_actual = firma_lote
        reiniciar_lote()
        st.rerun()


# ----------------------------
# PROCESAMIENTO UNO POR UNO
# ----------------------------
if uploaded_files:
    st.write("---")
    st.subheader("Estado del procesamiento")

    total_archivos = len(uploaded_files)

    # 1) Si hay un ZIP ya convertido y pendiente de descarga, NO volver a convertir
    if st.session_state.pending_download is not None:
        pendiente = st.session_state.pending_download

        zip_bytes = pendiente["zip_bytes"]
        zip_name = pendiente["zip_name"]
        total_paginas = pendiente["total_paginas"]
        dpi = pendiente["dpi"]
        jpg_quality = pendiente["jpg_quality"]
        perfil_usado = pendiente["perfil_usado"]
        tamano_zip_mb = pendiente["tamano_mb"]
        historial = pendiente["historial"]
        file_index = pendiente["file_index"]
        pdf_name = pendiente["pdf_name"]
        file_id = pendiente["file_id"]

        st.info(f"Preparando descarga de: {pdf_name}")

        st.success(f"Conversión completada: {pdf_name}")
        st.caption(
            f"Páginas: {total_paginas} | Ajuste usado: {perfil_usado} | "
            f"Resolución: {dpi} DPI | Calidad JPG: {jpg_quality} | "
            f"Tamaño ZIP: {tamano_zip_mb:.2f} MB"
        )

        if len(historial) > 1:
            st.write("### Historial de ajustes")
            for intento in historial:
                st.write(
                    f"- {intento['etiqueta']} | "
                    f"{intento['dpi']} DPI | JPG {intento['jpg_quality']} | "
                    f"{intento['tamano_mb']:.2f} MB"
                )

        render_descarga_nativa_y_autoclick(zip_bytes, zip_name, file_index)

        # Marcar como procesado ANTES de esperar/rerun
        st.session_state.procesados.add(file_id)
        st.session_state.ultima_calidad_usada[pdf_name] = (
            f"{perfil_usado} | {dpi} DPI | JPG {jpg_quality}"
        )

        st.caption(
            f"Descarga automática enviada. Esperando {WAIT_SECONDS} segundos antes de continuar..."
        )

        time.sleep(WAIT_SECONDS)

        limpiar_memoria_objetos(zip_bytes)
        st.session_state.pending_download = None
        st.session_state.indice_actual = file_index + 1
        gc.collect()
        st.rerun()

    # 2) Si no hay descarga pendiente, buscar el siguiente archivo NO procesado
    if st.session_state.proceso_activo:
        while st.session_state.indice_actual < total_archivos:
            pdf_file = uploaded_files[st.session_state.indice_actual]
            file_id = obtener_id_archivo(pdf_file)

            if file_id in st.session_state.procesados:
                st.session_state.indice_actual += 1
            else:
                break

        if st.session_state.indice_actual < total_archivos:
            indice_actual = st.session_state.indice_actual
            pdf_file = uploaded_files[indice_actual]
            file_id = obtener_id_archivo(pdf_file)

            st.info(f"Procesando archivo {indice_actual + 1} de {total_archivos}: {pdf_file.name}")

            try:
                resultado = convertir_pdf_con_ajuste_automatico(pdf_file, perfil_calidad)

                zip_bytes = resultado["zip_bytes"]
                zip_name = resultado["zip_name"]
                total_paginas = resultado["total_paginas"]
                dpi = resultado["dpi"]
                jpg_quality = resultado["jpg_quality"]
                perfil_usado = resultado["perfil_usado"]
                tamano_zip_mb = resultado["tamano_mb"]
                historial = resultado["historial"]

                if tamano_zip_mb >= MAX_STREAMLIT_MB:
                    st.error(
                        f"El ZIP final quedó en {tamano_zip_mb:.2f} MB y todavía rebasa el límite de Streamlit."
                    )
                    st.session_state.proceso_activo = False
                    st.session_state.mensaje_final = (
                        f"Proceso detenido: {pdf_file.name} aún supera el límite de {MAX_STREAMLIT_MB} MB."
                    )
                else:
                    st.session_state.pending_download = {
                        "zip_bytes": zip_bytes,
                        "zip_name": zip_name,
                        "total_paginas": total_paginas,
                        "dpi": dpi,
                        "jpg_quality": jpg_quality,
                        "perfil_usado": perfil_usado,
                        "tamano_mb": tamano_zip_mb,
                        "historial": historial,
                        "file_index": indice_actual,
                        "pdf_name": pdf_file.name,
                        "file_id": file_id,
                    }
                    gc.collect()
                    st.rerun()

            except Exception as e:
                st.session_state.proceso_activo = False
                st.error(f"Error procesando {pdf_file.name}: {e}")
                st.session_state.mensaje_final = f"Proceso detenido por error en {pdf_file.name}."
        else:
            st.session_state.proceso_activo = False
            st.session_state.mensaje_final = "Todos los archivos fueron procesados."

    progreso = 0
    if total_archivos > 0:
        progreso = min(len(st.session_state.procesados) / total_archivos, 1.0)

    st.progress(progreso)

    if st.session_state.mensaje_final:
        st.success(st.session_state.mensaje_final)

    restantes = max(total_archivos - len(st.session_state.procesados), 0)
    st.write(f"Pendientes: {restantes}")

    if st.session_state.ultima_calidad_usada:
        st.write("### Ajuste final usado por archivo")
        for nombre, calidad in st.session_state.ultima_calidad_usada.items():
            st.write(f"- {nombre}: {calidad}")

else:
    st.caption("Sube uno o varios PDFs para comenzar automáticamente.")
