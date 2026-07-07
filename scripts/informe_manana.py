#!/usr/bin/env python3
"""
Informe matutino - Juan Antonio Panadero Jiménez
Agencia ONCE 576 Getafe

Combina los JSON de hoy con detalle completo (generados por
parsear_html_portal.py a partir del HTML crudo del portal: paquetes con
productos/cupones/series, almacén con libros, premios con desglose
Tradicional/TPV/BONO) con la agenda del día (agenda.py) y genera un PDF
de gestión empresarial: solo datos, sin relleno ni redacción libre. Se
construye enteramente en Python (sin llamadas a IA, con reportlab para
la maquetación) para garantizar que las cifras que aparecen son
exactamente las descargadas.

Guarda el resultado en IBMPS1/informes/informe_YYYYMMDD.pdf.
"""

import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import agenda
import cuadre_diario
import portal_once as po

INFORMES_PATH = Path(__file__).resolve().parent.parent / "informes"

COLOR_CABECERA = colors.HexColor("#1a3c34")
COLOR_FILA_ALTERNA = colors.HexColor("#f2f5f4")
COLOR_BORDE = colors.HexColor("#bdc3c7")


def _cargar_json(ruta):
    if not ruta.exists():
        return None
    with open(ruta) as f:
        return json.load(f)


def cargar_datos_once(hoy):
    """Lee los JSON de hoy generados por parsear_html_portal.py /
    portal_once.py. Un valor None significa que esa sección no se ha
    descargado todavía hoy."""
    return {
        "paquetes_previsto": _cargar_json(po.PAQUETES_PATH / f"control_retirada_previsto_{hoy}.json"),
        "paquetes_a_retirar": _cargar_json(po.PAQUETES_PATH / f"control_retirada_a_retirar_{hoy}.json"),
        "paquetes_retirado": _cargar_json(po.PAQUETES_PATH / f"control_retirada_retirado_{hoy}.json"),
        "stock_rascas": _cargar_json(po.STOCK_PATH / f"control_almacen_instantanea_{hoy}.json"),
        "premios_activa": _cargar_json(po.PREMIOS_PATH / f"premios_activa_{hoy}.json"),
        "premios_instantanea": _cargar_json(po.PREMIOS_PATH / f"premios_instantanea_{hoy}.json"),
        "premios_pasiva": _cargar_json(po.PREMIOS_PATH / f"premios_pasiva_{hoy}.json"),
        "comisiones": _cargar_json(po.COMISIONES_PATH / f"comisiones_{hoy}.json"),
        "estadisticas_venta": _cargar_json(po.ESTADISTICAS_PATH / f"estadisticas_venta_{hoy}.json"),
        "liquidacion_diaria": _cargar_json(po.LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.json"),
        "incidencias": _cargar_json(po.CONSULTAS_PATH / f"incidencias_{hoy}.json"),
        "solicitudes": _cargar_json(po.CONSULTAS_PATH / f"solicitudes_{hoy}.json"),
    }


def cargar_datos_agenda():
    try:
        return agenda.obtener_agenda()
    except RuntimeError as e:
        return {"error": str(e)}


def _numero_paquete(descripcion):
    if not descripcion:
        return "?"
    return descripcion.replace("PAQUETE NÚMERO:", "").strip()


def _fmt(valor):
    return "-" if valor is None else valor


def _formatear_detalle_producto(producto):
    """Cupones+series o libros de un producto de paquete, como texto con
    saltos de línea (<br/>) para una celda de tabla de reportlab. Las
    claves de "detalle" varían según el tipo de producto (ver
    parsers_portal.parsear_control_retirada)."""
    detalle = producto.get("detalle") or []
    if not detalle:
        return "-"
    claves = set(detalle[0].keys())
    if "libro" in claves:
        return "Libros: " + ", ".join(d.get("libro", "?") for d in detalle)
    clave_cupon = "cupón" if "cupón" in claves else ("cupon" if "cupon" in claves else None)
    if clave_cupon:
        return "<br/>".join(
            f"Cupón {d.get(clave_cupon)} · series {d.get('series')} ({d.get('cantidad')} u.)"
            for d in detalle
        )
    return "<br/>".join(", ".join(f"{k}: {v}" for k, v in d.items()) for d in detalle)


def _formatear_libros(detalle):
    if not detalle:
        return "-"
    return "<br/>".join(f"{d.get('libro')} — {d.get('estado')} ({d.get('fecha')})" for d in detalle)


def _premios_pasiva_agrupados_por_juego(premios, ayer):
    """Agrupa por juego (no por categoría) sumando importe/cantidad
    totales y el desglose Tradicional/TPV de cada fila de ese juego."""
    grupos = {}
    for fila in premios:
        if fila.get("fecha") != ayer:
            continue
        juego = fila.get("juego") or "?"
        g = grupos.setdefault(juego, {
            "importe_total": 0, "cantidad_total": 0,
            "trad_cant": 0, "trad_imp": 0, "tpv_cant": 0, "tpv_imp": 0,
        })
        g["importe_total"] += fila.get("importe_total") or 0
        g["cantidad_total"] += fila.get("cantidad_total") or 0
        for d in fila.get("detalle") or []:
            tipo = (d.get("tipo") or "").strip().lower()
            if tipo == "tradicional":
                g["trad_cant"] += d.get("cantidad") or 0
                g["trad_imp"] += d.get("importe") or 0
            elif tipo == "tpv":
                g["tpv_cant"] += d.get("cantidad") or 0
                g["tpv_imp"] += d.get("importe") or 0
    return grupos


# ── Estilos y helpers de maquetación ─────────────────────────────────

def _crear_estilos():
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "titulo", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=18, textColor=colors.white, leading=22,
        ),
        "fecha_cabecera": ParagraphStyle(
            "fecha_cabecera", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=12, textColor=colors.white, alignment=TA_RIGHT,
        ),
        "subtitulo": ParagraphStyle(
            "subtitulo", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=9, textColor=colors.HexColor("#555555"),
        ),
        "seccion": ParagraphStyle(
            "seccion", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=12, textColor=COLOR_CABECERA, spaceBefore=12, spaceAfter=6,
        ),
        "subseccion": ParagraphStyle(
            "subseccion", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9.5, textColor=colors.HexColor("#333333"), spaceBefore=6, spaceAfter=3,
        ),
        "normal": ParagraphStyle("normal", parent=base["Normal"], fontName="Helvetica", fontSize=9, leading=12),
        "celda": ParagraphStyle("celda", parent=base["Normal"], fontName="Helvetica", fontSize=8, leading=10),
        "bullet": ParagraphStyle(
            "bullet", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, leading=13, leftIndent=10,
        ),
    }


