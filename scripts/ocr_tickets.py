#!/usr/bin/env python3
"""
OCR de tickets TPV - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Procesa los PDFs de tickets de TPV en ONCE/LIQUIDACIONES/tickets/
(escaneos de 5 páginas: PAGOS RASCA, CIERRE DE VENTAS, CIERRE DEVOLUCION,
CIERRE PAGO PREMIOS, RESUMEN ACTIVIDAD DIARIA) y genera un JSON
estructurado por ticket.

IMPORTANTE (regla del proyecto, ver CLAUDE.md — "los cálculos del cuadre
ONCE se hacen SIEMPRE con Python, nunca con IA"): el modelo de visión se
usa ÚNICAMENTE para transcribir lo que hay impreso en cada página (título,
filas de la tabla, líneas de "ETIQUETA valor"), tal cual, sin interpretar
ni sumar nada — se le pide explícitamente que no calcule. Todo el
parseo (texto -> número, identificación de qué línea es cada total,
qué tipo de ticket es cada página) se hace después en Python con reglas
explícitas y verificables, nunca dejando que el modelo "calcule" un total.

Enrutamiento del modelo de visión:
  1. Claude Sonnet 5 (claude-sonnet-5) — modelo PRINCIPAL: el más fiable
     para seguir las reglas de transcripción sin omitir líneas de total
     (se probó Haiku 4.5 como alternativa más barata/rápida, pero omitía
     de forma persistente varios totales incluso con reintentos — no
     compensa el ahorro). Requiere clave en el Keychain de macOS
     (servicio "IBMPS1-ClaudeAPI", cuenta "ANTHROPIC_API_KEY").
  2. NVIDIA NIM (meta/llama-3.2-90b-vision-instruct) — FALLBACK: solo se
     usa si la llamada a Claude falla (sin crédito, error de red, límite
     de tasa...). Es gratis pero mucho más lento. Requiere clave en el
     Keychain (servicio "NVIDIA", cuenta "API_KEY").

Uso:
    python3 ocr_tickets.py                  # procesa todos los PDF de la carpeta
    python3 ocr_tickets.py 040726.pdf       # procesa solo un ticket y lo imprime
"""

import base64
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import anthropic
import fitz  # PyMuPDF
import keyring

from portal_once import ONCE_PATH

TICKETS_PATH = ONCE_PATH / "LIQUIDACIONES" / "tickets"

KEYCHAIN_SERVICE_CLAUDE = "IBMPS1-ClaudeAPI"
KEYCHAIN_ACCOUNT_CLAUDE = "ANTHROPIC_API_KEY"
MODELO_CLAUDE = "claude-sonnet-5"

KEYCHAIN_SERVICE_NVIDIA = "NVIDIA"
KEYCHAIN_ACCOUNT_NVIDIA = "API_KEY"
MODELO_NVIDIA = "meta/llama-3.2-90b-vision-instruct"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

