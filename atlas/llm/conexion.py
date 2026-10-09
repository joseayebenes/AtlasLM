#!/usr/bin/env python3
"""
conexion.py — Todo lo relativo a la conexión con las APIs (LLM y embeddings):
fichero de configuración, proxy, certificados, creación de clientes OpenAI y
diagnóstico de errores. Lo usan pdf_struct.py y doc_rag.py.

Orden de prioridad de cada valor (de menor a mayor):
  1. valores por defecto (algunos leen variables estándar: HTTPS_PROXY, NO_PROXY,
     SSL_CERT_FILE, OPENAI_BASE_URL, OPENAI_API_KEY, LLM_MODEL, EMBED_MODEL)
  2. fichero de configuración: sección [network] común, luego [llm] / [embeddings]
     y sus subsecciones [llm.network] / [embeddings.network]
  3. argumentos de línea de comandos

Fichero: --config RUTA, o la variable RAG_CONFIG, o conexion.toml en el directorio
actual, o conexion.toml junto a este script. Los valores admiten ${VARIABLE} y
${VARIABLE:-por_defecto}, para no escribir claves ni contraseñas en el fichero.

Uso directo:
  python conexion.py init               # crea conexion.toml de ejemplo
  python conexion.py show               # configuración efectiva (secretos ocultos)
  python conexion.py check              # prueba real contra LLM y embeddings

Requisitos: pip install openai  (+ tomli si Python < 3.11, + truststore para ca_cert="system")
"""

from __future__ import annotations

import argparse
import importlib
import os
import random
import re
import ssl
import sys
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

NOMBRE_FICHERO = "conexion.toml"


# --------------------------------------------------------------------------
# Modelo de configuración
# --------------------------------------------------------------------------

def _env(*nombres: str, default: str = "") -> str:
    for n in nombres:
        if os.environ.get(n):
            return os.environ[n]
    return default


@dataclass
class NetConfig:
    proxy: str = field(default_factory=lambda: _env(
        "RAG_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"))
    ca_cert: str = field(default_factory=lambda: _env(
        "RAG_CA_CERT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"))
    client_cert: str = ""
    client_key: str = ""
    no_proxy: str = field(default_factory=lambda: _env(
        "RAG_NO_PROXY", "NO_PROXY", "no_proxy", default="localhost,127.0.0.1,::1"))
    insecure: bool = False
    timeout: float = 600.0
    # Límites de la API (0 = sin límite). Si LLM y embeddings van al mismo servidor
    # con la misma clave, comparten el contador.
    requests_per_minute: int = 0
    tokens_per_minute: int = 0   # estimado como caracteres/4 del cuerpo de la petición
    adaptive: bool = True        # bajar el ritmo solo al recibir rechazos por límite
    max_concurrent: int = 0
    max_retries: int = 6              # reintentos ante errores 5xx o de conexión
    max_backoff: float = 60.0
    rate_limit_max_wait: float = 900.0  # espera total máxima por petición ante 409/429 (s)
    retry_statuses: list = field(default_factory=lambda: [409, 429, 500, 502, 503, 504])


@dataclass
class ServiceConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.1     # solo LLM
    max_tokens: int = 0          # solo LLM: tope de salida; 0 = el valor por defecto del servidor
    batch: int = 32              # solo embeddings
    net: NetConfig = field(default_factory=NetConfig)


@dataclass
class Conexion:
    llm: ServiceConfig
    embeddings: ServiceConfig
    fichero: str = ""            # ruta del fichero cargado ("" si ninguno)


# --------------------------------------------------------------------------
# Carga del fichero
# --------------------------------------------------------------------------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expandir(valor):
    if isinstance(valor, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), valor)
    if isinstance(valor, dict):
        return {k: _expandir(v) for k, v in valor.items()}
    return valor


def _leer_toml(ruta: Path) -> dict:
    try:
        import tomllib                      # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib         # Python < 3.11: pip install tomli
        except ImportError:
            sys.exit("Para leer el fichero de configuración en Python < 3.11: pip install tomli")
    try:
        with open(ruta, "rb") as f:
            return _expandir(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"Error de sintaxis en {ruta}: {e}")


