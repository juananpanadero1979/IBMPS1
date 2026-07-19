#!/usr/bin/env python3
"""
OCR de Comunicaciones TPV - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Procesa los PDFs de avisos impresos por el TPV en
ONCE/LIQUIDACIONES/Comunicaciones TPV/ (escaneos de tickets térmicos con
avisos como finalización de venta voluntaria de un juego, plazos de
devolución de boletos, etc.) y genera un JSON junto a cada PDF.

IMPORTANTE (misma regla que ocr_tickets.py, ver CLAUDE.md — "los cálculos
del cuadre ONCE se hacen SIEMPRE con Python, nunca con IA"): el modelo de
visión SOLO transcribe lo que está impreso — fechas, tipo de aviso, texto
— sin interpretar cuál de varias fechas es "la importante". Se le pide
que liste TODAS las fechas que aparecen con una breve descripción tal
cual las describe el texto; decidir cuál cae dentro de los próximos N
días (para la sección "AVISOS PRÓXIMOS" de informe_manana.py) es un
cálculo de fechas que hace Python, no el modelo.

A diferencia de ocr_tickets.py, aquí no hay tipos de página distintos que
identificar por título ni tablas con columnas — cada PDF es un único
aviso de texto libre (normalmente 1 página), así que no hace falta la
lógica de extractores-por-tipo ni el reintento "hay filas pero falta el
total". Si el PDF tiene varias páginas, se envían todas juntas al modelo
en una sola llamada y se pide un único JSON combinado.

Los nombres de fichero NO codifican una fecha (a diferencia de los
tickets DDMMAA.pdf) — son descriptivos (p.ej. "L52 GALLETA FORTUNA.pdf"),
así que el JSON se guarda con el mismo nombre base que el PDF.

Uso:
    python3 ocr_comunicaciones.py                  # procesa todos los PDF pendientes
    python3 ocr_comunicaciones.py "L52 GALLETA FORTUNA.pdf"   # procesa solo uno y lo imprime
"""

import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import anthropic
import fitz  # PyMuPDF

from portal_once import ONCE_PATH

COMUNICACIONES_PATH = ONCE_PATH / "LIQUIDACIONES" / "Comunicaciones TPV"

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
Esta es una imagen de un ticket/aviso impreso por un TPV de un punto de \
venta de lotería (ONCE) — un aviso administrativo (p.ej. finalización de \
venta voluntaria de un juego, plazo de devolución de boletos), no un \
ticket de ventas. Transcribe EXACTAMENTE lo que ves o lees, sin \
interpretar ni decidir cuál dato es "el importante" — eso lo hace \
después un programa, tú solo transcribes.

Devuelve SOLO un objeto JSON válido (sin texto adicional, sin bloques de \
código markdown, sin explicaciones) con esta forma exacta:

{
  "fecha_aviso": "<fecha de emisión del aviso, formato DD/MM/AAAA, tal cual aparece>",
  "hora_aviso": "<hora de emisión si aparece, formato HH:MM:SS, o null>",
  "tipo_comunicacion": "<el título/tipo de aviso tal cual aparece, p.ej. 'L52 FINALIZACIÓN VENTA VOLUNTARIA PRIMER AVISO'>",
  "juego": "<nombre del juego/producto al que se refiere el aviso, si se menciona, o null>",
  "fechas_mencionadas": [
    {"fecha": "DD/MM/AAAA", "descripcion": "<qué significa esa fecha, resumido con las propias palabras del texto>"}
  ],
  "texto_completo": "<transcripción completa y literal de todo el texto del aviso, línea a línea>"
}

