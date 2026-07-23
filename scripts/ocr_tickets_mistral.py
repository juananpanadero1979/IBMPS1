#!/usr/bin/env python3
"""
OCR de tickets TPV - Portal ONCE - variante con Mistral OCR
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

Prueba experimental (2026-07-23): versión alternativa de ocr_tickets.py que
usa Mistral OCR ("mistral-ocr-latest", el motor de documento dedicado de
Mistral, distinto de sus modelos de chat/visión) en vez de Claude Vision,
para ver si transcribe mejor la página RESUMEN ACTIVIDAD DIARIA en los
tickets donde Claude la leyó mal (ver "ocr_revision_pendiente": true en
esos JSON — 050626, 050726, 140626, 150726, 240526, 250526).

IMPORTANTE (misma regla del proyecto que ocr_tickets.py, ver CLAUDE.md —
"los cálculos del cuadre ONCE se hacen SIEMPRE con Python, nunca con IA"):
Mistral OCR NO es un modelo de chat al que se le pide una interpretación —
es un motor de documento que transcribe el PDF completo a texto/markdown
estructurado (títulos, tablas) sin que se le pida nada más. Aquí tampoco se
le pide que calcule ni identifique columnas: eso lo hace exclusivamente
Python (`_filas_desde_markdown` + los mismos extractores `_extraer_*` de
ocr_tickets.py, reutilizados tal cual — no se duplica esa lógica).

Diferencia de flujo frente a ocr_tickets.py: Mistral procesa el PDF entero
en una sola llamada (sube el fichero con `purpose=ocr` y luego pide el OCR
sobre ese file_id) en vez de renderizar página a página con fitz y llamar
al modelo una vez por página.

No se ha añadido el SDK `mistralai` como dependencia nueva sin confirmarlo
antes (regla del proyecto sobre instalar paquetes) — se usan peticiones
HTTP directas con urllib, igual que ya hace el fallback de NVIDIA en
ocr_tickets.py.

Uso:
    python3 ocr_tickets_mistral.py 050626.pdf 050726.pdf ...
    python3 ocr_tickets_mistral.py               # procesa los 5-6 tickets
                                                  # ya conocidos con OCR
                                                  # sospechoso, por defecto

Escribe {fecha}_mistral.json junto al PDF original (NO sobrescribe el
{fecha}.json ya generado por ocr_tickets.py/Claude), para poder comparar
ambos motores campo a campo.
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ocr_tickets import (
    TICKETS_PATH,
    _es_ruido,
    _extraer_libros_vendidos,
    _extraer_pagos_rasca,
    _extraer_premios_pagados,
    _extraer_resumen,
    _extraer_ventas_o_devoluciones,
    _fecha_desde_nombre,
    _identificar_tipo,
)

KEYCHAIN_SERVICE_MISTRAL = "MistralAPI"
KEYCHAIN_ACCOUNT_MISTRAL = "API_KEY"

MISTRAL_BASE_URL = "https://api.mistral.ai"
MODELO_MISTRAL_OCR = "mistral-ocr-latest"

# Tickets marcados como "ocr_revision_pendiente": true tras el diagnóstico
# del cuadre diario (2026-07-23) — usados por defecto si no se pasan
# nombres de fichero por línea de comandos.
TICKETS_PENDIENTES_POR_DEFECTO = [
    "050626.pdf", "050726.pdf", "140626.pdf",
    "150726.pdf", "240526.pdf", "250526.pdf",
]

_RE_FILA_SEPARADORA_MARKDOWN = re.compile(r"^[\s|:-]+$")
_RE_IMAGEN_MARKDOWN = re.compile(r"^!\[.*\]\(.*\)$")


def _leer_clave_mistral():
    resultado = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE_MISTRAL,
         "-a", KEYCHAIN_ACCOUNT_MISTRAL, "-w"],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0 or not resultado.stdout.strip():
        raise RuntimeError(
            f"No hay clave de Mistral guardada en el Keychain "
            f"(servicio '{KEYCHAIN_SERVICE_MISTRAL}', cuenta '{KEYCHAIN_ACCOUNT_MISTRAL}')."
        )
    return resultado.stdout.strip()


def _multipart_encode(campos, nombre_campo_archivo, ruta_archivo):
    """Codifica un cuerpo multipart/form-data a mano (sin la librería
    requests ni el SDK de Mistral) para subir el PDF tal cual hace
    POST /v1/files del API real de Mistral (confirmado leyendo el código
    fuente de mistralai/client-python, no solo su documentación)."""
    boundary = uuid.uuid4().hex
    partes = []
    for clave, valor in campos.items():
        partes.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{clave}\"\r\n\r\n{valor}\r\n"
            .encode("utf-8")
        )
    contenido = ruta_archivo.read_bytes()
    cabecera_archivo = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{nombre_campo_archivo}\"; "
        f"filename=\"{ruta_archivo.name}\"\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    partes.append(cabecera_archivo + contenido + b"\r\n")
    partes.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(partes)


def _peticion_json(url, api_key, datos=None, content_type="application/json", timeout=180):
    peticion = urllib.request.Request(
        url,
        data=datos,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mistral API error {e.code}: {cuerpo}") from e


def _subir_archivo(api_key, ruta_pdf):
    """POST /v1/files con purpose=ocr. Devuelve el file_id."""
    content_type, cuerpo = _multipart_encode({"purpose": "ocr"}, "file", ruta_pdf)
    respuesta = _peticion_json(
        f"{MISTRAL_BASE_URL}/v1/files", api_key, datos=cuerpo,
        content_type=content_type, timeout=300,
    )
    return respuesta["id"]


def _procesar_ocr(api_key, file_id):
    """POST /v1/ocr sobre un file_id ya subido. Devuelve la respuesta
    completa (dict) con "pages": [{"index":..., "markdown":...}, ...]."""
    payload = json.dumps({
        "model": MODELO_MISTRAL_OCR,
        "document": {"type": "file", "file_id": file_id},
        "table_format": "markdown",
    }).encode("utf-8")
    return _peticion_json(f"{MISTRAL_BASE_URL}/v1/ocr", api_key, datos=payload, timeout=300)


def _resolver_markdown(pagina):
    """El markdown de una página NO trae el contenido de las tablas en
    línea — deja una referencia tipo "[tbl-0.md](tbl-0.md)" y el
    contenido real (en formato markdown, porque se pidió table_format=
    "markdown") vive aparte en pagina["tables"], una lista de objetos
    {"id": "tbl-0.md", "content": "...tabla markdown..."}. Sin resolver
    esta referencia, la página RESUMEN ACTIVIDAD DIARIA parece vacía
    (todo su contenido numérico está detrás de los tbl-N.md) — confirmado
    inspeccionando la respuesta cruda de la API en un ticket real."""
    markdown = pagina.get("markdown") or ""
    for tabla in pagina.get("tables") or []:
        tabla_id = tabla.get("id")
        contenido = tabla.get("content") or ""
        if not tabla_id:
            continue
        enlace = f"[{tabla_id}]({tabla_id})"
        markdown = markdown.replace(enlace, "\n" + contenido + "\n")
    return markdown


_RE_VALOR_LINEA = re.compile(r"^-?\d[\d.,]*\s*(EUR|E)?$", re.IGNORECASE)


_CABECERAS_SECCION = {
    "JUEGO", "PAGOS CON TARJETA", "PAGOS CONTARJETA", "PRODUCTOS COMPLEMENTARIOS",
}


def _zip_etiquetas_y_valores(lineas):
    """Cuando Mistral NO detecta la página como una tabla (a diferencia
    del caso resuelto por _resolver_markdown), a veces linealiza un
    layout de dos columnas del ticket (etiqueta | importe) leyendo
    primero TODA una columna y luego TODA la otra, en vez de intercalarlas
    fila a fila. Se ha visto en AMBOS órdenes:

    - etiqueta-run seguido de valor-run (050726, 240526): "2 ANUL",
      "108 VENTAS", "59 PAGOS", "TOTAL" seguidas de "5,00 E", "183,00 E",
      "113,00 E", "65,00 E".
    - valor-run seguido de etiqueta-run (140626, pie "TOTAL BOLETOS" de
      la página PAGOS RASCA): "193,00 EUR", "31" seguidas de "TOTAL",
      "BOLETOS" — aquí el importe real (193,00 EUR) aparece ANTES que su
      etiqueta. Sin cubrir este orden, _extraer_pagos_rasca nunca ve el
      importe junto a "TOTAL" y el total sale mal (confirmado con datos
      reales: sin este fix salía 15,00 en vez de 193,00).

    En ambos casos se detectan dos tramos consecutivos de tamaño IGUAL
    (uno con líneas que parecen un número, otro con líneas que no) y se
    combinan 1:1 por posición, etiqueta primero, tal como esperan los
    extractores. Si los tamaños no coinciden, se deja el tramo tal cual
    en vez de arriesgar un emparejamiento incorrecto.

    Las cabeceras de sección sin valor propio ("PAGOS CON TARJETA",
    "PRODUCTOS COMPLEMENTARIOS", "JUEGO") rompen cualquier tramo — si no,
    su línea de más se cuenta contra el tramo contrario y el zip nunca
    cuadra. _extraer_resumen (en ocr_tickets.py) compara "PAGOS CON
    TARJETA" con igualdad exacta, así que aquí también se normaliza la
    variante sin espacio "PAGOS CONTARJETA" vista en un ticket real
    (240526)."""
    resultado = []
    i, n = 0, len(lineas)

    def es_valor(idx):
        return bool(_RE_VALOR_LINEA.match(lineas[idx]))

    def es_cabecera(idx):
        return lineas[idx].upper() in _CABECERAS_SECCION

    while i < n:
        if es_cabecera(i):
            texto = "PAGOS CON TARJETA" if lineas[i].upper() == "PAGOS CONTARJETA" else lineas[i]
            resultado.append(texto)
            i += 1
            continue

        valor_en_i = es_valor(i)
        j = i
        while j < n and es_valor(j) == valor_en_i and not es_cabecera(j):
            j += 1
        k = j
        while k < n and es_valor(k) != valor_en_i and not es_cabecera(k):
            k += 1
        tam_a, tam_b = j - i, k - j

        if tam_a > 0 and tam_a == tam_b:
            if valor_en_i:
                resultado.extend(f"{et} {val}" for et, val in zip(lineas[j:k], lineas[i:j]))
            else:
                resultado.extend(f"{et} {val}" for et, val in zip(lineas[i:j], lineas[j:k]))
            i = k
        else:
            resultado.extend(lineas[i:j])
            i = j
    return resultado


def _filas_desde_markdown(markdown):
    """Convierte el markdown de una página (título + texto + tablas) al
    mismo esquema "filas" de col_1..col_4 que producía el prompt de Claude
    Vision — así los extractores _extraer_* de ocr_tickets.py sirven tal
    cual, sin duplicar ninguna regla de parseo.

    - Filas de tabla markdown ("| a | b | c | d |") -> una celda por
      columna, tal cual.
    - Filas separadoras de tabla ("| --- | --- |") -> se descartan.
    - Líneas de texto libre (la página RESUMEN no es una tabla, es una
      lista de "ETIQUETA valor") -> se tokenizan por espacios: las 3
      primeras palabras a col_1/col_2/col_3, el resto (p.ej. "44,00 EUR")
      a col_4 — igual que el modelo de visión repartía una etiqueta de
      varias palabras entre columnas, para que _valor_numerico_de_fila
      (que mira col_2..col_4) encuentre el importe sin depender de en
      qué columna exacta cayó.

    Antes de esa tokenización se pasa por _zip_etiquetas_y_valores, para
    el caso (visto en algunos tickets) en que Mistral linealiza un layout
    de dos columnas leyendo primero toda una columna y luego toda la
    otra, en vez de intercalarlas fila a fila. Se descartan antes el pie
    de fecha/hora de impresión y las referencias a imagen
    ("![img-0.jpeg](img-0.jpeg)") — si no, esas líneas se cuelan en medio
    de un tramo de etiquetas/valores y rompen el conteo 1:1 del zip."""
    lineas_crudas = []
    for linea in markdown.splitlines():
        linea = linea.strip()
        if linea.startswith("#"):
            linea = linea.lstrip("#").strip()
        if not linea or _RE_FILA_SEPARADORA_MARKDOWN.match(linea):
            continue
        if _es_ruido(linea) or _RE_IMAGEN_MARKDOWN.match(linea):
            continue
        lineas_crudas.append(linea)

    filas = []
    for linea in _zip_etiquetas_y_valores(lineas_crudas):
        if linea.count("|") >= 2:
            celdas = [c.strip() for c in linea.strip("|").split("|")]
        else:
            tokens = linea.split()
            celdas = tokens[:3] + [" ".join(tokens[3:])]
        celdas = (celdas + ["", "", "", ""])[:4]
        filas.append({"col_1": celdas[0], "col_2": celdas[1], "col_3": celdas[2], "col_4": celdas[3]})
    return filas


_EXTRACTOR_Y_CLAVE = {
    "ventas": (_extraer_ventas_o_devoluciones, "ventas", "total"),
    "devoluciones": (_extraer_ventas_o_devoluciones, "devoluciones", "total"),
    "premios_pagados": (_extraer_premios_pagados, "premios_pagados", "total"),
    "pagos_rasca": (_extraer_pagos_rasca, "pagos_rasca", "total"),
    "libros_vendidos": (_extraer_libros_vendidos, "libros_vendidos", "total_unidades"),
}


def procesar_ticket_mistral(ruta_pdf, api_key):
    """Procesa un PDF de ticket (5 páginas) con Mistral OCR y devuelve el
    JSON con el mismo esquema que procesar_ticket() de ocr_tickets.py."""
    resultado = {
        "fecha": _fecha_desde_nombre(ruta_pdf.stem),
        "ocr_motor": MODELO_MISTRAL_OCR,
        "ventas": {"detalle": [], "total": None},
        "devoluciones": {"detalle": [], "total": None},
        "premios_pagados": {"detalle": [], "total": None},
        "pagos_tarjeta": None,
        "resumen": {"ventas_total": None, "pagos_total": None, "resultado": None},
        "pagos_rasca": {"detalle": [], "total": None},
        "libros_vendidos": {"detalle": [], "total_unidades": None},
    }

    print(f"  Subiendo {ruta_pdf.name} a Mistral (purpose=ocr)...")
    file_id = _subir_archivo(api_key, ruta_pdf)
    print(f"  Procesando OCR (file_id={file_id})...")
    respuesta = _procesar_ocr(api_key, file_id)

    for pagina in respuesta.get("pages", []):
        markdown = _resolver_markdown(pagina)
        indice = pagina.get("index")
        tipo = _identificar_tipo(markdown)
        if tipo is None:
            print(f"  ⚠️  Página {indice}: no se reconoció el título en el markdown de Mistral")
            continue
        filas = _filas_desde_markdown(markdown)

        if tipo in _EXTRACTOR_Y_CLAVE:
            extractor, clave, nombre_total = _EXTRACTOR_Y_CLAVE[tipo]
            detalle, total = extractor(filas)
            resultado[clave] = {"detalle": detalle, nombre_total: total}
        elif tipo == "resumen":
            resumen, pagos_tarjeta = _extraer_resumen(filas)
            resultado["resumen"] = resumen
            resultado["pagos_tarjeta"] = pagos_tarjeta

    return resultado


def main():
    nombres = sys.argv[1:] or TICKETS_PENDIENTES_POR_DEFECTO
    api_key = _leer_clave_mistral()

    for nombre in nombres:
        ruta_pdf = TICKETS_PATH / nombre
        print(f"--- Procesando {nombre} con Mistral OCR ---")
        if not ruta_pdf.exists():
            print(f"  ⚠️  No existe {ruta_pdf}")
            continue
        try:
            resultado = procesar_ticket_mistral(ruta_pdf, api_key)
        except Exception as e:
            print(f"  ⚠️  Error procesando {nombre}: {e}")
            continue

        destino = ruta_pdf.with_name(ruta_pdf.stem + "_mistral.json")
        with open(destino, "w") as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)
        print(f"  ✅ Guardado {destino.name}")
        print()


if __name__ == "__main__":
    main()
