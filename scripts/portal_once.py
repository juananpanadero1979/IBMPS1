#!/usr/bin/env python3
"""
Portal ONCE - Descarga automática diaria
Agencia 576 Getafe - Juan Antonio Panadero Jiménez

ejecutar_descarga_diaria() — cada mañana:
- Control de retirada (previsto / a retirar / retirado)
- Control de almacén de lotería instantánea
- Consulta de premios repartidos (activa / instantánea / pasiva)
- Comisiones (+ detalle de ventas)
- Estadísticas de venta
- Liquidación
- Consulta de incidencias
- Consulta de registro de jornada
- Consulta de solicitudes

ejecutar_descarga_mensual() — solo el día 5 de cada mes:
- Nóminas (todos los años/meses/períodos disponibles)

Simulación de comisiones, informes de ingresos por fecha/producto y otros
documentos tienen función propia pero no están conectados a ninguna de las
dos orquestaciones anteriores (llamar manualmente si se necesitan).

Credenciales: se leen del Keychain de macOS (nunca en texto plano en este archivo).
Configuración inicial: ejecutar `python3 portal_once.py --setup` una vez para
guardar el usuario en config/portal_once.json y la contraseña en el Keychain.

Selectores reales capturados con scripts/grabar_portal.py (ver
config/selectores_once.json). Los puntos marcados con TODO son secciones
para las que aún no se ha capturado el selector exacto del botón de
descarga/exportación dentro de la página de detalle.

⚠️ SOLO LECTURA: este script únicamente navega, filtra (año/mes/centro/
producto) y consulta/descarga. Nunca rellena campos de importe ni pulsa
botones que envíen, confirmen o guarden datos en el portal (ver
BOTONES_PROHIBIDOS). No accede a "Ingresos a cuenta producto".
"""

import json
import random
import re
import subprocess
import sys
import time
import getpass
from datetime import datetime, timedelta
from pathlib import Path

import keyring
from playwright.sync_api import sync_playwright

# ── Rutas ─────────────────────────────────────────────────────────
ONCE_PATH = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/ONCE"
LIQUIDACIONES_PATH = ONCE_PATH / "liquidaciones"
NOMINAS_PATH = ONCE_PATH  # las nóminas están sueltas en la raíz de ONCE

# TODO: confirmar con Juan Antonio si estas rutas son las definitivas.
STOCK_PATH = ONCE_PATH / "stock"  # control de almacén de lotería instantánea
PAQUETES_PATH = ONCE_PATH / "paquetes_previstos"  # control de retirada
ESTADISTICAS_PATH = ONCE_PATH / "estadisticas_venta"
PREMIOS_PATH = ONCE_PATH / "premios_repartidos"
COMISIONES_PATH = ONCE_PATH / "comisiones"
SIMULACION_COMISIONES_PATH = ONCE_PATH / "simulacion_comisiones"
INFORMES_LIQUIDACION_PATH = ONCE_PATH / "informes_liquidacion"
OTROS_DOCUMENTOS_PATH = ONCE_PATH / "otros_documentos"
CONSULTAS_PATH = ONCE_PATH / "consultas"

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "portal_once.json"

# ── Credenciales (Keychain) ──────────────────────────────────────
KEYCHAIN_SERVICE = "IBMPS1-PortalONCE"
KEYCHAIN_CLAVE_SEGURA_ACCOUNT = "ONCE_CLAVE_SEGURA"

PORTAL_URL = "https://portal.once.es/empleado/acl_users/credentials_cookie_auth/require_login?came_from=https://portal.once.es/empleado/"

# Portal Gestiona (nóminas)
GESTIONA_URL = "https://portal.once.es/GestionLaboralNomina/Bienvenido.aspx"
GESTIONA_BASE = "https://portal.once.es/GestionLaboralNomina/"

# URLs reales descubiertas en el log de navegación (navegación directa,
# sin pasar por los menús)
URL_LIQUIDACION_DIARIA = GESTIONA_BASE + "Vendedores/IngresoACuenta/InformeLiquidacionDiaria.aspx"
URL_CONTROL_PAQUETE_PREVISTO = GESTIONA_BASE + "JuegosONCE/ControlPaquete.aspx?tipoPaq=PREVISTO"
URL_CONTROL_PAQUETE_A_RETIRAR = GESTIONA_BASE + "JuegosONCE/ControlPaquete.aspx?tipoPaq=A%20RETIRAR"
URL_CONTROL_PAQUETE_RETIRADO = GESTIONA_BASE + "JuegosONCE/ControlPaquete.aspx?tipoPaq=RETIRADO"
URL_ALMACEN_RASCAS = GESTIONA_BASE + "JuegosONCE/Almacen.aspx"
URL_PREMIOS_ACTIVA = GESTIONA_BASE + "JuegosONCE/Premios.aspx?tipoLot=LOTERIA%20ACTIVA"
URL_PREMIOS_INSTANTANEA = GESTIONA_BASE + "JuegosONCE/Premios.aspx?tipoLot=LOTERIA%20INSTANTANEA"
URL_PREMIOS_PASIVA = GESTIONA_BASE + "JuegosONCE/Premios.aspx?tipoLot=LOTERIA%20PASIVA"
URL_COMISIONES = GESTIONA_BASE + "Vendedores/comisiones.aspx"
URL_SIMULACION_COMISIONES = GESTIONA_BASE + "Vendedores/ConsultaSimulacion.aspx"
URL_ESTADISTICAS_VENTA = GESTIONA_BASE + "Vendedores/estadisticasventa.aspx"
URL_INFORME_INGRESOS_FECHA = GESTIONA_BASE + "Vendedores/IngresoACuenta/InformesLiquidacion/InformeIngresosPorFechas.aspx"
URL_INFORME_INGRESOS_PRODUCTO = GESTIONA_BASE + "Vendedores/IngresoACuenta/InformesLiquidacion/InformeIngresosPorProducto.aspx"
URL_OTROS_DOCUMENTOS = GESTIONA_BASE + "OtrosDocumentos/OtrosDocumentos.aspx"
URL_CONSULTA_INCIDENCIAS = GESTIONA_BASE + "Consultas/ResumenIncidencias.aspx"
URL_CONSULTA_REGISTRO_JORNADA = GESTIONA_BASE + "Consultas/ConsultaMarcajes.aspx"
URL_CONSULTA_SOLICITUDES = GESTIONA_BASE + "Consultas/consultasolicitudes.aspx"

