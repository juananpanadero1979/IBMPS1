#!/bin/bash
# Ejecución diaria automática - Portal ONCE
# Agencia 576 Getafe - Juan Antonio Panadero Jiménez
#
# ⚠️ Esta copia (en iCloud Drive) es solo la fuente versionada en git.
# El LaunchAgent com.ibmps1.informe NO ejecuta este archivo: macOS no
# permite que launchd lance scripts alojados en iCloud Drive. La copia
# que de verdad se ejecuta cada mañana vive en
# ~/IBMPS1_scripts/run_diario.sh (fuera de iCloud) — si cambias algo
# aquí, copia el archivo también allí (o vuelve a ejecutar el cp).
#
# Invocado por el LaunchAgent com.ibmps1.informe
# (~/Library/LaunchAgents/com.ibmps1.informe.plist) cada mañana a las
# 07:00. No se ejecuta manualmente salvo para pruebas.
#
# Cada paso se ejecuta aunque el anterior falle (sin "set -e"): si
# portal_once.py no descarga algo, parsear_html_portal.py simplemente
# omite ese archivo, e informe_manana.py ya está preparado para mostrar
# "sin datos descargados hoy" en las secciones que falten.
set -u

PYTHON3="/Library/Developer/CommandLineTools/usr/bin/python3"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPTS_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') - inicio informe diario ====="

echo "--- portal_once.py ---"
"$PYTHON3" portal_once.py
echo "portal_once.py salida: $?"

echo "--- capturar_html_portal.py ---"
"$PYTHON3" capturar_html_portal.py
echo "capturar_html_portal.py salida: $?"

echo "--- parsear_html_portal.py ---"
"$PYTHON3" parsear_html_portal.py
echo "parsear_html_portal.py salida: $?"

echo "--- informe_manana.py ---"
"$PYTHON3" informe_manana.py
echo "informe_manana.py salida: $?"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') - fin informe diario ====="
