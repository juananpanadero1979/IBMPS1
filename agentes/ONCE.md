# Dominio ONCE — Agencia 576 Getafe

Reglas compartidas (modo voz, no mezclar dominios, Python calcula/la IA nunca calcula, seguridad) → ver `CLAUDE.md` raíz. Aquí solo lo específico de este dominio.

## Responsabilidades
- Cuadre diario de ventas
- Liquidaciones descargadas del portal (importe a liquidar, saldo acreedor aplicado, desglose por producto)
- Control de stock: cupones, libros rasca, series, numeraciones
- Alertas de caducidad de cupones y libros
- Seguimiento de paquetes previstos / a retirar / retirados
- Análisis de nóminas
- Consejos de venta basados en datos reales

## Cómo se calcula el cuadre (real, `cuadre_diario.py`)
1. Si el ticket TPV de ayer (`ocr_tickets.py`) trae `resumen.resultado` (se pudo leer la página RESUMEN ACTIVIDAD DIARIA), ese valor se usa DIRECTAMENTE como EFECTIVO — ya es el cálculo completo del propio TPV, e incluye dentro de "PAGOS" tanto premios de cupones como de rasca.
2. Si esa página no existe en el ticket, se usa la fórmula de respaldo: `EFECTIVO = ventas.total − devoluciones.total − premios_pagados.total − pagos_tarjeta` (sin sumar `pagos_rasca` aparte, para no contarlo dos veces).
3. Nunca se calcula sumando cupones × precio por producto a mano — esa fórmula antigua vivía en `cuadre.py`, ahora deprecado (ver `CLAUDE.md`).

## Productos (código → precio unitario del cupón)
DOM (2€), TRI (0,50€), MID (1€), SUP (1€), EUJ (2€), VIE (3€), ORD (2€).

## Liquidación diaria — qué significa cada dato
"IMPORTE A LIQUIDAR" es el importe real a pagar/ingresar a ONCE. "SALDO ACREEDOR APLICADO" no es una alternativa — es un saldo a favor YA descontado dentro de ese importe (lo dice el propio informe del portal: "...incluyendo el saldo deudor/acreedor aplicado en este día"). Muestra siempre ambos si los dos existen, nunca solo uno.