# ── Selectores reales (capturados con grabar_portal.py) ────────────

# LOGIN (portal.once.es/empleado)
USUARIO_INPUT = "#__ac_name"
PASSWORD_INPUT = "#__ac_password"
BOTON_ENTRAR = '[name="submit"]'
COOKIE_ACEPTAR = "#CybotCookiebotDialogBodyButtonAllowAll"

# GESTIONA (clave segura)
PASSWORD_GESTIONA = "#ctl00_ContentPlaceHolder1_txtPassword"
BOTON_ENTRAR_GESTIONA = "#ctl00_ContentPlaceHolder1_btnEntrar"

# OFICINA VIRTUAL VENDEDOR
MENU_OFICINA_VIRTUAL = "#lnkMenu_1_Oficina-virtual-vendedor"
MENU_LIQUIDACION = "#lnkMenu_2_Liquidación"
MENU_INFORME_LIQUIDACION_DIARIA = "#lnkMenu_3_Informe-liquidación-diaria"
MENU_CONTROL_RETIRADA = "#lnkMenu_2_Control-de-Retirada"
MENU_CONTROL_ALMACEN = "#lnkMenu_2_Control-de-almacén-de-lotería-instantánea"
MENU_ESTADISTICAS_VENTA = "#lnkMenu_2_Estadísticas-de-venta"
MENU_CONSULTA_PREMIOS = "#lnkMenu_2_Consulta-de-premios-repartidos"
MENU_COMISIONES = "#lnkMenu_2_Comisiones"
BOTON_CONSULTAR_COMISIONES = "#ctl00_ContentPlaceHolder1_btConsultar"

# NÓMINA
MENU_NOMINA = "#lnkMenu_1_Nómina"
SELECT_ANIO = "#ctl00_ContentPlaceHolder1_ddlAnio"
SELECT_MES = "#ctl00_ContentPlaceHolder1_ddlMes"
SELECT_PERIODO = "#ctl00_ContentPlaceHolder1_ddlPeriodo"
BOTON_VER_NOMINA = "#ctl00_ContentPlaceHolder1_btnVer"
BOTON_VER_COMISIONES = "#ctl00_ContentPlaceHolder1_btnVerDetalleComisiones"

# CONSULTAS
MENU_CONSULTAS = "#lnkMenu_1_Consultas"
MENU_CONSULTA_INCIDENCIAS = "#lnkMenu_2_Consulta-de-incidencias"
MENU_CONSULTA_SOLICITUDES = "#lnkMenu_2_Consulta-de-solicitudes"
MENU_CONSULTA_CERTIFICADO_IRPF = "#lnkMenu_2_Consulta-del-certificado-de-haberes-y-retenciones-(IRPF)"

def cargar_usuario():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No existe {CONFIG_PATH}. Ejecuta primero: python3 portal_once.py --setup"
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)["usuario"]