def _estilo_tabla():
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_CABECERA),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, COLOR_BORDE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_FILA_ALTERNA]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ])


def _cabecera(hoy_date, estilos):
    tabla = Table(
        [[Paragraph("AGENCIA ONCE 576", estilos["titulo"]),
          Paragraph(hoy_date.strftime("%d/%m/%Y"), estilos["fecha_cabecera"])]],
        colWidths=[11 * cm, 5 * cm],
    )
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_CABECERA),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    subtitulo = Paragraph("Informe diario de gestión — Getafe", estilos["subtitulo"])
    return [tabla, Spacer(1, 0.15 * cm), subtitulo, Spacer(1, 0.4 * cm)]


# ── Bloques de contenido (tablas por sección) ────────────────────────

def _tablas_paquetes(datos, estilos):
    flowables = [Paragraph("PAQUETES — DETALLE COMPLETO", estilos["seccion"])]
    etiquetas = (
        ("Previsto", "paquetes_previsto"),
        ("A retirar", "paquetes_a_retirar"),
        ("Retirado", "paquetes_retirado"),
    )
    hay_datos = False
    for etiqueta, clave in etiquetas:
        bloque = datos.get(clave)
        paquetes = (bloque or {}).get("paquetes") or []
        if not paquetes:
            flowables.append(Paragraph(f"{etiqueta}: sin datos descargados hoy", estilos["normal"]))
            continue
        for paquete in paquetes:
            hay_datos = True
            numero = _numero_paquete(paquete.get("descripcion"))
            importe = _fmt(paquete.get("importe"))
            soportes = _fmt(paquete.get("soportes"))
            flowables.append(Paragraph(
                f"{etiqueta} — Paquete {numero} · {importe} € · {soportes} soportes",
                estilos["subseccion"],
            ))
            filas = [["Producto", "Cantidad", "Detalle"]]
            for producto in paquete.get("productos") or []:
                filas.append([
                    Paragraph(producto.get("producto") or "?", estilos["celda"]),
                    str(_fmt(producto.get("cantidad"))),
                    Paragraph(_formatear_detalle_producto(producto), estilos["celda"]),
                ])
            tabla = Table(filas, colWidths=[4.3 * cm, 1.8 * cm, 9 * cm], repeatRows=1)
            tabla.setStyle(_estilo_tabla())
            flowables.append(tabla)
            flowables.append(Spacer(1, 0.3 * cm))
    if not hay_datos:
        flowables.append(Paragraph("(sin paquetes registrados hoy)", estilos["normal"]))
    return flowables


