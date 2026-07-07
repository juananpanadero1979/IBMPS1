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
from datetime import datetime
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


def _contexto_liquidacion():
    hoy = datetime.now().strftime("%Y%m%d")
    datos = _cargar_json(LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.json")
    if not datos:
        return "LIQUIDACIÓN DIARIA: no hay datos descargados hoy."
    if not datos.get("ejecutado", True):
        return f"LIQUIDACIÓN DIARIA: {datos.get('mensaje')}"
    linea = f"LIQUIDACIÓN DIARIA ({datos.get('fecha_consulta')}): {datos.get('mensaje')}"
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
        if datos is None:
            partes.append(f"{etiqueta}: sin datos hoy")
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


def _necesita_detalle_paquetes(pregunta):
    texto_norm = _normalizar(pregunta)
    return any(
        re.search(rf"\b{re.escape(p)}\b", texto_norm)
        for p in PALABRAS_CLAVE_DETALLE_PAQUETES
    )


def _contexto_paquetes_detalle():
    """Detalle completo de cada paquete: por cada producto (X10, X50,
    Mega Millonario, Cuponazo...), su cantidad y el desglose de cada
    cupón (número, cantidad, rango de series) o libro (lotería
    instantánea)."""
    hoy = datetime.now().strftime("%Y%m%d")
    bloques = []
    for etiqueta, nombre in (
        ("previsto", "control_retirada_previsto"),
        ("a retirar", "control_retirada_a_retirar"),
        ("retirado", "control_retirada_retirado"),
    ):
        datos = _cargar_json(PAQUETES_PATH / f"{nombre}_{hoy}.json")
        if not datos or not datos.get("paquetes"):
            bloques.append(f"PAQUETE ({etiqueta}): sin datos hoy.")
            continue
        for paquete in datos["paquetes"]:
            descripcion = paquete.get("descripcion", "?")
            lineas_producto = []
            for prod in paquete.get("productos", []):
                nombre_prod = prod.get("producto", "?")
                cantidad = prod.get("cantidad", "?")
                items = []
                for d in prod.get("detalle", []):
                    if "cupón" in d:
                        items.append(
                            f"cupón {d['cupón']} x{d.get('cantidad', '?')} "
                            f"series {d.get('series', '?')}"
                        )
                    elif "libro" in d:
                        items.append(f"libro {d['libro']}")
                detalle_txt = "; ".join(items) if items else "(sin desglose)"
                lineas_producto.append(f"  - {nombre_prod} (cantidad {cantidad}): {detalle_txt}")
            bloques.append(f"PAQUETE {etiqueta} ({descripcion}):\n" + "\n".join(lineas_producto))
    return "\n\n".join(bloques)


def _contexto_almacen():
    hoy = datetime.now().strftime("%Y%m%d")
    datos = _cargar_json(STOCK_PATH / f"control_almacen_instantanea_{hoy}.json")
    if not datos:
        return "ALMACÉN RASCAS: sin datos hoy."
    return f"ALMACÉN RASCAS: {len(datos.get('productos', []))} producto(s) registrados hoy."


def _contexto_premios():
    hoy = datetime.now().strftime("%Y%m%d")
    partes = []
    for tipo in ("pasiva", "activa", "instantanea"):
        datos = _cargar_json(PREMIOS_PATH / f"premios_{tipo}_{hoy}.json")
        if not datos:
            partes.append(f"{tipo}: sin datos hoy")
            continue
        entradas = datos.get("premios", [])
        total = sum(p.get("importe_total") or 0 for p in entradas)
        partes.append(f"{tipo}: {total:.2f}€ acumulado, no filtrado por fecha")
    return "PREMIOS (" + ", ".join(partes) + ")"


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
    "liquidacion": ["liquidacion", "liquidaciones", "saldo", "acreedor", "cuadre", "efectivo"],
    "premios": ["premio", "premios"],
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
}

_CONSTRUCTORES_CONTEXTO = {
    "paquetes": _contexto_paquetes,
    "almacen": _contexto_almacen,
    "liquidacion": _contexto_liquidacion,
    "premios": _contexto_premios,
    "agenda": _contexto_agenda,
    "comisiones": _contexto_comisiones,
    "estadisticas": _contexto_estadisticas,
    "incidencias": _contexto_incidencias,
    "registro_jornada": _contexto_registro_jornada,
    "solicitudes": _contexto_solicitudes,
    "nomina": _contexto_nomina,
}

# Categorías del "portal" en sentido estricto (excluye agenda, que es
# calendario/recordatorios personales, no datos del portal ONCE) — son
# las que se cargan TODAS de golpe cuando la pregunta es genérica sobre
# el portal y no encaja en ninguna categoría concreta. nomina se excluye
# de esta carga general porque implica leer un PDF entero (más lento);
# solo se carga si se pregunta explícitamente por nómina.
CATEGORIAS_PORTAL_COMPLETO = [
    "paquetes", "almacen", "liquidacion", "premios",
    "comisiones", "estadisticas", "incidencias", "registro_jornada", "solicitudes",
]


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
    categorias = [
        categoria for categoria, palabras in PALABRAS_CLAVE_CONTEXTO.items()
        if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in palabras)
    ]
    if not categorias:
        categorias = CATEGORIAS_PORTAL_COMPLETO

    partes = []
    for c in categorias:
        if c == "paquetes" and _necesita_detalle_paquetes(pregunta):
            partes.append(_contexto_paquetes_detalle())
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
solicitudes, nómina...) → usa SOLO los datos reales de la sección \
"DATOS DE HOY" de abajo, para lo que pregunten en concreto.
- Los cálculos de cuadre/dinero de ONCE SIEMPRE los hace Python, nunca \
tú — si no hay un cálculo ya hecho en los datos de hoy, dilo, no \
inventes ni recalcules cifras.
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
    1. Si es un saludo/cortesía simple → respuesta fija en Python, sin
       llamar a Claude.
    2. Si no, llama a Claude con el system prompt combinado (contexto de
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
