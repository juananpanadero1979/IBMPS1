# IBMPS1 — Sistema de Gestión Personal
Juan Antonio Panadero Jiménez — Vendedor titular ONCE Agencia 576 Getafe (código de vendedor en Keychain/config, no en el repo).

## Regla de seguridad (una sola vez, aplica a todo el repo)
- Los datos de ONCE (ventas, liquidaciones, cupones, código de vendedor) nunca salen del ordenador local ni se envían a servicios externos no autorizados.
- Cualquier acción destructiva (borrar archivos, mover datos, instalar/actualizar dependencias) requiere confirmación explícita — nunca por defecto.
- No instalar paquetes de terceros sin verificar identidad del mantenedor y coherencia de sus fuentes primero (repo real, no solo lo que dice su propio README).
- Credenciales SIEMPRE en el Keychain de macOS (ver tabla abajo), nunca en texto plano en el repo.

## Filosofía: Python calcula, la IA nunca calcula
Regla fundamental del proyecto: cualquier cifra de dinero, cuadre o cómputo numérico se calcula en Python con reglas explícitas y verificables — nunca se le pide al modelo que sume, cuente o calcule por sí mismo. La IA solo (a) transcribe lo que ve en un documento (OCR de tickets/avisos), o (b) interpreta/explica en lenguaje natural un resultado que Python ya calculó. Cuando `asistente.py` necesita que el modelo no se equivoque contando o sumando una lista, se lo da precalculado en el propio contexto (bloques "RESUMEN"/"RESPUESTA DIRECTA" en `_contexto_almacen()` y `_contexto_paquetes_detalle()`).

## Estructura real del proyecto
- `scripts/` → todo el código Python.
- `agentes/` → contexto de dominio en Markdown que `asistente.py` inyecta en su system prompt (ver mapa abajo).
- `config/` → `portal_once.json` (usuario), `selectores_once.json` (selectores del portal), `html_backup/`.
- `informes/` → PDFs diarios generados por `informe_manana.py`.
- `datos/` → **vacía, sin uso actual**. No hay base de datos SQLite pese a lo que decía una versión anterior de este archivo — todo el almacenamiento real es JSON por archivo (uno por fecha/concepto), bajo `~/Library/Mobile Documents/com~apple~CloudDocs/ONCE/`.

## Mapa de rutas (quién llama a quién)

### Automático — LaunchAgent `com.ibmps1.informe`, 08:00 diario
```
run_diario.sh → portal_once.py → capturar_html_portal.py → parsear_html_portal.py
              → ocr_tickets.py → ocr_comunicaciones.py → ocr_devolucion_libros.py
              → cuadre_diario.py → informe_manana.py
```
Ejecuta la copia en `~/IBMPS1_scripts/run_diario.sh` (fuera de iCloud; launchd no puede lanzar scripts alojados ahí) — mantenerla sincronizada con `scripts/run_diario.sh` tras cualquier cambio.

### Bajo demanda — voz (Atajo de Siri)
`siri_query.sh` → `asistente.py "<pregunta>"` (Claude Sonnet 5, clave en Keychain `IBMPS1-ClaudeAPI`).

### Deprecados (no borrar sin confirmación explícita, no editar salvo para eso)
`ibmps1.py`, `agente_cerebro.py`, `decode_ticket.py`, `cuadre.py`, y `agentes/CEREBRO.md` — sustituidos por `asistente.py` / `ocr_tickets.py` / `cuadre_diario.py`. Cada archivo tiene su propio comentario `DEPRECATED` al inicio.

## Servicios de Keychain
| Servicio | Cuenta | Para qué |
|---|---|---|
| `IBMPS1-PortalONCE` | usuario del vendedor | login portal.once.es + clave segura Gestiona |
| `IBMPS1-ClaudeAPI` | `ANTHROPIC_API_KEY` | Claude Sonnet 5 (asistente + OCR) |
| `NVIDIA` | `API_KEY` | fallback de visión si Claude falla |

## Referencias (no dupliques su contenido aquí)
- Contexto de dominio (fórmula real de cuadre, protocolo de salud, stack tecnológico, agenda) → `agentes/ONCE.md`, `agentes/SALUD.md`, `agentes/INGENIERO.md`, `agentes/BUTLER.md`.
- Snapshot del estado actual del proyecto (qué está cerrado, qué queda pendiente) → `MEMORY.md`.
- Qué hace cada script y qué JSON produce/consume → docstring al inicio de cada archivo en `scripts/`.

## Reglas duras (no las repitas en `agentes/*.md`, ya están aquí)
- Modo voz (Siri): respuestas de 2-3 frases máximo, sin markdown — ya aplicado en `PLANTILLA_SYSTEM_PROMPT` de `asistente.py`.
- No mezclar dominios (ONCE/SALUD/INGENIERO/BUTLER) en una misma respuesta salvo que se pregunte explícitamente por más de uno.

## Reglas de Harness
Cada bug real encontrado en `asistente.py` (u otro script que responda preguntas) se convierte en una regla aquí, no solo en el parche puntual que lo arregla — así el mismo tipo de fallo no vuelve a aparecer con otro producto, periodo o dato que todavía no se haya cubierto explícitamente (método de Mitchell Hashimoto: cada error del agente es una regla estructural, no un caso aislado).

- **REGLA — Agregaciones temporales/por producto:** cualquier pregunta que combine un producto (cupón, rasca, Eurojackpot, Triplex, etc.) con un periodo de tiempo ("esta semana", "este mes", "cuánto llevo de X") DEBE resolverse sumando en Python los datos ya existentes en los tickets/JSON procesados del rango de fechas correspondiente. NUNCA se debe responder "consulta el portal" ni remitir al usuario a buscarlo él mismo si el dato existe en los archivos locales. Si el dato realmente no existe (faltan tickets de ese rango sin procesar), decirlo explícitamente: "faltan los tickets de tal fecha" — nunca fallar en silencio ni desviar la pregunta.
  - Origen: bug real detectado el 2026-07-16 — "cuánto Eurojackpot llevo vendido esta semana" respondía "consulta el portal" en vez de sumar `ventas.detalle` de los tickets ya procesados por `ocr_tickets.py`.
  - Implementación de referencia: `scripts/ventas_producto_periodo.py` (suma pura en Python, con `dias_sin_ticket` explícito para los huecos) + detección producto/periodo en `scripts/asistente.py` (`_necesita_ventas_producto`, `_contexto_ventas_producto`) + refuerzo en `PLANTILLA_SYSTEM_PROMPT`. Al añadir un producto o periodo nuevo, extender esa implementación en vez de crear un mecanismo paralelo.
