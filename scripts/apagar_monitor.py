#!/usr/bin/env python3
"""
Apagado directo del interruptor del monitor (TP-Link Tapo P100)
Juan Antonio Panadero Jiménez - IBMPS1

Habla directamente con el enchufe Tapo P100 por red local usando el
protocolo KLAP — sin pasar por HomeKit, Atajos ni escenas de Casa. Se
llegó a esto tras comprobar que `shortcuts run "APAGAR"` falla en
segundo plano ("falta una app necesaria", 2026-07-20) al ejecutarse
desde un LaunchAgent, y que el plugin homebridge-tapo (que sí controla
el dispositivo con éxito) usa este mismo protocolo.

La implementación replica línea a línea la lógica real de
homebridge-tapo (paquete instalado en
/opt/homebrew/lib/node_modules/homebridge-tapo/dist/utils/p100.js y
newTpLinkCipher.js) — no es una reimplementación genérica de KLAP de
terceros, es exactamente lo que ese plugin hace para autenticarse y
enviar comandos a este mismo dispositivo:

  1. handshake1: se manda un local_seed aleatorio (16 bytes); el
     dispositivo responde con remote_seed (16 bytes) + un hash SHA256
     que verifica que ambos lados conocen las credenciales de la
     cuenta Tapo (auth_hash = sha256(sha1(email) + sha1(password))).
  2. handshake2: se confirma el hash en la otra dirección.
  3. Con local_seed + remote_seed + auth_hash se derivan la clave AES,
     el IV base y una firma — sin RSA, a diferencia del protocolo Tapo
     antiguo (por eso KLAP es más simple de implementar).
  4. Cada petición ("request") va cifrada con AES-128-CBC (clave fija,
     IV = iv_base + número de secuencia) y firmada con SHA256; la
     respuesta se descifra con la misma clave/IV.
  5. El comando para apagar es "set_device_info" con
     {"device_on": false}.

Las credenciales (cuenta Tapo) y la IP del dispositivo se leen de
~/.homebridge/config.json (plataforma "TapoP100", dispositivo
"Interruptor monitor") — las mismas que ya usa HomeBridge, no se
duplican en este repo en texto plano aparte.

Uso:
    python3 apagar_monitor.py            # apaga "Interruptor monitor"
    python3 apagar_monitor.py --encender # lo enciende (pruebas)
"""

import hashlib
import json
import os
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOMEBRIDGE_CONFIG_PATH = Path.home() / ".homebridge" / "config.json"
NOMBRE_DISPOSITIVO = "Interruptor monitor"
TIMEOUT_SEGUNDOS = 8


def _leer_credenciales_dispositivo(nombre_dispositivo=NOMBRE_DISPOSITIVO):
    """Lee email/password de la cuenta Tapo y la IP del dispositivo
    `nombre_dispositivo` de la plataforma TapoP100 en el config.json de
    HomeBridge — las mismas credenciales que ya usa HomeBridge, leídas
    en el momento, nunca copiadas a este repo."""
    with open(HOMEBRIDGE_CONFIG_PATH) as f:
        config = json.load(f)
    for plataforma in config.get("platforms", []):
        if plataforma.get("platform") != "TapoP100":
            continue
        email = plataforma["username"]
        password = plataforma["password"]
        for dispositivo in plataforma.get("devices", []):
            if dispositivo.get("name") == nombre_dispositivo:
                return email, password, dispositivo["host"]
    raise RuntimeError(
        f"No se encontró el dispositivo {nombre_dispositivo!r} en la plataforma "
        f"TapoP100 de {HOMEBRIDGE_CONFIG_PATH}"
    )


def _sha256(*partes):
    h = hashlib.sha256()
    for p in partes:
        h.update(p)
    return h.digest()


def _calcular_auth_hash(email, password):
    """Réplica de calc_auth_hash() en p100.js: sha256(sha1(email) +
    sha1(password)), normalizando ambos strings a NFKC como hace el JS
    (`.normalize('NFKC')`) antes de codificar a UTF-8."""
    email_digest = hashlib.sha1(unicodedata.normalize("NFKC", email).encode("utf-8")).digest()
    password_digest = hashlib.sha1(unicodedata.normalize("NFKC", password).encode("utf-8")).digest()
    return _sha256(email_digest + password_digest)


class DispositivoNoAutenticado(RuntimeError):
    pass