def _tabla_almacen(datos, estilos):
    flowables = [Paragraph("CONTROL DE ALMACÉN RASCAS — DETALLE DE LIBROS", estilos["seccion"])]
    bloque = datos.get("stock_rascas")
    productos = (bloque or {}).get("productos") or []
    if not productos:
        flowables.append(Paragraph("Sin datos descargados hoy.", estilos["normal"]))
        return flowables

    filas = [["Producto", "Retirado", "Confirmado", "Activado", "Vendido", "Detalle de libros"]]
    for producto in productos:
        filas.append([
            Paragraph((producto.get("producto") or "?").strip(), estilos["celda"]),
            str(_fmt(producto.get("retirado"))),
            str(_fmt(producto.get("confirmado"))),
            str(_fmt(producto.get("activado"))),
            str(_fmt(producto.get("vendido"))),
            Paragraph(_formatear_libros(producto.get("detalle") or []), estilos["celda"]),
        ])
    tabla = Table(filas, colWidths=[3.3 * cm, 1.7 * cm, 1.9 * cm, 1.7 * cm, 1.7 * cm, 6.6 * cm], repeatRows=1)
    tabla.setStyle(_estilo_tabla())
    flowables.append(tabla)

    totales = (bloque or {}).get("totales")
    if totales:
        flowables.append(Spacer(1, 0.2 * cm))
        flowables.append(Paragraph(
            f"<b>TOTAL</b> — Retirado: {_fmt(totales.get('retirado'))} · "
            f"Confirmado: {_fmt(totales.get('confirmado'))} · "
            f"Activado: {_fmt(totales.get('activado'))} · "
            f"Vendido: {_fmt(totales.get('vendido'))}",
            estilos["normal"],
        ))
    return flowables


def _tabla_premios_pasiva(datos, ayer, ayer_legible, estilos):
    flowables = [Paragraph(f"PREMIOS PASIVA — AYER ({ayer_legible})", estilos["seccion"])]
    bloque = datos.get("premios_pasiva")
    if bloque is None:
        flowables.append(Paragraph("Sin datos descargados hoy.", estilos["normal"]))
        return flowables
    grupos = _premios_pasiva_agrupados_por_juego(bloque.get("premios") or [], ayer)
    if not grupos:
        flowables.append(Paragraph("Sin premios repartidos ayer en los datos descargados.", estilos["normal"]))
        return flowables

    filas = [["Juego", "Importe total", "Tradicional", "TPV"]]
    for juego, g in grupos.items():
        filas.append([
            juego,
            f"{g['importe_total']} €",
            f"{g['trad_cant']} u / {g['trad_imp']} €",
            f"{g['tpv_cant']} u / {g['tpv_imp']} €",
        ])
    tabla = Table(filas, colWidths=[5 * cm, 3 * cm, 3.8 * cm, 3.8 * cm], repeatRows=1)
    tabla.setStyle(_estilo_tabla())
    flowables.append(tabla)
    return flowables


def _tabla_premios_plana(datos, clave, titulo, ayer, ayer_legible, estilos):
    flowables = [Paragraph(f"{titulo} — AYER ({ayer_legible})", estilos["seccion"])]
    bloque = datos.get(clave)
    if bloque is None:
        flowables.append(Paragraph("Sin datos descargados hoy.", estilos["normal"]))
        return flowables
    filas_datos = [f for f in (bloque.get("premios") or []) if f.get("fecha") == ayer]
    if not filas_datos:
        flowables.append(Paragraph("Sin premios repartidos ayer en los datos descargados.", estilos["normal"]))
        return flowables

    filas = [["Juego", "Categoría", "Cantidad", "Importe"]]
    for f in filas_datos:
        filas.append([
            f.get("juego") or "?",
            f.get("categoria") or "?",
            str(_fmt(f.get("cantidad_total"))),
            f"{_fmt(f.get('importe_total'))} €",
        ])
    tabla = Table(filas, colWidths=[5.5 * cm, 4.5 * cm, 2.3 * cm, 2.3 * cm], repeatRows=1)
    tabla.setStyle(_estilo_tabla())
    flowables.append(tabla)
    return flowables