Reglas:
- Incluye en "fechas_mencionadas" TODAS las fechas que aparecen en el \
cuerpo del aviso (no la fecha/hora de emisión, que ya va en "fecha_aviso" \
/ "hora_aviso" por separado), en el mismo orden en que aparecen.
- No incluyas el nombre/NIF/código de vendedor, ni el código de barras/QR, \
ni las marcas de agua repetidas de fondo ("ONCE", "JUEGA RESPONSABLEMENTE", \
textos girados/duplicados que se ven a través del papel) — solo el texto \
del aviso en sí.
- Si algún campo no aparece en el ticket, usa null (o lista vacía para \
fechas_mencionadas), no inventes ni deduzcas nada.
"""


def _parsear_json_respuesta(texto):
    """Extrae el primer objeto JSON de la respuesta del modelo, aunque
    venga envuelto en explicación o bloques de código markdown."""
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _leer_keychain(servicio, cuenta):
    """Lee una clave del Keychain de macOS invocando el comando `security`
    directamente, en vez de la librería keyring: keyring falla con el
    error -25308 (errSecInteractionNotAllowed) cuando se ejecuta desde
    una sesión sin interfaz gráfica (p.ej. por SSH) — `security` sí puede
    leer del Keychain de login ya desbloqueado en esos casos."""
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
    """Igual que en ocr_tickets.py: estos tickets vienen a veces
    escaneados en apaisado (ancho > alto) sin flag de rotación que
    PyMuPDF pueda corregir solo; girar 270º deja el texto derecho.

    Si el PNG a 200dpi supera LIMITE_BYTES_IMAGEN (algunos escaneos
    vienen en páginas muy grandes y superan los 10 MB que admite la API
    de Claude), reduce el zoom progresivamente y vuelve a renderizar
    hasta que quepa, en vez de dejar que la llamada falle y caiga al
    fallback de NVIDIA por accidente."""
    apaisada = pagina.rect.width > pagina.rect.height
    zoom = 200 / 72
    while True:
        matriz = fitz.Matrix(zoom, zoom).prerotate(270) if apaisada else fitz.Matrix(zoom, zoom)
        pix = pagina.get_pixmap(matrix=matriz)
        datos_png = pix.tobytes("png")
        if len(datos_png) <= LIMITE_BYTES_IMAGEN or zoom <= 50 / 72:
            return base64.b64encode(datos_png).decode("ascii")
        zoom *= 0.8


def _extraer_comunicacion_con_vision(clave_claude, imagenes_b64):
    """Envía todas las páginas del PDF (normalmente 1) en una sola
    llamada y pide un único JSON combinado — a diferencia de
    ocr_tickets.py, aquí no hay varios tipos de página distintos que
    identificar por separado."""
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


def procesar_comunicacion(ruta_pdf, clave_claude):
    """Procesa un PDF de aviso (normalmente 1 página) y devuelve el JSON
    estructurado."""
    doc = fitz.open(ruta_pdf)
    try:
        imagenes_b64 = [_imagen_de_pagina(pagina) for pagina in doc]
    finally:
        doc.close()

    datos = _extraer_comunicacion_con_vision(clave_claude, imagenes_b64)
    if datos is None:
        raise RuntimeError(f"No se pudo interpretar {ruta_pdf.name} tras {MAX_INTENTOS_VISION} intentos")

    return {
        "archivo": ruta_pdf.name,
        "fecha_aviso": datos.get("fecha_aviso"),
        "hora_aviso": datos.get("hora_aviso"),
        "tipo_comunicacion": datos.get("tipo_comunicacion"),
        "juego": datos.get("juego"),
        "fechas_mencionadas": datos.get("fechas_mencionadas") or [],
        "texto_completo": datos.get("texto_completo"),
    }


def procesar_todos(carpeta=COMUNICACIONES_PATH):
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
            datos = procesar_comunicacion(ruta_pdf, clave_claude)
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
            ruta = COMUNICACIONES_PATH / ruta
        clave_claude_unica = _clave_claude()
        datos_unico = procesar_comunicacion(ruta, clave_claude_unica)
        print(json.dumps(datos_unico, indent=2, ensure_ascii=False))
    else:
        procesar_todos()
