#!/usr/bin/env python3
# DEPRECATED — sustituido por asistente.py.
# No se usa en producción. Mantener solo como referencia
# histórica. Candidato a borrar en una futura limpieza.
"""
IBMPS1 - Punto de entrada único del sistema multi-agente
Juan Antonio Panadero Jiménez

Recibe el texto de la pregunta/orden como argumento de línea de
comandos, lo pasa al agente CEREBRO (agente_cerebro.py) — que decide
qué agente especializado debe responder — e imprime SOLO la respuesta
final en stdout, sin mensajes de diagnóstico ni logs, para que se pueda
leer directamente en voz alta (p.ej. desde un Atajo de Siri) sin tener
que filtrar nada.

Toda la operación (Keychain + llamada a Claude) tiene un límite de
tiempo duro (TIMEOUT_SEGUNDOS): si algo se queda colgado — por ejemplo,
una sesión SSH sin interfaz gráfica a la que el Keychain le pide una
confirmación que nunca podrá responderse — el script falla con un
mensaje claro en vez de quedarse mudo para siempre sin dar ninguna señal
por SSH.

Uso:
    python3 ibmps1.py "¿Cuál es mi saldo acreedor de hoy?"
"""

import signal
import sys

import agente_cerebro

TIMEOUT_SEGUNDOS = 25

# Instrucción de concisión específica para el uso por voz (Siri): se añade
# aquí, no en agente_cerebro.py, porque solo aplica a este punto de
# entrada — otras formas de llamar a agente_cerebro.procesar() (p.ej. en
# pruebas desde terminal) no necesitan una respuesta pensada para oírse.
INSTRUCCION_VOZ = "Responde en máximo 2 frases. Sin formato markdown."


def responder(pregunta):
    """Devuelve el texto final: la respuesta del agente correspondiente,
    o el mensaje de aclaración si CEREBRO no tiene claro a qué agente
    enrutar."""
    pregunta_para_voz = f"{pregunta}\n\n({INSTRUCCION_VOZ})"
    resultado = agente_cerebro.procesar(pregunta_para_voz)
    if resultado["aclaracion"]:
        return resultado["aclaracion"]
    return resultado["respuesta"]


def _timeout_handler(signum, frame):
    raise TimeoutError(
        f"La operación tardó más de {TIMEOUT_SEGUNDOS}s sin responder "
        f"(puede ser el acceso al Keychain o la llamada a Claude)."
    )


if __name__ == "__main__":
    texto = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not texto:
        print("ERROR: no se recibió ningún texto. Prueba de nuevo.", flush=True)
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(TIMEOUT_SEGUNDOS)
    try:
        texto_respuesta = responder(texto)
    except TimeoutError as e:
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)
    finally:
        signal.alarm(0)

    print(texto_respuesta, flush=True)
