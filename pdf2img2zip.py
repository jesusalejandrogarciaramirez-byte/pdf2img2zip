import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import io
import zipfile
import os
import base64
import gc
import time
from typing import Tuple

# ----------------------------
# CONFIGURACIÓN DE LA APP
# ----------------------------
st.set_page_config(page_title="PDF a JPG + ZIP", layout="centered")

MAX_STREAMLIT_MB = 200
SAFE_THRESHOLD_MB = 195
WAIT_SECONDS = 4  # cámbialo aquí si después quieres otro valor

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
def limpiar_nombre_archivo(nombre: str) -> str:
    base = os.path.splitext(nombre)[0]
    return base.strip()


def obtener_parametros_calidad(perfil: str) -> Tuple[int, int]:
    mapa = {
        "Alta": (300, 92),
        "Media alta": (200, 88),
        "Media": (150, 85),
        "Media baja": (120, 78),
        "Baja": (96, 70),
    }
    return mapa.get(perfil, (96, 70))


def siguiente_perfil_mas_bajo(perfil_actual: str):
    niveles = ["Alta", "Media alta", "Media", "Media baja", "Baja"]
    if perfil_actual not in niveles:
        return "Baja"
    idx = niveles.index(perfil_actual)
    if idx < len(niveles) - 1:
        return niveles[idx + 1]
    return None


def dpi_a_zoom(dpi: int) -> float:
    return dpi / 72.0


def renderizar_pagina_como_jpg(page, dpi=96, jpg_quality=70):
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


def convertir_pdf_a_zip(pdf_file, perfil_calidad="Baja", mostrar_progreso=False):
    nombre_base = limpiar_nombre_archivo(pdf_file.name)
    pdf_bytes = pdf_file.getvalue()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    total_paginas = len(doc)
    dpi, jpg_quality = obtener_parametros_calidad(perfil_calidad)

    zip_buffer = io.BytesIO()

    barra = None
    estado = None

    if mostrar_progreso:
        barra = st.progress(0, text=f"Procesando {pdf_file.name}...")
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
                    barra.progress(progreso, text=f"Procesando {pdf_file.name}...")
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


def convertir_pdf_con_ajuste_automatico(pdf_file, perfil_inicial):
    perfil_actual = perfil_inicial
    historial = []

    while True:
        zip_bytes, zip_name, total_paginas, dpi, jpg_quality = convertir_pdf_a_zip(
            pdf_file,
            perfil_calidad=perfil_actual,
            mostrar_progreso=True
        )

        tamano_mb = len(zip_bytes) / (1024 * 1024)
        historial.append((perfil_actual, tamano_mb))

        if tamano_mb < SAFE_THRESHOLD_MB:
            return {
                "ok": True,
                "zip_bytes": zip_bytes,
                "zip_name": zip_name,
                "total_paginas": total_paginas,
                "dpi": dpi,
                "jpg_quality": jpg_quality,
                "perfil_usado": perfil_actual,
                "tamano_mb": tamano_mb,
                "historial": historial
            }

        siguiente = siguiente_perfil_mas_bajo(perfil_actual)

        if siguiente is None:
            return {
                "ok": tamano_mb < MAX_STREAMLIT_MB,
                "zip_bytes": zip_bytes,
                "zip_name": zip_name,
                "total_paginas": total_paginas,
                "dpi": dpi,
                "jpg_quality": jpg_quality,
                "perfil_usado": perfil_actual,
                "tamano_mb": tamano_mb,
                "historial": historial
            }

        del zip_bytes
        gc.collect()

        st.warning(
            f"El ZIP de {pdf_file.name} salió con {tamano_mb:.2f} MB usando '{perfil_actual}'. "
            f"Se reintentará automáticamente con '{siguiente}'."
        )

        perfil_actual = siguiente


def generar_html_descarga_automatica(zip_bytes: bytes, zip_name: str):
    b64 = base64.b64encode(zip_bytes).decode()
    html = f"""
    <html>
    <body>
        <a id="descarga_zip" href="data:application/zip;base64,{b64}" download="{zip_name}"></a>
        <script>
            window.onload = function() {{
                const link = document.getElementById('descarga_zip');
                if (link) {{
                    link.click();
                }}
            }}
        </script>
    </body>
    </html>
    """
    return html