def buscar_fichero(explicito: str | None) -> Path | None:
    if explicito:
        p = Path(explicito).expanduser()
        if not p.exists():
            sys.exit(f"no existe el fichero de configuración {p}")
        return p
    for candidato in (os.environ.get("RAG_CONFIG"), Path.cwd() / NOMBRE_FICHERO,
                      Path(__file__).resolve().parent / NOMBRE_FICHERO):
        if candidato and Path(candidato).exists():
            return Path(candidato)
    return None


_CLAVES_NET = {f.name for f in fields(NetConfig)}
_CLAVES_SERV = {f.name for f in fields(ServiceConfig)} - {"net"}


def _aplicar(obj, datos: dict, donde: str, permitidas: set[str]):
    cambios = {}
    for k, v in datos.items():
        if k not in permitidas:
            print(f"[aviso] clave desconocida '{k}' en {donde} (se ignora)", file=sys.stderr)
            continue
        tipo = type(getattr(obj, k))
        if tipo is bool and isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
        try:
            cambios[k] = v if isinstance(v, tipo) else tipo(v)
        except (TypeError, ValueError):
            sys.exit(f"valor no válido para '{k}' en {donde}: {v!r}")
    return replace(obj, **cambios)


# --------------------------------------------------------------------------
# Argumentos de línea de comandos
# --------------------------------------------------------------------------

def añadir_args(ap: argparse.ArgumentParser, embeddings: bool = True) -> None:
    """Todas las opciones de conexión. Valor None = 'no indicado' (manda el fichero)."""
    g = ap.add_argument_group("conexión (también en conexion.toml)")
    g.add_argument("--config", default=None, help="fichero de configuración (por defecto conexion.toml)")
    g.add_argument("--base-url", default=None, help="URL de la API del LLM")
    g.add_argument("--model", default=None, help="modelo de chat")
    g.add_argument("--api-key", default=None)
    g.add_argument("--temperature", type=float, default=None)
    if embeddings:
        g.add_argument("--embed-base-url", default=None, help="URL de embeddings (por defecto la del LLM)")
        g.add_argument("--embed-model", default=None)
        g.add_argument("--embed-api-key", default=None)
    r = ap.add_argument_group("red: proxy y certificados (comunes a LLM y embeddings)")
    r.add_argument("--proxy", default=None, help="http://usuario:clave@proxy:8080")
    r.add_argument("--ca-cert", default=None, help="PEM con la CA corporativa o 'system'")
    r.add_argument("--client-cert", default=None, help="certificado cliente (TLS mutuo)")
    r.add_argument("--client-key", default=None, help="clave privada del certificado cliente")
    r.add_argument("--no-proxy", default=None, help="hosts sin proxy, separados por comas ('' = ninguno)")
    r.add_argument("--insecure", action="store_true", default=None,
                   help="sin verificación TLS (solo para diagnosticar)")
    r.add_argument("--timeout", type=float, default=None, help="timeout por petición (s)")
    r.add_argument("--rpm", type=int, default=None, help="máximo de peticiones por minuto a la API")
    r.add_argument("--tpm", type=int, default=None, help="máximo aproximado de tokens por minuto")
    r.add_argument("--max-concurrent", type=int, default=None, help="peticiones simultáneas máximas")
    if embeddings:
        r.add_argument("--embed-proxy", default=None, help="proxy distinto solo para embeddings")
        r.add_argument("--embed-ca-cert", default=None, help="CA distinta solo para embeddings")


def _de_args(args, mapa: dict[str, str]) -> dict:
    return {campo: getattr(args, opcion) for campo, opcion in mapa.items()
            if getattr(args, opcion, None) is not None}


