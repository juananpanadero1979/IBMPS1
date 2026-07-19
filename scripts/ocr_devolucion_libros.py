#!/usr/bin/env python3
"""
OCR de Devolución de libros - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Procesa los PDFs de ONCE/LIQUIDACIONES/DEVOLUCIÓN LIBROS/ y genera un JSON
junto a cada PDF. Esta carpeta contiene DOS tipos de documento distintos
que representan las dos fases del mismo trámite de devolución:

  1. "L## FINALIZACIÓN VENTA VOLUNTARIA ... AVISO" (tipo "aviso_inicio")
     — mismo formato que los avisos de Comunicaciones TPV (ver
     ocr_comunicaciones.py), pero a veces trae al pie una tabla con los
     libros de ese juego ya retirados. Inicia el trámite.
  2. "INFORME RETORNO DE LIBROS" (tipo "retorno_completado") — el
     justificante que se presenta en Correos al devolver físicamente los
     libros: una tabla con TODOS los libros incluidos en el envío (de
     uno o varios juegos) y un total. Cierra el trámite.

Misma regla que ocr_tickets.py / ocr_comunicaciones.py (ver CLAUDE.md —
"los cálculos del cuadre ONCE se hacen SIEMPRE con Python, nunca con
IA"): el modelo de visión SOLO transcribe título, fechas, texto libre y
filas de tabla tal cual aparecen. Identificar de cuál de los dos tipos se
trata (por el título) y estructurar el resultado final lo hace Python con
reglas explícitas, igual que _identificar_tipo() en ocr_tickets.py.

El JSON final siempre expone "fecha" y "libros_devueltos" (lista de
{"libro", "producto", "motivo"}) independientemente del tipo, para que
_contexto_devoluciones_libros() en asistente.py pueda leer cualquiera de
los dos sin distinguirlos; "motivo" queda siempre en null porque ninguno
de los dos tickets lo imprime — no se inventa.

Uso:
    python3 ocr_devolucion_libros.py                  # procesa todos los PDF pendientes
    python3 ocr_devolucion_libros.py archivo.pdf       # procesa solo uno y lo imprime
"""

import base64
import json
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import anthropic
import fitz  # PyMuPDF

from portal_once import ONCE_PATH

DEVOLUCION_LIBROS_PATH = ONCE_PATH / "LIQUIDACIONES" / "DEVOLUCIÓN LIBROS"

KEYCHAIN_SERVICE_CLAUDE = "IBMPS1-ClaudeAPI"
KEYCHAIN_ACCOUNT_CLAUDE = "ANTHROPIC_API_KEY"
MODELO_CLAUDE = "claude-sonnet-5"

KEYCHAIN_SERVICE_NVIDIA = "NVIDIA"
KEYCHAIN_ACCOUNT_NVIDIA = "API_KEY"
MODELO_NVIDIA = "meta/llama-3.2-90b-vision-instruct"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

MAX_INTENTOS_VISION = 3

# Margen de seguridad bajo el límite real de 10 MB (10.485.760 bytes) de
# la API de Claude Vision para el tamaño de una imagen en base64.
LIMITE_BYTES_IMAGEN = 9_000_000

