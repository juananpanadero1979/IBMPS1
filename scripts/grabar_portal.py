#!/usr/bin/env python3
"""
Grabador de navegación - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Abre Chromium en modo visible sobre el portal ONCE y deja que Juan Antonio
navegue manualmente durante un tiempo fijo. Mientras tanto va imprimiendo
en consola cada click y cada cambio de URL, y al terminar vuelca un resumen
de los selectores detectados a config/selectores_once.json.

Uso: python3 grabar_portal.py
Este resumen sirve como referencia para ajustar los selectores TODO de
portal_once.py; no sustituye la revisión manual de cada uno.
"""

import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from portal_once import PORTAL_URL

DURACION_SEGUNDOS = 600
SELECTORES_PATH = Path(__file__).resolve().parent.parent / "config" / "selectores_once.json"

# JS inyectado en cada documento de cada página para capturar clicks
SCRIPT_CAPTURA_CLICKS = """
document.addEventListener('click', (event) => {
    const el = event.target;
    const info = {
        tag: el.tagName ? el.tagName.toLowerCase() : null,
        id: el.id || null,
        name: el.getAttribute('name') || null,
        role: el.getAttribute('role') || null,
        type: el.getAttribute('type') || null,
        placeholder: el.getAttribute('placeholder') || null,
        ariaLabel: el.getAttribute('aria-label') || null,
        text: (el.innerText || el.value || '').trim().slice(0, 80) || null,
    };
    if (window.__registrarClick) {
        window.__registrarClick(info);
    }
}, true);
"""

urls_visitadas = []
elementos_clicados = {}
paginas_instrumentadas = set()


def registrar_click(info):
    hora = datetime.now().strftime("%H:%M:%S")
    descripcion = info.get("text") or info.get("ariaLabel") or info.get("id") or info.get("tag")
    print(f"[{hora}] 🖱️  click -> {descripcion}")

    clave = (info.get("tag"), info.get("id"), info.get("name"), info.get("role"), info.get("text"))
    if clave in elementos_clicados:
        elementos_clicados[clave]["veces_clicado"] += 1
    else:
        elementos_clicados[clave] = {
            "tag": info.get("tag"),
            "id": info.get("id"),
            "name": info.get("name"),
            "role": info.get("role"),
            "type": info.get("type"),
            "placeholder": info.get("placeholder"),
            "ariaLabel": info.get("ariaLabel"),
            "text": info.get("text"),
            "selector_sugerido": sugerir_selector(info),
            "veces_clicado": 1,
        }


def sugerir_selector(info):
    if info.get("id"):
        return f'#{info["id"]}'
    if info.get("role") and (info.get("text") or info.get("ariaLabel")):
        nombre = info.get("text") or info.get("ariaLabel")
        return f'page.get_by_role("{info["role"]}", name="{nombre}")'
    if info.get("placeholder"):
        return f'page.get_by_placeholder("{info["placeholder"]}")'
    if info.get("name"):
        return f'[name="{info["name"]}"]'
    if info.get("text"):
        return f'page.get_by_text("{info["text"]}")'
    return None


def instrumentar_pagina(page):
    if page in paginas_instrumentadas:
        return
    paginas_instrumentadas.add(page)

    page.add_init_script(SCRIPT_CAPTURA_CLICKS)
    page.expose_function("__registrarClick", registrar_click)

    def on_navegacion(frame):
        if frame == page.main_frame and frame.url not in urls_visitadas:
            urls_visitadas.append(frame.url)
            hora = datetime.now().strftime("%H:%M:%S")
            print(f"[{hora}] 🌐 navegación -> {frame.url}")

    page.on("framenavigated", on_navegacion)


def grabar():
    print("\n🎥 GRABADOR DE NAVEGACIÓN - Portal ONCE")
    print("=" * 50)
    print(f"Se abrirá Chromium durante {DURACION_SEGUNDOS} segundos.")
    print("Navega manualmente por el portal; cada click y URL se irá listando aquí.\n")

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)
        contexto = navegador.new_context()

        contexto.on("page", instrumentar_pagina)

        page = contexto.new_page()
        instrumentar_pagina(page)
        page.goto(PORTAL_URL)

        page.wait_for_timeout(DURACION_SEGUNDOS * 1000)

        navegador.close()

    guardar_resumen()


def guardar_resumen():
    resumen = {
        "capturado_en": datetime.now().isoformat(timespec="seconds"),
        "url_inicial": PORTAL_URL,
        "duracion_segundos": DURACION_SEGUNDOS,
        "urls_visitadas": urls_visitadas,
        "elementos_clicados": list(elementos_clicados.values()),
    }

    SELECTORES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SELECTORES_PATH, "w") as f:
        json.dump(resumen, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Resumen guardado en {SELECTORES_PATH}")
    print(f"   {len(urls_visitadas)} URLs visitadas, {len(elementos_clicados)} elementos distintos clicados.")


if __name__ == "__main__":
    grabar()