def cargar(args: argparse.Namespace | None = None, fichero: str | None = None) -> Conexion:
    """Construye la configuración efectiva: defaults < fichero < línea de comandos."""
    ruta = buscar_fichero(fichero or (getattr(args, "config", None) if args else None))
    datos = _leer_toml(ruta) if ruta else {}
    desconocidas = set(datos) - {"network", "llm", "embeddings"}
    for k in desconocidas:
        print(f"[aviso] sección desconocida [{k}] en {ruta} (se ignora)", file=sys.stderr)

    # Red común
    net = NetConfig()
    net = _aplicar(net, datos.get("network", {}), f"{ruta} [network]", _CLAVES_NET)

    def servicio(nombre: str, defaults: ServiceConfig) -> ServiceConfig:
        sec = dict(datos.get(nombre, {}))
        sub_net = sec.pop("network", {})
        s = _aplicar(defaults, sec, f"{ruta} [{nombre}]", _CLAVES_SERV)
        return replace(s, net=_aplicar(net, sub_net, f"{ruta} [{nombre}.network]", _CLAVES_NET))

    llm = servicio("llm", ServiceConfig(
        base_url=_env("OPENAI_BASE_URL", default="http://localhost:11434/v1"),
        model=_env("LLM_MODEL", default="gemma4"),
        api_key=_env("OPENAI_API_KEY", default="local")))
    emb = servicio("embeddings", ServiceConfig(model=_env("EMBED_MODEL", default="embeddinggemma")))

    # Línea de comandos
    if args is not None:
        net_cli = _de_args(args, {"proxy": "proxy", "ca_cert": "ca_cert", "client_cert": "client_cert",
                                  "client_key": "client_key", "no_proxy": "no_proxy",
                                  "insecure": "insecure", "timeout": "timeout",
                                  "requests_per_minute": "rpm", "tokens_per_minute": "tpm",
                                  "max_concurrent": "max_concurrent"})
        llm = replace(llm, **_de_args(args, {"base_url": "base_url", "model": "model",
                                             "api_key": "api_key", "temperature": "temperature"}),
                      net=replace(llm.net, **net_cli))
        emb = replace(emb, **_de_args(args, {"base_url": "embed_base_url", "model": "embed_model",
                                             "api_key": "embed_api_key"}),
                      net=replace(emb.net, **{**net_cli, **_de_args(
                          args, {"proxy": "embed_proxy", "ca_cert": "embed_ca_cert"})}))

    # Los embeddings heredan URL y clave del LLM si no se indican. La clave solo se hereda
    # si también se hereda la URL: no se envía la clave de un servidor a otro distinto.
    if not emb.base_url:
        emb = replace(emb, base_url=llm.base_url, api_key=emb.api_key or llm.api_key)
    return Conexion(llm=llm, embeddings=emb, fichero=str(ruta) if ruta else "")


# --------------------------------------------------------------------------
# TLS, proxy y clientes
# --------------------------------------------------------------------------

def ocultar_credenciales(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url or "")


def ocultar_clave(k: str) -> str:
    return "" if not k else (k[:3] + "***" if len(k) > 8 else "***")


def sin_proxy(host: str, no_proxy: str) -> bool:
    host = (host or "").lower()
    for patron in (p.strip().lower() for p in no_proxy.split(",") if p.strip()):
        if patron == "*" or host == patron.lstrip(".") or host.endswith("." + patron.lstrip(".")):
            return True
    return False


def proxy_efectivo(cfg: ServiceConfig) -> str:
    host = urlparse(cfg.base_url).hostname or ""
    return cfg.net.proxy if cfg.net.proxy and not sin_proxy(host, cfg.net.no_proxy) else ""


def contexto_ssl(net: NetConfig) -> ssl.SSLContext | bool:
    if net.insecure:
        return False
    if net.ca_cert.lower() == "system":
        try:
            import truststore
        except ImportError:
            sys.exit('ca_cert = "system" necesita: pip install truststore')
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        ctx = ssl.create_default_context()      # en Windows ya incluye el almacén del sistema
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
        if net.ca_cert:
            ruta = Path(net.ca_cert).expanduser()
            if not ruta.exists():
                sys.exit(f"no existe el certificado CA {ruta}")
            if ruta.is_dir():
                ctx.load_verify_locations(capath=str(ruta))
            else:
                ctx.load_verify_locations(cafile=str(ruta))
    if net.client_cert:
        ctx.load_cert_chain(str(Path(net.client_cert).expanduser()),
                            str(Path(net.client_key).expanduser()) if net.client_key else None,
                            password=os.environ.get("RAG_CLIENT_KEY_PASSWORD") or None)
    return ctx


