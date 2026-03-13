# Convertidor de PDF a JPG

Aplicación en Streamlit que recibe uno o varios archivos PDF, convierte cada página a imagen JPG y genera un archivo ZIP por cada PDF.

## Funcionalidades

- Carga uno o varios archivos PDF
- Conversión automática al subir los archivos
- Exportación de cada página en JPG
- Nombres automáticos por página:
  - `nombrearchivo_pag_001.jpg`
  - `nombrearchivo_pag_002.jpg`
- Compresión final en ZIP:
  - `nombrearchivo.zip`
- Selector de calidad:
  - Alta
  - Media alta
  - Media
  - Media baja
  - Baja

## Perfil por defecto

La app inicia con **Media baja** como calidad predeterminada.

Perfiles usados:

- Alta: 300 DPI, JPG 92
- Media alta: 200 DPI, JPG 88
- Media: 150 DPI, JPG 85
- Media baja: 120 DPI, JPG 78
- Baja: 96 DPI, JPG 70

## Estructura recomendada del repositorio

```bash
.
├── app.py
├── requirements.txt
└── README.md