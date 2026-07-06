#!/usr/bin/env python3
"""
Parsers de HTML crudo - Portal ONCE
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Funciones puras (sin red, sin Playwright): reciben el HTML guardado por
capturar_html_portal.py y devuelven la estructura ya parseada como dict,
lista para volcar a JSON.

Estructuras confirmadas analizando config/html_backup/*.html:

- premios_pasiva.html: tabla #gvwPremios con 3 columnas, la primera
  ("FECHA - JUEGO - CATEGORÍA") contiene además una subtabla oculta
  #gvwDetalles (Tradicional / TPV / BONO / R. prim. cifra / R. últ. cifra,
  variable según el juego). La subtabla YA está en el HTML crudo con
  style="display:none" independientemente del checkbox "Visualizar tablas
  anidadas" (ese checkbox solo hace show/hide vía JS, no cambia el HTML
  que devuelve el servidor).
- premios_activa.html / premios_instantanea.html: tabla #gvwPremios con
  5 columnas planas (FECHA, JUEGO, CATEGORÍA, CANTIDAD, IMPORTE) — sin
  subtabla ni desglose Tradicional/TPV/BONO, a diferencia de pasiva.
- control_retirada_*.html: 4 tablas independientes —
  #gvwPaquetes (paquetes, cada uno con subtabla #gvwProductosPaquete_N
  de productos, y cada producto con su propia subtabla
  #gvwDetalleProductoPaquete cuyas columnas varían: "Libro" para
  productos numerados por libro, "Cupón/Cantidad/Series" para productos
  numerados por cupón), #gvwOtrasRetiradasCentro (plana),
  #gvwDetallesProducto (plana) y #gvwDetallesExtra (plana, mismo
  esquema que la anterior).
- control_almacen.html: tabla #gvwAlmacen (PRODUCTO/RETIRADO/CONFIRMADO/
  ACTIVADO/VENDIDO), cada producto con subtabla #gvwDetalles
  (Nº de Libro / Estado / Fecha).
"""

import copy
import re

from bs4 import BeautifulSoup


def _texto_sin_subtablas(celda):
    """Texto de una celda ignorando el contenido de cualquier <table>
    anidada dentro de ella (patrón "resumen + detalle expandible" de
    ASP.NET GridView: si no se descarta la subtabla, su texto se mezcla
    con el de la celda exterior)."""
    clon = copy.deepcopy(celda)
    for tabla in clon.find_all("table"):
        tabla.decompose()
    return " ".join(clon.get_text(" ", strip=True).split())


def _num(texto):
    """Convierte un número en formato español ("1.410,00", "2,5", "-")
    a int/float. Devuelve None para el placeholder "-" y el texto tal
    cual si no es numérico."""
    texto = texto.strip()
    if texto in ("", "-"):
        return None
    normalizado = texto.replace(".", "").replace(",", ".")
    try:
        valor = float(normalizado)
    except ValueError:
        return texto
    return int(valor) if valor.is_integer() else valor


def _filas_directas(tabla):
    """<tr> hijos directos del <tbody> (o de la tabla) de una tabla,
    sin bajar a las <tr> de subtablas anidadas."""
    if tabla is None:
        return []
    contenedor = tabla.find("tbody", recursive=False) or tabla
    return contenedor.find_all("tr", recursive=False)


def _celdas_directas(fila):
    return fila.find_all(["th", "td"], recursive=False)


# ── Premios repartidos ───────────────────────────────────────────────