VENTANA = 60.0   # segundos de la ventana de requests_per_minute / tokens_per_minute
RATE_STATUSES = {409, 429}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Limitador:
    """Control de ritmo compartido por todo lo que va al mismo servidor con la misma clave.

    - Peticiones por minuto, en ventana deslizante Y espaciadas uniformemente (60/rpm s
      entre envíos): así también se cumple si el servidor mide con "cubo" o por segundos
      en lugar de por minuto completo.
    - Tokens por minuto (aproximados), para límites por volumen.
    - Adaptativo: cada rechazo por límite baja el ritmo un 30 %; tras un minuto sin
      rechazos se recupera poco a poco. Si no hay rpm configurado, lo descubre.
    - Pausa común: tras un rechazo o si el servidor avisa de que no quedan peticiones,
      esperan todos los hilos."""

    def __init__(self, net: NetConfig, etiqueta: str):
        self.etiqueta = etiqueta
        self.rpm_conf = net.requests_per_minute
        self.rpm = float(net.requests_per_minute)
        self.tpm = net.tokens_per_minute
        self.adaptive = net.adaptive
        self.sem = threading.BoundedSemaphore(net.max_concurrent) if net.max_concurrent > 0 else None
        self.lock = threading.Lock()
        self.marcas: deque[float] = deque()
        self.tokens: deque[tuple[float, int]] = deque()
        self.ultimo_envio = 0.0
        self.pausa_hasta = 0.0
        self.ultimo_rechazo = 0.0
        self.ultima_subida = 0.0
        self.rechazos = 0
        self.diagnosticado = False

    def _purgar(self, ahora: float) -> None:
        while self.marcas and ahora - self.marcas[0] >= VENTANA:
            self.marcas.popleft()
        while self.tokens and ahora - self.tokens[0][0] >= VENTANA:
            self.tokens.popleft()

    def _recuperar(self, ahora: float) -> None:
        """Sube el ritmo +1 pet./min cada 30 s sin rechazos, hasta el configurado."""
        if not self.adaptive or not self.rechazos or not self.rpm:
            return
        if ahora - self.ultimo_rechazo < VENTANA or ahora - self.ultima_subida < VENTANA / 2:
            return
        if self.rpm_conf and self.rpm >= self.rpm_conf:
            return
        self.rpm = min(self.rpm + 1, self.rpm_conf or self.rpm + 1)
        self.ultima_subida = ahora

    def esperar(self, tokens: int = 0) -> None:
        while True:
            with self.lock:
                ahora = time.monotonic()
                self._purgar(ahora)
                self._recuperar(ahora)
                espera = self.pausa_hasta - ahora
                if self.rpm:
                    if len(self.marcas) >= int(self.rpm):
                        espera = max(espera, VENTANA - (ahora - self.marcas[0]))
                    espera = max(espera, self.ultimo_envio + VENTANA / self.rpm - ahora)
                if self.tpm and self.tokens and sum(n for _, n in self.tokens) + tokens > self.tpm:
                    espera = max(espera, VENTANA - (ahora - self.tokens[0][0]))
                if espera <= 0:
                    self.marcas.append(ahora)
                    if self.tpm:
                        self.tokens.append((ahora, tokens))
                    self.ultimo_envio = ahora
                    return
            time.sleep(min(espera, 2.0) + 0.01)

    def pausar(self, segundos: float) -> None:
        with self.lock:
            self.pausa_hasta = max(self.pausa_hasta, time.monotonic() + segundos)

    def rechazo(self) -> None:
        """El servidor ha rechazado por límite: bajar el ritmo (una vez cada 10 s como mucho,
        para que varios hilos rechazados a la vez no lo hundan)."""
        with self.lock:
            ahora = time.monotonic()
            self.rechazos += 1
            if not self.adaptive or ahora - self.ultimo_rechazo < 10:
                return
            self._purgar(ahora)
            base = self.rpm or max(len(self.marcas), 2)
            self.rpm = max(1.0, base * 0.7)
            self.ultimo_rechazo = ahora
        _log(f"[API {self.etiqueta}] rechazo por límite: ritmo reducido a {self.rpm:.0f} pet./min "
             f"(se recupera solo si deja de haber rechazos)")


