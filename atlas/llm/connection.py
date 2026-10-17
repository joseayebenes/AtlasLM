#!/usr/bin/env python3
"""
connection.py — Everything related to connecting to the APIs (LLM and embeddings):
configuration file, proxy, certificates, OpenAI client creation and error
diagnosis.

Priority of each value (lowest to highest):
  1. defaults (some read standard variables: HTTPS_PROXY, NO_PROXY, SSL_CERT_FILE,
     OPENAI_BASE_URL, OPENAI_API_KEY, LLM_MODEL, EMBED_MODEL)
  2. configuration file: common [network] section, then [llm] / [embeddings] and
     their subsections [llm.network] / [embeddings.network]
  3. command-line arguments

File: --config PATH, or the RAG_CONFIG variable, or connection.toml in the current
directory, or connection.toml next to this module. Values accept ${VARIABLE} and
${VARIABLE:-default}, so keys and passwords need not be written in the file.

Standalone use:
  python -m atlas.llm.connection init     # writes an example connection.toml
  python -m atlas.llm.connection show     # effective configuration (secrets masked)
  python -m atlas.llm.connection check    # real request to the LLM and embeddings

Requirements: pip install openai  (+ tomli on Python < 3.11, + truststore for ca_cert="system")
"""

from __future__ import annotations

import argparse
import importlib
import json
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

FILE_NAME = "connection.toml"


# --------------------------------------------------------------------------
# Configuration model
# --------------------------------------------------------------------------

def _env(*names: str, default: str = "") -> str:
    for n in names:
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
    timeout: float = 600.0       # connecting, and between two reads of a request that is not a chat turn
    idle_timeout: float = 120.0  # chat turn: seconds without a chunk (streamed) before it is cut; 0 = no limit
    turn_timeout: float = 600.0  # chat turn: seconds in all before it is cut and asked again; 0 = no limit
    # API limits (0 = unlimited). If the LLM and embeddings go to the same server with
    # the same key, they share the counter.
    requests_per_minute: int = 0
    tokens_per_minute: int = 0   # estimated as characters/4 of the request body
    adaptive: bool = True        # slow down only when the server rejects for rate limit
    max_concurrent: int = 0
    max_retries: int = 6              # retries on 5xx or connection errors
    max_backoff: float = 60.0
    rate_limit_max_wait: float = 900.0  # max total wait per request on 409/429 (s)
    retry_statuses: list = field(default_factory=lambda: [409, 429, 500, 502, 503, 504])


@dataclass
class ServiceConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.1     # LLM only
    max_tokens: int = 0          # LLM only: output cap; 0 = the server default
    reasoning_effort: str = ""   # LLM only: "none" turns reasoning off (Ollama); "" = the server default
    extra_body: dict = field(default_factory=dict)   # LLM only: fields added as is to every chat request
    reasoning_field: str = "reasoning"   # LLM only: the field of an assistant message with tool calls in which
                                         # an agent gives the model back its reasoning; "" = never
    stream: str = ""             # LLM only: "" = stream unless the server cannot (atlas.llm.chat); true/false force it
    batch: int = 32              # embeddings only
    net: NetConfig = field(default_factory=NetConfig)


@dataclass
class Connection:
    llm: ServiceConfig
    embeddings: ServiceConfig
    file: str = ""               # path of the loaded file ("" if none)


# --------------------------------------------------------------------------
# Loading the file
# --------------------------------------------------------------------------

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _read_toml(path: Path) -> dict:
    try:
        import tomllib                      # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib         # Python < 3.11: pip install tomli
        except ImportError:
            sys.exit("To read the configuration file on Python < 3.11: pip install tomli")
    try:
        with open(path, "rb") as f:
            return _expand(tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"Syntax error in {path}: {e}")