def _leer_keychain(servicio, cuenta):
    """Lee una clave del Keychain de macOS invocando el comando `security`
    directamente, en vez de la librería keyring: keyring falla con el
    error -25308 (errSecInteractionNotAllowed) cuando se ejecuta desde
    una sesión sin interfaz gráfica (p.ej. por SSH) — `security` sí puede
    leer del Keychain de login ya desbloqueado en esos casos. (La
    escritura en setup_credenciales() sigue con keyring — ese flujo es
    siempre interactivo, nunca se ejecuta por SSH.)"""
    resultado = subprocess.run(
        ["security", "find-generic-password", "-s", servicio, "-a", cuenta, "-w"],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        return None
    return resultado.stdout.strip()


def obtener_password(usuario):
    password = _leer_keychain(KEYCHAIN_SERVICE, usuario)
    if not password:
        raise RuntimeError(
            f"No hay contraseña guardada en el Keychain para '{usuario}'. "
            "Ejecuta: python3 portal_once.py --setup"
        )
    return password


def obtener_clave_segura():
    clave_segura = _leer_keychain(KEYCHAIN_SERVICE, KEYCHAIN_CLAVE_SEGURA_ACCOUNT)
    if not clave_segura:
        raise RuntimeError(
            "No hay clave segura de Gestiona guardada en el Keychain. "
            "Ejecuta: python3 portal_once.py --setup"
        )
    return clave_segura


def setup_credenciales():
    """Guarda el usuario (config/portal_once.json), la contraseña y la
    clave segura de Gestiona (Keychain)."""
    print("\n🔐 CONFIGURACIÓN DE CREDENCIALES - Portal ONCE")
    usuario = input("Usuario / código vendedor: ").strip()
    password = getpass.getpass("Contraseña (no se mostrará en pantalla): ")

    clave_segura = getpass.getpass("Clave segura de Gestiona, 8 caracteres (no se mostrará en pantalla): ")
    while len(clave_segura) != 8:
        print(f"⚠️  La clave segura debe tener 8 caracteres (has introducido {len(clave_segura)}).")
        clave_segura = getpass.getpass("Clave segura de Gestiona, 8 caracteres: ")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump({"usuario": usuario}, f, indent=2)

    keyring.set_password(KEYCHAIN_SERVICE, usuario, password)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_CLAVE_SEGURA_ACCOUNT, clave_segura)
    print(f"✅ Usuario guardado en {CONFIG_PATH}")
    print(f"✅ Contraseña guardada en el Keychain de macOS (servicio: {KEYCHAIN_SERVICE})")
    print(f"✅ Clave segura de Gestiona guardada en el Keychain de macOS ({KEYCHAIN_CLAVE_SEGURA_ACCOUNT})")


# ── Login ─────────────────────────────────────────────────────────

def iniciar_sesion(page, usuario, password, clave_segura):
    """Inicia sesión en el portal público y encadena el acceso a Gestiona
    (que pide la clave segura de 8 caracteres antes de dar paso a Bienvenido.aspx)."""
    page.goto(PORTAL_URL)
    page.wait_for_load_state("networkidle")

    try:
        page.click(COOKIE_ACEPTAR, timeout=5000)
        page.wait_for_load_state("networkidle")
    except Exception:
        pass  # Las cookies ya fueron aceptadas antes, continuar

    print(f"URL actual: {page.url}")
    print(f"Título: {page.title()}")
    page.fill(USUARIO_INPUT, usuario, timeout=60000)
    page.fill(PASSWORD_INPUT, password, timeout=60000)
    page.click(BOTON_ENTRAR)
    page.wait_for_load_state("networkidle")

    page.locator("#enlacesConTextoViewlet").get_by_role("link", name="Gestiona").click()
    page.wait_for_load_state("networkidle")
    print(f"URL tras click Gestiona: {page.url}")

    page.fill(PASSWORD_GESTIONA, clave_segura, timeout=60000)
    page.click(BOTON_ENTRAR_GESTIONA)
    page.wait_for_load_state("networkidle")


def ir_a_gestiona(page):
    page.goto(GESTIONA_URL)
    page.wait_for_load_state("networkidle")


def click_menu(page, selector):
    page.click(selector)
    page.wait_for_load_state("networkidle")


