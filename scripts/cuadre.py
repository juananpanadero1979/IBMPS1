#!/usr/bin/env python3
"""
Cuadre ONCE - Agencia 576 Getafe
Juan Antonio Panadero Jiménez

Fórmula:
Ventas = (asignados - devueltos) × precio + libros vendidos
Efectivo = Ventas - Premios - Tarjeta
"""

# Precios por producto
PRECIOS = {
    'DOM': 2.00,
    'TRI': 0.50,
    'MID': 1.00,
    'SUP': 1.00,
    'EUJ': 2.00,
    'VIE': 3.00,
    'ORD': 2.00,
}

# Valor de libros rasca según precio
def valor_libro(precio_libro):
    if precio_libro <= 2.00:
        return 100.00
    else:
        return 150.00

def calcular_cuadre(cupones, libros, premios, tarjeta):
    """
    cupones: lista de dicts {tipo, asignados, devueltos}
    libros: lista de dicts {precio}
    premios: total premios pagados en efectivo
    tarjeta: total pagos con tarjeta
    """
    # Calcular ventas de cupones
    total_cupones = 0
    detalle_cupones = []
    for c in cupones:
        tipo = c['tipo'].upper()
        vendidos = c['asignados'] - c['devueltos']
        precio = PRECIOS.get(tipo, 0)
        importe = vendidos * precio
        total_cupones += importe
        detalle_cupones.append({
            'tipo': tipo,
            'asignados': c['asignados'],
            'devueltos': c['devueltos'],
            'vendidos': vendidos,
            'precio': precio,
            'importe': importe
        })

    # Calcular ventas de libros rasca
    total_libros = 0
    for l in libros:
        total_libros += valor_libro(l['precio'])

    # Totales
    total_ventas = total_cupones + total_libros
    efectivo = total_ventas - premios - tarjeta

    return {
        'detalle_cupones': detalle_cupones,
        'total_cupones': total_cupones,
        'total_libros': total_libros,
        'total_ventas': total_ventas,
        'premios': premios,
        'tarjeta': tarjeta,
        'efectivo': efectivo
    }

def imprimir_resultado(resultado):
    print("\n" + "="*40)
    print("CUADRE ONCE - AGENCIA 576")
    print("="*40)
    print("\nDETALLE CUPONES:")
    for c in resultado['detalle_cupones']:
        if c['vendidos'] > 0:
            print(f"  {c['tipo']}: {c['asignados']}-{c['devueltos']}={c['vendidos']} × {c['precio']}€ = {c['importe']:.2f}€")
    print(f"\nTotal cupones:  {resultado['total_cupones']:.2f}€")
    print(f"Total libros:   {resultado['total_libros']:.2f}€")
    print(f"TOTAL VENTAS:   {resultado['total_ventas']:.2f}€")
    print(f"\nPremios pagados: -{resultado['premios']:.2f}€")
    print(f"Pagos tarjeta:   -{resultado['tarjeta']:.2f}€")
    print("="*40)
    print(f"EFECTIVO HOY:   {resultado['efectivo']:.2f}€")
    print("="*40 + "\n")

if __name__ == "__main__":
    # Ejemplo de uso
    print("Script cuadre.py cargado correctamente.")
    print("Usa calcular_cuadre() para calcular el cuadre del día.")