PROMPT_EXTRACCION = """\
Esta es una imagen de un ticket impreso por un TPV de un punto de venta \
de lotería (ONCE), relacionado con la devolución de libros de rasca. Hay \
dos tipos posibles: un AVISO de finalización de venta voluntaria de un \
juego (texto libre con fechas), o un INFORME RETORNO DE LIBROS \
(justificante con una tabla de libros devueltos). Transcribe EXACTAMENTE \
lo que ves o lees, sin interpretar, calcular ni decidir cuál dato es "el \
importante" — eso lo hace después un programa, tú solo transcribes.

Devuelve SOLO un objeto JSON válido (sin texto adicional, sin bloques de \
código markdown, sin explicaciones) con esta forma exacta:

{
  "titulo": "<el título/tipo de documento tal cual aparece, p.ej. 'L58 FINALIZACIÓN VENTA VOLUNTARIA PRIMER AVISO' o 'INFORME RETORNO DE LIBROS'>",
  "fecha_emision": "<fecha de emisión, formato DD/MM/AAAA, tal cual aparece>",
  "hora_emision": "<hora de emisión si aparece, formato HH:MM:SS, o null>",
  "juego": "<nombre del juego/producto al que se refiere el documento, si se menciona de forma clara en el título o el texto, o null>",
  "fechas_mencionadas": [
    {"fecha": "DD/MM/AAAA", "descripcion": "<qué significa esa fecha, resumido con las propias palabras del texto>"}
  ],
  "texto_completo": "<transcripción completa y literal del párrafo de texto libre, si lo hay, o null>",
  "tabla_libros": [
    {"juego": "<código o nombre de juego de esa fila, tal cual aparece>", "libro": "<número de libro de esa fila>", "fecha_retirada": "<fecha de retirada de esa fila si la columna existe, formato DD/MM/AAAA, o null>"}
  ],
  "total_libros": "<el número que acompaña a 'Total libros:' si aparece, o null>"
}

Reglas:
- Incluye en "fechas_mencionadas" TODAS las fechas que aparecen en el \
cuerpo del texto libre (no la fecha/hora de emisión, que ya va aparte), \
en el mismo orden en que aparecen. Si el documento no tiene texto libre \
con fechas (p.ej. el INFORME RETORNO DE LIBROS), deja la lista vacía.
- Incluye en "tabla_libros" UNA entrada por cada fila de la tabla de \
libros, tanto si es la tabla corta al pie de un aviso (columnas Juego / \
Libro / F.Retirada) como la tabla completa de un INFORME RETORNO DE \
LIBROS (columnas Juego Descripción / Libro, sin fecha de retirada — usa \
null en ese caso). Si no hay tabla, deja la lista vacía.
- No incluyas el nombre/NIF/código de vendedor, el código de barras/QR, \
las marcas de agua repetidas de fondo ("ONCE", "JUEGA RESPONSABLEMENTE", \
textos girados que se ven a través del papel), la fila de cabecera de \
columnas, ni la línea de fecha/hora de impresión del pie del ticket.
- Si algún campo no aparece, usa null (o lista vacía si es una lista), no \
inventes ni deduzcas nada.
"""

_RE_TIPO_AVISO = re.compile(r"FINALIZACION VENTA VOLUNTARIA")
_RE_TIPO_RETORNO = re.compile(r"RETORNO DE LIBROS")


def _sin_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def _identificar_tipo(titulo):
    titulo_norm = _sin_acentos(titulo or "").upper()
    if _RE_TIPO_RETORNO.search(titulo_norm):
        return "retorno_completado"
    if _RE_TIPO_AVISO.search(titulo_norm):
        return "aviso_inicio"
    return None


def _parsear_json_respuesta(texto):
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _leer_keychain(servicio, cuenta):
    """Lee una clave del Keychain de macOS invocando el comando `security`
    directamente, en vez de la librería keyring (falla con -25308 en
    sesiones sin interfaz gráfica, p.ej. por SSH)."""
    resultado = subprocess.run(
        ["security", "find-generic-password", "-s", servicio, "-a", cuenta, "-w"],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        return None
    return resultado.stdout.strip()


def _clave_claude():
    api_key = _leer_keychain(KEYCHAIN_SERVICE_CLAUDE, KEYCHAIN_ACCOUNT_CLAUDE)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de Claude guardada en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE_CLAUDE}', cuenta '{KEYCHAIN_ACCOUNT_CLAUDE}')."
        )
    return api_key


def _clave_nvidia():
    api_key = _leer_keychain(KEYCHAIN_SERVICE_NVIDIA, KEYCHAIN_ACCOUNT_NVIDIA)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de NVIDIA guardada en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE_NVIDIA}', cuenta '{KEYCHAIN_ACCOUNT_NVIDIA}')."
        )
    return api_key


def _llamar_claude(api_key, bloques, max_tokens=2048):
    cliente = anthropic.Anthropic(api_key=api_key)
    respuesta = cliente.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": bloques}],
    )
    return next((b.text for b in respuesta.content if b.type == "text"), None)


def _llamar_nvidia(api_key, contenido, max_tokens=2048):
    payload = {
        "model": MODELO_NVIDIA,
        "messages": [{"role": "user", "content": contenido}],
        "max_tokens": max_tokens,
    }
    peticion = urllib.request.Request(
        f"{NVIDIA_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=240) as resp:
            datos = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"NVIDIA NIM error {e.code}: {cuerpo[:500]}") from e
    return datos["choices"][0]["message"]["content"]


def _imagen_de_pagina(pagina):
    """Igual que en ocr_tickets.py / ocr_comunicaciones.py: estos tickets
    vienen a veces escaneados en apaisado sin flag de rotación que
    PyMuPDF pueda corregir solo; girar 270º deja el texto derecho.

    Si el PNG a 200dpi supera LIMITE_BYTES_IMAGEN (p.ej. L58 PALABRAS
    GANADORAS.pdf, una página de 2889x5624px que salió de 16 MB y superó
    el límite de 10 MB de Claude), reduce el zoom progresivamente y
    vuelve a renderizar hasta que quepa, en vez de dejar que la llamada
    falle y caiga al fallback de NVIDIA por accidente."""
    apaisada = pagina.rect.width > pagina.rect.height
    zoom = 200 / 72
    while True:
        matriz = fitz.Matrix(zoom, zoom).prerotate(270) if apaisada else fitz.Matrix(zoom, zoom)
        pix = pagina.get_pixmap(matrix=matriz)
        datos_png = pix.tobytes("png")
        if len(datos_png) <= LIMITE_BYTES_IMAGEN or zoom <= 50 / 72:
            return base64.b64encode(datos_png).decode("ascii")
        zoom *= 0.8


