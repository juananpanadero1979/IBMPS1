#!/usr/bin/env python3
"""
Ventas de un producto en un periodo - Agencia ONCE 576 Getafe
Juan Antonio Panadero Jiménez

FIX de un bug real del asistente: al preguntar "cuánto Eurojackpot llevo
vendido esta semana" respondía que consultara el portal, en vez de sumar
el dato que YA existe en los tickets ya procesados por ocr_tickets.py —
cada ticket trae ventas.detalle con filas {producto, unidades, importe}
por código de producto (EUJ=Eurojackpot, TRI=Triplex, DUP=Dupla,
SUP=Super 11, MID="Mi día"...).

Punto importante sobre las fechas dentro de un ticket: el campo "fecha"
de nivel superior del ticket (p.ej. "13/07/2026") es el día real en que
se hizo esa caja/cierre — el dinero se cobró ese día. El campo "fecha"
de cada fila de ventas.detalle (p.ej. "14-07", sin año) es la fecha del
SORTEO al que corresponde ese cupón, no la fecha de venta: para
productos de venta anticipada (EUJ, VIE) es habitual que un ticket del
día 13 incluya filas con fecha de sorteo 14 o 17 porque ese día se
vendieron cupones para sorteos futuros. Por eso este módulo filtra
tickets por su fecha de nivel superior (día de venta real) y, dentro de
cada ticket que cae en el rango, suma TODAS las filas de ese producto
sin mirar la fecha de sorteo de cada fila — así "cuánto vendí esta
semana" refleja el dinero cobrado esa semana, no los sorteos a los que
corresponde.

Todo el cálculo (filtrado de fechas y suma) es Python puro (regla del
proyecto: "los cálculos SIEMPRE los hace Python, nunca la IA").

Uso:
    python3 ventas_producto_periodo.py EUJ                    # esta semana (lunes-domingo)
    python3 ventas_producto_periodo.py EUJ ultimos_7_dias
    python3 ventas_producto_periodo.py TRI mes_actual
"""

import json
import sys
from datetime import datetime, timedelta

from ocr_tickets import TICKETS_PATH


def _rango_semana_actual(hoy):
    lunes = hoy - timedelta(days=hoy.weekday())
    domingo = lunes + timedelta(days=6)
    return lunes, domingo


def _rango_ultimos_7_dias(hoy):
    return hoy - timedelta(days=6), hoy


def _rango_mes_actual(hoy):
    return hoy.replace(day=1), hoy


RANGOS_POR_MODO = {
    "semana_actual": _rango_semana_actual,
    "ultimos_7_dias": _rango_ultimos_7_dias,
    "mes_actual": _rango_mes_actual,
}


def resumen_ventas_producto(producto_codigo, modo="semana_actual", hoy=None):
    """Suma unidades/importe de `producto_codigo` (código de ventas.detalle,
    p.ej. "EUJ") en todos los tickets cuya fecha de venta (fecha de nivel
    superior del ticket, no la fecha de sorteo de cada fila) cae dentro
    del rango de `modo` ("semana_actual" = lunes-domingo en curso,
    "ultimos_7_dias" = hoy y los 6 anteriores, "mes_actual" = día 1 del
    mes hasta hoy).

    También devuelve `dias_sin_ticket`: fechas del rango, anteriores a
    hoy (el ticket de hoy mismo no puede existir todavía — se escanea la
    mañana siguiente, ver ocr_tickets.py), para las que no se encontró
    ningún ticket procesado. Sirve para poder decir explícitamente "faltan
    los tickets de tal fecha" en vez de dar un total silenciosamente
    incompleto."""
    if modo not in RANGOS_POR_MODO:
        raise ValueError(f"modo desconocido: {modo!r} (usa uno de {list(RANGOS_POR_MODO)})")

    hoy = hoy or datetime.now().date()
    producto_codigo = producto_codigo.upper()
    inicio, fin = RANGOS_POR_MODO[modo](hoy)

    total_importe = 0.0
    total_unidades = 0
    tickets_considerados = 0
    dias_con_ticket = set()

    for ruta in sorted(TICKETS_PATH.glob("*.json")):
        try:
            with open(ruta) as f:
                ticket = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        fecha_txt = ticket.get("fecha")
        if not fecha_txt:
            continue
        try:
            fecha_ticket = datetime.strptime(fecha_txt, "%d/%m/%Y").date()
        except ValueError:
            continue
        if not (inicio <= fecha_ticket <= fin):
            continue

        tickets_considerados += 1
        dias_con_ticket.add(fecha_ticket)
        for fila in (ticket.get("ventas") or {}).get("detalle") or []:
            if (fila.get("producto") or "").upper() != producto_codigo:
                continue
            total_importe += fila.get("importe") or 0
            total_unidades += fila.get("unidades") or 0

    # Solo se comprueban huecos hasta AYER: el ticket de hoy aún no puede
    # existir (se escanea al día siguiente), así que no es un hueco real.
    limite_hueco = min(fin, hoy - timedelta(days=1))
    dias_sin_ticket = []
    dia = inicio
    while dia <= limite_hueco:
        if dia not in dias_con_ticket:
            dias_sin_ticket.append(dia)
        dia += timedelta(days=1)

    return {
        "ejecutado": True,
        "producto": producto_codigo,
        "modo": modo,
        "inicio": inicio.strftime("%d/%m/%Y"),
        "fin": fin.strftime("%d/%m/%Y"),
        "total_importe": round(total_importe, 2),
        "total_unidades": total_unidades,
        "tickets_considerados": tickets_considerados,
        "dias_con_ticket": sorted(d.strftime("%d/%m/%Y") for d in dias_con_ticket),
        "dias_sin_ticket": [d.strftime("%d/%m/%Y") for d in dias_sin_ticket],
    }


def formatear_resultado(resultado, nombre_producto=None):
    nombre = nombre_producto or resultado["producto"]
    lineas = [
        f"VENTAS DE {nombre} ({resultado['modo']}, {resultado['inicio']} - {resultado['fin']})",
        "=" * 55,
        f"Total: {resultado['total_unidades']} unidad(es) — {resultado['total_importe']:.2f}€",
        f"({resultado['tickets_considerados']} ticket(s) procesados en el rango)",
    ]
    if resultado["dias_sin_ticket"]:
        lineas.append(
            "⚠️  Faltan los tickets de: " + ", ".join(resultado["dias_sin_ticket"])
            + " — el total de arriba puede estar incompleto."
        )
    return "\n".join(lineas)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 ventas_producto_periodo.py CODIGO [modo]", file=sys.stderr)
        sys.exit(1)
    codigo = sys.argv[1]
    modo_arg = sys.argv[2] if len(sys.argv) > 2 else "semana_actual"
    resultado = resumen_ventas_producto(codigo, modo_arg)
    print(formatear_resultado(resultado))
    print()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