PROMPT_EXTRACCION = """\
Esta es una página de un ticket térmico de TPV de un punto de venta de \
lotería (ONCE). Transcribe EXACTAMENTE lo que ves o lees, sin interpretar, \
calcular ni corregir nada — ni siquiera para que las cuentas cuadren.

Devuelve SOLO un objeto JSON válido (sin texto adicional, sin bloques de \
código markdown, sin explicaciones) con esta forma exacta:

{
  "titulo": "<el título grande de este ticket, tal cual aparece, p.ej. CIERRE DE VENTAS>",
  "filas": [
    {"col_1": "...", "col_2": "...", "col_3": "...", "col_4": "..."}
  ]
}

Reglas:
- Recorre la página de arriba a abajo y añade una entrada en "filas" por \
CADA línea con contenido, sin excepción: filas de una tabla, líneas de \
"ETIQUETA valor", cabeceras de sección (p.ej. "PAGOS CON TARJETA"), Y \
TAMBIÉN las líneas de total/resumen que aparecen después de una tabla \
(p.ej. "TOTAL CUPONES DEVUELTOS 11", "IMPORTE TOTAL EUROS 22,00 EUR", \
"TOTAL 83,00 EUR") — estas líneas de total son tan importantes como las \
filas de la tabla y NUNCA deben omitirse.
- No incluyas la fila de cabecera con los nombres de columna (p.ej. \
"Fecha Producto Unidades Eur"), ni el nombre/NIF del vendedor, ni el \
código de barras/QR final, ni la línea de fecha y hora de impresión del \
ticket (p.ej. "05 JUL 26 11:51:56").
- Usa col_1, col_2, col_3, col_4 en el mismo orden en que aparecen de \
izquierda a derecha. Si una línea tiene menos de 4 valores, deja los \
campos sobrantes como cadena vacía "". Si una etiqueta de varias palabras \
queda repartida en columnas (p.ej. "31" en col_1 y "VENTAS" en col_2), \
transcríbelo tal cual, no lo combines.
- Transcribe los números tal cual están impresos: con coma decimal, con \
signo negativo si lo hay, sin redondear ni sumar nada.

Ejemplo de cómo tratar una línea de total tras una tabla (no uses estos \
valores, es solo para ilustrar el formato):
{"col_1": "TOTAL", "col_2": "83,00 EUR", "col_3": "", "col_4": ""}

Caso especial — en el ticket "PAGOS RASCA", el pie de la tabla es la \
etiqueta "TOTAL" seguida en la línea de abajo por "BOLETOS", y a la \
derecha de esas dos palabras hay DOS números apilados: arriba un importe \
en euros (p.ej. "44,00 EUR") y abajo, justo debajo, un simple recuento \
de boletos (p.ej. "17", sin "EUR" y sin coma decimal). SON DOS VALORES \
DISTINTOS, no lo confundas ni omitas ninguno de los dos: vuelca ambos, \
en este formato exacto (no uses estos valores, es solo para ilustrar):
{"col_1": "TOTAL BOLETOS", "col_2": "44,00 EUR", "col_3": "17", "col_4": ""}
"""

# Orden de comprobación: del más específico al más genérico, para evitar
# falsos positivos (p.ej. "CIERRE PAGO PREMIOS" contiene "PAGO" igual que
# "PAGOS RASCA", así que se busca por la palabra más distintiva de cada uno).
ORDEN_TIPOS = [
    ("resumen", "RESUMEN ACTIVIDAD DIARIA"),
    ("libros_vendidos", "LIBROS VENDIDOS"),
    ("pagos_rasca", "PAGOS RASCA"),
    ("devoluciones", "DEVOLUCION"),
    ("premios_pagados", "PAGO PREMIOS"),
    ("ventas", "CIERRE DE VENTAS"),
]

_RE_FECHA_DDMM = re.compile(r"^\d{2}-\d{2}$")


def _sin_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def _parse_num(texto):
    """Convierte un importe/número en formato español ("83,00 EUR",
    "-38,40 E", "1.234,56") a float. None si no hay nada numérico."""
    if texto is None:
        return None
    texto = re.sub(r"[^\d,.\-]", "", str(texto)).strip()
    if not texto or texto in ("-", ".", ","):
        return None
    texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


def _parse_int(texto):
    valor = _parse_num(texto)
    return int(valor) if valor is not None else None


def _valor_numerico_de_fila(fila):
    """Primer valor numérico no vacío entre col_2, col_3, col_4 de una fila
    genérica (para localizar el importe de una línea de total sin asumir
    en qué columna exacta cayó)."""
    for clave in ("col_2", "col_3", "col_4"):
        valor = _parse_num(fila.get(clave))
        if valor is not None:
            return valor
    return None


def _linea_completa(fila):
    """Todas las columnas de una fila unidas en un solo texto, para poder
    buscar palabras clave ("TOTAL", "IMPORTE"...) sin depender de en qué
    columna exacta cayeron — el modelo no siempre reparte una etiqueta de
    varias palabras (p.ej. "31 VENTAS") de la misma forma."""
    partes = [str(fila.get(f"col_{i}") or "").strip() for i in range(1, 5)]
    return " ".join(p for p in partes if p)