def _importe_texto_a_float(texto):
    """Convierte un importe en formato español ("403,10€", "0,00€") a
    float, igual que portal_once._importe_a_float."""
    if not texto:
        return 0.0
    limpio = str(texto).replace("€", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def _seccion_liquidacion(datos, estilos):
    flowables = [Paragraph("LIQUIDACIÓN DIARIA", estilos["seccion"])]
    bloque = datos.get("liquidacion_diaria")
    if bloque is None:
        flowables.append(Paragraph("Sin datos descargados hoy.", estilos["normal"]))
        return flowables

    if not bloque.get("ejecutado", True):
        mensaje = bloque.get("mensaje") or "No se ejecuta liquidación (día no laborable)"
        flowables.append(Paragraph(mensaje, estilos["normal"]))
        return flowables

    saldo = _importe_texto_a_float(bloque.get("saldo_acreedor"))
    importe = _importe_texto_a_float(bloque.get("importe"))

    if saldo > 0:
        texto = f"Saldo acreedor: {bloque.get('saldo_acreedor')}"
    elif importe > 0:
        texto = f"Importe a liquidar: {bloque.get('importe')}"
    else:
        texto = "Sin liquidación hoy"
    flowables.append(Paragraph(texto, estilos["normal"]))
    return flowables


def _lista_alertas(datos, datos_agenda, hoy_date, estilos):
    flowables = [Paragraph("ALERTAS", estilos["seccion"])]
    alertas = []

    for r in datos_agenda.get("recordatorios_hoy") or []:
        vencimiento = datetime.fromisoformat(r["vencimiento"]).date()
        estado = "VENCIDO" if vencimiento < hoy_date else "VENCE HOY"
        alertas.append(f"{estado}: {r['titulo']} ({vencimiento.strftime('%d/%m/%Y')})")

    secciones_vacias = [
        nombre
        for nombre, clave in (
            ("comisiones", "comisiones"),
            ("estadísticas de venta", "estadisticas_venta"),
            ("liquidación diaria", "liquidacion_diaria"),
            ("incidencias", "incidencias"),
            ("solicitudes", "solicitudes"),
        )
        if not datos.get(clave)
    ]
    if secciones_vacias:
        alertas.append("Sin datos descargados hoy: " + ", ".join(secciones_vacias))

    if not alertas:
        flowables.append(Paragraph("Sin alertas.", estilos["normal"]))
    else:
        flowables.extend(Paragraph(f"• {a}", estilos["bullet"]) for a in alertas)
    return flowables


def _lista_agenda(datos_agenda, estilos):
    flowables = [Paragraph("AGENDA Y RECORDATORIOS", estilos["seccion"])]

    if "error" in datos_agenda:
        flowables.append(Paragraph(f"Agenda no disponible: {datos_agenda['error']}", estilos["normal"]))
        return flowables

    flowables.append(Paragraph("<b>Hoy:</b>", estilos["normal"]))
    eventos_hoy = datos_agenda.get("eventos_hoy") or []
    if not eventos_hoy:
        flowables.append(Paragraph("Sin eventos", estilos["bullet"]))
    else:
        for e in eventos_hoy:
            hora = datetime.fromisoformat(e["inicio"]).strftime("%H:%M")
            flowables.append(Paragraph(f"• {hora}  {e['titulo']} ({e['calendario']})", estilos["bullet"]))

    flowables.append(Paragraph("<b>Próximos 3 días:</b>", estilos["normal"]))
    eventos_prox = datos_agenda.get("eventos_proximos_3_dias") or []
    if not eventos_prox:
        flowables.append(Paragraph("Sin eventos", estilos["bullet"]))
    else:
        for e in eventos_prox:
            fecha = datetime.fromisoformat(e["inicio"]).strftime("%d/%m %H:%M")
            flowables.append(Paragraph(f"• {fecha}  {e['titulo']} ({e['calendario']})", estilos["bullet"]))

    flowables.append(Paragraph("<b>Recordatorios pendientes:</b>", estilos["normal"]))
    recordatorios = (datos_agenda.get("recordatorios_hoy") or []) + (datos_agenda.get("recordatorios_proximos_3_dias") or [])
    if not recordatorios:
        flowables.append(Paragraph("Ninguno", estilos["bullet"]))
    else:
        for r in recordatorios:
            fecha = datetime.fromisoformat(r["vencimiento"]).strftime("%d/%m/%Y")
            flowables.append(Paragraph(f"• {r['titulo']} (vence {fecha})", estilos["bullet"]))

    return flowables


def _seccion_cuadre_diario(hoy_date, estilos):
    """Cuadre del día anterior: si el ticket trae resumen.resultado se usa
    directamente como EFECTIVO (ya incluye premios de cupones y de rasca
    dentro de "PAGOS"); si no, se calcula con la fórmula de respaldo
    (ventas - devoluciones - premios - tarjeta, sin sumar pagos_rasca
    aparte, para no contarlo dos veces). Ver cuadre_diario.py."""
    flowables = [Paragraph("CUADRE DIARIO", estilos["seccion"])]
    ayer_dt = datetime.combine(hoy_date, datetime.min.time()) - timedelta(days=1)
    resultado = cuadre_diario.calcular_cuadre_diario(ayer_dt)

    if not resultado.get("ejecutado", True):
        flowables.append(Paragraph(resultado["mensaje"], estilos["normal"]))
        return flowables

    flowables.append(Paragraph(
        f"EFECTIVO DEL DÍA: {resultado['efectivo']:.2f}€ (origen: {resultado['origen']})",
        estilos["bullet"],
    ))

    dif = resultado["diferencia"]
    if dif is None:
        flowables.append(Paragraph(
            "ℹ️ Sin resumen.resultado en el ticket — EFECTIVO calculado con la fórmula "
            "de respaldo, no verificado contra un segundo valor.",
            estilos["normal"],
        ))
    elif abs(dif) < 0.01:
        flowables.append(Paragraph("✅ CUADRE CORRECTO: 0,00€ diferencia", estilos["normal"]))
    else:
        signo = "+" if dif > 0 else ""
        flowables.append(Paragraph(
            f"⚠️ DESCUADRE: {signo}{dif:.2f}€ diferencia", estilos["normal"],
        ))
    return flowables


def generar_informe_pdf(datos, datos_agenda, hoy_date, destino):
    ayer = (hoy_date - timedelta(days=1)).strftime("%d-%m")
    ayer_legible = (hoy_date - timedelta(days=1)).strftime("%d/%m/%Y")
    estilos = _crear_estilos()

    doc = SimpleDocTemplate(
        str(destino), pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        title=f"Informe diario ONCE 576 - {hoy_date.strftime('%d/%m/%Y')}",
    )

    story = []
    story.extend(_cabecera(hoy_date, estilos))
    story.extend(_tablas_paquetes(datos, estilos))
    story.extend(_tabla_almacen(datos, estilos))
    story.extend(_tabla_premios_pasiva(datos, ayer, ayer_legible, estilos))
    story.extend(_tabla_premios_plana(datos, "premios_activa", "PREMIOS ACTIVA", ayer, ayer_legible, estilos))
    story.extend(_tabla_premios_plana(datos, "premios_instantanea", "PREMIOS INSTANTÁNEA", ayer, ayer_legible, estilos))
    story.extend(_seccion_liquidacion(datos, estilos))
    story.extend(_lista_alertas(datos, datos_agenda, hoy_date, estilos))
    story.extend(_lista_agenda(datos_agenda, estilos))
    story.extend(_seccion_cuadre_diario(hoy_date, estilos))

    doc.build(story)


def main():
    hoy_date = datetime.now().date()
    hoy = hoy_date.strftime("%Y%m%d")

    datos_once = cargar_datos_once(hoy)
    datos_agenda = cargar_datos_agenda()

    INFORMES_PATH.mkdir(parents=True, exist_ok=True)
    destino = INFORMES_PATH / f"informe_{hoy}.pdf"
    generar_informe_pdf(datos_once, datos_agenda, hoy_date, destino)

    print(f"✅ Informe PDF guardado en {destino}", file=sys.stderr)


if __name__ == "__main__":
    main()
