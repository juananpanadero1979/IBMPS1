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
     "PAGOS" tanto premios de cupones como premios de rasca.
  2. Si no existe (p.ej. 120626.pdf, que no tiene página RESUMEN en el
     PDF original — confirmado, no se reprocesa), se calcula con:

         RESULTADO DEL DÍA = ventas.total - premios_pagados.total
                              - pagos_rasca.total

     FIX de un bug real (2026-07-23, ultrareview + diagnóstico manual
     campo a campo sobre los 31 tickets de 2026): la fórmula anterior
     (ventas - devoluciones - premios - tarjeta) estaba mal en dos
     sentidos a la vez. Verificado contra los tickets ya procesados,
     "resumen.resultado = ventas.total - premios_pagados.total -
     pagos_rasca.total" cuadra exacto (diferencia 0,00€) en 26 de 31
     casos (los 5 restantes tienen la propia página RESUMEN mal leída
     por OCR, marcados con "ocr_revision_pendiente": true en su JSON).
     Confirmado también que "resumen.pagos_total = premios_pagados.total
     + pagos_rasca.total" siempre — es decir, pagos_rasca NO se
     duplicaba al sumarlo (el comentario anterior de este docstring era
     erróneo), sencillamente faltaba. devoluciones_total y pagos_tarjeta
     NO entran en el RESULTADO DEL DÍA: no aparecen restados en ningún
     punto de la página RESUMEN ACTIVIDAD DIARIA del TPV — devoluciones
     afecta a la liquidación periódica de existencias, no al cuadre
     diario, y pagos_tarjeta es un desglose informativo de qué parte de
     los pagos fue con tarjeta, no un importe adicional. Ambos se siguen
     mostrando en el desglose informativo del informe, pero fuera del
     cálculo.

     Si no hay resumen.resultado, no hay un segundo valor con el que
     comparar, así que no se puede dar un veredicto de verificación —
     se informa el RESULTADO DEL DÍA calculado y se deja constancia de
     que no se pudo verificar.

