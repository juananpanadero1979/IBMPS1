#!/usr/bin/env python3
"""
Alertas de caducidad de libros de rasca - Agencia 576 Getafe
Juan Antonio Panadero Jiménez

Regla de negocio: un libro CONFIRMADO en el portal ONCE que lleva 3 meses
(90 días) sin venderse se da por VENDIDO automáticamente — ONCE lo cobra
igualmente, se haya vendido de verdad o no. Este script clasifica cada
libro Confirmado/Activado del almacén de hoy según cuántos días lleva
desde su fecha real (la que ya trae cada libro en el JSON de
control_almacen_instantanea_{fecha}.json), para poder venderlo a tiempo.

Todo el cálculo de fechas/días es Python puro (regla del proyecto — ver
CLAUDE.md, "los cálculos SIEMPRE los hace Python, nunca la IA": esto es
dinero real, igual que un cuadre).

Niveles (sobre el límite de 90 días):
  🔴 URGENTE: más de 60 días transcurridos — venderlo YA
  🟡 AVISO:   entre 45 y 60 días transcurridos — vigilar
  🟢 OK:      menos de 45 días transcurridos

Uso:
    python3 alertas_caducidad.py            # alertas de hoy
    python3 alertas_caducidad.py 040726     # alertas para otra fecha (DDMMAA)
"""

import json
import sys
from datetime import datetime

from portal_once import STOCK_PATH

LIMITE_CADUCIDAD_DIAS = 90
UMBRAL_URGENTE_DIAS = 60
UMBRAL_AVISO_DIAS = 45

ESTADOS_RELEVANTES = ("Confirmado", "Activado")


def _cargar_json(ruta):
    if not ruta.exists():
        return None
    with open(ruta) as f:
        return json.load(f)


def _nivel(dias_transcurridos):
    if dias_transcurridos > UMBRAL_URGENTE_DIAS:
        return "URGENTE", "🔴"
    if dias_transcurridos >= UMBRAL_AVISO_DIAS:
        return "AVISO", "🟡"
    return "OK", "🟢"


def calcular_alertas_caducidad(fecha_dt=None):
    """Clasifica todos los libros Confirmado/Activado del almacén de
    `fecha_dt` (por defecto, hoy) según días transcurridos desde su fecha
    real, y los devuelve ordenados de más a menos días transcurridos (el
    más próximo a caducar primero). Libros "Vendido"/"Retirado"/"Asignado
    a vendedor" (sin fecha real, "-") quedan fuera: no están pendientes
    de vender."""
    if fecha_dt is None:
        fecha_dt = datetime.now()
    hoy_date = fecha_dt.date()
    ruta = STOCK_PATH / f"control_almacen_instantanea_{fecha_dt.strftime('%Y%m%d')}.json"
    datos = _cargar_json(ruta)
    # Si solo se ha ejecutado portal_once.py hoy (sin parsear_html_portal.py
    # detrás, el siguiente paso normal del pipeline), este JSON todavía es
    # la tabla cruda de portal_once.py (una lista), no el dict con
    # "productos"/"detalle" que escribe parsear_html_portal.py encima —
    # se trata igual que "sin datos" en vez de petar con AttributeError.
    if not isinstance(datos, dict):
        return {
            "fecha": hoy_date.strftime("%d/%m/%Y"),
            "ejecutado": False,
            "mensaje": f"No hay datos de almacén descargados para el {hoy_date.strftime('%d/%m/%Y')}",
        }

    libros = []
    for producto in datos.get("productos", []):
        nombre_producto = producto.get("producto", "?")
        for libro in producto.get("detalle", []):
            estado = libro.get("estado")
            fecha_txt = libro.get("fecha")
            if estado not in ESTADOS_RELEVANTES or not fecha_txt or fecha_txt == "-":
                continue
            try:
                fecha_libro = datetime.strptime(fecha_txt, "%d/%m/%Y").date()
            except ValueError:
                continue
            dias_transcurridos = (hoy_date - fecha_libro).days
            dias_para_caducar = LIMITE_CADUCIDAD_DIAS - dias_transcurridos
            nivel, emoji = _nivel(dias_transcurridos)
            libros.append({
                "producto": nombre_producto,
                "libro": libro.get("libro"),
                "estado": estado,
                "fecha": fecha_txt,
                "dias_transcurridos": dias_transcurridos,
                "dias_para_caducar": dias_para_caducar,
                "nivel": nivel,
                "emoji": emoji,
            })

    libros.sort(key=lambda l: l["dias_transcurridos"], reverse=True)

    return {
        "fecha": hoy_date.strftime("%d/%m/%Y"),
        "ejecutado": True,
        "libros": libros,
        "totales": {
            "urgente": sum(1 for l in libros if l["nivel"] == "URGENTE"),
            "aviso": sum(1 for l in libros if l["nivel"] == "AVISO"),
            "ok": sum(1 for l in libros if l["nivel"] == "OK"),
        },
    }


def formatear_resultado(resultado):
    if not resultado.get("ejecutado", True):
        return f"ℹ️  {resultado['mensaje']}"

    lineas = [f"CADUCIDAD DE LIBROS — {resultado['fecha']}", "=" * 45]
    if not resultado["libros"]:
        lineas.append("Sin libros Confirmado/Activado con fecha registrada.")
        return "\n".join(lineas)

    for l in resultado["libros"]:
        lineas.append(
            f"{l['emoji']} {l['producto']} — Libro {l['libro']} — confirmado hace "
            f"{l['dias_transcurridos']} días ({l['dias_para_caducar']} días para caducar)"
        )

    t = resultado["totales"]
    lineas.append("")
    lineas.append(f"Total: {t['urgente']} urgente(s), {t['aviso']} aviso(s), {t['ok']} ok")
    return "\n".join(lineas)


if __name__ == "__main__":
    fecha_arg = None
    if len(sys.argv) > 1:
        fecha_arg = datetime.strptime(sys.argv[1], "%d%m%y")
    resultado = calcular_alertas_caducidad(fecha_arg)
    print(formatear_resultado(resultado))
    print()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