_RE_PIE_FECHA_HORA = re.compile(r"\d{1,2}\s+[A-Z]{3}\.?\s+\d{2,4}\s+\d{2}:\d{2}:\d{2}", re.IGNORECASE)


def _es_ruido(linea):
    """Líneas que el modelo a veces incluye por error pese a la instrucción
    de omitirlas (pie de fecha/hora de impresión del ticket)."""
    return bool(_RE_PIE_FECHA_HORA.search(linea))


def _fecha_desde_nombre(nombre):
    """"040726" -> "04/07/2026". El nombre de archivo DDMMAA indica la
    fecha del ticket; se asume siglo 20xx."""
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", nombre)
    if not m:
        return None
    dia, mes, anio = m.groups()
    return f"{dia}/{mes}/20{anio}"


def _identificar_tipo(titulo):
    titulo_norm = _sin_acentos(titulo or "").upper()
    for tipo, clave in ORDEN_TIPOS:
        if _sin_acentos(clave).upper() in titulo_norm:
            return tipo
    return None


def _extraer_ventas_o_devoluciones(filas):
    """CIERRE DE VENTAS / CIERRE DEVOLUCION: filas de datos = fecha (DD-MM)
    | producto | unidades | importe. El total se identifica por las
    líneas con "TOTAL"/"IMPORTE" en la etiqueta (en CIERRE DEVOLUCION hay
    dos candidatas — "TOTAL CUPONES DEVUELTOS" con un recuento de cupones,
    e "IMPORTE TOTAL EUROS" con el importe real — y no siempre aparecen en
    el mismo orden entre una llamada y otra), así que se prefiere el
    primer valor con pinta de importe (coma decimal o "EUR") sobre un
    simple recuento entero, igual que en pagos_rasca."""
    detalle = []
    total = None
    total_parece_importe = False
    for fila in filas:
        col1 = (fila.get("col_1") or "").strip()
        linea_norm = _sin_acentos(_linea_completa(fila)).upper()
        if _RE_FECHA_DDMM.match(col1):
            detalle.append({
                "fecha": col1,
                "producto": (fila.get("col_2") or "").strip(),
                "unidades": _parse_int(fila.get("col_3")),
                "importe": _parse_num(fila.get("col_4")),
            })
        elif "TOTAL" in linea_norm or "IMPORTE" in linea_norm:
            for clave in ("col_2", "col_3", "col_4"):
                texto = str(fila.get(clave) or "").strip()
                valor = _parse_num(texto)
                if valor is None:
                    continue
                parece_importe = bool(re.search(r",\d{2}|EUR|€", texto, re.IGNORECASE))
                if parece_importe or not total_parece_importe:
                    total = valor
                    total_parece_importe = total_parece_importe or parece_importe
                break
    return detalle, total


def _extraer_premios_pagados(filas):
    """CIERRE PAGO PREMIOS: filas de datos = producto | premio | unidades
    | importe, hasta la línea "TOTAL"; lo que viene después (CUPONES
    TRAD., CUPONES ELEC...) es metadata que no forma parte del esquema
    pedido y se ignora."""
    detalle = []
    total = None
    fin_tabla = False
    for fila in filas:
        col1 = (fila.get("col_1") or "").strip()
        linea_norm = _sin_acentos(_linea_completa(fila)).upper()
        if not fin_tabla and re.search(r"\bTOTAL\b", linea_norm):
            total = _valor_numerico_de_fila(fila)
            fin_tabla = True
            continue
        if fin_tabla or not col1 or _es_ruido(linea_norm) or linea_norm.startswith("CUPONES"):
            continue
        # una fila de datos real siempre trae al menos un valor numérico
        # (unidades y/o importe); si no, es ruido (p.ej. la fecha del
        # encabezado del ticket colándose como si fuera una fila más)
        if _valor_numerico_de_fila(fila) is None:
            continue
        detalle.append({
            "producto": col1,
            "premio": (fila.get("col_2") or "").strip(),
            "unidades": _parse_int(fila.get("col_3")),
            "importe": _parse_num(fila.get("col_4")),
        })
    return detalle, total