def _extraer_devolucion_con_vision(clave_claude, imagenes_b64):
    bloques_claude = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}
        for img in imagenes_b64
    ]
    contenido_nvidia = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}}
        for img in imagenes_b64
    ]
    bloques_claude.append({"type": "text", "text": PROMPT_EXTRACCION})
    contenido_nvidia.append({"type": "text", "text": PROMPT_EXTRACCION})

    for intento in range(1, MAX_INTENTOS_VISION + 1):
        try:
            texto_respuesta = _llamar_claude(clave_claude, bloques_claude)
        except Exception as e:
            print(f"  ⚠️  Claude falló ({e}) — usando NVIDIA NIM como respaldo")
            texto_respuesta = _llamar_nvidia(_clave_nvidia(), contenido_nvidia)
        datos = _parsear_json_respuesta(texto_respuesta) if texto_respuesta else None
        if datos is not None:
            return datos
        print(f"  ⚠️  Respuesta no interpretable en el intento {intento}/{MAX_INTENTOS_VISION}")
        if intento == MAX_INTENTOS_VISION and texto_respuesta:
            print(f"      Respuesta cruda (primeros 500 caracteres): {texto_respuesta[:500]!r}")
    return None


def procesar_devolucion(ruta_pdf, clave_claude):
    """Procesa un PDF (normalmente 1 página) y devuelve el JSON
    estructurado. Siempre expone "fecha" y "libros_devueltos" (lista de
    {"libro", "producto", "motivo"}) para que asistente.py pueda leer
    cualquiera de los dos tipos de documento sin distinguirlos."""
    doc = fitz.open(ruta_pdf)
    try:
        imagenes_b64 = [_imagen_de_pagina(pagina) for pagina in doc]
    finally:
        doc.close()

    datos = _extraer_devolucion_con_vision(clave_claude, imagenes_b64)
    if datos is None:
        raise RuntimeError(f"No se pudo interpretar {ruta_pdf.name} tras {MAX_INTENTOS_VISION} intentos")

    tipo = _identificar_tipo(datos.get("titulo"))
    if tipo is None:
        print(f"  ⚠️  Título no reconocido ({datos.get('titulo')!r}) — se guarda igualmente con tipo null")

    tabla = datos.get("tabla_libros") or []
    libros_devueltos = [
        {
            "libro": fila.get("libro"),
            "producto": fila.get("juego"),
            "motivo": None,
        }
        for fila in tabla
    ]

    return {
        "archivo": ruta_pdf.name,
        "tipo": tipo,
        "fecha": datos.get("fecha_emision"),
        "hora": datos.get("hora_emision"),
        "juego": datos.get("juego"),
        "libros_devueltos": libros_devueltos,
        "fechas_mencionadas": datos.get("fechas_mencionadas") or [],
        "texto_completo": datos.get("texto_completo"),
        "total_libros": datos.get("total_libros"),
    }


def procesar_todos(carpeta=DEVOLUCION_LIBROS_PATH):
    clave_claude = _clave_claude()
    pdfs = sorted(carpeta.glob("*.pdf"))
    if not pdfs:
        print(f"No hay PDFs en {carpeta}")
        return []

    resultados = []
    for ruta_pdf in pdfs:
        destino = ruta_pdf.with_suffix(".json")
        if destino.exists():
            print(f"--- {ruta_pdf.name} ---")
            print(f"  ⏭️  Saltado (ya existe {destino.name})")
            continue
        print(f"--- Procesando {ruta_pdf.name} ---")
        try:
            datos = procesar_devolucion(ruta_pdf, clave_claude)
        except Exception as e:
            print(f"  ⚠️  Error procesando {ruta_pdf.name}: {e}")
            continue
        with open(destino, "w") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        print(f"  ✅ Guardado: {destino.name}")
        resultados.append(datos)
    return resultados


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ruta = Path(sys.argv[1]).expanduser()
        if not ruta.is_absolute():
            ruta = DEVOLUCION_LIBROS_PATH / ruta
        clave_claude_unica = _clave_claude()
        datos_unico = procesar_devolucion(ruta, clave_claude_unica)
        print(json.dumps(datos_unico, indent=2, ensure_ascii=False))
    else:
        procesar_todos()