def esperar_postback(page, timeout=10000):
    """Espera a que termine un postback ASP.NET tras cambiar un <select>.
    Algunos UpdatePanel no disparan un networkidle claro, así que si el
    timeout salta se usa una espera fija como fallback (mismo patrón que
    seleccionar_todos_mas_reciente())."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        page.wait_for_timeout(2000)


def click_menu_con_reintento(page, selector, intentos=2):
    """Como click_menu, pero si el clic da timeout recarga Gestiona y
    reintenta una vez: en descargar_nominas() el menú puede colgarse de
    forma intermitente tras muchas descargas seguidas en la misma sesión."""
    for intento in range(intentos):
        try:
            click_menu(page, selector)
            return
        except Exception:
            if intento == intentos - 1:
                raise
            ir_a_gestiona(page)


def navegar(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")


def seleccionar_todos_mas_reciente(page):
    """Selecciona en cada <select> de la página la primera opción: los
    desplegables del portal (año, mes, período...) listan siempre de más
    reciente a más antiguo, así que el índice 0 es el más reciente."""
    ids = page.eval_on_selector_all("select[id]", "els => els.map(e => e.id)")
    for id_ in ids:
        selector = f"#{id_}"
        if not page.is_visible(selector) or not page.is_enabled(selector):
            continue  # algunas páginas ocultan/deshabilitan selects condicionales tras un postback previo
        opciones = opciones_select(page, selector)
        if opciones:
            page.select_option(selector, value=opciones[0]["value"])
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_timeout(2000)  # el postback ASP.NET no siempre dispara networkidle


# Este script es de solo lectura: nunca debe enviar, confirmar ni guardar
# datos en el portal, solo consultar y descargar. Este bloqueo hace que
# cualquier intento futuro de pulsar uno de estos botones falle de forma
# explícita en vez de ejecutarse.
BOTONES_PROHIBIDOS = ("enviar", "confirmar", "guardar")


def clic_boton_texto(page, texto, timeout=5000):
    if texto.strip().lower() in BOTONES_PROHIBIDOS:
        raise ValueError(
            f"Botón '{texto}' bloqueado: este script es de solo lectura y nunca "
            "debe pulsar botones que envíen, confirmen o guarden datos en el portal."
        )
    page.get_by_role("button", name=texto).click(timeout=timeout)
    page.wait_for_load_state("networkidle")


def opciones_select(page, selector):
    """Devuelve las opciones actuales de un <select> como [{"value", "label"}, ...]."""
    return page.eval_on_selector_all(
        f"{selector} option",
        "opts => opts.map(o => ({value: o.value, label: o.textContent.trim()}))",
    )


def nombre_archivo_seguro(texto):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", texto).strip("_")


def extraer_tabla(page, selector="table"):
    """Extrae la tabla indicada por `selector` (por defecto la primera
    tabla visible de la página) como lista de diccionarios (cabecera de
    la tabla como claves). Genérico: no depende de columnas fijas.

    Algunas páginas (control de retirada, control de almacén) anidan una
    subtabla de detalle oculta dentro de una celda de cada fila (patrón
    "resumen + detalle expandible" de ASP.NET GridView). querySelectorAll
    es recursivo, así que hay que limitarse a las filas y celdas propias de
    la tabla exterior — si no, las filas/celdas de la subtabla anidada se
    mezclan con las de la fila resumen real y todo sale desalineado.

    Otras páginas (informe de liquidación diaria) tienen varias tablas
    independientes (una por concepto: liquidación, saldo acreedor, pago
    de premios...) que solo se renderizan si tienen datos ese día — de
    ahí que se pueda pedir una tabla concreta por su id en vez de
    conformarse con "la primera"."""
    return page.evaluate(
        r"""
        (selector) => {
            const tabla = document.querySelector(selector);
            if (!tabla) return [];
            const filas = Array.from(tabla.querySelectorAll(':scope > tbody > tr, :scope > tr'));
            if (filas.length === 0) return [];

            const textoLimpio = (celda) => {
                const clon = celda.cloneNode(true);
                clon.querySelectorAll('table').forEach(t => t.remove());
                return clon.textContent.replace(/\s+/g, ' ').trim();
            };
            const celdasPropias = (fila) => Array.from(fila.children).filter(
                el => el.tagName === 'TD' || el.tagName === 'TH'
            );

            const cabecera = celdasPropias(filas[0]).map(textoLimpio);
            return filas.slice(1).map(fila => {
                const celdas = celdasPropias(fila).map(textoLimpio);
                const obj = {};
                cabecera.forEach((nombre, i) => { obj[nombre || `col_${i}`] = celdas[i] ?? null; });
                return obj;
            });
        }
        """,
        selector,
    )


def extraer_todas_tablas(page, selector="table"):
    """Como extraer_tabla(), pero devuelve TODAS las tablas que coincidan
    con `selector` (no solo la primera) como lista de
    {"id": <id del elemento o 'tabla_N' si no tiene>, "filas": [...]}.
    Se filtran las tablas sin filas de datos (0 filas), que en páginas
    como el informe de liquidación diaria son tablas de un concepto que
    no aplica ese día (p.ej. pago de premios, que solo se renderiza si
    hubo premios) — no vale la pena mantener una lista fija de ids de
    tabla cuando se puede pedir "todas las tablas visibles" directamente
    y quedarnos con las que tengan contenido."""
    return page.evaluate(
        r"""
        (selector) => {
            const textoLimpio = (celda) => {
                const clon = celda.cloneNode(true);
                clon.querySelectorAll('table').forEach(t => t.remove());
                return clon.textContent.replace(/\s+/g, ' ').trim();
            };
            const celdasPropias = (fila) => Array.from(fila.children).filter(
                el => el.tagName === 'TD' || el.tagName === 'TH'
            );
            const tablas = Array.from(document.querySelectorAll(selector));
            return tablas.map((tabla, indice) => {
                const filas = Array.from(tabla.querySelectorAll(':scope > tbody > tr, :scope > tr'));
                if (filas.length === 0) return {id: tabla.id || `tabla_${indice}`, filas: []};
                const cabecera = celdasPropias(filas[0]).map(textoLimpio);
                const datos = filas.slice(1).map(fila => {
                    const celdas = celdasPropias(fila).map(textoLimpio);
                    const obj = {};
                    cabecera.forEach((nombre, i) => { obj[nombre || `col_${i}`] = celdas[i] ?? null; });
                    return obj;
                });
                return {id: tabla.id || `tabla_${indice}`, filas: datos};
            }).filter(t => t.filas.length > 0);
        }
        """,
        selector,
    )


# ── Descargas ─────────────────────────────────────────────────────

# Calendario de turnos de Juan Antonio (agencia 576 Getafe):
# - Hasta el 12/07/2026 inclusive: tipo 4, trabaja miércoles a domingo.
# - Desde el 13/07/2026: tipo 1, trabaja lunes a viernes.
CAMBIO_TURNO_LIQUIDACION = datetime(2026, 7, 13).date()


def _hay_liquidacion_ese_dia(fecha):
    """Decide si toca ejecutar la descarga de liquidación diaria para
    `fecha` (la fecha consultada, no la de hoy), según el turno vigente
    ese día:

    - Tipo 4 (antes del cambio de turno, miércoles a domingo): se
      ejecuta TODOS los días de la semana. Lunes y martes no son
      jornada laboral pero se consulta igual por si hay cobros de
      libros o saldo acreedor pendiente (es normal que salga 0,00€).
    - Tipo 1 (desde el cambio de turno, lunes a viernes): se ejecuta de
      lunes a viernes; sábado y domingo se omite directamente, no hay
      jornada de ningún tipo esos días."""
    if fecha < CAMBIO_TURNO_LIQUIDACION:
        return True
    return fecha.weekday() not in (5, 6)  # 5 = sábado, 6 = domingo


def _importe_a_float(texto):
    """Convierte un importe en formato español ("403,10€", "0,00€") a
    float. Cualquier texto no reconocible se trata como 0."""
    if not texto:
        return 0.0
    limpio = texto.replace("€", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return 0.0


def descargar_liquidacion_diaria(page):
    """Vendedores/IngresoACuenta/InformeLiquidacionDiaria.aspx (navegación
    directa por URL, selectores confirmados con grabar_portal.py el
    06/07/2026). Pulsa "Enviar" (botón de consulta de este informe, no
    de envío de datos — distinto del "Enviar" bloqueado en
    BOTONES_PROHIBIDOS, que es para formularios de escritura como
    "Ingresos a cuenta producto", nunca tocado aquí), extrae la tabla a
    JSON y genera también un PDF de la página.

    IMPORTANTE: este informe NO permite consultar un día concreto
    pasado. Se comprobó experimentalmente (probando varias fechas muy
    distintas en el datepicker) que #datepickerFechaHasta no tiene
    ningún efecto sobre el resultado: la página siempre muestra el
    saldo pendiente de la PRÓXIMA liquidación tal como está hoy (su
    propio caption lo dice: "DESGLOSE PRÓXIMA LIQUIDACIÓN..."), no el
    balance de un día histórico. Por eso ya no se rellena el datepicker
    — solo se pulsa "Enviar" para forzar una consulta fresca del estado
    actual.

    Antes de nada comprueba con _hay_liquidacion_ese_dia() si AYER tenía
    jornada según el turno vigente (se usa ayer, el último día laborable
    completo, como referencia para decidir si merece la pena consultar);
    si no la tenía (solo posible en el turno de lunes a viernes, en
    sábado o domingo) se omite la consulta al portal por completo.

    El botón "Exportar a PDF" del propio portal (id btExportar) no sirve:
    inspeccionando InformeLiquidacionDiaria.js se confirmó que su
    manejador de clic (clickExportar()) está comentado en el JS del
    portal — no genera ningún PDF ni descarga, solo hace un postback
    vacío. Por eso el PDF se genera con page.pdf() (mismo método que
    descargar_nominas()), no pulsando ese botón."""
    hoy_dt = datetime.now().date()
    hoy = datetime.now().strftime("%Y%m%d")
    fecha_ayer = hoy_dt - timedelta(days=1)
    LIQUIDACIONES_PATH.mkdir(parents=True, exist_ok=True)
    destino = LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.json"

    if not _hay_liquidacion_ese_dia(fecha_ayer):
        datos = {
            "fecha_consulta": hoy_dt.strftime("%d/%m/%Y"),
            "ejecutado": False,
            "mensaje": "No se ejecuta liquidación (sábado/domingo, turno de lunes a viernes)",
        }
        with open(destino, "w") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        print(f"ℹ️  Liquidación diaria omitida ({hoy_dt.strftime('%d/%m/%Y')}): fin de semana, turno de lunes a viernes")
        return destino

    navegar(page, URL_LIQUIDACION_DIARIA)

    page.get_by_role("button", name="Enviar").click()
    page.wait_for_load_state("networkidle")

    tabla_liquidacion = extraer_tabla(page, "#ctl00_ContentPlaceHolder1_gvLiquidacionDiaria")
    tabla_saldo = extraer_tabla(page, "#ctl00_ContentPlaceHolder1_gvSaldoAcreedorDeudor")
    # Además de esas 2 tablas concretas ya conocidas, la página tiene más
    # tablas por concepto (gvProductosCupon, gvProductosActivos,
    # gvInstantanea, gvPagoPremios, gvPagoTarjeta, gvTWYP,
    # gvIngresoCuenta, gvTotalVentas...) que solo se renderizan si hay
    # datos ese día — en vez de mantener una lista fija de ids que habría
    # que descubrir y verificar uno a uno, se piden TODAS las tablas
    # dentro del área de contenido y se guardan tal cual.
    #
    # OJO con el contenedor: "#ctl00_ContentPlaceHolder1" (usado antes)
    # NO es un id real en el HTML — es un <asp:ContentPlaceHolder> de
    # ASP.NET, que no genera envoltorio propio en el DOM, así que ese
    # selector no encontraba NUNCA ninguna tabla (confirmado con
    # diagnóstico en vivo el 2026-07-10: 0 tablas en 5 días seguidos
    # pese a que el PDF sí mostraba desglose real). El contenedor que sí
    # existe de verdad envolviendo las 10 tablas es
    # "#ctl00_ContentPlaceHolder1_dvContenido" (el div de contenido real
    # que ASP.NET renderiza dentro de ese placeholder).
    PREFIJO_ID_PORTAL = "ctl00_ContentPlaceHolder1_"
    IDS_YA_CAPTURADOS = {"gvLiquidacionDiaria", "gvSaldoAcreedorDeudor"}
    tablas_brutas = extraer_todas_tablas(page, "#ctl00_ContentPlaceHolder1_dvContenido table")
    detalle_completo = [
        {"tabla": t["id"].removeprefix(PREFIJO_ID_PORTAL), "filas": t["filas"]}
        for t in tablas_brutas
        if t["id"].removeprefix(PREFIJO_ID_PORTAL) not in IDS_YA_CAPTURADOS
    ]

    importe_texto = tabla_liquidacion[0]["IMPORTE"] if tabla_liquidacion else "0,00€"
    saldo_texto = tabla_saldo[0]["IMPORTE"] if tabla_saldo else "0,00€"
    importe = _importe_a_float(importe_texto)
    saldo = _importe_a_float(saldo_texto)

    if importe > 0:
        mensaje = f"Importe a liquidar: {importe_texto}"
    elif saldo > 0:
        mensaje = f"Saldo acreedor aplicado: {saldo_texto}"
    else:
        mensaje = "Sin liquidación hoy (día libre)"

    datos = {
        "fecha_consulta": hoy_dt.strftime("%d/%m/%Y"),
        "ejecutado": True,
        "importe": importe_texto,
        "saldo_acreedor": saldo_texto,
        "mensaje": mensaje,
        "tabla_liquidacion": tabla_liquidacion,
        "tabla_saldo_acreedor": tabla_saldo,
        "detalle_completo": detalle_completo,
    }
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Liquidación diaria guardada: {destino.name} — {mensaje}")

    destino_pdf = LIQUIDACIONES_PATH / f"liquidacion_diaria_{hoy}.pdf"
    page.pdf(path=str(destino_pdf))
    print(f"✅ Liquidación diaria (PDF) guardada: {destino_pdf.name}")

    return destino


def descargar_control_retirada(page):
    """Control de Retirada: 3 pestañas (previsto / a retirar / retirado),
    cada una accesible por URL directa vía el parámetro tipoPaq. Extrae la
    tabla de cada una a JSON en PAQUETES_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    PAQUETES_PATH.mkdir(parents=True, exist_ok=True)

    urls = {
        "previsto": URL_CONTROL_PAQUETE_PREVISTO,
        "a_retirar": URL_CONTROL_PAQUETE_A_RETIRAR,
        "retirado": URL_CONTROL_PAQUETE_RETIRADO,
    }
    destinos = []
    for tipo, url in urls.items():
        navegar(page, url)
        datos = extraer_tabla(page)
        destino = PAQUETES_PATH / f"control_retirada_{tipo}_{hoy}.json"
        with open(destino, "w") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        print(f"✅ Control de retirada ({tipo}) guardado: {destino.name} ({len(datos)} filas)")
        destinos.append(destino)
        time.sleep(random.uniform(2, 5))
    return destinos