def _extraer_pagos_rasca(filas):
    """PAGOS RASCA: filas de datos = juego | premio (importe) | unidades
    | importe total categoría, hasta la línea "TOTAL BOLETOS". En el
    ticket real esta etiqueta va envuelta en 2-3 líneas físicas ("TOTAL" /
    "BOLETOS  44,00 EUR" / "17", donde "17" es el nº de boletos, no un
    importe), y el modelo no siempre las agrupa igual entre una pasada y
    otra. Por eso, en cuanto aparece "TOTAL" se deja de añadir filas a
    detalle y se acumulan TODOS los valores numéricos de las filas
    restantes como candidatos, prefiriendo al final el que tenga pinta de
    importe (coma decimal o "EUR") frente a un recuento entero suelto —
    así no se confunde el nº de boletos con el importe total."""
    detalle = []
    fin_tabla = False
    candidatos_total = []  # (parece_importe, valor)

    for fila in filas:
        col1 = (fila.get("col_1") or "").strip()
        linea_norm = _sin_acentos(_linea_completa(fila)).upper()

        if not fin_tabla and re.search(r"\bTOTAL\b", linea_norm):
            fin_tabla = True

        if fin_tabla:
            for clave in ("col_1", "col_2", "col_3", "col_4"):
                texto = str(fila.get(clave) or "").strip()
                valor = _parse_num(texto)
                if valor is not None:
                    parece_importe = bool(re.search(r",\d{2}|EUR|€", texto, re.IGNORECASE))
                    candidatos_total.append((parece_importe, valor))
            continue

        if not col1 or _es_ruido(linea_norm):
            continue
        # una fila de datos real siempre trae al menos un valor numérico
        # (premio, unidades y/o importe); si no, es ruido (p.ej. la fecha
        # del encabezado del ticket colándose como si fuera un juego más)
        if _valor_numerico_de_fila(fila) is None:
            continue
        detalle.append({
            "juego": col1,
            "premio": _parse_num(fila.get("col_2")),
            "unidades": _parse_int(fila.get("col_3")),
            "importe": _parse_num(fila.get("col_4")),
        })

    total = next((valor for parece, valor in candidatos_total if parece), None)
    if total is None and candidatos_total:
        total = candidatos_total[0][1]

    return detalle, total


def _extraer_libros_vendidos(filas):
    """LIBROS VENDIDOS: filas de datos = juego (código numérico, p.ej.
    "59") | libro (nº de libro) | tipo (p.ej. "NORM"). A diferencia de
    los demás tickets, este NO tiene ninguna columna de importe — el pie
    "TOTAL LIBROS N" es un recuento de libros vendidos, no un importe en
    euros, así que se expone como total_unidades en vez de total."""
    detalle = []
    total_unidades = None
    for fila in filas:
        col1 = (fila.get("col_1") or "").strip()
        col2 = (fila.get("col_2") or "").strip()
        linea_norm = _sin_acentos(_linea_completa(fila)).upper()

        if "TOTAL" in linea_norm and "LIBROS" in linea_norm:
            m = re.search(r"(\d+)\s*$", linea_norm)
            if m:
                total_unidades = int(m.group(1))
            continue

        if not col1 or not col2 or _es_ruido(linea_norm):
            continue
        # el modelo a veces incluye la fila de cabecera de columnas pese a
        # la instrucción de omitirla ("JUEGO" | "LIBRO" | "TIPO")
        if col1.strip().upper() == "JUEGO":
            continue
        detalle.append({
            "juego": col1,
            "libro": col2,
            "tipo": (fila.get("col_3") or "").strip(),
        })
    return detalle, total_unidades


