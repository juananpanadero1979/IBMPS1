#!/usr/bin/env python3
"""
Novedades del día - Claude Code, IA y categorías personales
Juan Antonio Panadero Jiménez - IBMPS1

Cada mañana busca novedades reales de las últimas 24h en:
  1. Claude Code (funciones nuevas, comandos, modelos)
  2. Herramientas de IA relevantes (modelos nuevos, lanzamientos)
  3. Las 12 categorías personales ya configuradas en el MCP remoto
     trends-mcp (ver memoria "reference-trendsmcp-categorias"): fitness,
     longevidad, salud, péptidos, culturismo natural, informática, mods
     de relojes Seiko, nuevas tecnologías, IA, ayudas sociales y
     pensiones, gadgets, videojuegos.

Cómo busca (todo vía la API de Anthropic, en una sola llamada — este
script corre sin sesión interactiva de Claude Code, así que no puede
usar mis herramientas de esta conversación; usa la herramienta nativa
web_search de la API, más el MISMO servidor MCP remoto trends-mcp que
ya está configurado en este proyecto, conectado vía el conector MCP
de la API — mcp_servers, beta "mcp-client-2025-11-20"):
  - Lee el token de trends-mcp de ~/.claude.json (el mismo que usa
    Claude Code, sin duplicarlo en texto plano en el repo).
  - Modelo Haiku por defecto (coste — ver CLAVE_COSTE abajo). Si algún
    día hace falta más calidad de filtrado, cambiar MODELO aquí, no
    escalar a Sonnet por defecto salvo necesidad real.

Deduplicación: guarda un histórico de lo ya informado
(datos/novedades_tech/historico.json) y se lo pasa a Claude en el
prompt para que SOLO reporte lo genuinamente nuevo desde la última
ejecución — nunca repite lo mismo si no ha cambiado nada.

Verificación honesta: mismo criterio aplicado en esta sesión al validar
SkillSpector antes de instalarlo — si algo parece sospechoso
(mantenedor inconsistente, cifras contradictorias, fuente única sin
corroborar) se marca explícitamente como "verificar con cautela" en
vez de darlo por bueno.

Uso:
    python3 novedades_tech.py
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

ONCE_ROOT = Path(__file__).resolve().parent.parent
DATOS_PATH = ONCE_ROOT / "datos" / "novedades_tech"
HISTORICO_PATH = DATOS_PATH / "historico.json"
CLAUDE_JSON_PATH = Path.home() / ".claude.json"
PROYECTO_CLAUDE_CODE = str(ONCE_ROOT)

KEYCHAIN_SERVICE = "IBMPS1-ClaudeAPI"
KEYCHAIN_ACCOUNT = "ANTHROPIC_API_KEY"

# Haiku por defecto — coste real de API cada mañana (web_search +
# conector MCP), no la suscripción de Claude Code. Solo subir a Sonnet
# si el filtrado con Haiku demuestra ser insuficiente en la práctica,
# nunca por defecto.
MODELO = "claude-haiku-4-5"
MAX_TOKENS = 2048
MAX_BUSQUEDAS_WEB = 6
DIAS_RETENCION_HISTORICO = 45
TIMEOUT_SEGUNDOS = 90

CATEGORIAS_PERSONALES = [
    "Fitness", "Longevidad", "Salud", "Péptidos", "Culturismo natural",
    "Informática", "Mods de relojes Seiko y variantes", "Nuevas tecnologías",
    "Inteligencia artificial", "Ayudas sociales y pensiones", "Gadgets",
    "Videojuegos",
]


def _leer_keychain(servicio, cuenta):
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
            f"No hay clave de Claude ni en ANTHROPIC_API_KEY ni en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE}', cuenta '{KEYCHAIN_ACCOUNT}')."
        )
    return api_key


def _token_trends_mcp():
    """Lee el token del conector trends-mcp ya configurado para este
    proyecto en ~/.claude.json (mismo que usa Claude Code) — nunca se
    copia a texto plano en el repo, se lee en el momento."""
    if not CLAUDE_JSON_PATH.exists():
        return None
    try:
        with open(CLAUDE_JSON_PATH) as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    proyecto = (datos.get("projects") or {}).get(PROYECTO_CLAUDE_CODE) or {}
    entrada = (proyecto.get("mcpServers") or {}).get("trends-mcp") or {}
    cabecera = (entrada.get("headers") or {}).get("Authorization")
    if not cabecera:
        return None
    return re.sub(r"^Bearer\s+", "", cabecera, flags=re.IGNORECASE).strip()


def _cargar_historico():
    if not HISTORICO_PATH.exists():
        return {"items": []}
    try:
        with open(HISTORICO_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"items": []}


def _guardar_historico(historico, items_nuevos):
    hoy = datetime.now().date()
    limite = hoy - timedelta(days=DIAS_RETENCION_HISTORICO)
    items = historico.get("items", [])
    for item in items_nuevos:
        items.append({
            "categoria": item.get("categoria", ""),
            "resumen": item.get("resumen", "")[:200],
            "fecha": hoy.isoformat(),
        })
    items = [
        i for i in items
        if _fecha_valida(i.get("fecha")) and _fecha_valida(i.get("fecha")) >= limite
    ]
    DATOS_PATH.mkdir(parents=True, exist_ok=True)
    with open(HISTORICO_PATH, "w") as f:
        json.dump({"items": items}, f, indent=2, ensure_ascii=False)


def _fecha_valida(texto):
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


SYSTEM_PROMPT = """\
Eres el investigador de novedades diarias de Juan Antonio Panadero \
Jiménez. Tu única tarea hoy: encontrar novedades REALES de las \
últimas 24-48 horas (no información antigua ya conocida) en estos \
temas, y nada más:

