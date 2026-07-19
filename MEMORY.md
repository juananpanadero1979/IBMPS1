# MEMORY.md — IBMPS1

Snapshot del estado del proyecto. Se actualiza a medida que se cierran o abren temas — no es un registro histórico completo, ver `git log` para eso. Ver `CLAUDE.md` para reglas y mapa de rutas fijos que no cambian sesión a sesión.

Última actualización: 2026-07-11

## Cerrado hoy
- **Liquidación con desglose completo**: selector corregido de `#ctl00_ContentPlaceHolder1` (no existía como nodo real del DOM — un `<asp:ContentPlaceHolder>` no genera envoltorio propio) a `#ctl00_ContentPlaceHolder1_dvContenido` en `portal_once.py:descargar_liquidacion_diaria()`. Ahora `detalle_completo` captura 8-10 tablas reales (gvProductosCupon, gvProductosActivos, gvInstantanea, gvPagoPremios, gvPagoTarjeta, gvTWYP, gvIngresoCuenta, gvTotalVentas) en vez de salir siempre vacío.
- **Fila TOPii con etiquetas correctas**: `_formatear_fila_liquidacion()` (duplicada en `informe_manana.py` y `asistente.py`) reconoce el caso especial de `gvTWYP` (cabecera real "TOPii"/"VENTA PROD ONCE"/"RETIRADA", sin clave "IMPORTE") y lo formatea como "TOPii — Venta prod. ONCE: X, Retirada: Y" en vez de concatenar valores sin contexto.
- **Cuadre diario integrado en el cron automático**: `cuadre_diario.py` añadido a `run_diario.sh` (ambas copias), entre `ocr_tickets.py` e `informe_manana.py`.
- **OCR tickets solo procesa `DDMMAA.pdf`**: `ocr_tickets.py` filtra por patrón de nombre antes de procesar, ignora explícitamente cualquier otro archivo que se cuele en `Tickets/`.
- **Devolución libros — esquema dual**: `ocr_devolucion_libros.py` reconoce los dos tipos de documento reales de la carpeta `DEVOLUCIÓN LIBROS/` (`aviso_inicio` = finalización venta voluntaria, `retorno_completado` = informe retorno de libros), con `libros_devueltos` normalizado igual en ambos casos para que `asistente.py` no tenga que distinguirlos.
- **Comunicaciones TPV — confirmado funcionando**: `ocr_comunicaciones.py` ya extraía bien fecha/tipo/fechas mencionadas; verificado contra un PDF real.
- **Stock total con conteo Python precalculado**: `_contexto_almacen()` en `asistente.py` añade "sin vender de X total" por producto y un TOTAL GENERAL sumado en Python, para que el modelo no tenga que contar/sumar él mismo (antes decía "48 libros" cuando el real era 46).
- **RESPUESTA DIRECTA con números de cupón agrupados por fecha**: `_contexto_paquetes_detalle()` antepone al contexto una línea con la cuenta y los números concretos ya agrupados por fecha cuando la pregunta nombra un producto (p.ej. Cuponazo) — el modelo dejaba fuera fechas/números de forma inconsistente incluso con instrucciones explícitas en el prompt; con los números ya listados en Python, deja de fallar.
- **Detección dinámica de nombres de producto**: `_necesita_detalle_paquetes()` ahora también dispara el contexto detallado si la pregunta menciona el nombre de un producto real de hoy (p.ej. "cuponazo"), no solo palabras genéricas ("cupón", "detalle"...) — evita respuestas vacías/erróneas cuando se pregunta por el nombre del producto directamente.
- **`ocr_comunicaciones.py` y `ocr_devolucion_libros.py` integrados en el cron de las 08:00**: añadidos a `run_diario.sh` (ambas copias) justo después de `ocr_tickets.py`, ya no hace falta ejecutarlos a mano.
- **4 archivos legacy marcados como deprecated** (comentario `# DEPRECATED` al inicio, sin borrar): `ibmps1.py`, `agente_cerebro.py`, `decode_ticket.py`, `cuadre.py`.
- Fase 1 (mapa de dependencias) y Fase 2 (`CLAUDE.md` + `MEMORY.md` raíz, patrón Jeff Su) completadas.

## Pendiente
- **HomeBridge no arranca solo tras reiniciar el Mac**: `hb-service restart` falla, `hb-service run` sí funciona (ejecutado a mano). Sin diagnosticar la causa raíz todavía.
- **Gemini API — cuota en 0** en Google Cloud Console. Bloquea cualquier uso de Gemini hasta resolverlo.
- **`_contexto_agenda()` tarda ~14s** (consulta Calendario/Recordatorios vía AppleScript/EventKit) — pendiente de optimizar, penaliza la latencia de `asistente.py` cuando se pregunta por agenda.
- **Cámara C210 — lag/calidad**: sin confirmar si la optimización pendiente se llegó a aplicar.
- **Nombres de dispositivos HomeKit sin simplificar**.
- **Stock tipo contable**: aparcado a propósito, no es prioridad ahora mismo.

## Decisiones tomadas
- **`headless=False` obligatorio en Playwright** para todo lo que toque el portal ONCE — el portal bloquea/no renderiza el login en modo headless. Ver comentarios en `portal_once.py` (`ejecutar_descarga_diaria()`, `ejecutar_descarga_mensual()`).
- **Graphify (paquete `graphifyy` en PyPI) — NO se instala**: identidad del mantenedor inconsistente entre fuentes (PyPI apunta a `safishamsi/graphify`, el propio README se atribuye a la organización "Graphify-Labs"), cifras de estrellas contradictorias según la fuente consultada (3.700+ vs 82.200), y texto del README que responde de forma sospechosamente preventiva a las preguntas de seguridad que haría un agente de IA antes de instalar algo ("no telemetry... nothing leaves your machine"). El mapeo de dependencias se hizo manualmente en su lugar (grep + lectura de código) con el mismo resultado, sin ese riesgo.
- **Firecrawl, Scrapling/StealthyFetcher y Ruflo descartados** por el mismo tipo de motivo: herramientas de terceros sin verificar, o innecesarias para el caso de uso real del proyecto.