def parsear_premios_pasiva(html, fecha_extraccion):
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="gvwPremios")
    filas = _filas_directas(tabla)

    premios = []
    for fila in filas[1:]:  # fila 0 es la cabecera
        celdas = _celdas_directas(fila)
        if len(celdas) < 3:
            continue

        celda_fecha_juego = celdas[0]
        texto = _texto_sin_subtablas(celda_fecha_juego)
        partes = [p.strip() for p in texto.split(" - ")]
        fecha = partes[0] if partes else None
        juego = partes[1] if len(partes) > 1 else None
        categoria = " - ".join(partes[2:]) if len(partes) > 2 else None

        detalle = []
        subtabla = celda_fecha_juego.find("table", id="gvwDetalles")
        for sub_fila in _filas_directas(subtabla)[1:]:
            sub_celdas = _celdas_directas(sub_fila)
            if len(sub_celdas) < 3:
                continue
            detalle.append({
                "tipo": _texto_sin_subtablas(sub_celdas[0]),
                "cantidad": _num(sub_celdas[1].get_text(strip=True)),
                "importe": _num(sub_celdas[2].get_text(strip=True)),
            })

        premios.append({
            "fecha": fecha,
            "juego": juego,
            "categoria": categoria,
            "cantidad_total": _num(celdas[1].get_text(strip=True)),
            "importe_total": _num(celdas[2].get_text(strip=True)),
            "detalle": detalle,
        })

    return {"fecha_extraccion": fecha_extraccion, "tipo": "pasiva", "premios": premios}


def _parsear_premios_plano(html, tipo, fecha_extraccion):
    """Activa e instantánea: tabla plana de 5 columnas, sin subtabla de
    desglose (no hay Tradicional/TPV/BONO que extraer)."""
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="gvwPremios")
    filas = _filas_directas(tabla)

    premios = []
    for fila in filas[1:]:
        celdas = _celdas_directas(fila)
        if len(celdas) < 5:
            continue
        premios.append({
            "fecha": celdas[0].get_text(strip=True),
            "juego": celdas[1].get_text(strip=True),
            "categoria": celdas[2].get_text(strip=True),
            "cantidad_total": _num(celdas[3].get_text(strip=True)),
            "importe_total": _num(celdas[4].get_text(strip=True)),
            "detalle": [],
        })

    return {"fecha_extraccion": fecha_extraccion, "tipo": tipo, "premios": premios}


def parsear_premios_activa(html, fecha_extraccion):
    return _parsear_premios_plano(html, "activa", fecha_extraccion)


def parsear_premios_instantanea(html, fecha_extraccion):
    return _parsear_premios_plano(html, "instantanea", fecha_extraccion)


# ── Control de retirada ───────────────────────────────────────────────

def _tabla_flat_generica(tabla):
    """Cabecera + filas de una tabla plana (sin subtablas), usando el
    texto de la cabecera como claves. Devuelve [] si la tabla está vacía
    (placeholder "No hay datos que mostrar" del GridView)."""
    filas = _filas_directas(tabla)
    if not filas:
        return []

    primera = _celdas_directas(filas[0])
    if len(filas) == 1 and len(primera) == 1 and "no hay datos" in primera[0].get_text(strip=True).lower():
        return []

    cabecera = [_texto_sin_subtablas(c) for c in primera]
    resultado = []
    for fila in filas[1:]:
        celdas = _celdas_directas(fila)
        resultado.append({
            nombre: _texto_sin_subtablas(celda)
            for nombre, celda in zip(cabecera, celdas)
        })
    return resultado


def _parsear_detalle_producto_flat(tabla):
    """#gvwDetallesProducto y #gvwDetallesExtra comparten el mismo
    esquema: Producto / Retirada Ordinaria / Retirada Adicional-Extra /
    Venta electrónica."""
    resultado = []
    for fila in _filas_directas(tabla)[1:]:
        celdas = _celdas_directas(fila)
        if len(celdas) < 4:
            continue
        resultado.append({
            "producto": celdas[0].get_text(strip=True),
            "retirada_ordinaria": _num(celdas[1].get_text(strip=True)),
            "retirada_adicional_extra": _num(celdas[2].get_text(strip=True)),
            "venta_electronica": _num(celdas[3].get_text(strip=True)),
        })
    return resultado