def find_config_file(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            sys.exit(f"configuration file {p} does not exist")
        return p
    for candidate in (os.environ.get("RAG_CONFIG"), Path.cwd() / FILE_NAME,
                      Path(__file__).resolve().parent / FILE_NAME):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


_NET_KEYS = {f.name for f in fields(NetConfig)}
_SERVICE_KEYS = {f.name for f in fields(ServiceConfig)} - {"net"}


def _apply(obj, data: dict, where: str, allowed: set[str]):
    changes = {}
    for k, v in data.items():
        if k not in allowed:
            print(f"[warning] unknown key '{k}' in {where} (ignored)", file=sys.stderr)
            continue
        kind = type(getattr(obj, k))
        if kind is bool and isinstance(v, str):
            v = v.strip().lower() in ("1", "true", "yes", "si", "sí", "on")
        try:
            changes[k] = v if isinstance(v, kind) else kind(v)
        except (TypeError, ValueError):
            sys.exit(f"invalid value for '{k}' in {where}: {v!r}")
    return replace(obj, **changes)


# --------------------------------------------------------------------------
# Command-line arguments
# --------------------------------------------------------------------------

def add_args(ap: argparse.ArgumentParser, embeddings: bool = True) -> None:
    """All connection options. A None value means 'not given' (the file decides)."""
    g = ap.add_argument_group("connection (also in connection.toml)")
    g.add_argument("--config", default=None, help="configuration file (default connection.toml)")
    g.add_argument("--base-url", default=None, help="LLM API URL")
    g.add_argument("--model", default=None, help="chat model")
    g.add_argument("--api-key", default=None)
    g.add_argument("--temperature", type=float, default=None)
    if embeddings:
        g.add_argument("--embed-base-url", default=None, help="embeddings URL (default: the LLM one)")
        g.add_argument("--embed-model", default=None)
        g.add_argument("--embed-api-key", default=None)
    r = ap.add_argument_group("network: proxy and certificates (shared by LLM and embeddings)")
    r.add_argument("--proxy", default=None, help="http://user:password@proxy:8080")
    r.add_argument("--ca-cert", default=None, help="PEM with the corporate CA, or 'system'")
    r.add_argument("--client-cert", default=None, help="client certificate (mutual TLS)")
    r.add_argument("--client-key", default=None, help="private key of the client certificate")
    r.add_argument("--no-proxy", default=None, help="comma-separated hosts without proxy ('' = none)")
    r.add_argument("--insecure", action="store_true", default=None,
                   help="no TLS verification (for diagnosis only)")
    r.add_argument("--timeout", type=float, default=None, help="per-request timeout (s)")
    r.add_argument("--rpm", type=int, default=None, help="max requests per minute to the API")
    r.add_argument("--tpm", type=int, default=None, help="approximate max tokens per minute")
    r.add_argument("--max-concurrent", type=int, default=None, help="max simultaneous requests")
    if embeddings:
        r.add_argument("--embed-proxy", default=None, help="different proxy for embeddings only")
        r.add_argument("--embed-ca-cert", default=None, help="different CA for embeddings only")


def _from_args(args, mapping: dict[str, str]) -> dict:
    return {fld: getattr(args, option) for fld, option in mapping.items()
            if getattr(args, option, None) is not None}


def load(args: argparse.Namespace | None = None, file: str | None = None) -> Connection:
    """Builds the effective configuration: defaults < file < command line."""
    path = find_config_file(file or (getattr(args, "config", None) if args else None))
    data = _read_toml(path) if path else {}
    unknown = set(data) - {"network", "llm", "embeddings"}
    for k in unknown:
        print(f"[warning] unknown section [{k}] in {path} (ignored)", file=sys.stderr)

    # Common network
    net = NetConfig()
    net = _apply(net, data.get("network", {}), f"{path} [network]", _NET_KEYS)

    def service(name: str, defaults: ServiceConfig) -> ServiceConfig:
        sec = dict(data.get(name, {}))
        sub_net = sec.pop("network", {})
        s = _apply(defaults, sec, f"{path} [{name}]", _SERVICE_KEYS)
        return replace(s, net=_apply(net, sub_net, f"{path} [{name}.network]", _NET_KEYS))

    llm = service("llm", ServiceConfig(
        base_url=_env("OPENAI_BASE_URL", default="http://localhost:11434/v1"),
        model=_env("LLM_MODEL", default="gemma4"),
        api_key=_env("OPENAI_API_KEY", default="local")))
    emb = service("embeddings", ServiceConfig(model=_env("EMBED_MODEL", default="embeddinggemma")))

    # Command line
    if args is not None:
        net_cli = _from_args(args, {"proxy": "proxy", "ca_cert": "ca_cert", "client_cert": "client_cert",
                                    "client_key": "client_key", "no_proxy": "no_proxy",
                                    "insecure": "insecure", "timeout": "timeout",
                                    "requests_per_minute": "rpm", "tokens_per_minute": "tpm",
                                    "max_concurrent": "max_concurrent"})
        llm = replace(llm, **_from_args(args, {"base_url": "base_url", "model": "model",
                                               "api_key": "api_key", "temperature": "temperature"}),
                      net=replace(llm.net, **net_cli))
        emb = replace(emb, **_from_args(args, {"base_url": "embed_base_url", "model": "embed_model",
                                               "api_key": "embed_api_key"}),
                      net=replace(emb.net, **{**net_cli, **_from_args(
                          args, {"proxy": "embed_proxy", "ca_cert": "embed_ca_cert"})}))

    # Embeddings inherit the LLM URL and key when not given. The key is only inherited
    # together with the URL: a key is never sent from one server to a different one.
    if not emb.base_url:
        emb = replace(emb, base_url=llm.base_url, api_key=emb.api_key or llm.api_key)
    return Connection(llm=llm, embeddings=emb, file=str(path) if path else "")


# --------------------------------------------------------------------------
# TLS, proxy and clients
# --------------------------------------------------------------------------

def mask_credentials(url: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", url or "")


def mask_key(k: str) -> str:
    return "" if not k else (k[:3] + "***" if len(k) > 8 else "***")


def bypasses_proxy(host: str, no_proxy: str) -> bool:
    host = (host or "").lower()
    for pattern in (p.strip().lower() for p in no_proxy.split(",") if p.strip()):
        if pattern == "*" or host == pattern.lstrip(".") or host.endswith("." + pattern.lstrip(".")):
            return True
    return False


def effective_proxy(cfg: ServiceConfig) -> str:
    host = urlparse(cfg.base_url).hostname or ""
    return cfg.net.proxy if cfg.net.proxy and not bypasses_proxy(host, cfg.net.no_proxy) else ""


def ssl_context(net: NetConfig) -> ssl.SSLContext | bool:
    if net.insecure:
        return False
    if net.ca_cert.lower() == "system":
        try:
            import truststore
        except ImportError:
            sys.exit('ca_cert = "system" needs: pip install truststore')
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        ctx = ssl.create_default_context()      # on Windows it already includes the system store
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
        if net.ca_cert:
            path = Path(net.ca_cert).expanduser()
            if not path.exists():
                sys.exit(f"CA certificate {path} does not exist")
            if path.is_dir():
                ctx.load_verify_locations(capath=str(path))
            else:
                ctx.load_verify_locations(cafile=str(path))
    if net.client_cert:
        ctx.load_cert_chain(str(Path(net.client_cert).expanduser()),
                            str(Path(net.client_key).expanduser()) if net.client_key else None,
                            password=os.environ.get("RAG_CLIENT_KEY_PASSWORD") or None)
    return ctx


WINDOW = 60.0   # seconds of the requests_per_minute / tokens_per_minute window
RATE_STATUSES = {409, 429}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class RateLimiter:
    """Pacing shared by everything that goes to the same server with the same key.

    - Requests per minute, in a sliding window AND evenly spaced (60/rpm s between
      sends): this also holds if the server measures with a "bucket" or per second
      instead of per full minute.
    - Tokens per minute (approximate), for volume limits.
    - Adaptive: each rate-limit rejection lowers the pace by 30 %; after a minute without
      rejections it slowly recovers. If no rpm is configured, it discovers it.
    - Shared pause: after a rejection, or if the server says no requests are left, all
      threads wait."""

    def __init__(self, net: NetConfig, label: str):
        self.label = label
        self.rpm_conf = net.requests_per_minute
        self.rpm = float(net.requests_per_minute)
        self.tpm = net.tokens_per_minute
        self.adaptive = net.adaptive
        self.sem = threading.BoundedSemaphore(net.max_concurrent) if net.max_concurrent > 0 else None
        self.lock = threading.Lock()
        self.stamps: deque[float] = deque()
        self.tokens: deque[tuple[float, int]] = deque()
        self.last_send = 0.0
        self.paused_until = 0.0
        self.last_rejection = 0.0
        self.last_increase = 0.0
        self.rejections = 0
        self.diagnosed = False

    def _purge(self, now: float) -> None:
        while self.stamps and now - self.stamps[0] >= WINDOW:
            self.stamps.popleft()
        while self.tokens and now - self.tokens[0][0] >= WINDOW:
            self.tokens.popleft()

    def _recover(self, now: float) -> None:
        """Raises the pace by +1 req/min every 30 s without rejections, up to the configured one."""
        if not self.adaptive or not self.rejections or not self.rpm:
            return
        if now - self.last_rejection < WINDOW or now - self.last_increase < WINDOW / 2:
            return
        if self.rpm_conf and self.rpm >= self.rpm_conf:
            return
        self.rpm = min(self.rpm + 1, self.rpm_conf or self.rpm + 1)
        self.last_increase = now

    def wait(self, tokens: int = 0) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self._purge(now)
                self._recover(now)
                delay = self.paused_until - now
                if self.rpm:
                    if len(self.stamps) >= int(self.rpm):
                        delay = max(delay, WINDOW - (now - self.stamps[0]))
                    delay = max(delay, self.last_send + WINDOW / self.rpm - now)
                if self.tpm and self.tokens and sum(n for _, n in self.tokens) + tokens > self.tpm:
                    delay = max(delay, WINDOW - (now - self.tokens[0][0]))
                if delay <= 0:
                    self.stamps.append(now)
                    if self.tpm:
                        self.tokens.append((now, tokens))
                    self.last_send = now
                    return
            time.sleep(min(delay, 2.0) + 0.01)

    def pause(self, seconds: float) -> None:
        with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    def rejected(self) -> None:
        """The server rejected for rate limit: lower the pace (at most once every 10 s, so
        that several threads rejected at the same time do not sink it)."""
        with self.lock:
            now = time.monotonic()
            self.rejections += 1
            if not self.adaptive or now - self.last_rejection < 10:
                return
            self._purge(now)
            base = self.rpm or max(len(self.stamps), 2)
            self.rpm = max(1.0, base * 0.7)
            self.last_rejection = now
        _log(f"[API {self.label}] rate-limit rejection: pace lowered to {self.rpm:.0f} req/min "
             f"(it recovers on its own once rejections stop)")


_LIMITERS: dict[tuple, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def rate_limiter_for(cfg: ServiceConfig) -> RateLimiter:
    u = urlparse(cfg.base_url)
    key = (u.scheme, u.hostname, u.port, cfg.api_key)
    n = cfg.net
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(key)
        if lim is None:
            lim = _LIMITERS[key] = RateLimiter(n, u.hostname or cfg.base_url)
        else:                                    # if they differ, the most restrictive wins
            if n.requests_per_minute and (not lim.rpm_conf or n.requests_per_minute < lim.rpm_conf):
                lim.rpm_conf = n.requests_per_minute
                lim.rpm = min(lim.rpm or n.requests_per_minute, n.requests_per_minute)
            if n.tokens_per_minute and (not lim.tpm or n.tokens_per_minute < lim.tpm):
                lim.tpm = n.tokens_per_minute
        return lim


def _duration(v: str | None) -> float | None:
    """'6m0s', '1.5s', '20ms', '30', or an epoch timestamp in seconds or milliseconds
    (OpenRouter's X-RateLimit-Reset) -> seconds."""
    if not v:
        return None
    v = v.strip()
    try:
        x = float(v)
        if x > 1e11:                    # milliseconds: in seconds that would be after the year 5000
            x /= 1000
        return max(0.0, x - time.time()) if x > 1e9 else x
    except ValueError:
        pass
    total, ok = 0.0, False
    for num, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", v):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
        ok = True
    return total if ok else None


def _wait_from_headers(h) -> float | None:
    """If the server says no requests or tokens are left, how long to wait."""
    for remaining, reset in (("x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
                             ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
                             ("ratelimit-remaining", "ratelimit-reset"),
                             ("x-ratelimit-remaining", "x-ratelimit-reset"),
                             ("x-rate-limit-remaining", "x-rate-limit-reset")):
        r = h.get(remaining)
        if r is None:
            continue
        try:
            if float(r) <= 0:
                return _duration(h.get(reset)) or 1.0
        except ValueError:
            continue
    return None


def _human_time(sec: float) -> str:
    return f"{sec:.0f} s" if sec < 120 else f"{sec / 60:.1f} min"


def _limit_headers(h) -> str:
    return ", ".join(f"{k}: {v}" for k, v in h.items()
                     if "ratelimit" in k.lower() or "rate-limit" in k.lower() or "retry" in k.lower()) \
        or "no rate-limit headers"


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


def _is_tls_error(e: BaseException) -> bool:
    x, n = e, 0
    while x is not None and n < 10:
        if isinstance(x, ssl.SSLError) or "CERTIFICATE" in str(x) or "SSL" in type(x).__name__:
            return True
        x, n = x.__cause__ or x.__context__, n + 1
    return False


def _http_module():
    """httpx, or whatever HTTP client the installed openai version uses."""
    from openai import DefaultHttpxClient
    base = next(c for c in DefaultHttpxClient.__mro__
                if c.__name__ == "Client" and not c.__module__.startswith("openai"))
    return importlib.import_module(base.__module__.split(".")[0])


# What went to the APIs since the process started, through every client: the time
# profiles of `atlas run` keep what each step added.
_USAGE: dict[str, float] = {"chat": 0, "embeddings": 0, "other": 0, "rejected": 0, "retried": 0,
                            "wait_seconds": 0.0, "api_seconds": 0.0, "sent_kb": 0.0}
_USAGE_LOCK = threading.Lock()


def _count(**deltas: float) -> None:
    with _USAGE_LOCK:
        for k, v in deltas.items():
            _USAGE[k] += v


def usage() -> dict[str, float]:
    """Cumulative counters of the requests of this process to the APIs: requests sent by
    endpoint (`chat`, `embeddings`, `other`), rate-limit rejections (`rejected`), retries
    after 5xx or connection errors (`retried`), seconds waiting for the pace or a pause of
    the limiter (`wait_seconds`) and for the server (`api_seconds`), and kilobytes sent
    (`sent_kb`)."""
    with _USAGE_LOCK:
        return dict(_USAGE)


# The chat turn (atlas.llm.chat) that the request of this thread belongs to: the transport tells
# it when the request goes out, so that its time does not count the wait for the limiter
_TURN = threading.local()


def _sent() -> None:
    watch = getattr(_TURN, "watch", None)
    if watch is not None:
        watch.sent()


def _endpoint(request) -> str:
    path = request.url.path.rstrip("/")
    return "chat" if path.endswith("/chat/completions") else "embeddings" if path.endswith("/embeddings") \
        else "other"


def _transport(hx, inner, lim: RateLimiter, net: NetConfig, label: str):
    """HTTP transport that honours the limit and retries 409/429/5xx with growing waits,
    using Retry-After and the x-ratelimit-* headers if the server sends them."""

    class Held(hx.SyncByteStream):
        """The body of a response that holds its slot of max_concurrent until it is closed: a
        streamed answer is in flight until its last chunk, not just until its headers."""

        def __init__(self, stream, release):
            self.stream, self.release = stream, release

        def __iter__(self):
            yield from self.stream

        def close(self):
            try:
                self.stream.close()
            finally:
                self.release()

    def slot():
        """The release of a slot of max_concurrent, once whatever happens."""
        if not lim.sem:
            return lambda: None
        lim.sem.acquire()
        released = threading.Event()

        def release():
            if not released.is_set():
                released.set()
                lim.sem.release()
        return release

    class RateLimitedTransport(hx.BaseTransport):
        def handle_request(self, request):
            try:
                size = len(request.content)
            except Exception:
                size = 0
            tokens = size // 4 if lim.tpm else 0
            endpoint = _endpoint(request)
            retries = 0             # for server (5xx) or connection errors
            rejections = 0          # for rate limit (409/429): they do not count as retries
            waited_for_limit = 0.0  # total time waited for rate limit in this request
            connection_failures = 0
            while True:
                queued = time.monotonic()
                release = slot()
                try:
                    lim.wait(tokens)
                    sent = time.monotonic()
                    _count(**{endpoint: 1}, wait_seconds=sent - queued, sent_kb=size / 1024)
                    _sent()
                    try:
                        resp = inner.handle_request(request)
                    except hx.ConnectError as e:
                        # TLS errors (certificate, mutual TLS) are not fixed by retrying;
                        # a one-off connection failure is, but only a couple of times.
                        connection_failures += 1
                        if _is_tls_error(e) or connection_failures > 2 or retries >= net.max_retries:
                            raise
                        resp = None
                    finally:
                        _count(api_seconds=time.monotonic() - sent)
                except BaseException:
                    release()
                    raise
                if resp is None:
                    release()
                else:
                    resp.stream = Held(resp.stream, release)

                is_limit = resp is not None and resp.status_code in RATE_STATUSES
                if is_limit:
                    _count(rejected=1)
                if resp is not None:
                    notice = _wait_from_headers(resp.headers)
                    if notice:                    # the server says no quota is left
                        lim.pause(min(notice, max(net.max_backoff, 300.0)))
                    if resp.status_code not in net.retry_statuses:
                        return resp
                    if is_limit and waited_for_limit >= net.rate_limit_max_wait:
                        return resp
                    if not is_limit and retries >= net.max_retries:
                        return resp

                delay = _retry_after(resp.headers) if resp is not None else None
                if resp is None:
                    delay = float(connection_failures)
                elif delay is None:
                    k = rejections if is_limit else retries
                    delay = min(net.max_backoff, 2.0 * 2 ** k) * (0.5 + random.random() / 2)
                delay = min(delay, max(net.max_backoff, 300.0))
                if resp is not None:
                    if is_limit:
                        if not lim.diagnosed:   # once: what the server says about the limit
                            lim.diagnosed = True
                            try:
                                body = " ".join(resp.read().decode("utf-8", "replace").split())[:300]
                            except Exception:
                                body = "?"
                            _log(f"[API {label}] first rate-limit rejection after "
                                 f"{len(lim.stamps)} requests in the last minute.\n"
                                 f"    response: {body}\n    headers: {_limit_headers(resp.headers)}")
                        lim.rejected()
                    resp.close()
                if is_limit:
                    rejections += 1
                    waited_for_limit += delay
                    _log(f"[API {label}] HTTP {resp.status_code} (rate limit): waiting {delay:.0f}s "
                         f"({_human_time(waited_for_limit)} so far, out of a maximum of "
                         f"{_human_time(net.rate_limit_max_wait)})")
                else:
                    retries += 1
                    _count(retried=1)
                    reason = f"HTTP {resp.status_code}" if resp is not None else "no connection"
                    _log(f"[API {label}] {reason}: retry {retries}/{net.max_retries} "
                         f"in {delay:.0f}s")
                lim.pause(delay)

        def close(self):
            inner.close()

    return RateLimitedTransport()


def create_client(cfg: ServiceConfig):
    """OpenAI client with proxy, TLS, request limit and retries. Returns (client, proxy
    used). Retries are done by the transport; the SDK does not retry."""
    try:
        from openai import OpenAI, DefaultHttpxClient
    except ImportError:
        sys.exit("Install the client: pip install openai")
    hx = _http_module()
    proxy = effective_proxy(cfg) or None
    inner = hx.HTTPTransport(verify=ssl_context(cfg.net),
                             proxy=hx.Proxy(proxy) if proxy else None)
    label = urlparse(cfg.base_url).hostname or cfg.base_url
    transport = _transport(hx, inner, rate_limiter_for(cfg), cfg.net, label)
    http = DefaultHttpxClient(transport=transport, timeout=cfg.net.timeout, trust_env=False)
    client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key or "local",
                    timeout=cfg.net.timeout, max_retries=0, http_client=http)
    return client, proxy


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------

def is_connection_error(e: BaseException) -> bool:
    """Errors after which it makes no sense to go on with the next request."""
    return type(e).__name__ in ("APIConnectionError", "APITimeoutError", "TurnTimeout", "RateLimitError",
                                "ConflictError", "AuthenticationError", "PermissionDeniedError") \
        or getattr(e, "status_code", None) == 402          # no credit left


def _server_message(chain: list) -> str:
    """What the server said in the body of an HTTP error, if it said anything. OpenRouter
    puts what the provider of the model answered in metadata.raw."""
    for c in chain:
        body = getattr(c, "body", None)
        if isinstance(body, dict) and body.get("message"):
            raw = (body.get("metadata") or {}).get("raw") if isinstance(body.get("metadata"), dict) else None
            return (f"{body['message']}: {raw}" if isinstance(raw, str) and raw else str(body["message"]))[:300]
    return ""


def diagnose(e: BaseException) -> str:
    """Turns connection errors typical of corporate networks into a concrete hint."""
    chain, x = [], e
    while x is not None and len(chain) < 10:
        chain.append(x)
        x = x.__cause__ or x.__context__
    text = " | ".join(f"{type(c).__name__}: {c}" for c in chain)
    said = _server_message(chain)
    said = f" (the server says: {said})" if said else ""
    if "CERTIFICATE_VERIFY_FAILED" in text or any(isinstance(c, ssl.SSLCertVerificationError) for c in chain):
        return ("server certificate not verified. If a proxy inspects TLS, set the corporate "
                "root CA in ca_cert (or ca_cert = \"system\" to use the system certificate "
                "store).")
    if any(type(c).__name__ in ("RateLimitError", "ConflictError") for c in chain) \
            or "rate limit" in text.lower():
        return ("API request limit: the server kept rejecting for the whole rate_limit_max_wait. "
                "It may be an hourly or daily quota. Wait before running again, lower "
                f"requests_per_minute or raise rate_limit_max_wait{said}.")
    if "407" in text:
        return "the proxy requires authentication: proxy = \"http://user:password@proxy:port\""
    if "CERTIFICATE_REQUIRED" in text or "certificate required" in text.lower() \
            or "HANDSHAKE_FAILURE" in text:
        return "the server requires a client certificate: client_cert and client_key"
    if any(getattr(c, "status_code", None) == 402 for c in chain):
        return ("the API asks for payment (HTTP 402): no credit left, or the spending limit of the "
                f"key was reached. Add credit or raise the limit of the key{said}")
    if "401" in text or "AuthenticationError" in text:
        return "credentials rejected by the API: check api_key (and that its variable is set)"
    if "404" in text or "NotFoundError" in text:
        return f"path or model not found: check base_url (missing /v1?) and the model name{said}"
    if any(type(c).__name__ == "ProxyError" for c in chain):
        return "cannot connect through the proxy: check proxy and no_proxy"
    if any(type(c).__name__ in ("ConnectError", "ConnectTimeout") for c in chain):
        return ("cannot connect. If the server is internal it may not need the proxy (add it "
                "to no_proxy); if it is external it may need one.")
    return text[:400]


def describe(cfg: ServiceConfig) -> str:
    n = cfg.net
    tls = ("NOT verified" if n.insecure else
           f"extra CA {n.ca_cert}" if n.ca_cert else "system CAs + certifi")
    return (f"{mask_credentials(cfg.base_url)}  model={cfg.model}  api_key={mask_key(cfg.api_key) or '—'}"
            + (f"  max_tokens={cfg.max_tokens}" if cfg.max_tokens else "")
            + (f"  reasoning_effort={cfg.reasoning_effort}" if cfg.reasoning_effort else "")
            + (f"  extra_body={json.dumps(cfg.extra_body, ensure_ascii=False)}" if cfg.extra_body else "")
            + (f"  stream={str(cfg.stream).lower()}" if str(cfg.stream).strip() not in ("", "auto") else "") + "\n"
            f"  proxy: {mask_credentials(effective_proxy(cfg)) or 'direct'} | TLS: {tls}"
            + (f" | client cert {n.client_cert}" if n.client_cert else "")
            + f" | timeout {n.timeout:g}s"
            + (f"; a chat turn: {n.idle_timeout:g}s idle, {n.turn_timeout:g}s in all"
               if n.idle_timeout or n.turn_timeout else "")
            + f"\n  limits: {n.requests_per_minute or 'unlimited'} req/min"
            + (f", ~{n.tokens_per_minute} tokens/min" if n.tokens_per_minute else "")
            + f", {n.max_concurrent or 'unlimited'} simultaneous; on 409/429 waits up to "
            f"{n.rate_limit_max_wait / 60:g} min, on 5xx {n.max_retries} retries"
            + (", adaptive pace" if n.adaptive else ""))


# Output budget of the check request. Reasoning models think before answering and that
# reasoning counts against max_tokens: with a tiny budget they never get to the answer.
CHECK_MAX_TOKENS = 512


def reasoning_of(message) -> str:
    """The reasoning a server gives with a message: `reasoning` (OpenRouter, Ollama),
    `reasoning_content` (vLLM, LM Studio) or the texts of `reasoning_details` (OpenRouter).
    The OpenAI client keeps the fields it does not know in `model_extra`."""
    extra = getattr(message, "model_extra", None) or {}
    for name in ("reasoning", "reasoning_content"):
        text = getattr(message, name, None) or extra.get(name)
        if isinstance(text, str) and text.strip():
            return text
    details = getattr(message, "reasoning_details", None) or extra.get("reasoning_details") or []
    texts = [d.get("text") if isinstance(d, dict) else getattr(d, "text", None) for d in details]
    return "\n".join(t for t in texts if isinstance(t, str) and t.strip())


def _reasoning(response, message) -> str:
    """Describes the reasoning of a reasoning model, or "" if there is none. The text comes
    as `reasoning` (Ollama) or `reasoning_content` (vLLM, LM Studio); servers that hide it
    may still report `usage.completion_tokens_details.reasoning_tokens`."""
    for name in ("reasoning", "reasoning_content"):
        text = getattr(message, name, None)
        if isinstance(text, str) and text.strip():
            return f"{len(text)} characters of reasoning"
    details = getattr(getattr(response, "usage", None), "completion_tokens_details", None)
    tokens = getattr(details, "reasoning_tokens", None)
    return f"{tokens} reasoning tokens" if tokens else ""


def _check_chat(response) -> tuple[bool, str]:
    """(ok, detail) for the answer to the check request."""
    choice = response.choices[0]
    answer = (choice.message.content or "").strip()
    reasoning = _reasoning(response, choice.message)
    if answer:
        return True, repr(answer[:30]) + (f" (reasoning model: {reasoning})" if reasoning else "")
    if choice.finish_reason == "length" and reasoning:
        return False, (f"the server answered, but the model spent the whole {CHECK_MAX_TOKENS}-token "
                       f"budget reasoning ({reasoning}) and wrote no answer. Check that it is not "
                       "stuck reasoning, or turn reasoning off in the server.")
    if choice.finish_reason == "length":
        return False, (f"the server answered, but the reply was cut at {CHECK_MAX_TOKENS} tokens "
                       "before any text")
    return False, f"the server answered, but the model returned no text (finish_reason={choice.finish_reason})"


def _structured_output(cfg: ServiceConfig) -> str:
    """The response_format tier the server accepts (json_schema | json_object | none), with
    one small request per tier it rejects."""
    from atlas.llm.structured import JsonLlm
    try:
        return JsonLlm(cfg).probe_structured()
    except Exception as e:
        return f"unknown ({diagnose(e)})"


def check(conn: Connection) -> int:
    """Real chat and embeddings request. Returns 0 if everything works."""
    print(f"configuration: {conn.file or 'no file (defaults + environment + arguments)'}")
    failures = 0
    for name, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{name}: {describe(cfg)}")
        try:
            client, _ = create_client(cfg)
            t0 = time.time()
            if name == "LLM":
                r = client.chat.completions.create(
                    model=cfg.model, max_tokens=CHECK_MAX_TOKENS,
                    messages=[{"role": "user", "content": "Reply with: ok"}],
                    **({"reasoning_effort": cfg.reasoning_effort} if cfg.reasoning_effort else {}),
                    **({"extra_body": cfg.extra_body} if cfg.extra_body else {}))
                ok, detail = _check_chat(r)
            else:
                r = client.embeddings.create(model=cfg.model, input=["test"])
                ok, detail = True, f"dimension {len(r.data[0].embedding)}"
            print(f"  {'OK' if ok else 'FAILED'} ({time.time() - t0:.1f}s): {detail}")
            if not ok:
                failures += 1
            elif name == "LLM":
                print(f"  structured output: {_structured_output(cfg)}")
        except SystemExit:
            raise
        except Exception as e:
            failures += 1
            print(f"  FAILED: {diagnose(e)}")
    if conn.llm.net.insecure or conn.embeddings.net.insecure:
        print("\n[warning] insecure turns TLS verification off; use it only for diagnosis.")
    return 1 if failures else 0


def show(conn: Connection) -> None:
    print(f"configuration: {conn.file or 'no file (defaults + environment + arguments)'}")
    for name, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{name}: {describe(cfg)}")
        if cfg.net.no_proxy:
            print(f"  no_proxy: {cfg.net.no_proxy}")


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------

TEMPLATE = '''# connection.toml — connection to the LLM and embeddings APIs
# Priority: command-line arguments > this file > environment variables > defaults.
# Any value accepts ${VARIABLE} or ${VARIABLE:-default} so secrets need not be stored here.

[network]                       # shared by LLM and embeddings
proxy = "${HTTPS_PROXY:-}"      # "http://user:${PROXY_PASS}@proxy.company:8080"
ca_cert = ""                    # "C:/certs/corporate_root.pem", or "system" (Windows store)
client_cert = ""                # only if the server requires mutual TLS
client_key = ""                 # key password: RAG_CLIENT_KEY_PASSWORD variable
no_proxy = "localhost,127.0.0.1,::1"
insecure = false                # true ONLY to diagnose certificate problems
timeout = 600                   # connecting, and between two reads of what is not a chat turn (s)
idle_timeout = 120              # a chat turn without a chunk for this long is cut and asked again (s)
turn_timeout = 600              # a chat turn that takes longer in all is cut and asked again (s)

# API limits. If the LLM and embeddings go to the same server with the same key, they
# share the counter. Leave some margin below the real limit.
requests_per_minute = 0         # 0 = unlimited; e.g. 28 if the server allows 30
tokens_per_minute = 0           # 0 = unlimited; only if the server also limits by tokens
adaptive = true                 # slows down on rejections and recovers afterwards
max_concurrent = 0              # simultaneous requests; 0 = unlimited
max_retries = 6                 # retries on 5xx or connection errors
rate_limit_max_wait = 900       # on 409/429 it waits (without failing) up to this total per request (s)
max_backoff = 60                # max wait between retries when there is no Retry-After (s)
retry_statuses = [409, 429, 500, 502, 503, 504]

[llm]
base_url = "http://localhost:11434/v1"
model = "gemma4"                # the tag shown by `ollama list`
api_key = "${OPENAI_API_KEY:-local}"
temperature = 0.1
max_tokens = 0                  # 0 = the server default; raise it (e.g. 8192) if answers come out cut
reasoning_effort = ""           # "none" turns reasoning off (gemma4 in Ollama reasons by default)
reasoning_field = "reasoning"   # agents: the reasoning of a turn with tool calls goes back to the model in this
                                # field of the message (gemma 4 asks for it); "reasoning_content" for some
                                # servers; "" = never
stream = ""                     # "" = streamed unless the server cannot (atlas probe says it); true / false force it

# [llm.extra_body]              # fields added as is to every chat request, for options of the server
# provider = { order = ["deepinfra"], allow_fallbacks = false }   # OpenRouter: always the same provider
# chat_template_kwargs = { enable_thinking = false }              # vLLM: no reasoning with Qwen3

# [llm.network]                 # uncomment to override the network for the LLM only
# proxy = ""

[embeddings]
base_url = ""                   # empty = the same server as the LLM
model = "embeddinggemma"
api_key = ""                    # empty = the same as the LLM
batch = 32

# [embeddings.network]          # e.g. local embeddings without proxy
# proxy = ""
'''


def main() -> None:
    ap = argparse.ArgumentParser(description="API connection configuration")
    ap.add_argument("action", choices=["init", "show", "check"])
    ap.add_argument("--force", action="store_true", help="init: overwrite if it exists")
    add_args(ap)
    args = ap.parse_args()

    if args.action == "init":
        target = Path(args.config or FILE_NAME)
        if target.exists() and not args.force:
            sys.exit(f"{target} already exists (use --force to overwrite it)")
        target.write_text(TEMPLATE, encoding="utf-8")
        print(f"created {target}")
        return
    conn = load(args)
    if args.action == "show":
        show(conn)
    else:
        sys.exit(check(conn))


if __name__ == "__main__":
    main()
