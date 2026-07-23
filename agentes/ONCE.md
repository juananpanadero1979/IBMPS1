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
1. Si el ticket TPV del último día trabajado (`ocr_tickets.py`) trae `resumen.resultado` (se pudo leer la página RESUMEN ACTIVIDAD DIARIA), ese valor se usa DIRECTAMENTE como RESULTADO DEL DÍA — ya es el cálculo completo del propio TPV, e incluye dentro de "PAGOS" tanto premios de cupones como de rasca. NO es efectivo/caja física (ver docstring de `cuadre_diario.py`).
2. Si esa página no existe en el ticket, se usa la fórmula de respaldo: `RESULTADO DEL DÍA = ventas.total − premios_pagados.total − pagos_rasca.total`. `devoluciones.total` y `pagos_tarjeta` NO entran en este cálculo (se muestran aparte, informativos) — verificado campo a campo el 2026-07-23 contra los 26 tickets fiables de 2026: esta fórmula cuadra exacto (diferencia 0,00€) en el 100% de los casos con página RESUMEN legible. La fórmula anterior (que sí restaba devoluciones y tarjeta, y omitía rasca) era errónea — ver commit de esa fecha.
3. Un ticket puede llevar `"ocr_revision_pendiente": true` en su JSON cuando la página RESUMEN salió mal transcrita por OCR (típicamente `resumen.ventas_total` en `0.0`/`null` con el valor real desplazado a `resumen.pagos_total`). En ese caso `cuadre_diario.py` avisa explícitamente y NO da un veredicto de verificación — ese ticket necesita reprocesarse, no se debe dar por bueno.
4. Nunca se calcula sumando cupones × precio por producto a mano — esa fórmula antigua vivía en `cuadre.py`, ahora deprecado (ver `CLAUDE.md`).

## Productos (código → precio unitario del cupón)
DOM (2€), TRI (0,50€), MID (1€), SUP (1€), EUJ (2€), VIE (3€), ORD (2€).

## Liquidación diaria — qué significa cada dato
"IMPORTE A LIQUIDAR" es el importe real a pagar/ingresar a ONCE. "SALDO ACREEDOR APLICADO" no es una alternativa — es un saldo a favor YA descontado dentro de ese importe (lo dice el propio informe del portal: "...incluyendo el saldo deudor/acreedor aplicado en este día"). Muestra siempre ambos si los dos existen, nunca solo uno.
