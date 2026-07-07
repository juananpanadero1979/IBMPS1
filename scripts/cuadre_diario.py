#!/usr/bin/env python3
"""
Cuadre diario - Agencia ONCE 576 Getafe
Juan Antonio Panadero Jiménez

Determina el EFECTIVO del día anterior a partir del JSON del ticket TPV
(generado por ocr_tickets.py):

  1. Si el ticket trae resumen.resultado (la página RESUMEN ACTIVIDAD
     DIARIA existe y se pudo leer), se usa ese valor directamente — ya
     es el cálculo completo del propio TPV, incluyendo dentro de
     "PAGOS" tanto premios de cupones como premios de rasca. Sumar
     pagos_rasca aparte (como se hizo en una versión anterior de este
     script) contaba el rasca dos veces y por eso salía un descuadre
     grande y falso.
  2. Si no existe (p.ej. 120626.pdf, que no tiene página RESUMEN en el
     PDF original — confirmado, no se reprocesa), se calcula con:

         EFECTIVO = ventas.total - devoluciones.total
                    - premios_pagados.total - pagos_tarjeta

     (sin sumar pagos_rasca, por el mismo motivo). En este caso no hay
     un segundo valor con el que comparar, así que no se puede dar un
     veredicto CUADRE CORRECTO / DESCUADRE — se informa el EFECTIVO
     calculado y se deja constancia de que no se pudo verificar.

Todo el cálculo es Python puro (regla del proyecto: "los cálculos del
cuadre ONCE se hacen SIEMPRE con Python, nunca con IA").

Uso:
    python3 cuadre_diario.py            # cuadre de ayer
    python3 cuadre_diario.py 040726     # cuadre de una fecha concreta (DDMMAA)
"""

import json
import sys
from datetime import datetime, timedelta

from ocr_tickets import TICKETS_PATH


def _cargar_json(ruta):
    if not ruta.exists():
        return None
    with open(ruta) as f:
        return json.load(f)


def leer_ticket(fecha_dt):
    ruta = TICKETS_PATH / f"{fecha_dt.strftime('%d%m%y')}.json"
    return _cargar_json(ruta)


def calcular_cuadre_diario(fecha_dt=None):
    """Calcula el EFECTIVO para fecha_dt (por defecto, ayer). Devuelve un
    dict con "ejecutado": False si no hay ticket para esa fecha, o con
    el efectivo y su origen (resumen.resultado o fórmula de respaldo)."""
    if fecha_dt is None:
        fecha_dt = datetime.now() - timedelta(days=1)

    ticket = leer_ticket(fecha_dt)
    if ticket is None:
        return {
            "fecha": fecha_dt.strftime("%d/%m/%Y"),
            "ejecutado": False,
            "mensaje": f"No hay ticket procesado para el {fecha_dt.strftime('%d/%m/%Y')}",
        }

    ventas_total = ticket.get("ventas", {}).get("total") or 0.0
    devoluciones_total = ticket.get("devoluciones", {}).get("total") or 0.0
    premios_total = ticket.get("premios_pagados", {}).get("total") or 0.0
    tarjeta = ticket.get("pagos_tarjeta") or 0.0
    rasca_total = ticket.get("pagos_rasca", {}).get("total") or 0.0
    resultado_ticket = ticket.get("resumen", {}).get("resultado")

    formula_calculada = round(ventas_total - devoluciones_total - premios_total - tarjeta, 2)

    if resultado_ticket is not None:
        efectivo = round(resultado_ticket, 2)
        origen = "resumen.resultado"
        diferencia = 0.0
    else:
        efectivo = formula_calculada
        origen = "fórmula de respaldo (sin resumen.resultado en el ticket)"
        diferencia = None

    return {
        "fecha": fecha_dt.strftime("%d/%m/%Y"),
        "ejecutado": True,
        "ventas_total": ventas_total,
        "devoluciones_total": devoluciones_total,
        "premios_total": premios_total,
        "tarjeta": tarjeta,
        "rasca_total": rasca_total,
        "formula_calculada": formula_calculada,
        "resultado_ticket": resultado_ticket,
        "efectivo": efectivo,
        "origen": origen,
        "diferencia": diferencia,
    }


def formatear_resultado(resultado):
    lineas = []
    if not resultado.get("ejecutado", True):
        lineas.append(f"ℹ️  {resultado['mensaje']}")
        return "\n".join(lineas)

    lineas.append(f"CUADRE DIARIO — {resultado['fecha']}")
    lineas.append("=" * 45)
    lineas.append(f"EFECTIVO DEL DÍA: {resultado['efectivo']:.2f}€  (origen: {resultado['origen']})")

    dif = resultado["diferencia"]
    if dif is None:
        lineas.append(
            f"ℹ️  Sin resumen.resultado en el ticket — EFECTIVO calculado con la fórmula "
            f"de respaldo (ventas {resultado['ventas_total']:.2f} - devoluciones "
            f"{resultado['devoluciones_total']:.2f} - premios {resultado['premios_total']:.2f} "
            f"- tarjeta {resultado['tarjeta']:.2f} = {resultado['formula_calculada']:.2f}€), "
            f"no se ha podido verificar contra un segundo valor."
        )
    elif abs(dif) < 0.01:
        lineas.append("✅ CUADRE CORRECTO: 0,00€ diferencia")
    else:
        signo = "+" if dif > 0 else ""
        lineas.append(f"⚠️  DESCUADRE: {signo}{dif:.2f}€ diferencia")

    return "\n".join(lineas)


if __name__ == "__main__":
    fecha_arg = None
    if len(sys.argv) > 1:
        fecha_arg = datetime.strptime(sys.argv[1], "%d%m%y")
    resultado = calcular_cuadre_diario(fecha_arg)
    print(formatear_resultado(resultado))
    print()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
