#!/usr/bin/env python3
"""
Agente CEREBRO - Orquestador del sistema multi-agente IBMPS1
Juan Antonio Panadero Jiménez

Recibe una pregunta/tarea, decide (por palabras clave, según las reglas
de enrutamiento de agentes/CEREBRO.md) qué agente especializado debe
responderla, y llama a Claude con el system prompt de ese agente (el
contenido íntegro de su .md en agentes/).

Enrutamiento (agentes/CEREBRO.md):
  ONCE      → cuadre, liquidación, stock, portal, tickets
  SALUD     → Zepp, biomarcadores, longevidad, suplementos
  INGENIERO → IA, tecnología, modelos, novedades
  BUTLER    → calendario, recordatorios, tareas, agenda

Si la pregunta no encaja claramente con un solo agente (ninguna palabra
clave reconocida, o coincide con varios agentes por igual), CEREBRO no
elige a ciegas — su propia regla de enrutamiento dice "si hay duda,
pregunta al usuario antes de actuar" — así que se devuelve una petición
de aclaración en vez de una respuesta.

Uso:
    python3 agente_cerebro.py "¿cuánto cuadre tengo hoy?"
    python3 agente_cerebro.py              # modo interactivo
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import anthropic

import cuadre_diario
from portal_once import LIQUIDACIONES_PATH, PAQUETES_PATH, PREMIOS_PATH, STOCK_PATH

AGENTES_PATH = Path(__file__).resolve().parent.parent / "agentes"

KEYCHAIN_SERVICE = "IBMPS1-ClaudeAPI"
KEYCHAIN_ACCOUNT = "ANTHROPIC_API_KEY"
MODELO = "claude-sonnet-5"

# Palabras clave de enrutamiento, tal como las definen agentes/CEREBRO.md
# y la petición original (con singular/plural donde aplica, para que el
# matching por palabra completa no falle por una simple "s" al final).
PALABRAS_CLAVE = {
    "ONCE": [
        "once", "cuadre", "liquidacion", "liquidaciones",
        "stock", "portal", "ticket", "tickets", "saldo", "acreedor",
        "paquete", "paquetes", "premio", "premios", "rasca", "rascas",
        "almacen", "retirada",
    ],
    "SALUD": [
        "salud", "zepp", "biomarcador", "biomarcadores",
        "longevidad", "suplemento", "suplementos",
    ],
    "INGENIERO": [
        "ia", "tecnologia", "modelo", "modelos", "novedades",
    ],
    "BUTLER": [
        "calendario", "recordatorio", "recordatorios",
        "tarea", "tareas", "agenda",
    ],
}


def _sin_acentos(texto):
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )


def _normalizar(texto):
    return _sin_acentos(texto).lower()


def decidir_agente(pregunta):
    """Devuelve (nombre_agente, motivo) si hay una decisión clara, o
    (None, motivo) si la pregunta es ambigua o no encaja con ninguno."""
    texto_norm = _normalizar(pregunta)
    puntuaciones = {}
    coincidencias = {}
    for agente, palabras in PALABRAS_CLAVE.items():
        encontradas = [
            p for p in palabras
            if re.search(rf"\b{re.escape(p)}\b", texto_norm)
        ]
        if encontradas:
            puntuaciones[agente] = len(encontradas)
            coincidencias[agente] = encontradas

    if not puntuaciones:
        return None, "Ninguna palabra clave reconocida — no está claro a qué agente corresponde."

    max_puntuacion = max(puntuaciones.values())
    ganadores = [a for a, p in puntuaciones.items() if p == max_puntuacion]

    if len(ganadores) > 1:
        detalle = "; ".join(f"{a} ({', '.join(coincidencias[a])})" for a in ganadores)
        return None, f"La pregunta encaja con varios agentes por igual: {detalle}."

    agente = ganadores[0]
    return agente, f"Coincide con {agente} por: {', '.join(coincidencias[agente])}"


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
    print(f"DEBUG ENV: {os.environ.get('ANTHROPIC_API_KEY', 'NO ENCONTRADA')[:20]}", file=sys.stderr)

    # Primero, variable de entorno (evita depender del Keychain por
    # completo en sesiones sin interfaz gráfica, p.ej. SSH).
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    # Si no está en el entorno, Keychain (vía `security`, no keyring).
    api_key = _leer_keychain(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if not api_key:
        raise RuntimeError(
            f"No hay clave de Claude ni en la variable de entorno "
            f"ANTHROPIC_API_KEY ni en el Keychain (servicio "
            f"'{KEYCHAIN_SERVICE}', cuenta '{KEYCHAIN_ACCOUNT}')."
        )
    return api_key


def leer_system_prompt(agente):
    ruta = AGENTES_PATH / f"{agente}.md"
    if not ruta.exists():
        raise FileNotFoundError(f"No existe el fichero de agente: {ruta}")
    return ruta.read_text(encoding="utf-8")


# Palabras clave para decidir, DENTRO del agente ONCE, qué categoría(s)
# de datos reales son relevantes para la pregunta concreta — para no
# meter siempre todo el contexto (liquidación, paquetes, almacén,
# premios) cuando solo se ha preguntado por una cosa.
PALABRAS_CLAVE_CONTEXTO_ONCE = {
    "paquetes": ["paquete", "paquetes", "retirada", "retirar", "retirado"],
    "almacen": ["rasca", "rascas", "almacen", "instantanea", "instantaneas"],
    "liquidacion": ["liquidacion", "liquidaciones", "saldo", "acreedor", "cuadre", "efectivo"],
    "premios": ["premio", "premios"],
}


def _categorias_contexto_once(pregunta):
    """Devuelve la lista de categorías de datos (paquetes/almacen/
    liquidacion/premios) que coinciden por palabra clave con la
    pregunta. Puede haber varias, o ninguna."""
    texto_norm = _normalizar(pregunta)
    return [
        categoria
        for categoria, palabras in PALABRAS_CLAVE_CONTEXTO_ONCE.items()
        if any(re.search(rf"\b{re.escape(p)}\b", texto_norm) for p in palabras)
    ]


def _contexto_paquetes():
    hoy = datetime.now().strftime("%Y%m%d")
    partes = []
    for etiqueta, nombre in (
        ("previsto", "control_retirada_previsto"),
        ("a retirar", "control_retirada_a_retirar"),
        ("retirado", "control_retirada_retirado"),
    ):
        ruta = PAQUETES_PATH / f"{nombre}_{hoy}.json"
        if not ruta.exists():
            partes.append(f"PAQUETES ({etiqueta}): no hay datos descargados hoy.")
            continue
        with open(ruta) as f:
            datos = json.load(f)
        partes.append(f"PAQUETES ({etiqueta}): {len(datos.get('paquetes', []))} paquete(s) registrados hoy.")
    return "\n".join(partes)


def _contexto_almacen():
    hoy = datetime.now().strftime("%Y%m%d")
    ruta = STOCK_PATH / f"control_almacen_instantanea_{hoy}.json"
    if not ruta.exists():
        return "CONTROL DE ALMACÉN (rascas): no hay datos descargados hoy."
    with open(ruta) as f:
        datos = json.load(f)
    return f"CONTROL DE ALMACÉN (rascas): {len(datos.get('productos', []))} producto(s) con libros registrados hoy."


def _contexto_liquidacion():
    """Liquidación de hoy + cuadre de ayer (ver cuadre_diario.py) — ambos
    conceptos caen bajo la misma categoría "liquidacion/saldo/cuadre"."""
    partes = []

    hoy = datetime.now().strftime("%Y%m%d")
    ruta_liquidacion = LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.json"
    if ruta_liquidacion.exists():
        with open(ruta_liquidacion) as f:
            datos = json.load(f)
        if datos.get("ejecutado", True):
            partes.append(f"LIQUIDACIÓN DIARIA ({datos.get('fecha_consulta')}): {datos.get('mensaje')}")
        else:
            partes.append(f"LIQUIDACIÓN DIARIA: {datos.get('mensaje')}")
    else:
        partes.append("LIQUIDACIÓN DIARIA: no hay datos descargados del portal todavía hoy.")

    try:
        resultado_cuadre = cuadre_diario.calcular_cuadre_diario()
        if resultado_cuadre.get("ejecutado"):
            partes.append(
                f"CUADRE DE AYER ({resultado_cuadre['fecha']}): efectivo "
                f"{resultado_cuadre['efectivo']:.2f}€ (origen: {resultado_cuadre['origen']})"
            )
        else:
            partes.append(f"CUADRE DE AYER: {resultado_cuadre['mensaje']}")
    except Exception as e:
        partes.append(f"CUADRE DE AYER: no disponible ({e})")

    return "\n".join(partes)


def _contexto_premios():
    """Los premios_pasiva/activa/instantanea son listados ACUMULADOS del
    portal (mezclan entradas de varias fechas en un mismo fichero) —
    aquí se da solo un recuento/total agregado, señalando explícitamente
    que no está filtrado por fecha, para no dar una cifra que parezca
    "la de hoy" sin serlo."""
    hoy = datetime.now().strftime("%Y%m%d")
    partes = []
    for tipo in ("pasiva", "activa", "instantanea"):
        ruta = PREMIOS_PATH / f"premios_{tipo}_{hoy}.json"
        if not ruta.exists():
            partes.append(f"PREMIOS {tipo.upper()}: no hay datos descargados hoy.")
            continue
        with open(ruta) as f:
            datos = json.load(f)
        entradas = datos.get("premios", [])
        total = sum(p.get("importe_total") or 0 for p in entradas)
        partes.append(
            f"PREMIOS {tipo.upper()}: {len(entradas)} entrada(s) acumuladas del portal, "
            f"importe total {total:.2f}€ (dato acumulado, no filtrado por fecha)."
        )
    return "\n".join(partes)


_CONSTRUCTORES_CONTEXTO_ONCE = {
    "paquetes": _contexto_paquetes,
    "almacen": _contexto_almacen,
    "liquidacion": _contexto_liquidacion,
    "premios": _contexto_premios,
}


def _contexto_real_once(pregunta):
    """Datos reales ya descargados del portal y ya calculados con Python,
    pero SOLO los relevantes para la pregunta concreta — nunca todo el
    contexto siempre. Si la pregunta no pide ningún dato reconocible
    (p.ej. una pregunta genérica sobre la fórmula del cuadre), no se
    incluye ningún dato: Claude responde con lo que ya sabe de su system
    prompt, sin contexto que no viene a cuento."""
    categorias = _categorias_contexto_once(pregunta)
    if not categorias:
        return None
    return "\n\n".join(_CONSTRUCTORES_CONTEXTO_ONCE[c]() for c in categorias)


def preguntar_agente(agente, pregunta, api_key=None):
    """Llama a Claude con el system prompt del agente indicado y
    devuelve el texto de la respuesta. Para ONCE, antepone los datos
    reales ya descargados/calculados (ver _contexto_real_once) para que
    la respuesta cite cifras reales en vez de hablar en genérico."""
    api_key = api_key or _clave_claude()
    system_prompt = leer_system_prompt(agente)

    contenido_usuario = pregunta
    if agente == "ONCE":
        contexto = _contexto_real_once(pregunta)
        if contexto:
            contenido_usuario = (
                "[DATOS REALES ya descargados del portal y ya calculados con Python — "
                "interpreta y explica estos datos, no inventes ni recalcules otros valores]\n"
                f"{contexto}\n\n"
                f"Pregunta de Juan Antonio: {pregunta}"
            )

    cliente = anthropic.Anthropic(api_key=api_key)
    respuesta = cliente.messages.create(
        model=MODELO,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": contenido_usuario}],
    )
    return next((b.text for b in respuesta.content if b.type == "text"), None)


def procesar(pregunta):
    """Punto de entrada principal: decide el agente y devuelve su
    respuesta, o un mensaje de aclaración si la pregunta es ambigua."""
    agente, motivo = decidir_agente(pregunta)
    if agente is None:
        return {
            "agente": None,
            "motivo_enrutamiento": motivo,
            "respuesta": None,
            "aclaracion": (
                f"CEREBRO: no tengo claro qué agente debe responder esto. {motivo} "
                f"¿Puedes concretar si es sobre ONCE, SALUD, INGENIERO o BUTLER?"
            ),
        }
    respuesta = preguntar_agente(agente, pregunta)
    return {
        "agente": agente,
        "motivo_enrutamiento": motivo,
        "respuesta": respuesta,
        "aclaracion": None,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        pregunta_usuario = " ".join(sys.argv[1:])
    else:
        pregunta_usuario = input("Pregunta para CEREBRO: ").strip()

    resultado = procesar(pregunta_usuario)
    if resultado["aclaracion"]:
        print(resultado["aclaracion"])
    else:
        print(f"[{resultado['agente']}] {resultado['motivo_enrutamiento']}\n")
        print(resultado["respuesta"])