def parsear_control_retirada(html, tipo, fecha_extraccion):
    soup = BeautifulSoup(html, "html.parser")

    paquetes = []
    for fila in _filas_directas(soup.find("table", id="gvwPaquetes"))[1:]:
        celdas = _celdas_directas(fila)
        if len(celdas) < 3:
            continue

        celda_descripcion = celdas[0]
        productos = []
        tabla_productos = celda_descripcion.find("table", id=re.compile(r"^gvwProductosPaquete"))
        for fila_p in _filas_directas(tabla_productos)[1:]:
            celdas_p = _celdas_directas(fila_p)
            if len(celdas_p) < 2:
                continue

            celda_nombre = celdas_p[0]
            detalle = []
            tabla_detalle = celda_nombre.find("table", id="gvwDetalleProductoPaquete")
            filas_detalle = _filas_directas(tabla_detalle)
            if filas_detalle:
                cabecera = [c.get_text(strip=True) for c in _celdas_directas(filas_detalle[0])]
                for fila_d in filas_detalle[1:]:
                    celdas_d = _celdas_directas(fila_d)
                    detalle.append({
                        cabecera[i].lower().replace(" ", "_"): celdas_d[i].get_text(strip=True)
                        for i in range(min(len(cabecera), len(celdas_d)))
                    })

            productos.append({
                "producto": _texto_sin_subtablas(celda_nombre),
                "cantidad": _num(celdas_p[1].get_text(strip=True)),
                "detalle": detalle,
            })

        paquetes.append({
            "descripcion": _texto_sin_subtablas(celda_descripcion),
            "importe": _num(celdas[1].get_text(strip=True)),
            "soportes": _num(celdas[2].get_text(strip=True)),
            "productos": productos,
        })

    return {
        "fecha_extraccion": fecha_extraccion,
        "tipo": tipo,
        "paquetes": paquetes,
        "otras_retiradas_centro": _tabla_flat_generica(soup.find("table", id="gvwOtrasRetiradasCentro")),
        "detalle_por_producto": _parsear_detalle_producto_flat(soup.find("table", id="gvwDetallesProducto")),
        "producto_extraordinario": _parsear_detalle_producto_flat(soup.find("table", id="gvwDetallesExtra")),
    }


# ── Control de almacén de lotería instantánea ────────────────────────

def parsear_control_almacen(html, fecha_extraccion):
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", id="gvwAlmacen")

    productos = []
    totales = None
    for fila in _filas_directas(tabla)[1:]:
        celdas = _celdas_directas(fila)
        if len(celdas) < 5:
            continue

        celda_producto = celdas[0]

        if celda_producto.find("div", id="divTotal"):
            # fila de totales del pie de tabla (id="divTotal"), no un
            # producto: se recoge aparte en vez de listarla como tal.
            totales = {
                "retirado": _num(celdas[1].get_text(strip=True)),
                "confirmado": _num(celdas[2].get_text(strip=True)),
                "activado": _num(celdas[3].get_text(strip=True)),
                "vendido": _num(celdas[4].get_text(strip=True)),
            }
            continue

        detalle = []
        subtabla = celda_producto.find("table", id="gvwDetalles")
        for sub_fila in _filas_directas(subtabla)[1:]:
            sub_celdas = _celdas_directas(sub_fila)
            if len(sub_celdas) < 3:
                continue
            detalle.append({
                "libro": sub_celdas[0].get_text(strip=True),
                "estado": sub_celdas[1].get_text(strip=True),
                "fecha": sub_celdas[2].get_text(strip=True),
            })

        productos.append({
            "producto": _texto_sin_subtablas(celda_producto),
            "retirado": _num(celdas[1].get_text(strip=True)),
            "confirmado": _num(celdas[2].get_text(strip=True)),
            "activado": _num(celdas[3].get_text(strip=True)),
            "vendido": _num(celdas[4].get_text(strip=True)),
            "detalle": detalle,
        })

    return {"fecha_extraccion": fecha_extraccion, "productos": productos, "totales": totales}