def descargar_control_almacen(page):
    """Control de almacén de lotería instantánea (rascas), navegación directa
    por URL. Extrae la tabla de la página a JSON en STOCK_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    STOCK_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_ALMACEN_RASCAS)

    datos = extraer_tabla(page)
    destino = STOCK_PATH / f"control_almacen_instantanea_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Control de almacén guardado: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_premios_repartidos(page):
    """Consulta de premios repartidos: 3 tipos de lotería (activa,
    instantánea, pasiva), cada uno vía URL directa con el parámetro
    tipoLot. Extrae la tabla de cada uno a JSON en PREMIOS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    PREMIOS_PATH.mkdir(parents=True, exist_ok=True)

    urls = {
        "activa": URL_PREMIOS_ACTIVA,
        "instantanea": URL_PREMIOS_INSTANTANEA,
        "pasiva": URL_PREMIOS_PASIVA,
    }
    destinos = []
    for tipo, url in urls.items():
        navegar(page, url)
        datos = extraer_tabla(page)
        destino = PREMIOS_PATH / f"premios_{tipo}_{hoy}.json"
        with open(destino, "w") as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        print(f"✅ Premios repartidos ({tipo}) guardados: {destino.name} ({len(datos)} filas)")
        destinos.append(destino)
        time.sleep(random.uniform(2, 5))
    return destinos