def _extraer_resumen(filas):
    """RESUMEN ACTIVIDAD DIARIA: no es una tabla de 4 columnas sino una
    lista de líneas "ETIQUETA valor". Hay dos líneas "TOTAL" a secas en la
    página (el resultado general, y el total de "PAGOS CON TARJETA") que
    solo se distinguen por el ORDEN en que aparecen, así que se recorre
    la página de arriba a abajo llevando el estado de en qué sección
    estamos, en vez de buscar por palabra clave suelta."""
    ventas_total = pagos_total = resultado = pagos_tarjeta = None
    en_seccion_tarjeta = False
    total_visto = 0

    for fila in filas:
        # el "valor" (importe) puede caer en cualquier columna según cómo
        # el modelo reparta una etiqueta de varias palabras (p.ej. "31" en
        # col_1 y "VENTAS" en col_2, con el importe entonces en col_3) —
        # _valor_numerico_de_fila ya prueba col_2..col_4 en orden.
        etiqueta = _sin_acentos(_linea_completa(fila)).upper()
        col1_solo = _sin_acentos((fila.get("col_1") or "").strip()).upper()
        valor = _valor_numerico_de_fila(fila)

        if etiqueta == "PAGOS CON TARJETA":
            en_seccion_tarjeta = True
            continue
        if re.search(r"\b\d+\s+VENTAS\b", etiqueta):
            ventas_total = valor
        elif re.search(r"\b\d+\s+PAGOS\b", etiqueta):
            pagos_total = valor
        elif col1_solo == "TOTAL" and _parse_num(fila.get("col_2")) is not None:
            # distingue "TOTAL" a secas (seguido del importe) de etiquetas
            # como "TOTAL PAGOS" / "TOTAL RET." (sección TOPii), que también
            # empiezan por "TOTAL" pero cuyo siguiente token no es un número
            total_visto += 1
            if total_visto == 1:
                resultado = valor
            elif en_seccion_tarjeta and pagos_tarjeta is None:
                pagos_tarjeta = valor

    resumen = {
        "ventas_total": ventas_total,
        "pagos_total": pagos_total,
        "resultado": resultado,
    }
    return resumen, pagos_tarjeta


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


def _clave_claude():
    api_key = keyring.get_password(KEYCHAIN_SERVICE_CLAUDE, KEYCHAIN_ACCOUNT_CLAUDE)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de Claude guardada en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE_CLAUDE}', cuenta '{KEYCHAIN_ACCOUNT_CLAUDE}')."
        )
    return api_key


def _clave_nvidia():
    api_key = keyring.get_password(KEYCHAIN_SERVICE_NVIDIA, KEYCHAIN_ACCOUNT_NVIDIA)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de NVIDIA guardada en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE_NVIDIA}', cuenta '{KEYCHAIN_ACCOUNT_NVIDIA}')."
        )
    return api_key


def _llamar_claude(api_key, bloques, max_tokens=4096):
    """Llama a Claude Sonnet 5 (modelo principal) y devuelve el texto de
    la respuesta. `bloques` son content blocks al estilo Anthropic
    ({"type": "text"|"image", ...})."""
    cliente = anthropic.Anthropic(api_key=api_key)
    respuesta = cliente.messages.create(
        model=MODELO_CLAUDE,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": bloques}],
    )
    return next((b.text for b in respuesta.content if b.type == "text"), None)


def _llamar_nvidia(api_key, contenido, max_tokens=4096):
    """POST a la API de NVIDIA NIM (compatible con OpenAI) y devuelve el
    texto de la respuesta. Es el FALLBACK: solo se llama si Claude falla.
    `contenido` es la lista de content-parts al estilo OpenAI (bloques
    {"type": "text", ...} y opcionalmente {"type": "image_url", ...})."""
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


MAX_INTENTOS_VISION = 3
MAX_INTENTOS_TOTAL_VACIO = 2


