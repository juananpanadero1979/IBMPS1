#!/usr/bin/env python3
"""
Agenda - Calendario y Recordatorios de Apple (vía AppleScript)
Juan Antonio Panadero Jiménez

Para el informe matutino:
1. Eventos de hoy
2. Eventos de los próximos 3 días
3. Recordatorios pendientes de hoy (con vencimiento hoy, sin completar)
4. Recordatorios pendientes de los próximos 3 días

Requiere que Terminal/Python tenga permiso de "Calendario" y "Recordatorios"
en Ajustes del Sistema > Privacidad y seguridad (macOS lo pedirá la primera vez).
"""

import json
import subprocess
import sys
from datetime import date, datetime, timedelta

CAMPO_SEP = "||"


def _ejecutar_osascript(script):
    resultado = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if resultado.returncode != 0:
        error = resultado.stderr.strip()
        if "1743" in error or "not allowed" in error.lower():
            raise RuntimeError(
                "Sin permiso para acceder a Calendario/Recordatorios. "
                "Ve a Ajustes del Sistema > Privacidad y seguridad > Calendarios "
                "(y Recordatorios) y da acceso a Terminal/Python. Error original: "
                f"{error}"
            )
        raise RuntimeError(f"osascript falló: {error}")
    return resultado.stdout


def _parsear_fecha(campo):
    """Convierte "AAAA-M-D-segundos_desde_medianoche" en un datetime."""
    anio, mes, dia, segundos = campo.split("-")
    return datetime.combine(date(int(anio), int(mes), int(dia)), datetime.min.time()) + timedelta(
        seconds=int(segundos)
    )


def _codificar_fecha_applescript(variable):
    """Fragmento AppleScript que serializa una variable de tipo date como
    "AAAA-M-D-segundos_desde_medianoche", evitando el string localizado."""
    return (
        f'((year of {variable}) as string) & "-" & '
        f'(((month of {variable}) as integer) as string) & "-" & '
        f'((day of {variable}) as string) & "-" & '
        f'((time of {variable}) as string)'
    )


def obtener_eventos():
    """Eventos de todos los calendarios entre hoy y dentro de 4 días
    (hoy + próximos 3 días), sin clasificar todavía por día."""
    sd_enc = _codificar_fecha_applescript("sd")
    ed_enc = _codificar_fecha_applescript("ed")
    script = f'''
    tell application "Calendar"
        set salida to {{}}
        set hoy to current date
        set time of hoy to 0
        set limite to hoy + 4 * days
        repeat with cal in calendars
            set eventosCal to (every event of cal whose start date ≥ hoy and start date < limite)
            repeat with e in eventosCal
                set sd to start date of e
                set ed to end date of e
                set esTodoElDia to allday event of e
                set ubic to ""
                try
                    set ubic to (location of e) as string
                end try
                set tit to summary of e
                set nombreCal to name of cal
                set sdEnc to {sd_enc}
                set edEnc to {ed_enc}
                set end of salida to nombreCal & "{CAMPO_SEP}" & tit & "{CAMPO_SEP}" & sdEnc & "{CAMPO_SEP}" & edEnc & "{CAMPO_SEP}" & (esTodoElDia as string) & "{CAMPO_SEP}" & ubic
            end repeat
        end repeat
        set AppleScript's text item delimiters to linefeed
        set salidaTexto to salida as string
        set AppleScript's text item delimiters to ""
        return salidaTexto
    end tell
    '''
    salida = _ejecutar_osascript(script)

    eventos = []
    for linea in salida.splitlines():
        if not linea.strip():
            continue
        calendario, titulo, inicio, fin, todo_el_dia, ubicacion = linea.split(CAMPO_SEP)
        eventos.append(
            {
                "calendario": calendario,
                "titulo": titulo,
                "inicio": _parsear_fecha(inicio).isoformat(),
                "fin": _parsear_fecha(fin).isoformat(),
                "todo_el_dia": todo_el_dia.strip().lower() == "true",
                "ubicacion": ubicacion,
            }
        )
    eventos.sort(key=lambda e: e["inicio"])
    return eventos


def obtener_recordatorios():
    """Recordatorios sin completar con vencimiento, desde cualquier fecha
    pasada (vencidos que siguen pendientes) hasta dentro de 4 días (hoy +
    próximos 3 días), de todas las listas."""
    dd_enc = _codificar_fecha_applescript("dd")
    script = f'''
    tell application "Reminders"
        set salida to {{}}
        set hoy to current date
        set time of hoy to 0
        set limite to hoy + 4 * days
        repeat with lst in lists
            set recsLista to (every reminder of lst whose completed is false)
            repeat with r in recsLista
                set dd to due date of r
                if dd is not missing value and dd < limite then
                    set tit to name of r
                    set nombreLista to name of lst
                    set ddEnc to {dd_enc}
                    set end of salida to nombreLista & "{CAMPO_SEP}" & tit & "{CAMPO_SEP}" & ddEnc
                end if
            end repeat
        end repeat
        set AppleScript's text item delimiters to linefeed
        set salidaTexto to salida as string
        set AppleScript's text item delimiters to ""
        return salidaTexto
    end tell
    '''
    salida = _ejecutar_osascript(script)

    recordatorios = []
    for linea in salida.splitlines():
        if not linea.strip():
            continue
        lista, titulo, vencimiento = linea.split(CAMPO_SEP)
        recordatorios.append(
            {
                "lista": lista,
                "titulo": titulo,
                "vencimiento": _parsear_fecha(vencimiento).isoformat(),
            }
        )
    recordatorios.sort(key=lambda r: r["vencimiento"])
    return recordatorios


def obtener_agenda():
    """Agrupa eventos y recordatorios en "hoy" / "próximos 3 días" para el
    informe matutino."""
    hoy = date.today()

    eventos = obtener_eventos()
    eventos_hoy = [e for e in eventos if datetime.fromisoformat(e["inicio"]).date() == hoy]
    eventos_proximos = [e for e in eventos if datetime.fromisoformat(e["inicio"]).date() > hoy]

    recordatorios = obtener_recordatorios()
    # "hoy" incluye vencidos de días anteriores que siguen pendientes, para
    # no perder de vista recordatorios olvidados en el informe matutino.
    recordatorios_hoy = [
        r for r in recordatorios if datetime.fromisoformat(r["vencimiento"]).date() <= hoy
    ]
    recordatorios_proximos = [
        r for r in recordatorios if datetime.fromisoformat(r["vencimiento"]).date() > hoy
    ]

    return {
        "generado_en": datetime.now().isoformat(),
        "eventos_hoy": eventos_hoy,
        "eventos_proximos_3_dias": eventos_proximos,
        "recordatorios_hoy": recordatorios_hoy,
        "recordatorios_proximos_3_dias": recordatorios_proximos,
    }


if __name__ == "__main__":
    try:
        agenda = obtener_agenda()
    except RuntimeError as e:
        print(f"⚠️  {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(agenda, indent=2, ensure_ascii=False))