Todo el cálculo es Python puro (regla del proyecto: "los cálculos del
cuadre ONCE se hacen SIEMPRE con Python, nunca con IA").

SEGUNDA OPINIÓN AUTOMÁTICA CON MISTRAL OCR (2026-07-23) — ver
verificar_segunda_opinion(): cuando el ticket del día no cuadra con la
fórmula, antes de marcarlo ocr_revision_pendiente se reprocesa
automáticamente el PDF con ocr_tickets_mistral.py (Mistral OCR) como
segunda opinión, y solo si Mistral TAMBIÉN falla se marca para revisión.
Esto se ejecuta únicamente desde el bloque __main__ (es decir, desde
run_diario.sh a las 08:00) — calcular_cuadre_diario() en sí NO llama a
Mistral ni reescribe el JSON del ticket, para que asistente.py (voz) e
informe_manana.py sigan siendo rápidos y sin llamadas de red ni coste
como efecto secundario de una simple consulta.

Uso:
    python3 cuadre_diario.py            # cuadre del último ticket disponible
    python3 cuadre_diario.py 040726     # cuadre de una fecha concreta (DDMMAA)
"""

import json
import sys
from datetime import datetime, timedelta

import ocr_tickets_mistral
from ocr_tickets import TICKETS_PATH

TOLERANCIA_DIFERENCIA = 0.01


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

    ocr_revision_pendiente = bool(ticket.get("ocr_revision_pendiente"))
    revision_manual_necesaria = bool(ticket.get("revision_manual_necesaria"))

    ventas_total = ticket.get("ventas", {}).get("total") or 0.0
    devoluciones_total = ticket.get("devoluciones", {}).get("total") or 0.0
    premios_total = ticket.get("premios_pagados", {}).get("total") or 0.0
    tarjeta = ticket.get("pagos_tarjeta") or 0.0
    rasca_total = ticket.get("pagos_rasca", {}).get("total") or 0.0
    resultado_ticket = ticket.get("resumen", {}).get("resultado")

    formula_calculada = round(ventas_total - premios_total - rasca_total, 2)

    if resultado_ticket is not None:
        resultado_dia = round(resultado_ticket, 2)
        origen = "resumen.resultado"
        diferencia = round(formula_calculada - resultado_ticket, 2)
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
        "ocr_revision_pendiente": ocr_revision_pendiente,
        "revision_manual_necesaria": revision_manual_necesaria,
    }


def _formula_y_diferencia(datos):
    """A partir de un dict con el esquema de un ticket JSON (venga de
    Claude o de Mistral), devuelve (formula_calculada, resultado,
    diferencia) — diferencia es None si no hay resumen.resultado con el
    que comparar. Reutilizado tanto por calcular_cuadre_diario() como
    por verificar_segunda_opinion(), para no calcular la fórmula de dos
    formas distintas."""
    ventas = datos.get("ventas", {}).get("total") or 0.0
    premios = datos.get("premios_pagados", {}).get("total") or 0.0
    rasca = datos.get("pagos_rasca", {}).get("total") or 0.0
    resultado = datos.get("resumen", {}).get("resultado")
    formula = round(ventas - premios - rasca, 2)
    diferencia = round(formula - resultado, 2) if resultado is not None else None
    return formula, resultado, diferencia


def verificar_segunda_opinion(fecha_dt=None):
    """Paso de mantenimiento diario — llamado SOLO desde el bloque
    __main__ (es decir, desde run_diario.sh a las 08:00), nunca desde
    calcular_cuadre_diario(), para no disparar una llamada de pago a
    Mistral ni reescribir el JSON del ticket como efecto secundario de
    una pregunta de voz (asistente.py) o de generar el PDF matutino
    (informe_manana.py) — ambos llaman a calcular_cuadre_diario()
    directamente y deben seguir siendo rápidos y sin coste de red.

    Si el ticket (el más reciente, o el de fecha_dt) no cuadra con la
    fórmula y todavía no se ha probado un segundo motor de OCR en él
    (sin "mistral_verificado" en su JSON), procesa el PDF original con
    Mistral OCR (ocr_tickets_mistral.py) ANTES de marcarlo
    ocr_revision_pendiente:

      - Si Mistral SÍ cuadra -> sustituye ventas, devoluciones,
        premios_pagados, pagos_tarjeta, pagos_rasca, resumen y
        libros_vendidos por los datos de Mistral, sin ninguna alerta
        (igual que se hizo a mano el 2026-07-23 con 050626 y 240526).
      - Si Mistral tampoco cuadra pero coincide con lo que ya leyó
        Claude -> ocr_revision_pendiente=True (los dos motores fallan
        igual, hace falta reprocesar/revisar).
      - Si Mistral tampoco cuadra Y además da un resultado DISTINTO al
        de Claude -> revision_manual_necesaria=True (ni los propios
        datos son consistentes entre motores — como pasó con 250526).

    En los tres casos se marca "mistral_verificado": true para no volver
    a gastar una llamada a Mistral en este ticket en ejecuciones futuras
    (control de coste: Mistral solo se llama cuando Claude falla, nunca
    de forma rutinaria en todos los tickets).

    No hace nada (sin llamar a Mistral) si: no hay ticket, no hay
    resumen.resultado con el que comparar (fórmula de respaldo), el
    ticket ya cuadra, el ticket ya se intentó antes
    ("mistral_verificado" ya presente), o no hay PDF original con el
    que reprocesar."""
    if fecha_dt is None:
        fecha_encontrada = ticket_mas_reciente()
        if fecha_encontrada is None:
            return
        fecha_dt = datetime.combine(fecha_encontrada, datetime.min.time())

    ruta_json = TICKETS_PATH / f"{fecha_dt.strftime('%d%m%y')}.json"
    ticket = _cargar_json(ruta_json)
    if ticket is None or ticket.get("mistral_verificado"):
        return

    _, resultado_claude, diferencia_claude = _formula_y_diferencia(ticket)
    if resultado_claude is None or abs(diferencia_claude) < TOLERANCIA_DIFERENCIA:
        return

    ruta_pdf = TICKETS_PATH / f"{fecha_dt.strftime('%d%m%y')}.pdf"
    if not ruta_pdf.exists():
        return

    hoy = datetime.now().strftime("%Y-%m-%d")
    print(
        f"  cuadre_diario.py: {ruta_json.name} no cuadra con la fórmula "
        f"(diferencia {diferencia_claude:.2f}€) — probando segunda opinión con Mistral OCR..."
    )

    try:
        api_key = ocr_tickets_mistral._leer_clave_mistral()
        datos_mistral = ocr_tickets_mistral.procesar_ticket_mistral(ruta_pdf, api_key)
    except Exception as e:
        print(
            f"  ⚠️  Segunda opinión con Mistral falló ({e}) — se deja el ticket sin marcar "
            f"para reintentarlo en la próxima ejecución."
        )
        return

    _, resultado_mistral, diferencia_mistral = _formula_y_diferencia(datos_mistral)

    ticket.pop("ocr_revision_pendiente", None)
    ticket.pop("revision_manual_necesaria", None)
    ticket["mistral_verificado"] = True

    if diferencia_mistral is not None and abs(diferencia_mistral) < TOLERANCIA_DIFERENCIA:
        for clave in ("ventas", "devoluciones", "premios_pagados", "pagos_tarjeta",
                      "pagos_rasca", "resumen", "libros_vendidos"):
            if clave in datos_mistral:
                ticket[clave] = datos_mistral[clave]
        ticket["ocr_correccion"] = (
            f"Corregido automáticamente el {hoy} con Mistral OCR (segunda opinión "
            f"automática, cuadre_diario.py) tras detectar que Claude no cuadraba con la "
            f"fórmula (diferencia {diferencia_claude:.2f}€)."
        )
        print(f"  ✅ Mistral sí cuadra — JSON de {ruta_json.name} corregido automáticamente.")
    else:
        coinciden = (
            resultado_mistral is not None
            and abs(round(resultado_mistral - resultado_claude, 2)) < TOLERANCIA_DIFERENCIA
        )
        if coinciden:
            ticket["ocr_revision_pendiente"] = True
            ticket["ocr_correccion"] = (
                f"Segunda opinión automática el {hoy}: Mistral OCR tampoco cuadra con la "
                f"fórmula (diferencia {diferencia_mistral:.2f}€) y coincide con lo que ya "
                f"leyó Claude (resultado {resultado_claude:.2f}€) — necesita "
                f"reprocesarse/revisarse."
            )
            print(
                f"  🔶 Mistral tampoco cuadra (coincide con Claude) — "
                f"{ruta_json.name} marcado ocr_revision_pendiente."
            )
        else:
            ticket["revision_manual_necesaria"] = True
            texto_mistral = f"{resultado_mistral:.2f}€" if resultado_mistral is not None else "sin resumen.resultado"
            ticket["motivo_revision_manual"] = (
                f"Segunda opinión automática el {hoy}: ni Claude ({resultado_claude:.2f}€) "
                f"ni Mistral ({texto_mistral}) cuadran con la fórmula, y además dan "
                f"resultados distintos entre sí — hace falta revisión manual del ticket "
                f"físico."
            )
            print(
                f"  🔴 Mistral tampoco cuadra y da un resultado distinto al de Claude — "
                f"{ruta_json.name} marcado revision_manual_necesaria."
            )

    with open(ruta_json, "w") as f:
        json.dump(ticket, f, ensure_ascii=False, indent=2)


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

    if resultado.get("revision_manual_necesaria"):
        lineas.append(
            "🔴 Este ticket está marcado como revision_manual_necesaria — ni siquiera los "
            "propios datos del ticket son internamente consistentes (la fórmula no cuadra "
            "contra resumen.resultado con ningún motor de OCR probado). Hace falta mirar el "
            "ticket físico, no reprocesarlo automáticamente. No se verifica la fórmula contra "
            "un dato que ya se sabe corrupto."
        )
        return "\n".join(lineas)

    if resultado.get("ocr_revision_pendiente"):
        lineas.append(
            "🔶 Este ticket está marcado como ocr_revision_pendiente — la página RESUMEN "
            "salió mal transcrita (OCR) y este RESULTADO DEL DÍA no es fiable hasta "
            "reprocesarlo. No se verifica la fórmula contra un dato que ya se sabe corrupto."
        )
        return "\n".join(lineas)

    dif = resultado["diferencia"]
    if dif is None:
        lineas.append(
            f"ℹ️  Sin resumen.resultado en el ticket — RESULTADO DEL DÍA calculado con la "
            f"fórmula de respaldo (ventas {resultado['ventas_total']:.2f} - premios "
            f"{resultado['premios_total']:.2f} - rasca {resultado['rasca_total']:.2f} "
            f"= {resultado['formula_calculada']:.2f}€), no se ha podido verificar contra "
            f"un segundo valor."
        )
    elif abs(dif) < TOLERANCIA_DIFERENCIA:
        lineas.append("✅ Cálculo del ticket verificado: coincide con resumen.resultado")
    else:
        signo = "+" if dif > 0 else ""
        lineas.append(
            f"⚠️  Verificación del ticket: la fórmula no coincide con resumen.resultado "
            f"({signo}{dif:.2f}€ de diferencia)"
        )

    lineas.append(
        f"ℹ️  Informativo (no entra en el RESULTADO DEL DÍA): devoluciones "
        f"{resultado['devoluciones_total']:.2f}€, pagos con tarjeta {resultado['tarjeta']:.2f}€"
    )

    return "\n".join(lineas)


if __name__ == "__main__":
    fecha_arg = None
    if len(sys.argv) > 1:
        fecha_arg = datetime.strptime(sys.argv[1], "%d%m%y")
    verificar_segunda_opinion(fecha_arg)
    resultado = calcular_cuadre_diario(fecha_arg)
    print(formatear_resultado(resultado))
    print()
    print(json.dumps(resultado, indent=2, ensure_ascii=False))