_LIMITADORES: dict[tuple, Limitador] = {}
_LOCK_LIMITADORES = threading.Lock()


def limitador_para(cfg: ServiceConfig) -> Limitador:
    u = urlparse(cfg.base_url)
    clave = (u.scheme, u.hostname, u.port, cfg.api_key)
    n = cfg.net
    with _LOCK_LIMITADORES:
        lim = _LIMITADORES.get(clave)
        if lim is None:
            lim = _LIMITADORES[clave] = Limitador(n, u.hostname or cfg.base_url)
        else:                                    # si difieren, manda el más restrictivo
            if n.requests_per_minute and (not lim.rpm_conf or n.requests_per_minute < lim.rpm_conf):
                lim.rpm_conf = n.requests_per_minute
                lim.rpm = min(lim.rpm or n.requests_per_minute, n.requests_per_minute)
            if n.tokens_per_minute and (not lim.tpm or n.tokens_per_minute < lim.tpm):
                lim.tpm = n.tokens_per_minute
        return lim


def _duracion(v: str | None) -> float | None:
    """'6m0s', '1.5s', '20ms', '30', o una marca de tiempo epoch -> segundos."""
    if not v:
        return None
    v = v.strip()
    try:
        x = float(v)
        return max(0.0, x - time.time()) if x > 1e9 else x
    except ValueError:
        pass
    total, ok = 0.0, False
    for num, unidad in re.findall(r"([\d.]+)\s*(ms|h|m|s)", v):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unidad]
        ok = True
    return total if ok else None


