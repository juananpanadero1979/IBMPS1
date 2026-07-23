#!/usr/bin/env python3
"""
Cuadre diario - Agencia ONCE 576 Getafe
Juan Antonio Panadero Jiménez

Determina el RESULTADO DEL DÍA del último día trabajado (el ticket ya
procesado más reciente disponible, no una fecha fija "ayer" — ver
ticket_mas_reciente()) a partir del JSON del ticket TPV (generado por
ocr_tickets.py).

IMPORTANTE — esto NO es dinero físico ("efectivo" en caja) que el
vendedor tenga en su poder ni que arquee: es una diferencia CONTABLE
entre ventas y pagos que se resta automáticamente y pasa a formar
parte del saldo acreedor/deudor de la liquidación periódica. El
vendedor nunca maneja ni cuadra este importe como caja física —
corrección de terminología aplicada el 2026-07-20 (antes se llamaba
"EFECTIVO DEL DÍA", nombre engañoso).

  1. Si el ticket trae resumen.resultado (la página RESUMEN ACTIVIDAD
     DIARIA existe y se pudo leer), se usa ese valor directamente — ya
     es el cálculo completo del propio TPV, incluyendo dentro de
     "PAGOS" tanto premios de cupones como premios de rasca. Sumar
     pagos_rasca aparte (como se hizo en una versión anterior de este
     script) contaba el rasca dos veces y por eso salía una diferencia
     grande y falsa.
  2. Si no existe (p.ej. 120626.pdf, que no tiene página RESUMEN en el
     PDF original — confirmado, no se reprocesa), se calcula con:

         RESULTADO DEL DÍA = ventas.total - devoluciones.total
                              - premios_pagados.total - pagos_tarjeta

     (sin sumar pagos_rasca, por el mismo motivo). En este caso no hay
     un segundo valor con el que comparar, así que no se puede dar un
     veredicto de verificación — se informa el RESULTADO DEL DÍA
     calculado y se deja constancia de que no se pudo verificar.

Todo el cálculo es Python puro (regla del proyecto: "los cálculos del
cuadre ONCE se hacen SIEMPRE con Python, nunca con IA").

Uso:
    python3 cuadre_diario.py            # cuadre del último ticket disponible
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


def ticket_mas_reciente(antes_de=None):
    """Devuelve la fecha (date) del ticket JSON ya procesado más
    reciente en TICKETS_PATH, con fecha <= `antes_de` (por defecto,
    hoy) — o None si no hay ninguno.

    Los nombres de archivo son DDMMAA.json — NO se pueden ordenar
    alfabéticamente como si fueran fechas (p.ej. "170726" queda antes
    que "190626" alfabéticamente pese a que 17/07/26 es posterior a
    19/06/26), así que hay que parsear cada nombre y comparar la fecha
    real, no el string."""
    if antes_de is None:
        antes_de = datetime.now().date()
    elif isinstance(antes_de, datetime):
        antes_de = antes_de.date()

    mejor = None
    for ruta in TICKETS_PATH.glob("*.json"):
        try:
            fecha = datetime.strptime(ruta.stem, "%d%m%y").date()
        except ValueError:
            continue
        if fecha > antes_de:
            continue
        if mejor is None or fecha > mejor:
            mejor = fecha
    return mejor


def calcular_cuadre_diario(fecha_dt=None):
    """Calcula el RESULTADO DEL DÍA (diferencia contable ventas-pagos,
    NO efectivo/caja física — ver aviso al inicio del módulo) para
    fecha_dt. Si no se especifica, usa el ÚLTIMO ticket ya procesado
    disponible (ver ticket_mas_reciente) en vez de una fecha fija
    "ayer".

    FIX de un bug real (2026-07-20, mismo patrón que el ya arreglado en
    la liquidación diaria de portal_once.py): con fecha fija "ayer", el
    cuadre de cualquier lunes fallaba siempre con "no hay ticket
    procesado para el domingo", aunque el ticket del viernes (último
    día trabajado real) sí estuviera disponible — domingo nunca tiene
    ticket, así que "ayer a secas" garantizaba fallo todos los lunes.
    Buscar el ticket más reciente en disco, sea cual sea su fecha,
    también cubre automáticamente cualquier arrastre por festivos o
    gaps más largos sin necesitar lógica de calendario aparte: no hace
    falta saber POR QUÉ faltan días intermedios, solo mirar qué es lo
    último que existe.

    Si se pasa fecha_dt explícitamente (p.ej. desde la CLI con una
    fecha concreta), se respeta tal cual — la búsqueda de "lo último
    disponible" solo se activa cuando no se pide un día en particular.
    Si no hay ningún ticket en absoluto (carpeta vacía, primer uso), se
    cae de vuelta al comportamiento anterior (ayer) para dar un mensaje
    de "no hay ticket" con una fecha de referencia sensata.

    Devuelve un dict con "ejecutado": False si no hay ticket disponible,
    o con "resultado_dia" (la diferencia contable, no efectivo) y su
    origen (resumen.resultado o fórmula de respaldo)."""
    if fecha_dt is None:
        fecha_encontrada = ticket_mas_reciente()
        if fecha_encontrada is None:
            fecha_dt = datetime.now() - timedelta(days=1)
        else:
            fecha_dt = datetime.combine(fecha_encontrada, datetime.min.time())

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
        resultado_dia = round(resultado_ticket, 2)
        origen = "resumen.resultado"
        diferencia = 0.0
    else:
        resultado_dia = formula_calculada
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
        "resultado_dia": resultado_dia,
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
    lineas.append(
        f"RESULTADO DEL DÍA: {resultado['resultado_dia']:.2f}€  (origen: {resultado['origen']})"
    )

    dif = resultado["diferencia"]
    if dif is None:
        lineas.append(
            f"ℹ️  Sin resumen.resultado en el ticket — RESULTADO DEL DÍA calculado con la "
            f"fórmula de respaldo (ventas {resultado['ventas_total']:.2f} - devoluciones "
            f"{resultado['devoluciones_total']:.2f} - premios {resultado['premios_total']:.2f} "
            f"- tarjeta {resultado['tarjeta']:.2f} = {resultado['formula_calculada']:.2f}€), "
            f"no se ha podido verificar contra un segundo valor."
        )
    elif abs(dif) < 0.01:
        lineas.append("✅ Cálculo del ticket verificado: coincide con resumen.resultado")
    else:
        signo = "+" if dif > 0 else ""
        lineas.append(
            f"⚠️  Verificación del ticket: la fórmula no coincide con resumen.resultado "
            f"({signo}{dif:.2f}€ de diferencia)"
        )

    return "\n".join(lineas)


if __name__ == "__main__":
    fecha_arg = None
    if len(sys.argv) > 1:
        fecha_arg = datetime.strptime(sys.argv[1], "%d%m%y")
    resultado = calcular_cuadre_diario(fecha_arg)
    print(formatear_resultado(resultado))
    print()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
