# IBMPS1 — Sistema de Gestión Personal Juan Antonio

## Quién soy
Vendedor titular ONCE Agencia 576 Getafe (código de vendedor en Keychain/config, no en el repo)

## Estructura del proyecto
- scripts/ → código Python (cuadre, decode tickets)
- agentes/ → configuración de los 5 agentes
- datos/ → bases de datos SQLite
- config/ → configuraciones del sistema

## Reglas importantes
- Los cálculos del cuadre ONCE se hacen SIEMPRE con Python, nunca con IA
- Los datos sensibles de ONCE nunca salen al exterior
- Pedir confirmación antes de cualquier acción destructiva
- Nunca borrar archivos sin aprobación explícita

## Los 5 agentes
1. CEREBRO → orquestador, decide qué agente responde
2. ONCE → gestión ONCE: cuadre, portal, liquidaciones, stock
3. SALUD → salud personal: Zepp, analíticas, longevidad
4. INGENIERO → novedades IA y tecnología
5. BUTLER → calendario, notas, recordatorios