def _espera_por_cabeceras(h) -> float | None:
    """Si el servidor avisa de que no quedan peticiones o tokens, cuánto esperar."""
    for resto, reinicio in (("x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
                            ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
                            ("ratelimit-remaining", "ratelimit-reset"),
                            ("x-ratelimit-remaining", "x-ratelimit-reset"),
                            ("x-rate-limit-remaining", "x-rate-limit-reset")):
        r = h.get(resto)
        if r is None:
            continue
        try:
            if float(r) <= 0:
                return _duracion(h.get(reinicio)) or 1.0
        except ValueError:
            continue
    return None


def _tiempo(seg: float) -> str:
    return f"{seg:.0f} s" if seg < 120 else f"{seg / 60:.1f} min"


def _cabeceras_limite(h) -> str:
    return ", ".join(f"{k}: {v}" for k, v in h.items()
                     if "ratelimit" in k.lower() or "rate-limit" in k.lower() or "retry" in k.lower()) \
        or "ninguna cabecera de límite"


def _retry_after(headers) -> float | None:
    try:
        if headers.get("retry-after-ms"):
            return float(headers["retry-after-ms"]) / 1000
        v = headers.get("retry-after")
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return max(0.0, (parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def _es_error_tls(e: BaseException) -> bool:
    x, n = e, 0
    while x is not None and n < 10:
        if isinstance(x, ssl.SSLError) or "CERTIFICATE" in str(x) or "SSL" in type(x).__name__:
            return True
        x, n = x.__cause__ or x.__context__, n + 1
    return False


def _modulo_http():
    """httpx o el cliente HTTP que use la versión instalada de openai."""
    from openai import DefaultHttpxClient
    base = next(c for c in DefaultHttpxClient.__mro__
                if c.__name__ == "Client" and not c.__module__.startswith("openai"))
    return importlib.import_module(base.__module__.split(".")[0])


def _transporte(hx, interno, lim: Limitador, net: NetConfig, etiqueta: str):
    """Transporte HTTP que respeta el límite y reintenta 409/429/5xx con espera creciente,
    usando Retry-After y las cabeceras x-ratelimit-* si el servidor las envía."""

    class TransporteLimitado(hx.BaseTransport):
        def handle_request(self, request):
            tokens = 0
            if lim.tpm:
                try:
                    tokens = len(request.content) // 4
                except Exception:
                    tokens = 0
            reintentos = 0          # por errores de servidor (5xx) o de conexión
            rechazos = 0            # por límite (409/429): no cuentan como reintentos
            esperado_limite = 0.0   # tiempo total esperado por límite en esta petición
            fallos_conexion = 0
            while True:
                if lim.sem:
                    lim.sem.acquire()
                try:
                    lim.esperar(tokens)
                    try:
                        resp = interno.handle_request(request)
                    except hx.ConnectError as e:
                        # Errores TLS (certificado, TLS mutuo) no se arreglan reintentando;
                        # un fallo de conexión puntual sí, pero solo un par de veces.
                        fallos_conexion += 1
                        if _es_error_tls(e) or fallos_conexion > 2 or reintentos >= net.max_retries:
                            raise
                        resp = None
                finally:
                    if lim.sem:
                        lim.sem.release()

                es_limite = resp is not None and resp.status_code in RATE_STATUSES
                if resp is not None:
                    aviso = _espera_por_cabeceras(resp.headers)
                    if aviso:                     # el servidor dice que no queda cupo
                        lim.pausar(min(aviso, max(net.max_backoff, 300.0)))
                    if resp.status_code not in net.retry_statuses:
                        return resp
                    if es_limite and esperado_limite >= net.rate_limit_max_wait:
                        return resp
                    if not es_limite and reintentos >= net.max_retries:
                        return resp

                espera = _retry_after(resp.headers) if resp is not None else None
                if resp is None:
                    espera = float(fallos_conexion)
                elif espera is None:
                    k = rechazos if es_limite else reintentos
                    espera = min(net.max_backoff, 2.0 * 2 ** k) * (0.5 + random.random() / 2)
                espera = min(espera, max(net.max_backoff, 300.0))
                if resp is not None:
                    if es_limite:
                        if not lim.diagnosticado:   # una vez: qué dice el servidor del límite
                            lim.diagnosticado = True
                            try:
                                cuerpo = " ".join(resp.read().decode("utf-8", "replace").split())[:300]
                            except Exception:
                                cuerpo = "?"
                            _log(f"[API {etiqueta}] primer rechazo por límite tras "
                                 f"{len(lim.marcas)} peticiones en el último minuto.\n"
                                 f"    respuesta: {cuerpo}\n    cabeceras: {_cabeceras_limite(resp.headers)}")
                        lim.rechazo()
                    resp.close()
                if es_limite:
                    rechazos += 1
                    esperado_limite += espera
                    _log(f"[API {etiqueta}] HTTP {resp.status_code} (límite): espero {espera:.0f}s "
                         f"(llevo {_tiempo(esperado_limite)} de un máximo de "
                         f"{_tiempo(net.rate_limit_max_wait)})")
                else:
                    reintentos += 1
                    motivo = f"HTTP {resp.status_code}" if resp is not None else "sin conexión"
                    _log(f"[API {etiqueta}] {motivo}: reintento {reintentos}/{net.max_retries} "
                         f"en {espera:.0f}s")
                lim.pausar(espera)

        def close(self):
            interno.close()

    return TransporteLimitado()


def crear_cliente(cfg: ServiceConfig):
    """Cliente OpenAI con proxy, TLS, límite de peticiones y reintentos. Devuelve
    (cliente, proxy usado). Los reintentos los hace el transporte; el SDK no reintenta."""
    try:
        from openai import OpenAI, DefaultHttpxClient
    except ImportError:
        sys.exit("Instala el cliente: pip install openai")
    hx = _modulo_http()
    proxy = proxy_efectivo(cfg) or None
    interno = hx.HTTPTransport(verify=contexto_ssl(cfg.net),
                               proxy=hx.Proxy(proxy) if proxy else None)
    etiqueta = urlparse(cfg.base_url).hostname or cfg.base_url
    transporte = _transporte(hx, interno, limitador_para(cfg), cfg.net, etiqueta)
    http = DefaultHttpxClient(transport=transporte, timeout=cfg.net.timeout, trust_env=False)
    cliente = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "local",
                     timeout=cfg.net.timeout, max_retries=0, http_client=http)
    return cliente, proxy


# --------------------------------------------------------------------------
# Diagnóstico
# --------------------------------------------------------------------------

def es_error_conexion(e: BaseException) -> bool:
    """Errores tras los que no tiene sentido seguir con la siguiente petición."""
    return type(e).__name__ in ("APIConnectionError", "APITimeoutError", "RateLimitError",
                                "ConflictError", "AuthenticationError", "PermissionDeniedError")


def diagnosticar(e: BaseException) -> str:
    """Traduce errores de conexión típicos de redes corporativas a una pista concreta."""
    cadena, x = [], e
    while x is not None and len(cadena) < 10:
        cadena.append(x)
        x = x.__cause__ or x.__context__
    texto = " | ".join(f"{type(c).__name__}: {c}" for c in cadena)
    if "CERTIFICATE_VERIFY_FAILED" in texto or any(isinstance(c, ssl.SSLCertVerificationError) for c in cadena):
        return ("certificado del servidor no verificado. Si hay un proxy que inspecciona TLS, "
                "indica la CA raíz corporativa en ca_cert (o ca_cert = \"system\" para usar el "
                "almacén de certificados del sistema).")
    if any(type(c).__name__ in ("RateLimitError", "ConflictError") for c in cadena) \
            or "rate limit" in texto.lower():
        return ("límite de peticiones de la API: el servidor siguió rechazando durante todo "
                "rate_limit_max_wait. Puede ser una cuota por hora o por día. Espera antes de "
                "relanzar, baja requests_per_minute o sube rate_limit_max_wait.")
    if "407" in texto:
        return "el proxy pide autenticación: proxy = \"http://usuario:clave@proxy:puerto\""
    if "CERTIFICATE_REQUIRED" in texto or "certificate required" in texto.lower() \
            or "HANDSHAKE_FAILURE" in texto:
        return "el servidor exige certificado cliente: client_cert y client_key"
    if "401" in texto or "AuthenticationError" in texto:
        return "credenciales rechazadas por la API: revisa api_key"
    if "404" in texto or "NotFoundError" in texto:
        return "ruta o modelo no encontrado: revisa base_url (¿falta /v1?) y el nombre del modelo"
    if any(type(c).__name__ == "ProxyError" for c in cadena):
        return "no se puede conectar a través del proxy: revisa proxy y no_proxy"
    if any(type(c).__name__ in ("ConnectError", "ConnectTimeout") for c in cadena):
        return ("no se puede conectar. Si el servidor es interno quizá no deba ir por el proxy "
                "(añádelo a no_proxy); si es externo quizá necesite proxy.")
    return texto[:400]


def describir(cfg: ServiceConfig) -> str:
    n = cfg.net
    tls = ("SIN verificar" if n.insecure else
           f"CA extra {n.ca_cert}" if n.ca_cert else "CAs del sistema + certifi")
    return (f"{cfg.base_url}  modelo={cfg.model}  api_key={ocultar_clave(cfg.api_key) or '—'}"
            + (f"  max_tokens={cfg.max_tokens}" if cfg.max_tokens else "") + "\n"
            f"  proxy: {ocultar_credenciales(proxy_efectivo(cfg)) or 'directo'} | TLS: {tls}"
            + (f" | cert. cliente {n.client_cert}" if n.client_cert else "")
            + f" | timeout {n.timeout:g}s"
            + f"\n  límites: {n.requests_per_minute or 'sin límite'} pet./min"
            + (f", ~{n.tokens_per_minute} tokens/min" if n.tokens_per_minute else "")
            + f", {n.max_concurrent or 'sin límite'} simultáneas; ante 409/429 espera hasta "
            f"{n.rate_limit_max_wait / 60:g} min, ante 5xx {n.max_retries} reintentos"
            + (", ritmo adaptativo" if n.adaptive else ""))


def comprobar(conn: Conexion) -> int:
    """Prueba real de chat y embeddings. Devuelve 0 si todo va bien."""
    print(f"configuración: {conn.fichero or 'sin fichero (defaults + entorno + argumentos)'}")
    fallos = 0
    for nombre, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{nombre}: {describir(cfg)}")
        try:
            cliente, _ = crear_cliente(cfg)
            t0 = time.time()
            if nombre == "LLM":
                r = cliente.chat.completions.create(
                    model=cfg.model, max_tokens=5,
                    messages=[{"role": "user", "content": "Reply with: ok"}])
                detalle = repr((r.choices[0].message.content or "").strip()[:30])
            else:
                r = cliente.embeddings.create(model=cfg.model, input=["prueba"])
                detalle = f"dimensión {len(r.data[0].embedding)}"
            print(f"  OK ({time.time() - t0:.1f}s): {detalle}")
        except SystemExit:
            raise
        except Exception as e:
            fallos += 1
            print(f"  FALLO: {diagnosticar(e)}")
    if conn.llm.net.insecure or conn.embeddings.net.insecure:
        print("\n[aviso] insecure desactiva la verificación TLS; úsalo solo para diagnosticar.")
    return 1 if fallos else 0


def mostrar(conn: Conexion) -> None:
    print(f"configuración: {conn.fichero or 'sin fichero (defaults + entorno + argumentos)'}")
    for nombre, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{nombre}: {describir(cfg)}")
        if cfg.net.no_proxy:
            print(f"  no_proxy: {cfg.net.no_proxy}")


# --------------------------------------------------------------------------
# Plantilla
# --------------------------------------------------------------------------

PLANTILLA = '''# conexion.toml — conexión con las APIs de LLM y embeddings
# Prioridad: argumentos de línea de comandos > este fichero > variables de entorno > defaults.
# Cualquier valor admite ${VARIABLE} o ${VARIABLE:-por_defecto} para no guardar secretos aquí.

[network]                       # común a LLM y embeddings
proxy = "${HTTPS_PROXY:-}"      # "http://usuario:${PROXY_PASS}@proxy.empresa:8080"
ca_cert = ""                    # "C:/certs/raiz_corporativa.pem", o "system" (almacén de Windows)
client_cert = ""                # solo si el servidor exige TLS mutuo
client_key = ""                 # contraseña de la clave: variable RAG_CLIENT_KEY_PASSWORD
no_proxy = "localhost,127.0.0.1,::1"
insecure = false                # true SOLO para diagnosticar problemas de certificados
timeout = 600

# Límites de la API. Si LLM y embeddings van al mismo servidor con la misma clave,
# comparten el contador. Deja algo de margen respecto al límite real.
requests_per_minute = 0         # 0 = sin límite; p. ej. 28 si el servidor permite 30
tokens_per_minute = 0           # 0 = sin límite; solo si el servidor limita también por tokens
adaptive = true                 # baja el ritmo al recibir rechazos y lo recupera después
max_concurrent = 0              # peticiones simultáneas; 0 = sin límite
max_retries = 6                 # reintentos ante errores 5xx o de conexión
rate_limit_max_wait = 900       # ante 409/429 se espera (sin fallar) hasta este total por petición (s)
max_backoff = 60                # espera máxima entre reintentos si no hay Retry-After (s)
retry_statuses = [409, 429, 500, 502, 503, 504]

[llm]
base_url = "http://localhost:11434/v1"
model = "gemma4"                # el tag que muestre `ollama list`
api_key = "${OPENAI_API_KEY:-local}"
temperature = 0.1
max_tokens = 0                  # 0 = el del servidor; súbelo (p. ej. 8192) si las respuestas salen cortadas

# [llm.network]                 # descomenta para sobrescribir la red solo del LLM
# proxy = ""

[embeddings]
base_url = ""                   # vacío = el mismo servidor que el LLM
model = "embeddinggemma"
api_key = ""                    # vacío = la misma que el LLM
batch = 32

# [embeddings.network]          # p. ej. embeddings en local sin proxy
# proxy = ""
'''


def main() -> None:
    ap = argparse.ArgumentParser(description="Configuración de conexión con las APIs")
    ap.add_argument("accion", choices=["init", "show", "check"])
    ap.add_argument("--force", action="store_true", help="init: sobrescribir si existe")
    añadir_args(ap)
    args = ap.parse_args()

    if args.accion == "init":
        destino = Path(args.config or NOMBRE_FICHERO)
        if destino.exists() and not args.force:
            sys.exit(f"{destino} ya existe (usa --force para sobrescribirlo)")
        destino.write_text(PLANTILLA, encoding="utf-8")
        print(f"creado {destino}")
        return
    conn = cargar(args)
    if args.accion == "show":
        mostrar(conn)
    else:
        sys.exit(comprobar(conn))


if __name__ == "__main__":
    main()
