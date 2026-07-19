#!/usr/bin/env python3
"""
Asistente personal único - Juan Antonio Panadero Jiménez - IBMPS1

Sustituye al sistema de 5 agentes rígidos (CEREBRO + ONCE/SALUD/
INGENIERO/BUTLER, en agente_cerebro.py) por un único asistente
conversacional con Claude Sonnet: un solo system prompt que combina todo
el contexto de dominio (reutilizando el contenido ya escrito en
agentes/*.md, sin duplicarlo) y responde de forma natural a cualquier
pregunta, sin tener que decidir de antemano "a qué agente" pertenece.

Carga automáticamente al inicio los datos reales del día (liquidación,
paquetes, rascas, premios, agenda) y los deja disponibles en el
contexto — el system prompt exige explícitamente responder solo sobre
lo que se pregunta, sin mezclar temas ni mencionar datos que no vienen
a cuento (misma regla que ya funcionó bien en el antiguo agente ONCE).

Puede buscar en la web (p.ej. el tiempo) mediante la herramienta de
búsqueda web nativa de Claude — no inventa datos que puede consultar.

La clave de Claude se lee, en este orden: variable de entorno
ANTHROPIC_API_KEY (p.ej. cargada por siri_query.sh desde ~/.ibmps1_env),
o si no, el Keychain de macOS vía el comando `security` (no la librería
keyring, que falla en sesiones sin interfaz gráfica como SSH).

Toda la llamada tiene un límite de tiempo duro (TIMEOUT_SEGUNDOS): si
algo se queda colgado, el script falla con un mensaje claro en vez de
quedarse mudo para siempre.

Uso:
    python3 asistente.py "¿cuál es mi saldo acreedor?"
    python3 asistente.py "¿qué tiempo hace hoy en Getafe?"
"""

import json
import os
import re
import signal
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from portal_once import (
    COMISIONES_PATH,
    CONSULTAS_PATH,
    ESTADISTICAS_PATH,
    GESTIONA_BASE,
    LIQUIDACIONES_PATH,
    NOMINAS_PATH,
    PAQUETES_PATH,
    PREMIOS_PATH,
    STOCK_PATH,
)

try:
    import cuadre_diario
except Exception:
    cuadre_diario = None

try:
    import alertas_caducidad
except Exception:
    alertas_caducidad = None

try:
    import ventas_producto_periodo
except Exception:
    ventas_producto_periodo = None

try:
    from ocr_comunicaciones import COMUNICACIONES_PATH
except Exception:
    COMUNICACIONES_PATH = None

try:
    from ocr_devolucion_libros import DEVOLUCION_LIBROS_PATH
except Exception:
    DEVOLUCION_LIBROS_PATH = None

try:
    import agenda
except Exception:
    agenda = None

AGENTES_PATH = Path(__file__).resolve().parent.parent / "agentes"

KEYCHAIN_SERVICE = "IBMPS1-ClaudeAPI"
KEYCHAIN_ACCOUNT = "ANTHROPIC_API_KEY"
# Haiku 4.5 en vez de Sonnet: para respuestas de voz cortas (charla,
# datos ONCE ya calculados en Python que solo hay que relayar) no hace
# falta el modelo más caro/lento — Haiku es más rápido y ~3x más barato.
MODELO = "claude-haiku-4-5"

# Dos niveles de timeout: la mayoría de preguntas (saludo, datos ONCE ya
# en JSON local, charla general) no necesitan buscar en la web y deben
# responder rápido; solo las que sí requieren búsqueda web tienen un
# margen mayor, porque se comprobó empíricamente que incluso la
# variante más rápida de búsqueda tarda ~20s como mínimo — un límite de
# 15s aplicado también ahí dejaría esas preguntas sin respuesta real
# nunca (no hay forma de "continuar" una llamada SSH de un solo disparo).
TIMEOUT_RAPIDO = 15
TIMEOUT_BUSQUEDA = 40
MENSAJE_TARDANZA = "Dame un momento, estoy buscando..."


# ── Normalización de texto ───────────────────────────────────────────

def _sin_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def _normalizar(texto):
    return _sin_acentos(texto).lower()


# ── Atajo rápido para saludos y frases de cortesía ───────────────────
# Se responde directamente en Python, sin llamar a Claude — latencia
# casi cero para el caso más común y barato, en vez de esperar varios
# segundos de llamada a la API para decir "hola".

RESPUESTAS_SIMPLES = {
    "hola": "¡Hola Juan Antonio! ¿En qué puedo ayudarte?",
    "buenos dias": "¡Buenos días! ¿En qué puedo ayudarte?",
    "buenas tardes": "¡Buenas tardes! ¿En qué puedo ayudarte?",
    "buenas noches": "¡Buenas noches! ¿En qué puedo ayudarte?",
    "que tal": "Todo bien por aquí. ¿En qué te ayudo?",
    "como estas": "Muy bien, listo para ayudarte. ¿Qué necesitas?",
    "como te va": "Bien, aquí estoy. ¿En qué te ayudo?",
    "gracias": "De nada, aquí estoy si necesitas algo más.",
    "muchas gracias": "De nada, aquí estoy si necesitas algo más.",
    "adios": "¡Hasta luego, Juan Antonio!",
    "hasta luego": "¡Hasta luego, Juan Antonio!",
}


def _respuesta_simple(texto):
    """Si la pregunta es un saludo/frase de cortesía simple y reconocida
    (tras quitar acentos, mayúsculas y signos de puntuación sueltos),
    devuelve la respuesta fija; si no, None."""
    texto_norm = _normalizar(texto).strip(" ¿?¡!.,")
    return RESPUESTAS_SIMPLES.get(texto_norm)


# ── Comando de voz: abrir aplicaciones del Mac ───────────────────────
# "Abre X" / "Abrir X" es una ACCIÓN del sistema, no una pregunta — se
# resuelve con `open -a` en Python puro, sin pasar por Claude (ni
# inteligencia de lenguaje ni latencia de red hacen falta solo para
# extraer un nombre de app de una frase que empieza por un verbo fijo).
#
# NO anclado al principio absoluto de la frase (a propósito, desde
# 2026-07-18): el Atajo de Siri real antepone frases de activación
# variables ("oye Siri", "por favor", el nombre del propio atajo...)
# que .match() con ^ no toleraba — confirmado que por SSH directo (sin
# ese prefijo) sí funcionaba, pero por el Atajo de Siri no. \b delante
# de "abr" evita el otro extremo: que "abre" como substring de otra
# palabra (p.ej. "labre") dispare el comando por error.

_RE_ABRIR_APP = re.compile(r"\babr(?:e|ir)\s+(.+?)\s*[.!?¡¿]*\s*$", re.IGNORECASE)

RUTAS_APLICACIONES = [
    Path("/Applications"),
    Path("/System/Applications"),
    Path.home() / "Applications",
]

TIMEOUT_COMANDO_SISTEMA = 8

# Lanzar apps con GUI directamente desde una sesión SSH devuelve éxito
# (returncode 0) pero la app nunca aparece en pantalla — macOS bloquea el
# lanzamiento de apps con interfaz gráfica desde sesiones sin usuario con
# sesión activa en el escritorio (confirmado en pruebas reales vía
# Termius/SSH, 2026-07-18). PROBADO Y DESCARTADO: `open -a` directo (no
# aparece nada) y `launchctl asuser <uid> open -a` (mismo resultado, sin
# error pero sin efecto — probado el mismo día). Ahora se usa
# `osascript -e 'tell application "X" to activate'`, que pasa por
# Eventos del Sistema/Apple Events en vez de lanzar el proceso
# directamente.


