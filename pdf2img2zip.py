import streamlit as st
import fitz  # PyMuPDF
from PIL import Image
import io
import zipfile
import os
import base64
from typing import Tuple

# ----------------------------
# CONFIGURACIÓN DE LA APP
# ----------------------------
st.set_page_config(page_title="PDF a JPG + ZIP", layout="wide")

st.markdown("<h1 style='text-align: center;'>Convertidor de PDF a JPG</h1>", unsafe_allow_html=True)
st.markdown(
    "<h4 style='text-align: center; color: gray;'>Extrae cada página como JPG y genera un ZIP descargable</h4>",
    unsafe_allow_html=True
)
st.write("---")

# ----------------------------
# OPCIONES DE EXPORTACIÓN
# ----------------------------
st.subheader("Configuración de exportación")
perfil_calidad = st.selectbox(
    "Selecciona la calidad",
    options=["Alta", "Media alta", "Media", "Media baja", "Baja"],
    index=3  # Media baja por default
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
    """
    Devuelve (dpi, jpeg_quality) según el perfil seleccionado.

    Media = 150 DPI + JPG 85
    Media baja = menor peso, pero aún legible
    """
    mapa = {
        "Alta": (300, 92),
        "Media alta": (200, 88),
        "Media": (150, 85),
        "Media baja": (120, 78),
        "Baja": (96, 70),
    }
    return mapa.get(perfil, (120, 78))


def dpi_a_zoom(dpi: int) -> float:
    """
    PyMuPDF trabaja sobre 72 DPI base.
    zoom = dpi / 72
    """
    return dpi / 72.0


def renderizar_pagina_como_jpg(page, dpi=120, jpg_quality=78):
    zoom = dpi_a_zoom(dpi)
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)

    mode = "RGB" if pix.n < 4 else "RGBA"
    img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)

    if img.mode != "RGB":
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=jpg_quality, optimize=True)
    buffer.seek(0)
    return buffer.getvalue()


def convertir_pdf_a_zip(pdf_file, perfil_calidad="Media baja"):
    nombre_base = limpiar_nombre_archivo(pdf_file.name)
    zip_buffer = io.BytesIO()

    pdf_bytes = pdf_file.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    total_paginas = len(doc)
    dpi, jpg_quality = obtener_parametros_calidad(perfil_calidad)

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for i, page in enumerate(doc, start=1):
            imagen_bytes = renderizar_pagina_como_jpg(page, dpi=dpi, jpg_quality=jpg_quality)
            nombre_imagen = f"{nombre_base}_pag_{i:03d}.jpg"
            zip_file.writestr(nombre_imagen, imagen_bytes)

    doc.close()
    zip_buffer.seek(0)

    return zip_buffer.getvalue(), f"{nombre_base}.zip", total_paginas, dpi, jpg_quality


def generar_link_descarga_automatica(zip_bytes: bytes, zip_name: str):
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


# ----------------------------
# PROCESAMIENTO AUTOMÁTICO
# ----------------------------
if uploaded_files:
    st.write("---")
    st.subheader("Procesamiento automático")

    for indice, pdf_file in enumerate(uploaded_files):
        st.markdown(f"### Archivo detectado: {pdf_file.name}")

        try:
            with st.spinner(f"Procesando {pdf_file.name}..."):
                zip_bytes, zip_name, total_paginas, dpi, jpg_quality = convertir_pdf_a_zip(
                    pdf_file,
                    perfil_calidad=perfil_calidad
                )

            st.success(f"Conversión completada. Total de páginas exportadas: {total_paginas}")
            st.caption(
                f"Perfil: {perfil_calidad} | Resolución: {dpi} DPI | Calidad JPG: {jpg_quality} | ZIP: {zip_name}"
            )

            # Botón visible como respaldo
            st.download_button(
                label=f"Descargar {zip_name}",
                data=zip_bytes,
                file_name=zip_name,
                mime="application/zip",
                key=f"download_{indice}"
            )

            # Intento de descarga automática
            st.components.v1.html(
                generar_link_descarga_automatica(zip_bytes, zip_name),
                height=0
            )

            st.write("---")

        except Exception as e:
            st.error(f"No se pudo procesar {pdf_file.name}: {e}")
else:
    st.info("Primero elige la calidad y después sube al menos un PDF para comenzar.")