class TapoKlapClient:
    """Cliente KLAP para un enchufe/dispositivo Tapo, réplica de P100 +
    NewTpLinkCipher en homebridge-tapo (ver docstring del módulo)."""

    def __init__(self, ip, email, password, timeout=TIMEOUT_SEGUNDOS):
        self.ip = ip
        self.email = email
        self.password = password
        self.timeout = timeout
        self.cookie = None
        self.key = None
        self.iv = None
        self.sig = None
        self.seq = None

    def _post(self, path, body, query=None):
        url = f"http://{self.ip}/app/{path}"
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        headers = {
            "Connection": "Keep-Alive",
            "Host": self.ip,
            "Accept": "*/*",
            "Content-Type": "application/octet-stream",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        peticion = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(peticion, timeout=self.timeout) as respuesta:
            cookies = respuesta.headers.get_all("Set-Cookie")
            if cookies:
                self.cookie = cookies[0].split(";")[0]
            return respuesta.read()

    def handshake(self):
        """handshake1 + handshake2 (handshake_new() en p100.js) y
        derivación de clave/IV/firma (constructor de NewTpLinkCipher)."""
        local_seed = os.urandom(16)
        respuesta1 = self._post("handshake1", local_seed)
        remote_seed, server_hash = respuesta1[:16], respuesta1[16:]

        auth_hash = _calcular_auth_hash(self.email, self.password)
        comprobacion = _sha256(local_seed + remote_seed + auth_hash)
        if comprobacion != server_hash:
            raise DispositivoNoAutenticado(
                "El dispositivo no reconoce las credenciales de la cuenta Tapo "
                "(hash de handshake1 no coincide) — revisa username/password en "
                f"{HOMEBRIDGE_CONFIG_PATH}."
            )

        confirmacion = _sha256(remote_seed + local_seed + auth_hash)
        self._post("handshake2", confirmacion)

        self.key = _sha256(b"lsk" + local_seed + remote_seed + auth_hash)[:16]
        iv_hash = _sha256(b"iv" + local_seed + remote_seed + auth_hash)
        self.seq = struct.unpack(">i", iv_hash[-4:])[0]
        self.iv = iv_hash[:12]
        self.sig = _sha256(b"ldk" + local_seed + remote_seed + auth_hash)[:28]

    def _iv_seq(self):
        return self.iv + struct.pack(">i", self.seq)

    def _cifrar(self, payload_dict):
        self.seq += 1
        datos = json.dumps(payload_dict).encode("utf-8")
        relleno = padding.PKCS7(algorithms.AES.block_size).padder()
        datos_rellenados = relleno.update(datos) + relleno.finalize()
        cifrador = Cipher(algorithms.AES(self.key), modes.CBC(self._iv_seq())).encryptor()
        texto_cifrado = cifrador.update(datos_rellenados) + cifrador.finalize()
        seq_buf = struct.pack(">i", self.seq)
        firma = _sha256(self.sig + seq_buf + texto_cifrado)
        return firma + texto_cifrado

    def _descifrar(self, datos_respuesta):
        descifrador = Cipher(algorithms.AES(self.key), modes.CBC(self._iv_seq())).decryptor()
        relleno_datos = descifrador.update(datos_respuesta[32:]) + descifrador.finalize()
        despadding = padding.PKCS7(algorithms.AES.block_size).unpadder()
        datos = despadding.update(relleno_datos) + despadding.finalize()
        return json.loads(datos.decode("utf-8"))

    def _enviar(self, payload_dict):
        if self.key is None:
            raise DispositivoNoAutenticado("Falta hacer handshake() antes de enviar comandos.")
        payload_cifrado = self._cifrar(payload_dict)
        respuesta_cruda = self._post("request", payload_cifrado, query={"seq": self.seq})
        respuesta = self._descifrar(respuesta_cruda)
        if respuesta.get("error_code") not in (0, "0"):
            raise RuntimeError(f"El dispositivo devolvió un error: {respuesta}")
        return respuesta

    def set_device_on(self, encendido):
        """Réplica de turnOn()/turnOff() en p100.js: set_device_info
        con device_on true/false."""
        payload = {
            "method": "set_device_info",
            "params": {"device_on": bool(encendido)},
            "terminalUUID": str(uuid4()),
            "requestTimeMils": round(time.time() * 1000) * 1000,
        }
        self._enviar(payload)

    def get_device_on(self):
        """Réplica de getDeviceInfo(force=true) en p100.js (rama KLAP):
        consulta el estado real del dispositivo. Se usa para VERIFICAR
        tras set_device_on() en vez de fiarse de que la petición no
        diera error — mismo principio ya aplicado en este proyecto a
        abrir/cerrar apps y a la liquidación diaria: un "sin error" no
        siempre significa que el cambio se aplicó de verdad."""
        payload = {"method": "get_device_info", "requestTimeMils": round(time.time() * 1000) * 1000}
        respuesta = self._enviar(payload)
        return respuesta["result"]["device_on"]


def controlar_dispositivo(encendido, nombre_dispositivo=NOMBRE_DISPOSITIVO):
    """Cambia el estado del dispositivo y devuelve el estado REAL leído
    después del cambio (no el que se pidió) — ver aviso en
    get_device_on()."""
    email, password, ip = _leer_credenciales_dispositivo(nombre_dispositivo)
    cliente = TapoKlapClient(ip, email, password)
    cliente.handshake()
    cliente.set_device_on(encendido)
    return cliente.get_device_on()


if __name__ == "__main__":
    encender = "--encender" in sys.argv
    accion = "Encendiendo" if encender else "Apagando"
    print(f"{accion} {NOMBRE_DISPOSITIVO!r} por KLAP directo...")
    try:
        estado_real = controlar_dispositivo(encendido=encender)
    except (urllib.error.URLError, DispositivoNoAutenticado, RuntimeError) as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

    if estado_real == encender:
        print(f"✅ {NOMBRE_DISPOSITIVO} {'encendido' if encender else 'apagado'} — verificado leyendo el estado real del dispositivo.")
    else:
        print(
            f"⚠️  La petición no dio error, pero el dispositivo sigue reportando "
            f"device_on={estado_real} (se pidió {encender}) — no verificado.",
            file=sys.stderr,
        )
        sys.exit(1)