def _ejecutar_comando_sistema(args, timeout=TIMEOUT_COMANDO_SISTEMA):
    """subprocess.run con timeout — comprobado en pruebas reales que
    algunas apps (p.ej. DIGI TV) se quedan colgadas indefinidamente al
    pedirles quit por AppleEvent si están mostrando algo en pantalla que
    pide confirmación; sin este timeout, ese cuelgue se propagaba a todo
    asistente.py (el usuario se quedaba esperando hasta el timeout
    genérico de 15s con un mensaje que no explicaba qué había pasado).
    Si el subproceso se cuelga, se trata igual que un fallo normal
    (returncode 1) en vez de tumbar la petición entera."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="timeout")


def _detectar_comando_abrir_app(pregunta):
    """Si `pregunta` contiene "abre"/"abrir" seguido de un nombre (en
    cualquier posición, no solo al principio — ver aviso junto a
    _RE_ABRIR_APP), devuelve ese nombre tal cual lo dijo el usuario
    (conserva mayúsculas para mostrarlo en la confirmación); si no,
    None. NOTA: esto significa que una frase como "voy a abrir la
    nevera" también dispara este comando (intentará abrir una app
    llamada "la nevera" y fallará con "no he encontrado ninguna
    aplicación...") en vez de responder la pregunta con Claude — riesgo
    aceptado a cambio de reconocer el comando real dicho con prefijos
    variables de Siri."""
    m = _RE_ABRIR_APP.search(pregunta)
    if not m:
        return None
    nombre = m.group(1).strip()
    return nombre or None


def _buscar_app_por_nombre_parcial(nombre_buscado):
    """`open -a` exige el nombre exacto de la app — falla si lo dicho
    por voz no coincide al carácter (p.ej. "digi tv" vs el nombre real
    "DIGI TV Go.app"). Recorre las carpetas de aplicaciones habituales y
    busca una coincidencia parcial case-insensitive en cualquier
    dirección (el nombre real contiene lo dicho, o al revés) antes de
    rendirse.

    FIX de un bug real (probado con "digi tv"): con varios candidatos
    parciales, quedarse con el más CORTO favorecía nombres genéricos que
    casualmente son substring de lo dicho (p.ej. una app llamada "TV"
    ganaba a la real "DIGI TV" porque "tv" está contenido en "digi tv")
    — y encima devolvía éxito falso, porque esa app corta sí existía y
    sí se pudo abrir/cerrar, solo que no era la que el usuario pedía. En
    vez de eso: match exacto primero si existe, y si no, el candidato
    cuya longitud esté más cerca de lo dicho por voz (el más específico,
    no el más corto)."""
    objetivo = nombre_buscado.strip().lower()
    if not objetivo:
        return None
    exactos = []
    parciales = []
    for carpeta in RUTAS_APLICACIONES:
        if not carpeta.exists():
            continue
        for item in carpeta.glob("*.app"):
            nombre_app_norm = item.stem.lower()
            if nombre_app_norm == objetivo:
                exactos.append(item.stem)
            elif objetivo in nombre_app_norm or nombre_app_norm in objetivo:
                parciales.append(item.stem)
    if exactos:
        return exactos[0]
    if not parciales:
        return None
    parciales.sort(key=lambda n: abs(len(n) - len(objetivo)))
    return parciales[0]


def _abrir_aplicacion_mac(nombre_app):
    """Intenta `tell application "nombre_app" to activate` vía osascript;
    si falla, busca una coincidencia parcial en /Applications (y
    variantes) y reintenta con el nombre real encontrado. Devuelve
    (éxito, nombre_realmente_usado).

    osascript/Apple Events en vez de `open -a` o `launchctl asuser`: ver
    aviso arriba de TIMEOUT_COMANDO_SISTEMA — ambas alternativas se
    probaron por SSH real y ninguna hacía aparecer la app en pantalla."""
    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", f'tell application "{nombre_app}" to activate'],
    )
    if resultado.returncode == 0:
        return True, nombre_app

    nombre_real = _buscar_app_por_nombre_parcial(nombre_app)
    if nombre_real is None:
        return False, nombre_app

    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", f'tell application "{nombre_real}" to activate'],
    )
    return resultado.returncode == 0, nombre_real


_RE_CERRAR_APP = re.compile(r"\b(?:cierra|sal\s+de)\s+(.+?)\s*[.!?¡¿]*\s*$", re.IGNORECASE)


def _detectar_comando_cerrar_app(pregunta):
    """Si `pregunta` contiene "cierra"/"sal de" seguido de un nombre (en
    cualquier posición), devuelve ese nombre; si no, None. Mismo
    criterio de no-anclado y mismo riesgo aceptado que
    _detectar_comando_abrir_app (ver aviso ahí)."""
    m = _RE_CERRAR_APP.search(pregunta)
    if not m:
        return None
    nombre = m.group(1).strip()
    return nombre or None


def _cerrar_aplicacion_mac(nombre_app):
    """`tell application "X" to quit` tal cual; si falla, igual que
    _abrir_aplicacion_mac, busca coincidencia parcial en /Applications
    antes de rendirse. Devuelve (éxito, nombre_realmente_usado)."""
    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", f'tell application "{nombre_app}" to quit'],
    )
    if resultado.returncode == 0:
        return True, nombre_app

    nombre_real = _buscar_app_por_nombre_parcial(nombre_app)
    if nombre_real is None:
        return False, nombre_app

    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", f'tell application "{nombre_real}" to quit'],
    )
    return resultado.returncode == 0, nombre_real


# ── Comando de voz: control multimedia (teclas de medios del sistema) ─
# AVISO VERIFICADO EN PRUEBAS (2026-07-18): esto requiere permiso de
# Accesibilidad para el proceso que ejecuta asistente.py (Terminal,
# Shortcuts, lo que sea) en Ajustes del Sistema → Privacidad y seguridad
# → Accesibilidad. Sin ese permiso, `osascript` devuelve
# "no tiene permiso para enviar pulsaciones de teclas" en comandos de
# System Events, y el envío directo de NSEvent/CGEvent de este bloque no
# da error pero el sistema descarta el evento en silencio — confirmado
# probando contra el estado real de la app Música (player state no
# cambiaba tras la pulsación simulada). No es un fallo del código: es
# una barrera de seguridad de macOS que no se puede saltar por script,
# solo concediendo el permiso a mano una vez.

# Códigos NX_KEYTYPE_* (IOKit/hidsystem/ev_keymap.h) — 16/17/18 son
# Play-Pause/Siguiente/Anterior, los mismos que las teclas físicas de un
# teclado Apple.
_TECLA_MEDIA_PLAY_PAUSA = 16
_TECLA_MEDIA_SIGUIENTE = 17
_TECLA_MEDIA_ANTERIOR = 18

_JXA_PULSAR_TECLA_MEDIA = """
ObjC.import("Cocoa")
ObjC.import("CoreGraphics")
function pressMediaKey(key) {{
    function post(down) {{
        var flags = down ? 0xa00 : 0xb00
        var data1 = (key << 16) | flags
        var ev = $.NSEvent.otherEventWithTypeLocationModifierFlagsTimestampWindowNumberContextSubtypeData1Data2(
            $.NSEventTypeSystemDefined, $.NSMakePoint(0,0), 0xa00, 0, 0, null, 8, data1, -1)
        $.CGEventPost($.kCGSessionEventTap, ev.CGEvent)
    }}
    post(true); post(false)
}}
pressMediaKey({tecla})
"""


def _pulsar_tecla_media(codigo_tecla):
    """Simula la pulsación de una tecla multimedia física (Play/Pause,
    Siguiente, Anterior) vía un evento NSSystemDefined/CGEvent — afecta
    a lo que sea que tenga el foco de "Now Playing" del sistema (Música,
    VLC, Kodi, Safari...), igual que una tecla real del teclado. Devuelve
    True si el comando se ejecutó sin error de script (ver aviso de
    permiso de Accesibilidad arriba — no garantiza que haya tenido
    efecto si falta ese permiso)."""
    script = _JXA_PULSAR_TECLA_MEDIA.format(tecla=codigo_tecla)
    resultado = _ejecutar_comando_sistema(["osascript", "-l", "JavaScript", "-e", script])
    return resultado.returncode == 0


PALABRAS_MEDIA_SIGUIENTE = ["avanza", "avanzar", "siguiente"]
PALABRAS_MEDIA_ANTERIOR = ["rebobina", "rebobinar", "atras", "anterior"]
# "para"/"parar" es ambiguo en español (también vale como preposición
# "para ti" o como "detener" en cualquier otro contexto) — se incluye
# porque se pidió explícitamente, es la entrada de todo este bloque con
# más riesgo real de falso positivo.
PALABRAS_MEDIA_PLAY_PAUSA = ["play", "reproduce", "reproducir", "pausa", "pausar", "para", "parar", "stop"]


def _detectar_comando_multimedia(pregunta):
    texto_norm = _normalizar(pregunta)
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_MEDIA_SIGUIENTE):
        return "siguiente"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_MEDIA_ANTERIOR):
        return "anterior"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_MEDIA_PLAY_PAUSA):
        return "play_pausa"
    return None


def _ejecutar_comando_multimedia(comando):
    codigo = {
        "play_pausa": _TECLA_MEDIA_PLAY_PAUSA,
        "siguiente": _TECLA_MEDIA_SIGUIENTE,
        "anterior": _TECLA_MEDIA_ANTERIOR,
    }[comando]
    exito = _pulsar_tecla_media(codigo)
    mensajes = {"play_pausa": "Hecho.", "siguiente": "Siguiente.", "anterior": "Anterior."}
    return exito, mensajes[comando]


# ── Comando de voz: volumen ───────────────────────────────────────────

def _detectar_comando_volumen(pregunta):
    texto_norm = _normalizar(pregunta)
    if re.search(r"\b(silencio|mute|muteado|enmudece)\b", texto_norm):
        return "mute"
    if "volumen" in texto_norm or "volume" in texto_norm:
        if re.search(r"\b(sube|subir|aumenta|aumentar)\b", texto_norm):
            return "subir"
        if re.search(r"\b(baja|bajar|disminuye|disminuir|reduce|reducir)\b", texto_norm):
            return "bajar"
    return None


def _ejecutar_comando_volumen(comando):
    """Sube/baja el volumen 10 puntos calculando el valor real en Python
    (lee el volumen actual, suma/resta y aplica tope 0-100) en vez de
    hacer la aritmética en una sola línea de AppleScript — "set volume
    output volume" da error si el resultado se sale de 0-100, así que
    resolver el tope aquí es más robusto que confiar en que nunca pase."""
    if comando == "mute":
        _ejecutar_comando_sistema(["osascript", "-e", "set volume output muted true"])
        return "Silenciado."

    resultado = _ejecutar_comando_sistema(["osascript", "-e", "output volume of (get volume settings)"])
    try:
        actual = int(resultado.stdout.strip())
    except ValueError:
        actual = 50
    delta = 10 if comando == "subir" else -10
    nuevo = max(0, min(100, actual + delta))
    _ejecutar_comando_sistema(["osascript", "-e", f"set volume output volume {nuevo}"])
    return "Subiendo volumen." if comando == "subir" else "Bajando volumen."


# ── Comando de voz: control de ventana y navegación ───────────────────
# MISMA LIMITACIÓN QUE EL CONTROL MULTIMEDIA (ver arriba): "System
# Events" para keystroke/key code/atributos de ventana requiere permiso
# de Accesibilidad. Sin ese permiso concedido, estos comandos se
# ejecutan sin error mientras el sistema descarta el evento en
# silencio — confirmado en pruebas reales, no es teoría.

PALABRAS_VENTANA_MAXIMIZAR = ["maximiza", "maximizar", "pantalla completa"]
PALABRAS_VENTANA_MINIMIZAR = ["minimiza", "minimizar"]


def _detectar_comando_ventana(pregunta):
    texto_norm = _normalizar(pregunta)
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_VENTANA_MAXIMIZAR):
        return "maximizar"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_VENTANA_MINIMIZAR):
        return "minimizar"
    return None


_APPLESCRIPT_MAXIMIZAR = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set value of attribute "AXFullScreen" of window 1 of frontApp to true
end tell
"""
_APPLESCRIPT_MINIMIZAR = 'tell application "System Events" to keystroke "m" using command down'