1. Claude Code (funciones nuevas, comandos, cambios de modelos)
2. Herramientas de IA relevantes (modelos nuevos, lanzamientos \
importantes de la industria — no rumores)
3. Estas categorías personales, usando el servidor MCP trends-mcp \
(herramientas get_trends/get_growth/get_top_trends) cuando aporten \
señal real, y búsqueda web para corroborar o completar: {categorias}

## Regla de deduplicación — MUY IMPORTANTE
Esto es lo que YA se informó en los últimos {dias_retencion} días — \
NUNCA repitas nada de esta lista ni una variante trivial de lo mismo, \
solo lo genuinamente nuevo desde entonces:
{historico_texto}

## Regla de verificación honesta
Antes de dar algo por bueno, aplica el mismo criterio que se usaría \
para verificar un paquete de software antes de instalarlo: si una \
fuente es única y no corroborada, si las cifras se contradicen entre \
sí, o si algo suena a marketing sin sustancia verificable, NO lo \
presentes como un hecho — márcalo explícitamente como "⚠️ verificar \
con cautela" y di por qué dudas de ello, en vez de darlo por bueno.

## Formato de salida — ESTRICTO
Usa las herramientas que necesites (búsqueda web, trends-mcp), y \
termina tu respuesta SIEMPRE con un bloque de código JSON (```json \
... ```) con esta forma exacta, sin texto fuera del bloque en esa \
última parte:

{{
  "hay_novedades": true o false,
  "items": [
    {{"categoria": "...", "resumen": "una o dos frases, sin markdown, tono directo", "cautela": true o false}}
  ]
}}

Si no encuentras nada genuinamente nuevo y relevante en ningún tema, \
devuelve "hay_novedades": false y "items": [] — NUNCA inventes una \
novedad para rellenar ni fuerces algo trivial solo por reportar algo."""


def _construir_historico_texto(historico):
    items = historico.get("items", [])
    if not items:
        return "(sin histórico todavía — es la primera ejecución)"
    return "\n".join(f"- [{i['fecha']}] [{i['categoria']}] {i['resumen']}" for i in items)


def _extraer_json_final(texto):
    coincidencias = re.findall(r"```json\s*(\{.*?\})\s*```", texto, re.DOTALL)
    if not coincidencias:
        return None
    try:
        return json.loads(coincidencias[-1])
    except json.JSONDecodeError:
        return None


def buscar_novedades():
    api_key = _clave_claude()
    cliente = anthropic.Anthropic(api_key=api_key)
    historico = _cargar_historico()

    system = SYSTEM_PROMPT.format(
        categorias=", ".join(CATEGORIAS_PERSONALES),
        dias_retencion=DIAS_RETENCION_HISTORICO,
        historico_texto=_construir_historico_texto(historico),
    )

    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAX_BUSQUEDAS_WEB,
    }]
    mcp_servers = []
    token_trends = _token_trends_mcp()
    if token_trends:
        mcp_servers.append({
            "type": "url",
            "name": "trends-mcp",
            "url": "https://api.trendsmcp.ai/mcp",
            "authorization_token": token_trends,
        })
        # La API exige declarar explícitamente en `tools` qué servidor
        # MCP de `mcp_servers` se puede usar (mcp_toolset) — definir el
        # conector no basta por sí solo.
        tools.append({"type": "mcp_toolset", "mcp_server_name": "trends-mcp"})

    argumentos = dict(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{
            "role": "user",
            "content": "Busca las novedades de hoy siguiendo exactamente las reglas del system prompt.",
        }],
        tools=tools,
        betas=["mcp-client-2025-11-20"],
    )
    if mcp_servers:
        argumentos["mcp_servers"] = mcp_servers

    respuesta = cliente.beta.messages.create(**argumentos)
    texto_final = "\n".join(
        bloque.text for bloque in respuesta.content
        if getattr(bloque, "type", None) == "text"
    )
    resultado = _extraer_json_final(texto_final)
    if resultado is None:
        return {
            "fecha": datetime.now().strftime("%d/%m/%Y"),
            "ejecutado": False,
            "mensaje": "No se pudo extraer el bloque JSON de la respuesta — ver texto_crudo.",
            "texto_crudo": texto_final[:2000],
        }

    items = resultado.get("items") or []
    if items:
        _guardar_historico(historico, items)

    return {
        "fecha": datetime.now().strftime("%d/%m/%Y"),
        "ejecutado": True,
        "hay_novedades": bool(resultado.get("hay_novedades")) and bool(items),
        "items": items,
    }


if __name__ == "__main__":
    DATOS_PATH.mkdir(parents=True, exist_ok=True)
    hoy = datetime.now().strftime("%Y%m%d")
    destino = DATOS_PATH / f"novedades_{hoy}.json"

    try:
        resultado = buscar_novedades()
    except Exception as e:
        resultado = {
            "fecha": datetime.now().strftime("%d/%m/%Y"),
            "ejecutado": False,
            "mensaje": f"Error buscando novedades: {e}",
        }

    with open(destino, "w") as f:
        json.dump(resultado, f, indent=2, ensure_ascii=False)

    if not resultado.get("ejecutado"):
        print(f"⚠️  Novedades tech no ejecutado: {resultado.get('mensaje')}")
        sys.exit(0)

    n = len(resultado.get("items") or [])
    print(f"✅ Novedades tech guardadas ({n} novedad(es)): {destino}")
