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
import alertas_caducidad
import cuadre_diario
import portal_once as po
from ocr_comunicaciones import COMUNICACIONES_PATH
from ocr_devolucion_libros import DEVOLUCION_LIBROS_PATH

INFORMES_PATH = Path(__file__).resolve().parent.parent / "informes"
NOVEDADES_TECH_PATH = Path(__file__).resolve().parent.parent / "datos" / "novedades_tech"

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
        # Variantes por nivel de caducidad_libros: el font Helvetica no
        # tiene glifos de emoji (🔴🟡🟢 salen como cuadro negro relleno en
        # el PDF, igual que ✅/⚠️/📬 en el resto del informe) — la señal
        # visual real va en el color del texto + la etiqueta [NIVEL], no
        # en el emoji.
        "bullet_urgente": ParagraphStyle(
            "bullet_urgente", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9, leading=13, leftIndent=10, textColor=colors.HexColor("#C0392B"),
        ),
        "bullet_aviso": ParagraphStyle(
            "bullet_aviso", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9, leading=13, leftIndent=10, textColor=colors.HexColor("#B7770B"),
        ),
        "bullet_ok": ParagraphStyle(
            "bullet_ok", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, leading=13, leftIndent=10, textColor=colors.HexColor("#1E8449"),
        ),
        # Premio individual > UMBRAL_PREMIO_IMPORTANTE (ver
        # _seccion_premios_repartidos): igual que con la caducidad de
        # libros, el 🎉 no se ve en el PDF (Helvetica no tiene glifo),
        # así que la señal real es negrita + color dorado + el texto
        # "¡Premio importante!" explícito, no el emoji.
        "premio_importante": ParagraphStyle(
            "premio_importante", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9.5, leading=14, leftIndent=10, textColor=colors.HexColor("#B8860B"),
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
    # Si parsear_html_portal.py no llegó a ejecutarse hoy, "stock_rascas"
    # todavía es la tabla cruda de portal_once.py (una lista), no el dict
    # con "productos"/"detalle" — se trata igual que "sin datos".
    productos = bloque.get("productos") or [] if isinstance(bloque, dict) else []
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


def _seccion_caducidad_libros(hoy_date, estilos):
    """Aviso preventivo de caducidad: ONCE da por vendido automáticamente
    (cobrándolo igual) un libro Confirmado/Activado que lleva 90 días sin
    venderse de verdad. Todo el cálculo de días viene ya hecho por
    alertas_caducidad.calcular_alertas_caducidad() (Python puro, ver ese
    módulo) — aquí solo se maqueta, sin recalcular nada. Se muestran
    TODOS los libros pendientes (no solo los urgentes), ordenados de más
    a menos antiguo, para poder planificar con margen qué llevar a
    cambiar antes."""
    flowables = [Paragraph("⏰ CADUCIDAD DE LIBROS", estilos["seccion"])]
    fecha_dt = datetime.combine(hoy_date, datetime.min.time())
    resultado = alertas_caducidad.calcular_alertas_caducidad(fecha_dt)

    if not resultado.get("ejecutado", True):
        flowables.append(Paragraph(resultado["mensaje"], estilos["normal"]))
        return flowables

    libros = resultado["libros"]
    if not libros:
        flowables.append(Paragraph(
            "Sin libros Confirmado/Activado con fecha registrada.", estilos["normal"],
        ))
        return flowables

    t = resultado["totales"]
    flowables.append(Paragraph(
        f"{len(libros)} libro(s) pendiente(s) — {t['urgente']} urgente(s), "
        f"{t['aviso']} en aviso, {t['ok']} ok",
        estilos["normal"],
    ))
    flowables.append(Spacer(1, 0.15 * cm))
    estilo_por_nivel = {
        "URGENTE": estilos["bullet_urgente"],
        "AVISO": estilos["bullet_aviso"],
        "OK": estilos["bullet_ok"],
    }
    for l in libros:
        flowables.append(Paragraph(
            f"[{l['nivel']}] {l['producto']} — Libro {l['libro']} — confirmado hace "
            f"{l['dias_transcurridos']} días ({l['dias_para_caducar']} días para caducar)",
            estilo_por_nivel[l["nivel"]],
        ))
    return flowables


# Mismo umbral que UMBRAL_PREMIO_IMPORTANTE en asistente.py
# (_contexto_premios) — un premio individual (una línea juego+categoría)
# por encima de esto se destaca, aquí y por voz, no dos criterios
# distintos para el mismo concepto.
UMBRAL_PREMIO_IMPORTANTE = 50.0


def _fecha_corta_a_date(fecha_corta, referencia):
    """Convierte una fecha corta "DD-MM" (como vienen las filas de
    premios_{tipo}_{hoy}.json, sin año) a un date real, usando el año de
    `referencia` salvo que el resultado caiga en el futuro respecto a
    ella — en ese caso la fila es de diciembre del año anterior (nombre
    de archivo ya en enero). Devuelve None si "DD-MM" no es válido.
    Misma lógica que asistente.py:_fecha_corta_a_date — si cambias una,
    cambia la otra."""
    try:
        dia_str, mes_str = fecha_corta.split("-")
        fecha = referencia.replace(month=int(mes_str), day=int(dia_str))
    except (ValueError, AttributeError):
        return None
    if fecha > referencia:
        fecha = fecha.replace(year=fecha.year - 1)
    return fecha


def _premios_del_dia(datos, hoy_date):
    """Todas las líneas de premio (pasiva+activa+instantánea) del día
    MÁS RECIENTE que de verdad aparezca en los 3 JSON, ordenadas de
    mayor a menor importe.

    FIX de un bug real (el mismo que ya se corrigió en
    asistente.py:_contexto_premios): antes se filtraba por "ayer"
    calculado como hoy_date - 1 día, asumiendo que el portal siempre
    tiene publicado el cierre de ayer. Si ese hueco es de más de un día
    (fin de semana, festivo, retraso de publicación...), "ayer" no
    existe en los datos y la sección salía vacía aunque SÍ hubiera datos
    recientes, solo que de hace más de un día. Aquí se calcula la fecha
    MÁXIMA real presente en las filas descargadas y se filtra por esa.

    Devuelve (filas, fecha_objetivo) — fecha_objetivo es None si
    ninguno de los 3 archivos trae una fecha reconocible."""
    filas_por_tipo = {}
    for tipo, clave in (
        ("pasiva", "premios_pasiva"),
        ("activa", "premios_activa"),
        ("instantanea", "premios_instantanea"),
    ):
        bloque = datos.get(clave)
        if not isinstance(bloque, dict):
            continue
        filas_por_tipo[tipo] = [
            f for f in bloque.get("premios") or [] if isinstance(f, dict) and f.get("fecha")
        ]

    fechas_reales = [
        _fecha_corta_a_date(f["fecha"], hoy_date)
        for filas_tipo in filas_por_tipo.values() for f in filas_tipo
    ]
    fechas_reales = [d for d in fechas_reales if d is not None]
    if not fechas_reales:
        return [], None

    # La fecha objetivo NUNCA se calcula como "hoy_date - N días" — es
    # siempre la fecha máxima que de verdad aparece en los datos.
    fecha_objetivo = max(fechas_reales)
    fecha_objetivo_corta = fecha_objetivo.strftime("%d-%m")

    filas = []
    for tipo, filas_tipo in filas_por_tipo.items():
        for f in filas_tipo:
            if f.get("fecha") != fecha_objetivo_corta:
                continue
            filas.append({
                "tipo": tipo,
                "juego": f.get("juego") or "?",
                "categoria": f.get("categoria") or "?",
                "cantidad": f.get("cantidad_total") or 0,
                "importe": f.get("importe_total") or 0,
            })
    filas.sort(key=lambda f: f["importe"], reverse=True)
    return filas, fecha_objetivo


def _seccion_premios_repartidos(datos, hoy_date, estilos):
    """Vista unificada de TODOS los premios del día más reciente
    disponible (pasiva+activa+instantánea) con un total general, en vez
    de las 3 tablas separadas que había antes (desglose Tradicional/TPV
    de pasiva y el resto de detalle). Los premios individuales por
    encima de UMBRAL_PREMIO_IMPORTANTE se sacan de la tabla y se
    destacan aparte (negrita + color, ver estilo "premio_importante") —
    a propósito no aparecen como "una línea más" en la tabla."""
    flowables = [Paragraph("🏆 PREMIOS REPARTIDOS", estilos["seccion"])]
    filas, fecha_objetivo = _premios_del_dia(datos, hoy_date)

    if fecha_objetivo is None:
        flowables.append(Paragraph(
            "Sin archivos de premios con fecha reconocible.", estilos["normal"],
        ))
        return flowables

    fecha_legible = fecha_objetivo.strftime("%d/%m/%Y")
    if not filas:
        flowables.append(Paragraph(
            f"Sin premios registrados para el día más reciente disponible ({fecha_legible}).",
            estilos["normal"],
        ))
        return flowables

    total = round(sum(f["importe"] for f in filas), 2)
    flowables.append(Paragraph(
        f"DÍA MÁS RECIENTE DISPONIBLE ({fecha_legible}) — {len(filas)} línea(s) de premio, "
        f"TOTAL {total:.2f}€",
        estilos["normal"],
    ))
    flowables.append(Spacer(1, 0.15 * cm))

    importantes = [f for f in filas if f["importe"] > UMBRAL_PREMIO_IMPORTANTE]
    resto = [f for f in filas if f["importe"] <= UMBRAL_PREMIO_IMPORTANTE]

    for f in importantes:
        flowables.append(Paragraph(
            f"🎉 ¡Premio importante! {f['juego']} — {f['categoria']}: "
            f"{f['cantidad']} u., {f['importe']:.2f}€",
            estilos["premio_importante"],
        ))
    if importantes:
        flowables.append(Spacer(1, 0.2 * cm))

    if resto:
        filas_tabla = [["Tipo", "Producto", "Categoría", "Cantidad", "Importe"]]
        for f in resto:
            filas_tabla.append([
                f["tipo"], f["juego"], f["categoria"], str(f["cantidad"]), f"{f['importe']:.2f}€",
            ])
        tabla = Table(filas_tabla, colWidths=[2.3 * cm, 5.5 * cm, 4 * cm, 2.5 * cm, 2.7 * cm], repeatRows=1)
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


# Caso especial gvTWYP: nombres de columna reales -> etiqueta legible.
# Esa tabla no sigue el patrón CONCEPTO/IMPORTE del resto (ver
# _formatear_fila_liquidacion) — su cabecera real es "TOPii" | "VENTA
# PROD ONCE" | "RETIRADA", con "TOPii" como nombre del juego, no una
# etiqueta genérica.
_ETIQUETAS_COLUMNA_TWYP = {
    "VENTA PROD ONCE": "Venta prod. ONCE",
    "RETIRADA": "Retirada",
}


def _formatear_fila_liquidacion(fila):
    """Convierte una fila genérica de detalle_completo (columnas variables
    según la tabla: PRODUCTOS CUPÓN/FECHA/IMPORTE, CONCEPTO/IMPORTE...) en
    una línea legible, sin asumir nombres de columna fijos — el propio
    portal ya pone las etiquetas legibles como clave o como valor según
    la tabla, así que no hace falta traducir nada por producto.

    Caso especial (gvTWYP/TOPii): la fila no tiene clave "IMPORTE", pero
    sí una clave cuyo VALOR es literalmente "IMPORTE" — esa clave es en
    realidad el nombre del juego (p.ej. {"TOPii": "IMPORTE",
    "VENTA PROD ONCE": "0,00€", "RETIRADA": "0,00€"}), no una etiqueta
    genérica de columna, así que se usa como nombre y el resto de
    columnas se listan con su propia etiqueta en vez de concatenar los
    valores sin contexto."""
    importe = fila.get("IMPORTE")
    if importe is None:
        clave_juego = next((k for k, v in fila.items() if v == "IMPORTE"), None)
        if clave_juego is not None:
            resto = ", ".join(
                f"{_ETIQUETAS_COLUMNA_TWYP.get(k, k.title())}: {v}"
                for k, v in fila.items()
                if k != clave_juego and v not in (None, "")
            )
            return f"{clave_juego} — {resto}" if resto else clave_juego

    etiqueta = " - ".join(
        str(v) for k, v in fila.items() if k != "IMPORTE" and v not in (None, "")
    )
    if importe is not None:
        return f"{etiqueta}: {importe}" if etiqueta else str(importe)
    return etiqueta or "(sin datos)"


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

    # El portal aplica el saldo acreedor DENTRO del cálculo de "importe a
    # liquidar" (ver texto del propio informe: "...incluyendo el saldo
    # deudor/acreedor aplicado en este día"), no son alternativas: si hay
    # importe a liquidar, es el dato real a pagar y debe mostrarse
    # siempre, con el saldo acreedor como aclaración de que ya está
    # descontado — nunca al revés, o se omite el importe real a pagar.
    if importe > 0:
        texto = f"Importe a liquidar: {bloque.get('importe')}"
        if saldo > 0:
            texto += f" (incluye saldo acreedor a tu favor ya aplicado: {bloque.get('saldo_acreedor')})"
    elif saldo > 0:
        # caso raro: solo hay saldo acreedor, sin importe a liquidar
        texto = f"Saldo acreedor a tu favor: {bloque.get('saldo_acreedor')}"
    else:
        texto = "Sin liquidación hoy"
    flowables.append(Paragraph(texto, estilos["normal"]))

    for grupo in bloque.get("detalle_completo") or []:
        filas = grupo.get("filas") or []
        for fila in filas:
            flowables.append(Paragraph(f"• {_formatear_fila_liquidacion(fila)}", estilos["bullet"]))

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
    """Cuadre del último día trabajado: si el ticket trae resumen.resultado
    se usa directamente como RESULTADO DEL DÍA (ya incluye premios de
    cupones y de rasca dentro de "PAGOS"); si no, se calcula con la
    fórmula de respaldo (ventas - devoluciones - premios - tarjeta, sin
    sumar pagos_rasca aparte, para no contarlo dos veces). Ver
    cuadre_diario.py.

    IMPORTANTE — terminología corregida el 2026-07-20: este número NO es
    efectivo/caja física que el vendedor tenga en su poder, es una
    diferencia CONTABLE entre ventas y pagos que pasa al saldo
    acreedor/deudor de la liquidación periódica (antes se llamaba
    "EFECTIVO DEL DÍA", nombre engañoso — ver cuadre_diario.py).

    Sin fecha fija "ayer" — se llama a calcular_cuadre_diario() sin
    argumento para que use el ÚLTIMO ticket ya procesado disponible (ver
    ticket_mas_reciente() en cuadre_diario.py). FIX de un bug real
    (2026-07-20): con "ayer" fijo, el cuadre de cualquier lunes fallaba
    con "no hay ticket procesado para el domingo" aunque el ticket del
    viernes (último día trabajado real) sí estuviera disponible.

    Al no ser ya siempre "ayer" (puede arrastrar varios días atrás por
    festivos o huecos), la fecha del ticket usado se muestra siempre en
    el informe — antes no hacía falta decirla porque era implícita."""
    flowables = [Paragraph("CUADRE DIARIO", estilos["seccion"])]
    resultado = cuadre_diario.calcular_cuadre_diario()

    if not resultado.get("ejecutado", True):
        flowables.append(Paragraph(resultado["mensaje"], estilos["normal"]))
        return flowables

    flowables.append(Paragraph(
        f"Último día trabajado: {resultado['fecha']}", estilos["bullet"],
    ))
    flowables.append(Paragraph(
        f"RESULTADO DEL DÍA: {resultado['resultado_dia']:.2f}€ (origen: {resultado['origen']})",
        estilos["bullet"],
    ))

    if resultado.get("revision_manual_necesaria"):
        flowables.append(Paragraph(
            "🔴 Ticket marcado como revision_manual_necesaria — ni siquiera los propios "
            "datos del ticket son consistentes entre sí. Hace falta mirar el ticket físico.",
            estilos["normal"],
        ))
        return flowables

    if resultado.get("ocr_revision_pendiente"):
        flowables.append(Paragraph(
            "🔶 Ticket marcado como ocr_revision_pendiente — la página RESUMEN salió mal "
            "transcrita (OCR) y este RESULTADO DEL DÍA no es fiable hasta reprocesarlo.",
            estilos["normal"],
        ))
        return flowables

    dif = resultado["diferencia"]
    if dif is None:
        flowables.append(Paragraph(
            "ℹ️ Sin resumen.resultado en el ticket — RESULTADO DEL DÍA calculado con la "
            "fórmula de respaldo, no verificado contra un segundo valor.",
            estilos["normal"],
        ))
    elif abs(dif) < 0.01:
        flowables.append(Paragraph(
            "✅ Cálculo del ticket verificado: coincide con resumen.resultado", estilos["normal"],
        ))
    else:
        signo = "+" if dif > 0 else ""
        flowables.append(Paragraph(
            f"⚠️ Verificación del ticket: la fórmula no coincide con resumen.resultado "
            f"({signo}{dif:.2f}€ de diferencia)", estilos["normal"],
        ))

    flowables.append(Paragraph(
        f"Informativo (no entra en el RESULTADO DEL DÍA): devoluciones "
        f"{resultado['devoluciones_total']:.2f}€, pagos con tarjeta {resultado['tarjeta']:.2f}€",
        estilos["bullet"],
    ))
    return flowables


def _seccion_novedades_tech(hoy_date, estilos):
    """Novedades del día en Claude Code, IA y las 12 categorías
    personales (fitness, longevidad, péptidos, Seiko mods, IA,
    videojuegos...) — generadas por scripts/novedades_tech.py, que
    busca vía la API de Anthropic (web_search + conector MCP a
    trends-mcp) y deduplica contra su propio histórico. Si el hallazgo
    viene marcado como dudoso ("cautela": true), se antepone un aviso
    explícito en vez de presentarlo como un hecho — mismo criterio de
    verificación honesta ya aplicado al validar SkillSpector antes de
    instalarlo."""
    flowables = [Paragraph("📰 NOVEDADES DEL DÍA", estilos["seccion"])]
    hoy = hoy_date.strftime("%Y%m%d")
    ruta = NOVEDADES_TECH_PATH / f"novedades_{hoy}.json"

    if not ruta.exists():
        flowables.append(Paragraph("Sin novedades relevantes hoy.", estilos["normal"]))
        return flowables

    try:
        with open(ruta) as f:
            datos = json.load(f)
    except (json.JSONDecodeError, OSError):
        flowables.append(Paragraph("Sin novedades relevantes hoy.", estilos["normal"]))
        return flowables

    if not datos.get("ejecutado", True):
        flowables.append(Paragraph(
            f"ℹ️ {datos.get('mensaje', 'Búsqueda de novedades no ejecutada hoy.')}",
            estilos["normal"],
        ))
        return flowables

    items = datos.get("items") or []
    if not items:
        flowables.append(Paragraph("Sin novedades relevantes hoy.", estilos["normal"]))
        return flowables

    for item in items:
        prefijo = "⚠️ VERIFICAR CON CAUTELA — " if item.get("cautela") else "• "
        flowables.append(Paragraph(
            f"{prefijo}[{item.get('categoria', '?')}] {item.get('resumen', '')}",
            estilos["bullet"],
        ))
    return flowables


DIAS_AVISO_PROXIMO = 15


def _seccion_avisos_proximos(hoy_date, estilos):
    """Revisa todos los JSON de Comunicaciones TPV ya procesados por
    ocr_comunicaciones.py y muestra los que mencionan una fecha dentro de
    los próximos DIAS_AVISO_PROXIMO días — p.ej. la fecha en la que un
    juego se da por vendido tras finalizar su venta voluntaria. Las
    fechas las transcribe el modelo de visión tal cual (ver
    ocr_comunicaciones.py); decidir cuáles caen en la ventana de aviso es
    un cálculo de fechas que hace Python, no el modelo."""
    flowables = [Paragraph("AVISOS PRÓXIMOS", estilos["seccion"])]
    limite = hoy_date + timedelta(days=DIAS_AVISO_PROXIMO)

    avisos = []
    if COMUNICACIONES_PATH.exists():
        for ruta_json in sorted(COMUNICACIONES_PATH.glob("*.json")):
            try:
                with open(ruta_json) as f:
                    datos_aviso = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for entrada in datos_aviso.get("fechas_mencionadas") or []:
                fecha_txt = entrada.get("fecha")
                if not fecha_txt:
                    continue
                try:
                    fecha = datetime.strptime(fecha_txt, "%d/%m/%Y").date()
                except ValueError:
                    continue
                if hoy_date <= fecha <= limite:
                    avisos.append((fecha, datos_aviso, entrada))

    if not avisos:
        flowables.append(Paragraph(
            f"Sin avisos con fechas relevantes en los próximos {DIAS_AVISO_PROXIMO} días.",
            estilos["normal"],
        ))
        return flowables

    avisos.sort(key=lambda t: t[0])
    for fecha, datos_aviso, entrada in avisos:
        juego = datos_aviso.get("juego") or datos_aviso.get("tipo_comunicacion") or "?"
        flowables.append(Paragraph(
            f"⚠️ {fecha.strftime('%d/%m/%Y')} — {juego}: {entrada.get('descripcion', '')}",
            estilos["bullet"],
        ))
    return flowables


DIAS_DEVOLUCION_RECIENTE = 7


def _seccion_devoluciones_recientes(hoy_date, estilos):
    """Revisa todos los JSON de DEVOLUCIÓN LIBROS ya procesados por
    ocr_devolucion_libros.py y muestra los que tienen fecha dentro de los
    últimos DIAS_DEVOLUCION_RECIENTE días — tanto avisos de finalización
    de venta voluntaria (tipo "aviso_inicio") como justificantes de envío
    a Correos (tipo "retorno_completado"). Igual que en
    _seccion_avisos_proximos, decidir qué cae en la ventana es un cálculo
    de fechas que hace Python, no el modelo de visión."""
    flowables = [Paragraph("DEVOLUCIONES DE LIBROS RECIENTES", estilos["seccion"])]
    limite_inferior = hoy_date - timedelta(days=DIAS_DEVOLUCION_RECIENTE)

    eventos = []
    if DEVOLUCION_LIBROS_PATH.exists():
        for ruta_json in sorted(DEVOLUCION_LIBROS_PATH.glob("*.json")):
            try:
                with open(ruta_json) as f:
                    datos_evento = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            fecha_txt = datos_evento.get("fecha")
            if not fecha_txt:
                continue
            try:
                fecha = datetime.strptime(fecha_txt, "%d/%m/%Y").date()
            except ValueError:
                continue
            if limite_inferior <= fecha <= hoy_date:
                eventos.append((fecha, datos_evento))

    if not eventos:
        flowables.append(Paragraph(
            f"Sin devoluciones de libros en los últimos {DIAS_DEVOLUCION_RECIENTE} días.",
            estilos["normal"],
        ))
        return flowables

    eventos.sort(key=lambda t: t[0])
    for fecha, datos_evento in eventos:
        libros = datos_evento.get("libros_devueltos") or []
        if datos_evento.get("tipo") == "retorno_completado":
            juegos = sorted({l.get("producto") for l in libros if l.get("producto")})
            texto = (
                f"📬 {fecha.strftime('%d/%m/%Y')} — Retorno completado: "
                f"{len(libros)} libro(s) enviados a Correos"
                + (f" ({', '.join(juegos)})" if juegos else "")
            )
        else:
            juego = datos_evento.get("juego") or "?"
            texto = f"📬 {fecha.strftime('%d/%m/%Y')} — Aviso de finalización de venta voluntaria: {juego}"
            if libros:
                texto += f" ({len(libros)} libro(s) ya retirados)"
        flowables.append(Paragraph(texto, estilos["bullet"]))
    return flowables


def generar_informe_pdf(datos, datos_agenda, hoy_date, destino):
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
    story.extend(_seccion_caducidad_libros(hoy_date, estilos))
    story.extend(_seccion_premios_repartidos(datos, hoy_date, estilos))
    story.extend(_seccion_liquidacion(datos, estilos))
    story.extend(_lista_alertas(datos, datos_agenda, hoy_date, estilos))
    story.extend(_lista_agenda(datos_agenda, estilos))
    story.extend(_seccion_cuadre_diario(hoy_date, estilos))
    story.extend(_seccion_devoluciones_recientes(hoy_date, estilos))
    story.extend(_seccion_avisos_proximos(hoy_date, estilos))
    story.extend(_seccion_novedades_tech(hoy_date, estilos))

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
