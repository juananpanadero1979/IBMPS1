#!/usr/bin/env python3
"""
Parseo de HTML crudo -> JSON - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Lee los HTML guardados por capturar_html_portal.py en
config/html_backup/ y vuelca el resultado parseado (con el desglose de
subtablas incluido: Tradicional/TPV/BONO en premios, productos/libros en
control de retirada y almacén) a JSON en las carpetas de ONCE en iCloud.

Usa los mismos nombres de archivo que las funciones descargar_* de
portal_once.py (sin desglose de subtablas), a las que sustituye: estos
JSON con detalle completo son los definitivos.

Uso: python3 parsear_html_portal.py
"""

import json
from datetime import datetime
from pathlib import Path

from portal_once import PREMIOS_PATH, PAQUETES_PATH, STOCK_PATH
from parsers_portal import (
    parsear_premios_pasiva,
    parsear_premios_activa,
    parsear_premios_instantanea,
    parsear_control_retirada,
    parsear_control_almacen,
)

HTML_BACKUP_PATH = Path(__file__).resolve().parent.parent / "config" / "html_backup"


def _guardar(datos, carpeta, nombre_archivo):
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / nombre_archivo
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ JSON guardado: {destino}")
    return destino


def parsear_html_portal():
    hoy = datetime.now().strftime("%Y%m%d")
    fecha_extraccion = datetime.now().strftime("%Y-%m-%d")

    premios = [
        ("premios_pasiva.html", parsear_premios_pasiva, PREMIOS_PATH, f"premios_pasiva_{hoy}.json"),
        ("premios_activa.html", parsear_premios_activa, PREMIOS_PATH, f"premios_activa_{hoy}.json"),
        ("premios_instantanea.html", parsear_premios_instantanea, PREMIOS_PATH, f"premios_instantanea_{hoy}.json"),
    ]
    for nombre_html, funcion, carpeta, nombre_json in premios:
        ruta_html = HTML_BACKUP_PATH / nombre_html
        if not ruta_html.exists():
            print(f"⚠️  No existe {ruta_html}, se omite")
            continue
        datos = funcion(ruta_html.read_text(encoding="utf-8"), fecha_extraccion)
        _guardar(datos, carpeta, nombre_json)

    retiradas = [
        ("control_retirada_previsto.html", "previsto"),
        ("control_retirada_a_retirar.html", "a_retirar"),
        ("control_retirada_retirado.html", "retirado"),
    ]
    for nombre_html, tipo in retiradas:
        ruta_html = HTML_BACKUP_PATH / nombre_html
        if not ruta_html.exists():
            print(f"⚠️  No existe {ruta_html}, se omite")
            continue
        datos = parsear_control_retirada(ruta_html.read_text(encoding="utf-8"), tipo, fecha_extraccion)
        _guardar(datos, PAQUETES_PATH, f"control_retirada_{tipo}_{hoy}.json")

    ruta_almacen = HTML_BACKUP_PATH / "control_almacen.html"
    if ruta_almacen.exists():
        datos = parsear_control_almacen(ruta_almacen.read_text(encoding="utf-8"), fecha_extraccion)
        _guardar(datos, STOCK_PATH, f"control_almacen_instantanea_{hoy}.json")
    else:
        print(f"⚠️  No existe {ruta_almacen}, se omite")


if __name__ == "__main__":
    print("\n📊 PARSEO DE HTML A JSON - Portal ONCE")
    print("=" * 50)
    parsear_html_portal()