def _extraer_pagina_con_vision(clave_claude, pagina, num_pagina=None):
    """Convierte la página a imagen SOLO si no tiene capa de texto nativa
    (estos tickets son escaneos, pero por robustez se comprueba primero).
    En ambos casos, es el modelo de visión quien transcribe — nunca
    calcula — y Python quien estructura el resultado.

    Enrutamiento: se intenta primero con Claude Sonnet 5 (modelo
    principal, el más fiable). Si la llamada en sí falla (sin crédito,
    error de red, límite de tasa...) se reintenta esa misma página con
    NVIDIA NIM como red de seguridad — NVIDIA es gratis pero mucho más
    lento, así que solo se usa cuando Claude no responde en absoluto, no
    por variabilidad normal del modelo (eso lo cubre el reintento de más
    abajo).

    El formato exacto de la respuesta no es 100% determinista de una
    llamada a otra (a veces envuelve el JSON en texto extra o lo corta de
    forma distinta), así que si no se puede interpretar se reintenta unas
    pocas veces antes de rendirse — no es un fallo de esta página en
    concreto, sino variabilidad puntual del modelo.

    Algunos tickets están escaneados en apaisado (el contenido viene
    girado 90º dentro de una página con ancho > alto, sin que el PDF lo
    marque con un flag de rotación que PyMuPDF pueda corregir solo) — se
    comprobó visualmente que girar 270º deja el texto derecho. Sin esto,
    el modelo confunde columnas y hasta dígitos (p.ej. "66" leído como
    "99") al intentar leer el texto girado directamente."""
    texto_nativo = pagina.get_text().strip()
    if texto_nativo:
        texto_pagina = f"Texto extraído de la página (capa de texto nativa del PDF, no es una imagen escaneada):\n\n{texto_nativo}"
        bloques_claude = [{"type": "text", "text": texto_pagina}]
        contenido_nvidia = [{"type": "text", "text": texto_pagina}]
    else:
        apaisada = pagina.rect.width > pagina.rect.height
        zoom = 200 / 72
        matriz = fitz.Matrix(zoom, zoom).prerotate(270) if apaisada else fitz.Matrix(zoom, zoom)
        pix = pagina.get_pixmap(matrix=matriz)
        imagen_b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
        bloques_claude = [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": imagen_b64},
        }]
        contenido_nvidia = [{
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{imagen_b64}"},
        }]

    bloques_claude.append({"type": "text", "text": PROMPT_EXTRACCION})
    contenido_nvidia.append({"type": "text", "text": PROMPT_EXTRACCION})

    etiqueta_pagina = f"página {num_pagina}" if num_pagina else "página"

    for intento in range(1, MAX_INTENTOS_VISION + 1):
        try:
            texto_respuesta = _llamar_claude(clave_claude, bloques_claude)
        except Exception as e:
            print(f"  ⚠️  {etiqueta_pagina}: Claude falló ({e}) — usando NVIDIA NIM como respaldo")
            texto_respuesta = _llamar_nvidia(_clave_nvidia(), contenido_nvidia)
        datos = _parsear_json_respuesta(texto_respuesta) if texto_respuesta else None
        if datos is not None:
            return datos
        print(f"  ⚠️  {etiqueta_pagina}: respuesta no interpretable en el intento {intento}/{MAX_INTENTOS_VISION}")
        if intento == MAX_INTENTOS_VISION and texto_respuesta:
            print(f"      Respuesta cruda (primeros 500 caracteres): {texto_respuesta[:500]!r}")
    return None