def _ejecutar_comando_ventana(comando):
    script = _APPLESCRIPT_MAXIMIZAR if comando == "maximizar" else _APPLESCRIPT_MINIMIZAR
    resultado = _ejecutar_comando_sistema(["osascript", "-e", script])
    mensaje = "Maximizando." if comando == "maximizar" else "Minimizando."
    return resultado.returncode == 0, mensaje


# Key codes estándar de teclado Apple (no son teclas de medios como las
# de _pulsar_tecla_media — estas SÍ están en el rango normal 0-127 que
# entiende "key code" de System Events, pero igualmente necesitan el
# mismo permiso de Accesibilidad para surtir efecto).
_TECLA_IZQUIERDA = 123
_TECLA_DERECHA = 124
_TECLA_ARRIBA = 126
_TECLA_ABAJO = 125
_TECLA_RETURN = 36

PALABRAS_NAV_IZQUIERDA = ["izquierda"]
PALABRAS_NAV_DERECHA = ["derecha"]
PALABRAS_NAV_ARRIBA = ["arriba"]
PALABRAS_NAV_ABAJO = ["abajo"]
PALABRAS_NAV_SELECCIONAR = ["selecciona", "seleccionar", "aceptar"]


def _detectar_comando_navegacion(pregunta):
    texto_norm = _normalizar(pregunta)
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_NAV_IZQUIERDA):
        return "izquierda"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_NAV_DERECHA):
        return "derecha"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_NAV_ARRIBA):
        return "arriba"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_NAV_ABAJO):
        return "abajo"
    if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in PALABRAS_NAV_SELECCIONAR):
        return "seleccionar"
    return None


def _pulsar_tecla(codigo_tecla):
    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", f'tell application "System Events" to key code {codigo_tecla}'],
    )
    return resultado.returncode == 0


def _ejecutar_comando_navegacion(comando):
    codigos = {
        "izquierda": _TECLA_IZQUIERDA, "derecha": _TECLA_DERECHA,
        "arriba": _TECLA_ARRIBA, "abajo": _TECLA_ABAJO,
        "seleccionar": _TECLA_RETURN,
    }
    exito = _pulsar_tecla(codigos[comando])
    return exito, "Hecho."


# ── Comando de voz: canales (Kodi con PVR configurado) ────────────────
# Sube/baja canal son atajos de teclado (Page Up/Page Down), así que
# tienen la MISMA limitación de Accesibilidad de arriba. "Busca canal
# [nombre]" no es un atajo de teclado — necesitaría hablar con la API
# JSON-RPC de Kodi (Player.Open con el canal correspondiente), que es
# una integración aparte, no una tecla; de momento solo se detecta y se
# avisa de que no está implementado, no se inventa una respuesta.
#
# DIGI TV Go no tiene atajos de teclado públicos para cambiar de canal
# — si la app activa es esa, se avisa explícitamente en vez de fingir
# que el comando ha hecho algo.

_TECLA_PAGE_UP = 116
_TECLA_PAGE_DOWN = 121

_RE_BUSCAR_CANAL = re.compile(r"busca\s+canal\s+(.+?)\s*[.!?¡¿]*\s*$", re.IGNORECASE)

APPS_SIN_CANALES_POR_TECLADO = {"digitv", "digi tv", "digi tv go"}


def _detectar_comando_canal(pregunta):
    texto_norm = _normalizar(pregunta)
    if "sube canal" in texto_norm or "subir canal" in texto_norm:
        return ("subir", None)
    if "baja canal" in texto_norm or "bajar canal" in texto_norm:
        return ("bajar", None)
    m = _RE_BUSCAR_CANAL.search(pregunta)
    if m:
        return ("buscar", m.group(1).strip())
    return None


def _app_frontal():
    """Nombre del proceso frontal según System Events, o None si el
    propio comando falla (p.ej. sin permiso de Accesibilidad — en ese
    caso tampoco se podría pulsar la tecla igualmente, así que el
    comando de canal fallará más abajo de todos modos)."""
    resultado = _ejecutar_comando_sistema(
        ["osascript", "-e", 'tell application "System Events" to name of first application process whose frontmost is true'],
    )
    if resultado.returncode != 0:
        return None
    return resultado.stdout.strip()


def _ejecutar_comando_canal(accion, argumento):
    app_frontal = _app_frontal()
    if app_frontal and _normalizar(app_frontal) in APPS_SIN_CANALES_POR_TECLADO:
        return (
            "DIGI TV no permite cambiar de canal por comandos externos, "
            "tendrás que hacerlo manualmente."
        )

    if accion == "buscar":
        return (
            f"Buscar el canal {argumento} todavía no está implementado — "
            f"necesita hablar con la API de Kodi, no es un atajo de teclado."
        )

    codigo = _TECLA_PAGE_UP if accion == "subir" else _TECLA_PAGE_DOWN
    exito = _pulsar_tecla(codigo)
    if not exito:
        return "No he podido cambiar de canal."
    return "Subiendo canal." if accion == "subir" else "Bajando canal."


# ── Detección de necesidad de búsqueda web ───────────────────────────

PALABRAS_CLAVE_BUSQUEDA_WEB = [
    "tiempo", "clima", "temperatura", "lluvia", "prevision",
    "noticia", "noticias", "actualidad", "cotizacion", "bolsa",
]


def _necesita_busqueda_web(pregunta):
    """Solo se activa la herramienta de búsqueda web (más lenta, ~20s+)
    para preguntas que claramente piden información externa/actual
    (tiempo, noticias...). Para todo lo demás — datos ONCE que ya están
    en JSON local, salud, tecnología, agenda, charla general — ni
    siquiera se le da la herramienta a Claude: así no puede decidir
    buscar por su cuenta y la respuesta es más rápida y previsible."""
    texto_norm = _normalizar(pregunta)
    return any(
        re.search(rf"\b{re.escape(p)}\b", texto_norm)
        for p in PALABRAS_CLAVE_BUSQUEDA_WEB
    )


# ── Credenciales ──────────────────────────────────────────────────────

