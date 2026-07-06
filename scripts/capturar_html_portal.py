#!/usr/bin/env python3
"""
Captura de HTML crudo - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Hace login completo (portal + Gestiona) y, para cada sección de interés,
activa el checkbox "Visualizar tablas anidadas" si existe en la página,
espera a que la tabla se despliegue y guarda el HTML completo tal cual
(page.content()) en config/html_backup/. No parsea nada: sirve como
materia prima para ajustar extraer_tabla() en portal_once.py cuando el
HTML de estas páginas tiene subtablas anidadas.

Uso: python3 capturar_html_portal.py
Requiere credenciales ya configuradas (ver portal_once.py --setup).
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from portal_once import (
    cargar_usuario,
    obtener_password,
    obtener_clave_segura,
    iniciar_sesion,
    navegar,
    URL_PREMIOS_PASIVA,
    URL_PREMIOS_ACTIVA,
    URL_PREMIOS_INSTANTANEA,
    URL_CONTROL_PAQUETE_PREVISTO,
    URL_CONTROL_PAQUETE_A_RETIRAR,
    URL_CONTROL_PAQUETE_RETIRADO,
    URL_ALMACEN_RASCAS,
)

HTML_BACKUP_PATH = Path(__file__).resolve().parent.parent / "config" / "html_backup"

# checkbox "Visualizar tablas anidadas" (ver config/selectores_once.json)
CHECKBOX_TABLAS_ANIDADAS = "#chkGridAnidado"

SECCIONES = [
    ("premios_pasiva.html", URL_PREMIOS_PASIVA),
    ("premios_activa.html", URL_PREMIOS_ACTIVA),
    ("premios_instantanea.html", URL_PREMIOS_INSTANTANEA),
    ("control_retirada_previsto.html", URL_CONTROL_PAQUETE_PREVISTO),
    ("control_retirada_a_retirar.html", URL_CONTROL_PAQUETE_A_RETIRAR),
    ("control_retirada_retirado.html", URL_CONTROL_PAQUETE_RETIRADO),
    ("control_almacen.html", URL_ALMACEN_RASCAS),
]


def activar_tablas_anidadas(page):
    """Marca el checkbox "Visualizar tablas anidadas" si está presente en la
    página y espera 2 segundos a que se despliegue la subtabla."""
    checkbox = page.locator(CHECKBOX_TABLAS_ANIDADAS)
    if checkbox.count() == 0:
        return
    try:
        if not checkbox.is_checked():
            checkbox.check()
            page.wait_for_timeout(2000)
    except Exception as e:
        print(f"⚠️  No se pudo activar 'Visualizar tablas anidadas': {e}")


def capturar_html_portal():
    usuario = cargar_usuario()
    password = obtener_password(usuario)
    clave_segura = obtener_clave_segura()

    HTML_BACKUP_PATH.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)  # el portal ONCE bloquea/no renderiza el login en modo headless
        contexto = navegador.new_context()
        page = contexto.new_page()

        try:
            iniciar_sesion(page, usuario, password, clave_segura)

            for nombre_archivo, url in SECCIONES:
                try:
                    navegar(page, url)
                    activar_tablas_anidadas(page)

                    destino = HTML_BACKUP_PATH / nombre_archivo
                    destino.write_text(page.content(), encoding="utf-8")
                    print(f"✅ HTML guardado: {destino.name}")
                except Exception as e:
                    print(f"⚠️  Error capturando {nombre_archivo}: {e}")

                time.sleep(2)
        finally:
            navegador.close()


if __name__ == "__main__":
    print("\n📄 CAPTURA DE HTML CRUDO - Portal ONCE")
    print("=" * 50)
    capturar_html_portal()