def descargar_comisiones(page):
    """Vendedores/comisiones.aspx: selecciona año/mes/centro más recientes,
    pulsa "Consultar" y, si existe, "Ver detalle de ventas". Guarda ambas
    tablas en JSON en COMISIONES_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    COMISIONES_PATH.mkdir(parents=True, exist_ok=True)

    ir_a_gestiona(page)
    click_menu(page, MENU_OFICINA_VIRTUAL)
    click_menu(page, MENU_COMISIONES)
    seleccionar_todos_mas_reciente(page)
    page.click(BOTON_CONSULTAR_COMISIONES)
    page.wait_for_load_state("networkidle")

    datos = extraer_tabla(page)
    destino = COMISIONES_PATH / f"comisiones_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Comisiones guardadas: {destino.name} ({len(datos)} filas)")

    try:
        clic_boton_texto(page, "Ver detalle de ventas")
        detalle = extraer_tabla(page)
        destino_detalle = COMISIONES_PATH / f"comisiones_detalle_ventas_{hoy}.json"
        with open(destino_detalle, "w") as f:
            json.dump(detalle, f, indent=2, ensure_ascii=False)
        print(f"✅ Detalle de ventas guardado: {destino_detalle.name} ({len(detalle)} filas)")
    except Exception:
        pass  # no había detalle de ventas disponible para este período

    return destino


def descargar_simulacion_comisiones(page):
    """Vendedores/ConsultaSimulacion.aspx: simulación de comisiones del
    período más reciente. A inicio de mes es normal que todavía no haya
    datos, así que una tabla vacía se registra como aviso informativo
    (no como error) y la función continúa sin lanzar excepción."""
    hoy = datetime.now().strftime("%Y%m%d")
    SIMULACION_COMISIONES_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_SIMULACION_COMISIONES)
    seleccionar_todos_mas_reciente(page)
    try:
        clic_boton_texto(page, "Consultar")
    except Exception:
        pass  # esta página puede no tener un botón "Consultar" explícito

    datos = extraer_tabla(page)
    if not datos:
        print("ℹ️ Simulación de comisiones: sin datos todavía (normal a inicio de mes)")
        return None

    destino = SIMULACION_COMISIONES_PATH / f"simulacion_comisiones_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Simulación de comisiones guardada: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_informe_ingresos_fecha(page):
    """Informe de ingresos por fecha: selecciona año/mes más recientes.
    Extrae la tabla a JSON en INFORMES_LIQUIDACION_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    INFORMES_LIQUIDACION_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_INFORME_INGRESOS_FECHA)
    seleccionar_todos_mas_reciente(page)
    try:
        clic_boton_texto(page, "Consultar")
    except Exception:
        pass  # esta página puede no tener un botón "Consultar" explícito

    datos = extraer_tabla(page)
    destino = INFORMES_LIQUIDACION_PATH / f"informe_ingresos_por_fecha_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Informe de ingresos por fecha guardado: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_informe_ingresos_producto(page):
    """Informe de ingresos por producto, navegación directa por URL.
    Extrae la tabla a JSON en INFORMES_LIQUIDACION_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    INFORMES_LIQUIDACION_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_INFORME_INGRESOS_PRODUCTO)

    datos = extraer_tabla(page)
    destino = INFORMES_LIQUIDACION_PATH / f"informe_ingresos_por_producto_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Informe de ingresos por producto guardado: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_otros_documentos(page):
    """Otros documentos, navegación directa por URL.
    Extrae la tabla a JSON en OTROS_DOCUMENTOS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    OTROS_DOCUMENTOS_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_OTROS_DOCUMENTOS)

    datos = extraer_tabla(page)
    destino = OTROS_DOCUMENTOS_PATH / f"otros_documentos_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Otros documentos guardado: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_consulta_incidencias(page):
    """Consultas > Consulta de incidencias, navegación directa por URL.
    Extrae la tabla a JSON en CONSULTAS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    CONSULTAS_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_CONSULTA_INCIDENCIAS)

    datos = extraer_tabla(page)
    destino = CONSULTAS_PATH / f"incidencias_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Consulta de incidencias guardada: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_consulta_registro_jornada(page):
    """Consultas > Consulta de registro de jornada, navegación directa por
    URL. Extrae la tabla a JSON en CONSULTAS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    CONSULTAS_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_CONSULTA_REGISTRO_JORNADA)

    datos = extraer_tabla(page)
    destino = CONSULTAS_PATH / f"registro_jornada_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Consulta de registro de jornada guardada: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_consulta_solicitudes(page):
    """Consultas > Consulta de solicitudes, navegación directa por URL.
    Extrae la tabla a JSON en CONSULTAS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    CONSULTAS_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_CONSULTA_SOLICITUDES)

    datos = extraer_tabla(page)
    destino = CONSULTAS_PATH / f"solicitudes_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Consulta de solicitudes guardada: {destino.name} ({len(datos)} filas)")
    return destino


def descargar_nominas(page):
    """Nómina: itera TODOS los años, meses y períodos disponibles en los
    desplegables (sin adivinar ninguno) y guarda el recibo y, si existe,
    el detalle de comisiones de cada combinación en NOMINAS_PATH."""
    NOMINAS_PATH.mkdir(parents=True, exist_ok=True)
    destinos = []

    ir_a_gestiona(page)
    click_menu_con_reintento(page, MENU_NOMINA)
    anios = [o["value"] for o in opciones_select(page, SELECT_ANIO)]

    for anio in anios:
        try:
            click_menu_con_reintento(page, MENU_NOMINA)
            page.select_option(SELECT_ANIO, value=anio)
            esperar_postback(page)  # el mes se recarga tras elegir año
            meses = [o["value"] for o in opciones_select(page, SELECT_MES)]
        except Exception as e:
            print(f"⚠️  Error seleccionando año {anio}: {e}")
            continue

        for mes in meses:
            try:
                click_menu_con_reintento(page, MENU_NOMINA)
                page.select_option(SELECT_ANIO, value=anio)
                esperar_postback(page)
                page.select_option(SELECT_MES, value=mes)
                page.wait_for_load_state("networkidle")  # el período se recarga tras elegir año/mes
                periodos = [o["value"] for o in opciones_select(page, SELECT_PERIODO)]
            except Exception as e:
                print(f"⚠️  Error seleccionando mes {anio}/{mes}: {e}")
                continue

            for periodo in periodos:
                try:
                    click_menu_con_reintento(page, MENU_NOMINA)
                    page.select_option(SELECT_ANIO, value=anio)
                    esperar_postback(page)
                    page.select_option(SELECT_MES, value=mes)
                    page.wait_for_load_state("networkidle")
                    page.select_option(SELECT_PERIODO, value=periodo)
                    page.click(BOTON_VER_NOMINA)
                    page.wait_for_load_state("networkidle")

                    base = f"NM_{anio}_{mes}_{nombre_archivo_seguro(periodo)}"
                    destino = NOMINAS_PATH / f"{base}.pdf"
                    page.pdf(path=str(destino))
                    print(f"✅ Nómina guardada: {destino.name}")
                    destinos.append(destino)

                    try:
                        page.click(BOTON_VER_COMISIONES, timeout=5000)
                        page.wait_for_load_state("networkidle")
                        destino_comisiones = NOMINAS_PATH / f"{base}_comisiones.pdf"
                        page.pdf(path=str(destino_comisiones))
                        print(f"✅ Comisiones guardadas: {destino_comisiones.name}")
                        destinos.append(destino_comisiones)
                    except Exception:
                        pass  # este período no tiene detalle de comisiones
                except Exception as e:
                    print(f"⚠️  Error en nómina {anio}/{mes}/{periodo}: {e}")

                time.sleep(random.uniform(2, 5))

    return destinos


def descargar_estadisticas_venta(page):
    """Vendedores/estadisticasventa.aspx: selecciona año/mes/centro/producto
    más recientes y pulsa "Consultar". Extrae la tabla a JSON en ESTADISTICAS_PATH."""
    hoy = datetime.now().strftime("%Y%m%d")
    ESTADISTICAS_PATH.mkdir(parents=True, exist_ok=True)

    navegar(page, URL_ESTADISTICAS_VENTA)
    seleccionar_todos_mas_reciente(page)
    clic_boton_texto(page, "Consultar")

    datos = extraer_tabla(page)
    destino = ESTADISTICAS_PATH / f"estadisticas_venta_{hoy}.json"
    with open(destino, "w") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    print(f"✅ Estadísticas de venta guardadas: {destino.name} ({len(datos)} filas)")
    return destino


# ── Orquestación ──────────────────────────────────────────────────

def ejecutar_descarga_diaria():
    """Secciones de lectura que se comprueban cada mañana. Las nóminas NO
    se descargan aquí — ver ejecutar_descarga_mensual()."""
    usuario = cargar_usuario()
    password = obtener_password(usuario)
    clave_segura = obtener_clave_segura()

    resultados = {}
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)  # el portal ONCE bloquea/no renderiza el login en modo headless
        contexto = navegador.new_context(accept_downloads=True)
        page = contexto.new_page()

        try:
            iniciar_sesion(page, usuario, password, clave_segura)

            for nombre, funcion in [
                ("control_retirada", descargar_control_retirada),
                ("control_almacen", descargar_control_almacen),
                ("premios_repartidos", descargar_premios_repartidos),
                ("comisiones", descargar_comisiones),
                ("estadisticas_venta", descargar_estadisticas_venta),
                ("liquidacion_diaria", descargar_liquidacion_diaria),
                ("consulta_incidencias", descargar_consulta_incidencias),
                ("consulta_registro_jornada", descargar_consulta_registro_jornada),
                ("consulta_solicitudes", descargar_consulta_solicitudes),
            ]:
                try:
                    resultados[nombre] = funcion(page)
                except Exception as e:
                    print(f"⚠️  Error descargando {nombre}: {e}")
                    resultados[nombre] = None

                time.sleep(random.uniform(2, 5))
        finally:
            navegador.close()

    return resultados


def ejecutar_descarga_mensual():
    """Nóminas: solo se ejecuta el día 5 de cada mes. Si se invoca otro
    día, no abre navegador ni inicia sesión, solo lo registra y sale."""
    if datetime.now().day != 5:
        print("ℹ️  Descarga mensual de nóminas: hoy no es día 5, no se ejecuta.")
        return None

    usuario = cargar_usuario()
    password = obtener_password(usuario)
    clave_segura = obtener_clave_segura()

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)  # el portal ONCE bloquea/no renderiza el login en modo headless
        contexto = navegador.new_context(accept_downloads=True)
        page = contexto.new_page()

        try:
            iniciar_sesion(page, usuario, password, clave_segura)
            resultado = descargar_nominas(page)
        finally:
            navegador.close()

    return resultado


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_credenciales()
        sys.exit(0)

    print("\n🎰 PORTAL ONCE - Descarga automática diaria")
    print("=" * 50)
    ejecutar_descarga_diaria()

    print("\n🎰 PORTAL ONCE - Descarga mensual (nóminas)")
    print("=" * 50)
    ejecutar_descarga_mensual()