def limpiar_memoria_objetos(*objetos):
    for obj in objetos:
        try:
            del obj
        except Exception:
            pass
    gc.collect()


def obtener_firma_lote(files):
    if not files:
        return None
    return "|".join([f"{f.name}-{f.size}" for f in files])

# ----------------------------
# INICIO AUTOMÁTICO DEL PROCESO
# ----------------------------
if uploaded_files:
    firma_lote = obtener_firma_lote(uploaded_files)

    if st.session_state.lote_actual != firma_lote:
        st.session_state.lote_actual = firma_lote
        st.session_state.proceso_activo = True
        st.session_state.indice_actual = 0
        st.session_state.mensaje_final = ""
        st.session_state.ultima_calidad_usada = {}
        st.rerun()

# ----------------------------
# PROCESAMIENTO UNO POR UNO
# ----------------------------
if uploaded_files:
    st.write("---")
    st.subheader("Estado del procesamiento")

    total_archivos = len(uploaded_files)
    indice_actual = st.session_state.indice_actual

    if st.session_state.proceso_activo:
        if indice_actual < total_archivos:
            pdf_file = uploaded_files[indice_actual]

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

                st.session_state.ultima_calidad_usada[pdf_file.name] = perfil_usado

                st.success(f"Conversión completada: {pdf_file.name}")
                st.caption(
                    f"Páginas: {total_paginas} | Perfil usado: {perfil_usado} | "
                    f"Resolución: {dpi} DPI | Calidad JPG: {jpg_quality} | "
                    f"Tamaño ZIP: {tamano_zip_mb:.2f} MB"
                )

                if len(resultado["historial"]) > 1:
                    texto_historial = " → ".join(
                        [f"{perfil} ({mb:.2f} MB)" for perfil, mb in resultado["historial"]]
                    )
                    st.info(f"Ajuste automático aplicado: {texto_historial}")

                if tamano_zip_mb >= MAX_STREAMLIT_MB:
                    st.error(
                        f"El ZIP final quedó en {tamano_zip_mb:.2f} MB y rebasa el límite de Streamlit."
                    )
                    st.session_state.proceso_activo = False
                    st.session_state.mensaje_final = (
                        f"Proceso detenido: {pdf_file.name} todavía supera el límite de {MAX_STREAMLIT_MB} MB."
                    )
                else:
                    st.components.v1.html(
                        generar_html_descarga_automatica(zip_bytes, zip_name),
                        height=0
                    )

                    st.caption(
                        f"Descarga automática enviada. Esperando {WAIT_SECONDS} segundos antes de continuar..."
                    )

                    time.sleep(WAIT_SECONDS)

                    limpiar_memoria_objetos(zip_bytes)

                    try:
                        pdf_file.close()
                    except Exception:
                        pass

                    gc.collect()

                    st.session_state.indice_actual += 1

                    if st.session_state.indice_actual >= total_archivos:
                        st.session_state.proceso_activo = False
                        st.session_state.mensaje_final = "Todos los archivos fueron procesados."
                    else:
                        st.rerun()

            except Exception as e:
                st.session_state.proceso_activo = False
                st.error(f"No se pudo procesar {pdf_file.name}: {e}")
                gc.collect()

        else:
            st.session_state.proceso_activo = False
            st.session_state.mensaje_final = "Todos los archivos fueron procesados."

    progreso = 0
    if total_archivos > 0:
        progreso = min(st.session_state.indice_actual / total_archivos, 1.0)

    st.progress(progreso)

    if st.session_state.mensaje_final:
        st.success(st.session_state.mensaje_final)

    restantes = max(total_archivos - st.session_state.indice_actual, 0)
    st.write(f"Pendientes: {restantes}")

    if st.session_state.ultima_calidad_usada:
        st.write("### Calidad final usada por archivo")
        for nombre, calidad in st.session_state.ultima_calidad_usada.items():
            st.write(f"- {nombre}: {calidad}")

else:
    st.caption("Sube uno o varios PDFs para comenzar automáticamente.")
