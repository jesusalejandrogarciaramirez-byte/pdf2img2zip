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
st.set_page_config(page_title="PDF a JPG + ZIP", layout="wide")

st.markdown("<h1 style='text-align: center;'>Convertidor de PDF a JPG</h1>", unsafe_allow_html=True)
st.markdown(
    "<h4 style='text-align: center; color: gray;'>Extrae cada página como JPG y genera un ZIP descargable</h4>",
    unsafe_allow_html=True
)
st.write("---")

# ----------------------------
# ESTADO DE SESIÓN
# ----------------------------
if "proceso_activo" not in st.session_state:
    st.session_state.proceso_activo = False

if "indice_actual" not in st.session_state:
    st.session_state.indice_actual = 0

if "ultima_ejecucion" not in st.session_state:
    st.session_state.ultima_ejecucion = None

if "mensaje_final" not in st.session_state:
    st.session_state.mensaje_final = ""

# ----------------------------
# OPCIONES DE EXPORTACIÓN
# ----------------------------
st.subheader("Configuración de exportación")
perfil_calidad = st.selectbox(
    "Selecciona la calidad",
    options=["Alta", "Media alta", "Media", "Media baja", "Baja"],
    index=3  # Media baja por default
)

espera_segundos = st.slider(
    "Segundos de espera antes de pasar al siguiente archivo",
    min_value=2,
    max_value=10,
    value=4,
    help="No detecta la descarga real del navegador; solo espera un poco antes de continuar."
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
    return mapa.get(perfil, (120, 78))


def dpi_a_zoom(dpi: int) -> float:
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
    jpg_bytes = buffer.getvalue()
    buffer.close()
    return jpg_bytes


def convertir_pdf_a_zip(pdf_file, perfil_calidad="Media baja"):
    nombre_base = limpiar_nombre_archivo(pdf_file.name)

    # getvalue() evita problemas por puntero consumido
    pdf_bytes = pdf_file.getvalue()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    total_paginas = len(doc)
    dpi, jpg_quality = obtener_parametros_calidad(perfil_calidad)

    zip_buffer = io.BytesIO()

    try:
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for i, page in enumerate(doc, start=1):
                imagen_bytes = renderizar_pagina_como_jpg(page, dpi=dpi, jpg_quality=jpg_quality)
                nombre_imagen = f"{nombre_base}_pag_{i:03d}.jpg"
                zip_file.writestr(nombre_imagen, imagen_bytes)

                # liberar imagen de cada página en cuanto ya se escribió al ZIP
                del imagen_bytes
                gc.collect()

        zip_buffer.seek(0)
        zip_bytes = zip_buffer.getvalue()

    finally:
        doc.close()
        zip_buffer.close()
        del pdf_bytes
        gc.collect()

    return zip_bytes, f"{nombre_base}.zip", total_paginas, dpi, jpg_quality


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
        except:
            pass
    gc.collect()

# ----------------------------
# CONTROLES
# ----------------------------
col1, col2 = st.columns(2)

with col1:
    if st.button("Iniciar procesamiento automático", use_container_width=True):
        if uploaded_files:
            st.session_state.proceso_activo = True
            st.session_state.indice_actual = 0
            st.session_state.mensaje_final = ""
            st.rerun()
        else:
            st.warning("Primero sube al menos un PDF.")

with col2:
    if st.button("Detener proceso", use_container_width=True):
        st.session_state.proceso_activo = False
        st.session_state.mensaje_final = "Proceso detenido por el usuario."
        gc.collect()
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
                with st.spinner(f"Convirtiendo {pdf_file.name}..."):
                    zip_bytes, zip_name, total_paginas, dpi, jpg_quality = convertir_pdf_a_zip(
                        pdf_file,
                        perfil_calidad=perfil_calidad
                    )

                tamano_zip_mb = len(zip_bytes) / (1024 * 1024)

                st.success(f"Conversión completada: {pdf_file.name}")
                st.caption(
                    f"Páginas: {total_paginas} | Perfil: {perfil_calidad} | "
                    f"Resolución: {dpi} DPI | Calidad JPG: {jpg_quality} | "
                    f"Tamaño ZIP: {tamano_zip_mb:.2f} MB"
                )

                if tamano_zip_mb >= 200:
                    st.error(
                        "El ZIP generado alcanza o supera el límite de 200 MB que Streamlit puede enviar al navegador. "
                        "Este archivo en particular puede volver a romper la descarga aunque se procese uno por uno."
                    )
                else:
                    # Botón de respaldo visible
                    st.download_button(
                        label=f"Descargar {zip_name}",
                        data=zip_bytes,
                        file_name=zip_name,
                        mime="application/zip",
                        key=f"download_{indice_actual}"
                    )

                    # Descarga automática
                    # Sigue funcionando hoy; la advertencia del log es deprecación futura, no error actual.
                    st.components.v1.html(
                        generar_html_descarga_automatica(zip_bytes, zip_name),
                        height=0
                    )

                    st.warning(
                        f"Se lanzó la descarga automática. La app esperará {espera_segundos} segundos "
                        "antes de liberar memoria y continuar con el siguiente archivo."
                    )

                # Espera breve para permitir que el navegador reciba la descarga
                time.sleep(espera_segundos)

                # Liberación explícita de memoria
                limpiar_memoria_objetos(zip_bytes)

                try:
                    pdf_file.close()
                except:
                    pass

                gc.collect()

                # Siguiente archivo
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

else:
    st.info("Primero elige la calidad y después sube al menos un PDF para comenzar.")