def _leer_keychain(servicio, cuenta):
    """Lee una clave del Keychain de macOS invocando el comando `security`
    directamente (no la librería keyring, que falla con -25308
    errSecInteractionNotAllowed en sesiones sin interfaz gráfica, p.ej.
    por SSH)."""
    resultado = subprocess.run(
        ["security", "find-generic-password", "-s", servicio, "-a", cuenta, "-w"],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        return None
    return resultado.stdout.strip()


def _clave_claude():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key
    api_key = _leer_keychain(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de Claude ni en la variable de entorno "
            f"ANTHROPIC_API_KEY ni en el Keychain (servicio "
            f"'{KEYCHAIN_SERVICE}', cuenta '{KEYCHAIN_ACCOUNT}')."
        )
    return api_key


# ── Datos reales del día (Python puro — regla del proyecto: los ────────
# ── cálculos del cuadre ONCE se hacen SIEMPRE con Python, nunca con IA) ─

def _cargar_json(ruta):
    if not ruta.exists():
        return None
    with open(ruta) as f:
        return json.load(f)


# Caso especial gvTWYP: nombres de columna reales -> etiqueta legible.
# Esa tabla no sigue el patrón CONCEPTO/IMPORTE del resto (ver
# _formatear_fila_liquidacion) — su cabecera real es "TOPii" | "VENTA
# PROD ONCE" | "RETIRADA", con "TOPii" como nombre del juego, no una
# etiqueta genérica.
_ETIQUETAS_COLUMNA_TWYP = {
    "VENTA PROD ONCE": "Venta prod. ONCE",
    "RETIRADA": "Retirada",
}


def _formatear_fila_liquidacion(fila):
    """Convierte una fila genérica de detalle_completo (columnas variables
    según la tabla: PRODUCTOS CUPÓN/FECHA/IMPORTE, CONCEPTO/IMPORTE...) en
    una línea legible, sin asumir nombres de columna fijos — el propio
    portal ya pone las etiquetas legibles como clave o como valor según
    la tabla, así que no hace falta traducir nada por producto.

    Caso especial (gvTWYP/TOPii): la fila no tiene clave "IMPORTE", pero
    sí una clave cuyo VALOR es literalmente "IMPORTE" — esa clave es en
    realidad el nombre del juego (p.ej. {"TOPii": "IMPORTE",
    "VENTA PROD ONCE": "0,00€", "RETIRADA": "0,00€"}), no una etiqueta
    genérica de columna, así que se usa como nombre y el resto de
    columnas se listan con su propia etiqueta en vez de concatenar los
    valores sin contexto."""
    importe = fila.get("IMPORTE")
    if importe is None:
        clave_juego = next((k for k, v in fila.items() if v == "IMPORTE"), None)
        if clave_juego is not None:
            resto = ", ".join(
                f"{_ETIQUETAS_COLUMNA_TWYP.get(k, k.title())}: {v}"
                for k, v in fila.items()
                if k != clave_juego and v not in (None, "")
            )
            return f"{clave_juego} — {resto}" if resto else clave_juego

    etiqueta = " - ".join(
        str(v) for k, v in fila.items() if k != "IMPORTE" and v not in (None, "")
    )
    if importe is not None:
        return f"{etiqueta}: {importe}" if etiqueta else str(importe)
    return etiqueta or "(sin datos)"


def _contexto_liquidacion():
    hoy = datetime.now().strftime("%Y%m%d")
    datos = _cargar_json(LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.json")
    if not datos:
        return "LIQUIDACIÓN DIARIA: no hay datos descargados hoy."
    if not datos.get("ejecutado", True):
        return f"LIQUIDACIÓN DIARIA: {datos.get('mensaje')}"
    linea = f"LIQUIDACIÓN DIARIA ({datos.get('fecha_consulta')}): {datos.get('mensaje')}"

    partes_detalle = [
        _formatear_fila_liquidacion(fila)
        for grupo in (datos.get("detalle_completo") or [])
        for fila in (grupo.get("filas") or [])
    ]
    if partes_detalle:
        linea += " | DESGLOSE: " + "; ".join(partes_detalle)

    if cuadre_diario is not None:
        try:
            resultado_cuadre = cuadre_diario.calcular_cuadre_diario()
            if resultado_cuadre.get("ejecutado"):
                linea += (
                    f" | CUADRE DE AYER ({resultado_cuadre['fecha']}): efectivo "
                    f"{resultado_cuadre['efectivo']:.2f}€ (origen: {resultado_cuadre['origen']})"
                )
            else:
                linea += f" | CUADRE DE AYER: {resultado_cuadre.get('mensaje')}"
        except Exception as e:
            linea += f" | CUADRE DE AYER: no disponible ({e})"
    return linea


def _contexto_paquetes():
    hoy = datetime.now().strftime("%Y%m%d")
    partes = []
    for etiqueta, nombre in (
        ("previsto", "control_retirada_previsto"),
        ("a retirar", "control_retirada_a_retirar"),
        ("retirado", "control_retirada_retirado"),
    ):
        datos = _cargar_json(PAQUETES_PATH / f"{nombre}_{hoy}.json")
        # Si solo se ha ejecutado portal_once.py hoy (sin
        # parsear_html_portal.py detrás, el siguiente paso normal del
        # pipeline), el JSON todavía es la tabla cruda (una lista, sin
        # "paquetes"/"productos"/"detalle") en vez del esquema ya
        # parseado — se trata igual que "sin datos hoy" en vez de
        # petar con AttributeError.
        if not isinstance(datos, dict):
            partes.append(f"{etiqueta}: sin datos hoy" + (" (sin parsear todavía)" if datos else ""))
        else:
            partes.append(f"{etiqueta}: {len(datos.get('paquetes', []))}")
    return "PAQUETES (" + ", ".join(partes) + ")"


# Palabras que indican que la pregunta pide el detalle de CONTENIDO de
# los paquetes (qué productos, cupones, series, libros trae) y no solo
# el resumen por cantidad. Solo en ese caso se carga el detalle
# completo — el JSON real de cada paquete trae decenas de cupones
# individuales con su rango de series, así que meterlo siempre en el
# contexto sería mucho más texto del necesario para "cuántos paquetes
# tengo hoy".
PALABRAS_CLAVE_DETALLE_PAQUETES = [
    "cupon", "cupones", "serie", "series", "libro", "libros",
    "contiene", "contenido", "producto", "productos", "detalle",
]


def _familias_producto_hoy():
    """Nombres de familia de producto (p.ej. "CUPONAZO", sin el " - DD/MM"
    de la fecha) que aparecen HOY en los paquetes — para poder detectar
    "qué números tengo del cuponazo" como pregunta de detalle aunque no
    use ninguna palabra genérica de PALABRAS_CLAVE_DETALLE_PAQUETES.
    Se deriva de los datos reales en vez de mantener una lista fija de
    nombres de juegos, que se quedaría desactualizada."""
    hoy = datetime.now().strftime("%Y%m%d")
    familias = set()
    for nombre in (
        "control_retirada_previsto", "control_retirada_a_retirar", "control_retirada_retirado",
    ):
        datos = _cargar_json(PAQUETES_PATH / f"{nombre}_{hoy}.json")
        # Igual que en _contexto_paquetes(): antes de parsear_html_portal.py
        # este JSON es una lista cruda, no un dict con "paquetes".
        if not isinstance(datos, dict):
            continue
        for paquete in datos.get("paquetes", []):
            for prod in paquete.get("productos", []):
                nombre_prod = prod.get("producto", "")
                if nombre_prod:
                    familias.add(nombre_prod.split(" - ", 1)[0].strip())
    return familias


def _necesita_detalle_paquetes(pregunta):
    texto_norm = _normalizar(pregunta)
    if any(
        re.search(rf"\b{re.escape(p)}\b", texto_norm)
        for p in PALABRAS_CLAVE_DETALLE_PAQUETES
    ):
        return True
    return any(
        _normalizar(familia) in texto_norm
        for familia in _familias_producto_hoy() if familia
    )


def _contexto_paquetes_detalle(pregunta=""):
    """Detalle completo de cada paquete: por cada producto (X10, X50,
    Mega Millonario, Cuponazo...), su cantidad y el desglose de cada
    cupón (número, cantidad, rango de series) o libro (lotería
    instantánea).

    Recibe la pregunta original para poder anteponer una "RESPUESTA
    DIRECTA" ya calculada en Python cuando se detecta que pregunta por
    un producto concreto (ver más abajo) — el resumen agregado por sí
    solo no bastó: incluso con instrucciones explícitas en el prompt de
    "usa este total tal cual", el modelo seguía sin encontrar de forma
    fiable las 3 apariciones de "CUPONAZO" dispersas en un contexto largo
    (probado el 2026-07-10, dos veces, con resultados distintos e
    incompletos ambas veces). Una línea con la respuesta ya resuelta para
    el producto exacto por el que preguntan es mucho más difícil de
    ignorar que un resumen genérico de todos los productos."""
    hoy = datetime.now().strftime("%Y%m%d")
    bloques = []
    # familia de producto (p.ej. "CUPONAZO", sin el " - DD/MM" de la
    # fecha) -> recuento + fechas ya sumados en Python, para que el
    # modelo nunca tenga que contar cuántas fechas/paquetes distintos hay
    # de un mismo tipo de producto.
    resumen_por_familia = {}
    for etiqueta, nombre in (
        ("previsto", "control_retirada_previsto"),
        ("a retirar", "control_retirada_a_retirar"),
        ("retirado", "control_retirada_retirado"),
    ):
        datos = _cargar_json(PAQUETES_PATH / f"{nombre}_{hoy}.json")
        # Igual que en _contexto_paquetes()/_familias_producto_hoy(): sin
        # parsear_html_portal.py todavía, esto es una lista cruda, no un
        # dict con "paquetes".
        if not isinstance(datos, dict) or not datos.get("paquetes"):
            bloques.append(f"PAQUETE ({etiqueta}): sin datos hoy.")
            continue
        for paquete in datos["paquetes"]:
            descripcion = paquete.get("descripcion", "?")
            lineas_producto = []
            for prod in paquete.get("productos", []):
                nombre_prod = prod.get("producto", "?")
                cantidad = prod.get("cantidad", "?")
                items = []
                numeros = []
                num_cupones = num_libros = 0
                for d in prod.get("detalle", []):
                    if "cupón" in d:
                        items.append(
                            f"cupón {d['cupón']} x{d.get('cantidad', '?')} "
                            f"series {d.get('series', '?')}"
                        )
                        num_cupones += 1
                        numeros.append(d["cupón"])
                    elif "libro" in d:
                        items.append(f"libro {d['libro']}")
                        num_libros += 1
                        numeros.append(d["libro"])
                detalle_txt = "; ".join(items) if items else "(sin desglose)"
                lineas_producto.append(f"  - {nombre_prod} (cantidad {cantidad}): {detalle_txt}")

                partes_nombre = nombre_prod.split(" - ", 1)
                familia = partes_nombre[0].strip()
                fecha_prod = partes_nombre[1].strip() if len(partes_nombre) > 1 else None
                entrada = resumen_por_familia.setdefault(
                    familia, {"apariciones": 0, "cupones": 0, "libros": 0, "fechas": [], "por_fecha": []}
                )
                entrada["apariciones"] += 1
                entrada["cupones"] += num_cupones
                entrada["libros"] += num_libros
                if fecha_prod:
                    entrada["fechas"].append(fecha_prod)
                    entrada["por_fecha"].append({"fecha": fecha_prod, "numeros": numeros})
            bloques.append(f"PAQUETE {etiqueta} ({descripcion}):\n" + "\n".join(lineas_producto))

    if resumen_por_familia:
        lineas_resumen = [
            "RESUMEN POR TIPO DE PRODUCTO (calculado en Python — usa "
            "estos totales tal cual, no los recalcules ni cuentes tú). "
            "Suma TODOS los paquetes (previsto + a retirar + retirado), "
            "no filtres por estado ni digas \"pendientes\" salvo que te "
            "pregunten específicamente por eso — si preguntan \"cuántos "
            "cupones tengo de X\" sin más, es este total, no un subconjunto:"
        ]
        for familia in sorted(resumen_por_familia):
            r = resumen_por_familia[familia]
            partes = [f"{r['apariciones']} paquete(s)/fecha(s) distinta(s)"]
            if r["cupones"]:
                partes.append(f"{r['cupones']} cupón(es) en total")
            if r["libros"]:
                partes.append(f"{r['libros']} libro(s) en total")
            lineas_resumen.append(f"- {familia}: {', '.join(partes)}")
        bloques.insert(0, "\n".join(lineas_resumen))

    # "RESPUESTA DIRECTA": si la pregunta menciona el nombre de una
    # familia de producto concreta (comparando sin acentos/mayúsculas),
    # se antepone una línea con la respuesta ya resuelta para ESE
    # producto en particular — mucho más difícil de pasar por alto que
    # el resumen genérico de arriba, que lista todos los productos a la
    # vez.
    pregunta_norm = _normalizar(pregunta)
    for familia in sorted(resumen_por_familia, key=len, reverse=True):
        if _normalizar(familia) and _normalizar(familia) in pregunta_norm:
            r = resumen_por_familia[familia]
            unidad = "cupón(es)" if r["cupones"] else "libro(s)"
            cantidad_total = r["cupones"] or r["libros"] or r["apariciones"]

            def _clave_fecha(g):
                dia, _, mes = g["fecha"].partition("/")
                return (mes, dia) if mes else (g["fecha"],)

            if r["por_fecha"]:
                grupos_txt = ", ".join(
                    f"{g['fecha']}: [{', '.join(g['numeros'])}]" if g["numeros"] else g["fecha"]
                    for g in sorted(r["por_fecha"], key=_clave_fecha)
                )
            else:
                grupos_txt = ", ".join(r["fechas"]) if r["fechas"] else "sin fecha"

            bloques.insert(0,
                f"RESPUESTA DIRECTA para {familia}: {cantidad_total} {unidad} en "
                f"{r['apariciones']} fecha(s) — {grupos_txt}. Usa esta lista "
                f"completa tal cual, no busques en el detalle de abajo."
            )
            break

    return "\n\n".join(bloques)


def _contexto_almacen():
    hoy = datetime.now().strftime("%Y%m%d")
    datos = _cargar_json(STOCK_PATH / f"control_almacen_instantanea_{hoy}.json")
    # Si solo se ha ejecutado portal_once.py hoy (sin parsear_html_portal.py
    # detrás), este JSON todavía es la tabla cruda (una lista), no el dict
    # con "productos"/"detalle" que escribe parsear_html_portal.py encima.
    if not isinstance(datos, dict):
        return "ALMACÉN RASCAS: sin datos hoy."
    productos = datos.get("productos", [])
    if not productos:
        return "ALMACÉN RASCAS: sin productos registrados hoy."
    # "Quedan" y los totales se calculan aquí en Python (no los cuenta la
    # IA) sumando los libros cuyo estado no es "Vendido" ni "Retirado" —
    # el resto de estados (Confirmado, Activado, Asignado a vendedor...)
    # son libros todavía en poder del vendedor.
    lineas_producto = []
    total_libros = total_sin_vender = total_vendidos = total_retirados = 0
    for p in productos:
        nombre = p.get("producto", "?")
        detalle = p.get("detalle", [])
        vendidos = sum(1 for d in detalle if d.get("estado") == "Vendido")
        retirados = sum(1 for d in detalle if d.get("estado") == "Retirado")
        quedan = len(detalle) - vendidos - retirados
        total_libros += len(detalle)
        total_sin_vender += quedan
        total_vendidos += vendidos
        total_retirados += retirados
        libros = ", ".join(
            f"{d.get('libro')} ({d.get('estado')})" for d in detalle
        )
        lineas_producto.append(
            f"- {nombre}: {quedan} sin vender de {len(detalle)} total "
            f"({vendidos} vendido(s), {retirados} retirado(s)). Detalle: {libros}"
        )

    cabecera = (
        f"ALMACÉN RASCAS ({datos.get('fecha_extraccion', hoy)}) — "
        f"TOTAL GENERAL YA CALCULADO, no lo recalcules: {total_sin_vender} sin "
        f"vender de {total_libros} libro(s) en {len(productos)} producto(s) "
        f"({total_vendidos} vendido(s), {total_retirados} retirado(s))."
    )
    return "\n".join([cabecera] + lineas_producto)


def _contexto_caducidad_libros():
    """Caducidad de libros Confirmado/Activado: ONCE da por vendido
    automáticamente (cobrándolo igual) un libro que lleva 90 días sin
    venderse de verdad. Días transcurridos/restantes y el nivel
    (URGENTE/AVISO/OK) ya vienen calculados por
    alertas_caducidad.calcular_alertas_caducidad() (Python puro, nunca la
    IA) — aquí solo se listan tal cual, igual que en _contexto_almacen()."""
    if alertas_caducidad is None:
        return "CADUCIDAD DE LIBROS: no disponible."
    resultado = alertas_caducidad.calcular_alertas_caducidad()
    if not resultado.get("ejecutado", True):
        return f"CADUCIDAD DE LIBROS: {resultado['mensaje']}"
    libros = resultado["libros"]
    if not libros:
        return "CADUCIDAD DE LIBROS: sin libros Confirmado/Activado con fecha registrada."

    t = resultado["totales"]
    cabecera = (
        f"CADUCIDAD DE LIBROS ({resultado['fecha']}) — TOTALES YA CALCULADOS, no los "
        f"recalcules: {len(libros)} libro(s) pendiente(s) de vender antes de que ONCE "
        f"los dé por vendidos a los 90 días — {t['urgente']} urgente(s) (más de 60 días), "
        f"{t['aviso']} en aviso (45-60 días), {t['ok']} ok (menos de 45 días). Lista "
        f"completa ordenada de más a menos antiguo (el más urgente primero), usa esta "
        f"lista tal cual, no la resumas de memoria:"
    )
    lineas = [
        f"- {l['emoji']} {l['producto']} — libro {l['libro']} — confirmado hace "
        f"{l['dias_transcurridos']} días ({l['dias_para_caducar']} días para caducar, "
        f"nivel {l['nivel']})"
        for l in libros
    ]
    return "\n".join([cabecera] + lineas)


# Premio individual (una línea juego+categoría) por encima de este
# importe se destaca como "importante" — mismo umbral que la sección
# "🏆 PREMIOS REPARTIDOS" de informe_manana.py, no dos criterios
# distintos para el mismo concepto.
UMBRAL_PREMIO_IMPORTANTE = 50.0


def _fecha_corta_a_date(fecha_corta, referencia):
    """Convierte una fecha corta "DD-MM" (como vienen las filas de
    premios_{tipo}_{hoy}.json, sin año) a un date real, usando el año de
    `referencia` salvo que el resultado caiga en el futuro respecto a
    ella — en ese caso la fila es de diciembre del año anterior (nombre
    de archivo ya en enero). Devuelve None si "DD-MM" no es válido."""
    try:
        dia_str, mes_str = fecha_corta.split("-")
        fecha = referencia.replace(month=int(mes_str), day=int(dia_str))
    except (ValueError, AttributeError):
        return None
    if fecha > referencia:
        fecha = fecha.replace(year=fecha.year - 1)
    return fecha


def _contexto_premios():
    """Desglose completo de premios repartidos, con producto/categoría/
    cantidad/importe por línea, del día MÁS RECIENTE que realmente haya
    en los datos descargados.

    FIX de dos bugs reales seguidos:
    1. La función original sumaba el archivo premios_{tipo}_{hoy}.json
       ENTERO sin filtrar por fecha — esos 3 archivos acumulan un
       histórico de varios días (cada fila trae su propia fecha DD-MM),
       así que el total salía inflado y sin desglose por juego/categoría.
    2. El primer arreglo filtraba por "ayer" calculado en Python
       (hoy - 1 día) — asumía que el portal siempre tiene publicado el
       cierre de ayer en el momento de preguntar. Si ese hueco es de más
       de un día (fin de semana, festivo, retraso de publicación...),
       "ayer" no existe en los datos y la función devolvía "sin premios"
       aunque SÍ hubiera datos recientes disponibles, solo que de hace 2
       o 3 días. Aquí se calcula la fecha MÁXIMA real presente en las
       filas descargadas (premios_{tipo}_{hoy}.json es siempre la
       descarga más reciente, por eso se sigue usando "hoy" en el nombre
       del archivo) y se filtra por esa, sea cual sea."""
    hoy_dt = datetime.now()
    hoy = hoy_dt.strftime("%Y%m%d")
    hoy_date = hoy_dt.date()

    filas_por_tipo = {}
    sin_datos = []
    for tipo in ("pasiva", "activa", "instantanea"):
        datos = _cargar_json(PREMIOS_PATH / f"premios_{tipo}_{hoy}.json")
        # Algunos ficheros antiguos (p.ej. de julio 2026, antes de que el
        # esquema se asentara) guardan una lista suelta en vez de un
        # dict con "premios" — se ignoran igual que "sin datos".
        if not isinstance(datos, dict):
            sin_datos.append(tipo)
            continue
        filas_por_tipo[tipo] = [
            f for f in (datos.get("premios") or []) if isinstance(f, dict) and f.get("fecha")
        ]

    if not filas_por_tipo:
        return f"PREMIOS REPARTIDOS: no hay archivos de premios descargados hoy ({', '.join(sin_datos)})."

    fechas_reales = [
        _fecha_corta_a_date(f["fecha"], hoy_date)
        for filas_tipo in filas_por_tipo.values() for f in filas_tipo
    ]
    fechas_reales = [d for d in fechas_reales if d is not None]
    if not fechas_reales:
        return "PREMIOS REPARTIDOS: los archivos descargados hoy no traen ninguna fecha reconocible."

    # La fecha objetivo NUNCA se calcula como "hoy - N días" — es
    # siempre la fecha máxima que de verdad aparece en los datos.
    fecha_objetivo = max(fechas_reales)
    fecha_objetivo_corta = fecha_objetivo.strftime("%d-%m")
    fecha_objetivo_legible = fecha_objetivo.strftime("%d/%m/%Y")

    filas = []
    for tipo, filas_tipo in filas_por_tipo.items():
        for f in filas_tipo:
            if f.get("fecha") != fecha_objetivo_corta:
                continue
            filas.append({
                "tipo": tipo,
                "juego": f.get("juego") or "?",
                "categoria": f.get("categoria") or "?",
                "cantidad": f.get("cantidad_total") or 0,
                "importe": f.get("importe_total") or 0,
            })

    if not filas:
        return (
            f"PREMIOS REPARTIDOS: sin líneas de premio para el día más reciente "
            f"disponible ({fecha_objetivo_legible})."
        )

    total = round(sum(f["importe"] for f in filas), 2)
    cabecera = (
        f"PREMIOS REPARTIDOS — DÍA MÁS RECIENTE DISPONIBLE ({fecha_objetivo_legible}) — "
        f"TOTAL YA CALCULADO EN PYTHON, no lo recalcules: {total:.2f}€ en {len(filas)} "
        f"línea(s) de premio (pasiva+activa+instantánea). Detalle completo, usa esta "
        f"lista tal cual, no la resumas de memoria ni omitas ninguna línea:"
    )
    lineas = [
        f"- [{f['tipo']}] {f['juego']} — {f['categoria']}: {f['cantidad']} u., "
        f"{f['importe']:.2f}€"
        + (" 🎉 PREMIO IMPORTANTE (más de 50€)" if f["importe"] > UMBRAL_PREMIO_IMPORTANTE else "")
        for f in filas
    ]
    return "\n".join([cabecera] + lineas)


def _contexto_agenda():
    if agenda is None:
        return "AGENDA: no disponible."
    try:
        datos_agenda = agenda.obtener_agenda()
    except Exception as e:
        return f"AGENDA: no disponible ({e})"
    eventos_hoy = datos_agenda.get("eventos_hoy") or []
    recordatorios = (
        (datos_agenda.get("recordatorios_hoy") or [])
        + (datos_agenda.get("recordatorios_proximos_3_dias") or [])
    )
    return f"AGENDA: {len(eventos_hoy)} evento(s) hoy, {len(recordatorios)} recordatorio(s) pendientes."


def _contexto_lista_json(ruta, etiqueta, max_filas=15):
    """Helper genérico para los ficheros de "consultas" del portal que son
    simplemente una tabla exportada a JSON (lista de filas/diccionarios):
    incidencias, registro de jornada, solicitudes, comisiones,
    estadísticas de venta. Todos comparten la misma forma (ver
    extraer_tabla() en portal_once.py)."""
    datos = _cargar_json(ruta)
    if datos is None:
        return f"{etiqueta}: no hay datos descargados hoy."
    if not datos:
        return f"{etiqueta}: 0 filas (tabla vacía hoy)."
    resumen = json.dumps(datos[:max_filas], ensure_ascii=False)
    extra = f" (mostrando {max_filas} de {len(datos)})" if len(datos) > max_filas else ""
    return f"{etiqueta} ({len(datos)} fila(s)){extra}: {resumen}"


def _contexto_comisiones():
    hoy = datetime.now().strftime("%Y%m%d")
    return _contexto_lista_json(COMISIONES_PATH / f"comisiones_{hoy}.json", "COMISIONES")


def _contexto_estadisticas():
    hoy = datetime.now().strftime("%Y%m%d")
    return _contexto_lista_json(ESTADISTICAS_PATH / f"estadisticas_venta_{hoy}.json", "ESTADÍSTICAS DE VENTA")


def _contexto_incidencias():
    hoy = datetime.now().strftime("%Y%m%d")
    return _contexto_lista_json(CONSULTAS_PATH / f"incidencias_{hoy}.json", "INCIDENCIAS")


def _contexto_registro_jornada():
    hoy = datetime.now().strftime("%Y%m%d")
    return _contexto_lista_json(CONSULTAS_PATH / f"registro_jornada_{hoy}.json", "REGISTRO DE JORNADA")


def _contexto_solicitudes():
    hoy = datetime.now().strftime("%Y%m%d")
    return _contexto_lista_json(CONSULTAS_PATH / f"solicitudes_{hoy}.json", "SOLICITUDES")


def _contexto_comunicaciones():
    """Avisos TPV ya procesados por ocr_comunicaciones.py (finalización
    de venta voluntaria de juegos, plazos de devolución de boletos...).
    Se listan TODOS los avisos guardados (normalmente pocos), no solo
    los próximos — para eso está la sección AVISOS PRÓXIMOS de
    informe_manana.py; aquí se responde a preguntas puntuales tipo "qué
    decía el aviso de tal juego"."""
    if COMUNICACIONES_PATH is None or not COMUNICACIONES_PATH.exists():
        return "COMUNICACIONES TPV: no disponible."
    jsons = sorted(COMUNICACIONES_PATH.glob("*.json"))
    if not jsons:
        return "COMUNICACIONES TPV: no hay avisos procesados."
    partes = []
    for ruta in jsons:
        datos = _cargar_json(ruta)
        if not datos:
            continue
        fechas = "; ".join(
            f"{e.get('fecha')}: {e.get('descripcion')}" for e in (datos.get("fechas_mencionadas") or [])
        )
        partes.append(
            f"- {datos.get('juego') or datos.get('tipo_comunicacion')} "
            f"(aviso del {datos.get('fecha_aviso')}): {fechas}"
        )
    return "COMUNICACIONES TPV:\n" + "\n".join(partes)


def _contexto_devoluciones_libros():
    """Devoluciones de libros de rasca ya procesadas por
    ocr_devolucion_libros.py."""
    if DEVOLUCION_LIBROS_PATH is None or not DEVOLUCION_LIBROS_PATH.exists():
        return "DEVOLUCIÓN DE LIBROS: no disponible."
    jsons = sorted(DEVOLUCION_LIBROS_PATH.glob("*.json"))
    if not jsons:
        return "DEVOLUCIÓN DE LIBROS: no hay devoluciones registradas."
    partes = []
    for ruta in jsons:
        datos = _cargar_json(ruta)
        if not datos:
            continue
        for libro in datos.get("libros_devueltos") or []:
            partes.append(
                f"- {datos.get('fecha')}: libro {libro.get('libro')} "
                f"({libro.get('producto')}), motivo: {libro.get('motivo') or 'no indicado'}"
            )
    if not partes:
        return "DEVOLUCIÓN DE LIBROS: no hay devoluciones registradas."
    return "DEVOLUCIÓN DE LIBROS:\n" + "\n".join(partes)


def _contexto_nomina():
    """No hay JSON estructurado para nóminas — descargar_nominas() en
    portal_once.py guarda un PDF por período (patrón NM_{año}_{mes}_...
    suelto en la raíz de ONCE, ver NOMINAS_PATH). Se localiza el más
    reciente por año/mes en el nombre de fichero y se extrae su texto
    con PyMuPDF (fitz), truncado a un tamaño razonable."""
    try:
        import fitz
    except ImportError:
        return "NÓMINA: no se pudo leer (falta la librería PyMuPDF/fitz)."

    candidatos = []
    patron = re.compile(r"^NM_(\d{4})_(\d{2})_")
    for ruta in NOMINAS_PATH.glob("NM_*.pdf"):
        if "comisiones" in ruta.name.lower():
            continue
        m = patron.match(ruta.name)
        if m:
            candidatos.append((int(m.group(1)), int(m.group(2)), ruta))

    if not candidatos:
        return "NÓMINA: no se encontró ningún PDF de nómina descargado."

    candidatos.sort(key=lambda t: (t[0], t[1]))
    _, _, ruta_reciente = candidatos[-1]

    try:
        doc = fitz.open(ruta_reciente)
        texto = "\n".join(pagina.get_text() for pagina in doc).strip()
        doc.close()
    except Exception as e:
        return f"NÓMINA: no se pudo leer {ruta_reciente.name} ({e})."

    if not texto:
        return f"NÓMINA ({ruta_reciente.name}): el PDF no tiene texto extraíble (puede ser un escaneo)."
    if len(texto) > 3000:
        texto = texto[:3000] + "... (truncado)"
    return f"NÓMINA ({ruta_reciente.name}):\n{texto}"


# Palabras clave para decidir qué categoría(s) de datos reales son
# relevantes para la pregunta concreta — no siempre se cargan todas.
# Esto no es solo por precisión (evitar que Claude mencione datos que no
# vienen a cuento): _contexto_agenda() en particular tarda ~14s porque
# consulta Calendario/Recordatorios vía EventKit/AppleScript, y
# _contexto_nomina() lee un PDF entero, así que saltarlas cuando no
# hacen falta es clave para la velocidad — el resto (JSON local) son
# casi instantáneas de por sí.
PALABRAS_CLAVE_CONTEXTO = {
    "paquetes": ["paquete", "paquetes", "retirada", "retirar", "retirado"],
    "almacen": ["rasca", "rascas", "almacen", "instantanea", "instantaneas"],
    # "llevan" cubre preguntas de antigüedad tipo "cuanto tiempo llevan
    # mis rascas" (que ya dispara "almacen" vía "rascas", pero sin esta
    # categoría no traería los días transcurridos/restantes por libro).
    "caducidad": [
        "caducidad", "caducar", "caduca", "caducan", "caducado", "caducados",
        "vencer", "vencido", "vencidos", "antiguedad", "llevan",
    ],
    "liquidacion": ["liquidacion", "liquidaciones", "liquidar", "saldo", "acreedor", "cuadre", "efectivo"],
    "premios": ["premio", "premios", "repartido", "repartidos", "reparto"],
    "agenda": [
        "calendario", "recordatorio", "recordatorios", "tarea", "tareas",
        "agenda", "evento", "eventos", "cita", "citas",
    ],
    "comisiones": ["comision", "comisiones"],
    "estadisticas": ["estadistica", "estadisticas", "venta", "ventas"],
    "incidencias": ["incidencia", "incidencias"],
    "registro_jornada": ["jornada", "fichaje", "fichar", "marcaje", "marcajes"],
    "solicitudes": ["solicitud", "solicitudes"],
    "nomina": ["nomina", "nominas", "sueldo"],
    "comunicaciones": [
        "aviso", "avisos", "comunicacion", "comunicaciones",
        "finalizacion", "finaliza", "finalizar",
    ],
    "devoluciones_libros": ["devolucion", "devoluciones", "devolver", "devuelto", "devueltos"],
}

_CONSTRUCTORES_CONTEXTO = {
    "paquetes": _contexto_paquetes,
    "almacen": _contexto_almacen,
    "caducidad": _contexto_caducidad_libros,
    "liquidacion": _contexto_liquidacion,
    "premios": _contexto_premios,
    "agenda": _contexto_agenda,
    "comisiones": _contexto_comisiones,
    "estadisticas": _contexto_estadisticas,
    "incidencias": _contexto_incidencias,
    "registro_jornada": _contexto_registro_jornada,
    "solicitudes": _contexto_solicitudes,
    "nomina": _contexto_nomina,
    "comunicaciones": _contexto_comunicaciones,
    "devoluciones_libros": _contexto_devoluciones_libros,
}

# Categorías del "portal" en sentido estricto (excluye agenda, que es
# calendario/recordatorios personales, no datos del portal ONCE) — son
# las que se cargan TODAS de golpe cuando la pregunta es genérica sobre
# el portal y no encaja en ninguna categoría concreta. nomina se excluye
# de esta carga general porque implica leer un PDF entero (más lento);
# solo se carga si se pregunta explícitamente por nómina. comunicaciones
# y devoluciones_libros también se excluyen del genérico: no son datos
# "del portal" propiamente (son PDFs de TPV ya procesados aparte), solo
# se cargan si se pregunta explícitamente por avisos/devoluciones.
CATEGORIAS_PORTAL_COMPLETO = [
    "paquetes", "almacen", "caducidad", "liquidacion", "premios",
    "comisiones", "estadisticas", "incidencias", "registro_jornada", "solicitudes",
]


# FIX (bug real): "cuánto Eurojackpot llevo vendido esta semana" hacía
# que el asistente respondiera "consulta el portal" en vez de sumar el
# dato, que YA existe en ventas.detalle de cada ticket procesado (ver
# ventas_producto_periodo.py). Esta sección detecta la combinación
# producto + periodo en la pregunta; el cálculo real lo hace Python en
# ese módulo, nunca se estima aquí.
#
# DOM/VIE/ORD: solo se reconoce el código de 3 letras — no se ha
# confirmado su nombre comercial completo en ningún JSON del portal
# todavía (a diferencia de EUJ/TRI/DUP/SUP/MID, confirmados en
# liquidacion_diaria_*.json → detalle_completo → gvProductosActivos).
ALIAS_PRODUCTO_VENTAS = {
    "EUJ": ["eurojackpot", "euro jackpot", "euj"],
    "TRI": ["triplex", "tri"],
    "DUP": ["dupla", "dup"],
    "SUP": ["super 11", "super once", "sup"],
    "MID": ["mi dia", "mid"],
    "DOM": ["dom"],
    "VIE": ["vie"],
    "ORD": ["ord"],
}

NOMBRES_PRODUCTO_VENTAS = {
    "EUJ": "Eurojackpot", "TRI": "Triplex", "DUP": "Dupla",
    "SUP": "Super 11", "MID": "Mi día", "DOM": "DOM", "VIE": "VIE", "ORD": "ORD",
}

# Frases con espacio: se comprueban con "in" simple (no \b...\b) porque
# son lo bastante específicas para no dar falsos positivos, y algunas
# palabras clave de un solo token (semanal, mensual) sí usan \b para no
# enganchar substrings de otra palabra.
PALABRAS_PERIODO_VENTAS = {
    "semana_actual": ["esta semana", "semana actual", "semanal", "de la semana", "en la semana"],
    "mes_actual": ["este mes", "mes actual", "mensual", "del mes", "en el mes"],
    "ultimos_7_dias": ["ultimos 7 dias", "ultima semana", "los ultimos 7 dias", "7 dias"],
}

ETIQUETAS_MODO_VENTAS = {
    "semana_actual": "esta semana (lunes-domingo)",
    "mes_actual": "este mes",
    "ultimos_7_dias": "los últimos 7 días",
}


def _identificar_producto_ventas(texto_norm):
    for codigo, alias in ALIAS_PRODUCTO_VENTAS.items():
        if any(re.search(rf"\b{re.escape(a)}\b", texto_norm) for a in alias):
            return codigo
    return None


def _detectar_periodo_ventas(texto_norm):
    for modo, frases in PALABRAS_PERIODO_VENTAS.items():
        if any(frase in texto_norm for frase in frases):
            return modo
    # "cuánto llevo de X" sin periodo explícito -> se asume la semana en
    # curso, es la lectura más natural de "llevo" (acumulado hasta hoy).
    if "llevo" in texto_norm:
        return "semana_actual"
    return None


def _necesita_ventas_producto(pregunta):
    """Devuelve (codigo_producto, modo) si la pregunta combina un
    producto reconocido con un periodo de tiempo, o None si no aplica.
    Ver ventas_producto_periodo.py para el cálculo real."""
    texto_norm = _normalizar(pregunta)
    codigo = _identificar_producto_ventas(texto_norm)
    if codigo is None:
        return None
    modo = _detectar_periodo_ventas(texto_norm)
    if modo is None:
        return None
    return codigo, modo


def _contexto_ventas_producto(codigo, modo):
    if ventas_producto_periodo is None:
        return "VENTAS POR PRODUCTO: no disponible."
    resultado = ventas_producto_periodo.resumen_ventas_producto(codigo, modo)
    nombre = NOMBRES_PRODUCTO_VENTAS.get(codigo, codigo)
    etiqueta_modo = ETIQUETAS_MODO_VENTAS[modo]
    texto = (
        f"VENTAS DE {nombre} ({etiqueta_modo}, {resultado['inicio']}-{resultado['fin']}) — "
        f"TOTAL YA CALCULADO EN PYTHON, no lo recalcules: {resultado['total_unidades']} "
        f"unidad(es), {resultado['total_importe']:.2f}€ ({resultado['tickets_considerados']} "
        f"ticket(s) procesados en ese rango)."
    )
    if resultado["dias_sin_ticket"]:
        texto += (
            f" ATENCIÓN: faltan los tickets de {', '.join(resultado['dias_sin_ticket'])} "
            f"(no procesados todavía) — dilo explícitamente en tu respuesta, este total "
            f"puede estar incompleto. NUNCA lo presentes como definitivo sin avisar del "
            f"hueco, y NUNCA remitas al portal en su lugar."
        )
    return texto


def contexto_del_dia(pregunta):
    """Datos reales ya descargados/calculados en Python, pero SOLO los
    relevantes para la pregunta concreta — nunca todos a la vez porque
    sí. Si la pregunta no encaja en ninguna categoría concreta, se
    asume que es una pregunta genérica sobre el portal y se cargan de
    golpe todas las categorías "del portal" (ver
    CATEGORIAS_PORTAL_COMPLETO) — pero no agenda (otro dominio) ni
    nomina (PDF, más lento), que solo se cargan si se piden
    explícitamente."""
    texto_norm = _normalizar(pregunta)
    venta_producto = _necesita_ventas_producto(pregunta)
    categorias = [
        categoria for categoria, palabras in PALABRAS_CLAVE_CONTEXTO.items()
        if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in palabras)
    ]
    # Si la pregunta ya encajó en "producto + periodo" (p.ej. "cuánto
    # Eurojackpot llevo vendido esta semana"), es una pregunta lo
    # bastante concreta como para NO caer en el fallback de cargar todo
    # el portal — igual que cualquier otra categoría específica.
    if not categorias and not venta_producto:
        categorias = CATEGORIAS_PORTAL_COMPLETO

    partes = []
    if venta_producto:
        partes.append(_contexto_ventas_producto(*venta_producto))
    for c in categorias:
        if c == "paquetes" and _necesita_detalle_paquetes(pregunta):
            partes.append(_contexto_paquetes_detalle(pregunta))
        else:
            partes.append(_CONSTRUCTORES_CONTEXTO[c]())
    return "\n".join(partes)


# ── System prompt ────────────────────────────────────────────────────

def _leer_md(nombre):
    ruta = AGENTES_PATH / f"{nombre}.md"
    if not ruta.exists():
        return f"(no disponible: falta {ruta})"
    return ruta.read_text(encoding="utf-8")


PLANTILLA_SYSTEM_PROMPT = """\
Eres el asistente personal único de Juan Antonio Panadero Jiménez. Antes \
había 4 agentes separados (ONCE, SALUD, INGENIERO, BUTLER) que había que \
enrutar según la pregunta; ahora eres tú solo, respondes directamente \
sobre cualquier tema sin necesidad de decidir "a qué agente" pertenece.

## Cómo comportarte
- Si te saludan ("hola", etc.) → saluda con normalidad, sin rodeos.
- Si preguntan por el tiempo/clima o cualquier otro dato que puedas \
buscar en la web → búscalo con tu herramienta de búsqueda web, no lo \
inventes ni respondas con conocimiento desactualizado.
- Si preguntan por datos de ONCE (saldo, paquetes, rascas, premios, \
comisiones, estadísticas de venta, incidencias, registro de jornada, \
solicitudes, nómina, avisos de finalización de juegos, devoluciones de \
libros...) → usa SOLO los datos reales de la sección \
"DATOS DE HOY" de abajo, para lo que pregunten en concreto.
- Los cálculos de cuadre/dinero de ONCE SIEMPRE los hace Python, nunca \
tú — si no hay un cálculo ya hecho en los datos de hoy, dilo, no \
inventes ni recalcules cifras.
- Si preguntan cuánto llevan vendido de un producto en un periodo \
("esta semana", "este mes", "cuánto llevo de...") y SÍ hay un bloque \
"VENTAS DE ..." en los datos de hoy, usa ese total tal cual (avisando \
del hueco si el bloque menciona tickets que faltan). Si NO hay ese \
bloque para el producto/periodo que preguntan, di explícitamente que no \
tienes ese cálculo hecho para eso — NUNCA remitas a "consultar el \
portal": el dato vive en local, si falta es porque el ticket de esa \
fecha no se ha procesado todavía, no porque haya que ir a buscarlo tú \
mismo a otro sitio.
- Cuando listes elementos de una lista (paquetes, cupones, productos), \
cuenta primero cuántos hay en los datos que recibiste y menciona \
TODOS, nunca omitas ninguno. Si el contexto trae un total ya \
calculado, úsalo tal cual — nunca recalcules ni resumas de memoria.
- No filtres por estado del paquete (previsto / a retirar / retirado) \
salvo que te lo pidan explícitamente, ni añadas matices como \
"pendientes" que no te hayan pedido: un paquete "retirado" sigue siendo \
tuyo, cuenta igual. Si el contexto trae un RESUMEN con un total ya \
sumado, ese total ya incluye los 3 estados — repórtalo tal cual.
- Si preguntan por TRÁMITES, VACACIONES o cómo SOLICITAR algo (pedir \
vacaciones, iniciar una solicitud nueva...) → no hay forma de hacer eso \
desde aquí; dile que entre directamente al portal ONCE en \
{gestiona_url} para gestionarlo. Esto es distinto de consultar \
solicitudes YA hechas, que sí puedes responder con los datos reales de \
"DATOS DE HOY" si están disponibles.
- REGLA ESTRICTA: responde solo sobre lo que te preguntan. Si preguntan \
por paquetes, habla solo de paquetes. Si preguntan por rascas, habla \
solo de rascas. NUNCA menciones saldo, liquidación ni cuadre a menos \
que te lo pidan explícitamente. Lo mismo aplica entre dominios: si \
preguntan por salud no metas tecnología ni agenda, y viceversa.

## Modo voz (Siri)
Tus respuestas se LEEN EN VOZ ALTA — sé extremadamente conciso: máximo \
2-3 frases cortas. Sin markdown, sin asteriscos, sin listas, sin \
encabezados. Solo texto plano directo que suene bien hablado. Siempre \
en español.

---

## Contexto de dominio ONCE
{once_md}

## Contexto de dominio SALUD
{salud_md}

## Contexto de dominio INGENIERO (IA y tecnología)
{ingeniero_md}

## Contexto de dominio BUTLER (agenda y organización personal)
{butler_md}

---

## DATOS DE HOY (ya descargados/calculados en Python — interprétalos, \
no los recalcules ni inventes otros)
{contexto_dia}
"""


def construir_system_prompt(pregunta):
    return PLANTILLA_SYSTEM_PROMPT.format(
        once_md=_leer_md("ONCE"),
        salud_md=_leer_md("SALUD"),
        ingeniero_md=_leer_md("INGENIERO"),
        butler_md=_leer_md("BUTLER"),
        contexto_dia=contexto_del_dia(pregunta),
        gestiona_url=GESTIONA_BASE,
    )


# ── Llamada a Claude ─────────────────────────────────────────────────

def responder(pregunta, api_key=None):
    """Devuelve el texto de respuesta para `pregunta`, por la vía más
    rápida posible:
    1. Comandos de sistema/multimedia (abrir/cerrar app, play-pausa-
       siguiente-anterior, volumen) → acción directa en Python/osascript,
       sin llamar a Claude — son acciones instantáneas, no necesitan
       inteligencia de lenguaje, solo reconocer la palabra clave.
    2. Si es un saludo/cortesía simple → respuesta fija en Python, sin
       llamar a Claude.
    3. Si no, llama a Claude con el system prompt combinado (contexto de
       dominio + datos reales de hoy ya leídos de JSON local). La
       herramienta de búsqueda web SOLO se incluye si la pregunta la
       necesita (ver _necesita_busqueda_web) — para datos ONCE que ya
       están en el contexto no se le da la herramienta a Claude en
       absoluto, así no puede decidir buscar por su cuenta.

    Se usa la variante básica de búsqueda web (web_search_20250305), sin
    el filtrado dinámico de la variante más nueva (web_search_20260209):
    se probó directamente y el filtrado dinámico añade 1-2 rondas de
    ejecución de código para filtrar resultados, lo que en la práctica
    duplicaba o triplicaba el tiempo de respuesta (41-62s vs ~22s) para
    preguntas sencillas como el tiempo."""
    # DEBUG TEMPORAL (2026-07-18) — quitar en cuanto se identifique el
    # texto real que llega desde el Atajo de Siri. Ver /tmp/debug_siri.log.
    with open('/tmp/debug_siri.log', 'a') as f:
        f.write(f"{datetime.now()}: TEXTO RECIBIDO = '{pregunta}'\n")

    nombre_app = _detectar_comando_abrir_app(pregunta)
    if nombre_app is not None:
        exito, nombre_usado = _abrir_aplicacion_mac(nombre_app)
        if exito:
            return f"Abriendo {nombre_usado}."
        return f"No he encontrado ninguna aplicación llamada {nombre_app}."

    nombre_app_cerrar = _detectar_comando_cerrar_app(pregunta)
    if nombre_app_cerrar is not None:
        exito, nombre_usado = _cerrar_aplicacion_mac(nombre_app_cerrar)
        if exito:
            return f"Cerrando {nombre_usado}."
        return f"No he encontrado ninguna aplicación llamada {nombre_app_cerrar}."

    comando_media = _detectar_comando_multimedia(pregunta)
    if comando_media is not None:
        exito, mensaje = _ejecutar_comando_multimedia(comando_media)
        return mensaje if exito else "No he podido enviar el comando multimedia."

    comando_canal = _detectar_comando_canal(pregunta)
    if comando_canal is not None:
        accion, argumento = comando_canal
        return _ejecutar_comando_canal(accion, argumento)

    comando_nav = _detectar_comando_navegacion(pregunta)
    if comando_nav is not None:
        exito, mensaje = _ejecutar_comando_navegacion(comando_nav)
        return mensaje if exito else "No he podido enviar el comando de navegación."

    comando_ventana = _detectar_comando_ventana(pregunta)
    if comando_ventana is not None:
        exito, mensaje = _ejecutar_comando_ventana(comando_ventana)
        return mensaje if exito else "No he podido mover la ventana."

    comando_volumen = _detectar_comando_volumen(pregunta)
    if comando_volumen is not None:
        return _ejecutar_comando_volumen(comando_volumen)

    respuesta_directa = _respuesta_simple(pregunta)
    if respuesta_directa is not None:
        return respuesta_directa

    api_key = api_key or _clave_claude()
    cliente = anthropic.Anthropic(api_key=api_key)

    argumentos = dict(
        model=MODELO,
        max_tokens=1024,
        system=construir_system_prompt(pregunta),
        messages=[{"role": "user", "content": pregunta}],
    )
    if _necesita_busqueda_web(pregunta):
        argumentos["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    respuesta = cliente.messages.create(**argumentos)
    return next((b.text for b in respuesta.content if b.type == "text"), None)


def _timeout_handler(signum, frame):
    raise TimeoutError("timeout")


if __name__ == "__main__":
    texto = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not texto:
        print("ERROR: no se recibió ningún texto. Prueba de nuevo.", flush=True)
        sys.exit(1)

    # El límite depende de si la pregunta va a necesitar búsqueda web
    # (más lenta) o no — ver TIMEOUT_RAPIDO / TIMEOUT_BUSQUEDA arriba.
    limite = TIMEOUT_BUSQUEDA if _necesita_busqueda_web(texto) else TIMEOUT_RAPIDO

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(limite)
    try:
        texto_respuesta = responder(texto)
    except TimeoutError:
        print(f"DEBUG: timeout tras {limite}s", file=sys.stderr)
        print(MENSAJE_TARDANZA, flush=True)
        sys.exit(0)
    finally:
        signal.alarm(0)

    print(texto_respuesta, flush=True)
