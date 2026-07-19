#!/usr/bin/env python3
# DEPRECATED — sustituido por ocr_tickets.py.
# No se usa en producción. Mantener solo como referencia
# histórica. Candidato a borrar en una futura limpieza.
"""
Decode Ticket ONCE - Agencia 576 Getafe
Lee y procesa las fotos de tickets del TPV subidas a iCloud
"""

import os
import json
from datetime import datetime
from pathlib import Path

# Rutas reales en iCloud - Agencia 576 Getafe
ONCE_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/ONCE"
TICKETS_PATH = ONCE_PATH / "tickets"          # Fotos TPV subidas desde el iPhone
LIQUIDACIONES_PATH = ONCE_PATH / "liquidaciones"  # Liquidaciones descargadas del portal
NOMINAS_PATH = ONCE_PATH                      # Las nóminas están sueltas en la raíz de ONCE

def listar_tickets_pendientes():
    """Lista los tickets de fotos pendientes de procesar"""
    if not TICKETS_PATH.exists():
        print(f"⚠️  Carpeta de tickets no encontrada: {TICKETS_PATH}")
        return []

    tickets = []
    extensiones = ['.jpg', '.jpeg', '.png', '.heic']

    for archivo in sorted(TICKETS_PATH.iterdir()):
        if archivo.suffix.lower() in extensiones:
            tickets.append({
                'nombre': archivo.name,
                'ruta': str(archivo),
                'fecha_modificacion': datetime.fromtimestamp(
                    archivo.stat().st_mtime
                ).strftime('%Y-%m-%d %H:%M')
            })

    return tickets

def procesar_tickets_del_dia(fecha=None):
    """
    Muestra los tickets pendientes del día para procesarlos
    fecha: string en formato YYYY-MM-DD (por defecto hoy)
    """
    if fecha is None:
        fecha = datetime.now().strftime('%Y-%m-%d')

    tickets = listar_tickets_pendientes()

    if not tickets:
        print(f"📭 No hay tickets pendientes en: {TICKETS_PATH}")
        return

    print(f"\n📋 TICKETS PENDIENTES ({len(tickets)} fotos encontradas)")
    print(f"📁 Carpeta: {TICKETS_PATH}")
    print("="*50)

    for i, t in enumerate(tickets, 1):
        print(f"{i}. {t['nombre']} ({t['fecha_modificacion']})")

    print("="*50)
    print(f"✅ Total: {len(tickets)} tickets por procesar")
    return tickets

def verificar_carpeta_once():
    """Verifica que existe la estructura real de carpetas ONCE en iCloud"""
    carpetas = [ONCE_PATH, TICKETS_PATH, LIQUIDACIONES_PATH]

    for carpeta in carpetas:
        if not carpeta.exists():
            carpeta.mkdir(parents=True, exist_ok=True)
            print(f"✅ Carpeta creada: {carpeta.name}")
        else:
            print(f"✅ Carpeta existe: {carpeta.name}")

    print(f"ℹ️  Las nóminas están sueltas en la raíz de: {NOMINAS_PATH}")

if __name__ == "__main__":
    print("\n🎰 DECODE TICKET ONCE - Agencia 576")
    print("="*50)
    print("\nVerificando estructura de carpetas ONCE en iCloud...")
    verificar_carpeta_once()
    print("\nBuscando tickets pendientes...")
    procesar_tickets_del_dia()