def procesar_ticket(ruta_pdf, clave_claude):
    """Procesa un PDF de ticket (5 páginas) y devuelve el JSON estructurado
    con el esquema acordado. Identifica cada página por su título, no por
    su posición, así que no depende de que las 5 secciones vengan siempre
    en el mismo orden."""
    resultado = {
        "fecha": _fecha_desde_nombre(ruta_pdf.stem),
        "ventas": {"detalle": [], "total": None},
        "devoluciones": {"detalle": [], "total": None},
        "premios_pagados": {"detalle": [], "total": None},
        "pagos_tarjeta": None,
        "resumen": {"ventas_total": None, "pagos_total": None, "resultado": None},
        "pagos_rasca": {"detalle": [], "total": None},
        "libros_vendidos": {"detalle": [], "total_unidades": None},
    }

    doc = fitz.open(ruta_pdf)
    try:
        for num_pagina, pagina in enumerate(doc, start=1):
            datos_pagina = _extraer_pagina_con_vision(clave_claude, pagina, num_pagina)
            if not datos_pagina:
                print(f"  ⚠️  Página {num_pagina}: no se pudo interpretar tras {MAX_INTENTOS_VISION} intentos, se omite")
                continue

            tipo = _identificar_tipo(datos_pagina.get("titulo"))
            filas = datos_pagina.get("filas") or []

            extractor_y_clave = {
                "ventas": (_extraer_ventas_o_devoluciones, "ventas", "total"),
                "devoluciones": (_extraer_ventas_o_devoluciones, "devoluciones", "total"),
                "premios_pagados": (_extraer_premios_pagados, "premios_pagados", "total"),
                "pagos_rasca": (_extraer_pagos_rasca, "pagos_rasca", "total"),
                "libros_vendidos": (_extraer_libros_vendidos, "libros_vendidos", "total_unidades"),
            }

            if tipo in extractor_y_clave:
                extractor, clave, nombre_total = extractor_y_clave[tipo]
                detalle, total = extractor(filas)
                # si hay filas de detalle pero el total salió vacío, lo más
                # probable es que el modelo haya omitido esa línea en esta
                # pasada (variabilidad ya observada) — se vuelve a pedir la
                # misma página unas pocas veces más antes de aceptarlo así
                intentos_extra = 0
                while total is None and detalle and intentos_extra < MAX_INTENTOS_TOTAL_VACIO:
                    intentos_extra += 1
                    print(f"  ⚠️  Página {num_pagina} ({tipo}): hay filas de detalle pero el total salió vacío, "
                          f"reintentando página completa ({intentos_extra}/{MAX_INTENTOS_TOTAL_VACIO})...")
                    datos_reintento = _extraer_pagina_con_vision(clave_claude, pagina, num_pagina)
                    if not datos_reintento:
                        break
                    detalle, total = extractor(datos_reintento.get("filas") or [])
                resultado[clave] = {"detalle": detalle, nombre_total: total}
            elif tipo == "resumen":
                resumen, pagos_tarjeta = _extraer_resumen(filas)
                # a diferencia de los demás tipos, aquí no hace falta
                # comprobar que "la página se leyó" como condición para
                # reintentar: el título ya confirmó que es la página
                # RESUMEN correcta (no hay riesgo de reintentar un tipo
                # equivocado), así que si falta pagos_tarjeta o resultado
                # se reintenta siempre, aunque el resto también haya
                # salido vacío — es precisamente el caso que más lo necesita
                intentos_extra = 0
                while (
                    (pagos_tarjeta is None or resumen.get("resultado") is None)
                    and intentos_extra < MAX_INTENTOS_TOTAL_VACIO
                ):
                    intentos_extra += 1
                    print(f"  ⚠️  Página {num_pagina} (resumen): pagos_tarjeta o resultado salieron vacíos, "
                          f"reintentando página completa ({intentos_extra}/{MAX_INTENTOS_TOTAL_VACIO})...")
                    datos_reintento = _extraer_pagina_con_vision(clave_claude, pagina, num_pagina)
                    if not datos_reintento:
                        break
                    resumen, pagos_tarjeta = _extraer_resumen(datos_reintento.get("filas") or [])
                resultado["resumen"] = resumen
                resultado["pagos_tarjeta"] = pagos_tarjeta
            else:
                print(f"  ⚠️  Página {num_pagina}: título no reconocido ({datos_pagina.get('titulo')!r})")
    finally:
        doc.close()

    return resultado


def procesar_todos(carpeta=TICKETS_PATH):
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
            datos = procesar_ticket(ruta_pdf, clave_claude)
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
            ruta = TICKETS_PATH / ruta
        clave_claude_unica = _clave_claude()
        datos_unico = procesar_ticket(ruta, clave_claude_unica)
        print(json.dumps(datos_unico, indent=2, ensure_ascii=False))
    else:
        procesar_todos()
