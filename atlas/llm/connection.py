"""
connection.py — Everything related to connecting to the APIs (LLM and embeddings):
configuration model, proxy, certificates, OpenAI client creation and error diagnosis.

The configuration comes from the [llm], [embeddings] and [network] sections of atlas.toml
(`atlas.llm.from_config`). The defaults of the network read the standard variables
HTTPS_PROXY, NO_PROXY and SSL_CERT_FILE.

Warnings (rate-limit rejections, retries) go to the "atlas.llm" logger; a configuration
that cannot work (a CA certificate that does not exist, a missing package) raises
ConfigError.

Requirements: pip install openai  (+ truststore for ca_cert="system")
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import random
import re
import ssl
import sys
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from atlas.config import ConfigError

__all__ = ["CHECK_MAX_TOKENS", "Connection", "NetConfig", "RateLimiter", "ServiceConfig", "SharedWindow",
           "bypasses_proxy", "check", "check_service", "create_client", "describe", "diagnose", "effective_proxy",
           "is_connection_error", "limits_dir", "mask_credentials", "mask_key", "note_usage", "rate_limiter_for",
           "reasoning_of", "served_context", "set_usage_label", "show", "ssl_context", "usage", "usage_label",
           "usage_since"]

log = logging.getLogger("atlas.llm")


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
    max_concurrent: int = 0      # in this process
    shared_limit: bool = True    # with a limit: the window is shared with the other processes of the workspace
    limits_dir: str = "limits"   # the folder of that window, relative to the workspace (atlas.llm.from_config)
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
    concurrency: int = 0         # LLM only: requests of one JsonLlm in flight; 0 = no cap (in docs, the workers)
    context_tokens: int = 0      # LLM only: the context of the model (input + output); 0 = unknown
    batch: int = 32              # embeddings only
    family: str = ""             # embeddings only: embeddinggemma | nomic | e5 | none; "" = from the model name
    query_prefix: str = ""       # embeddings only: literal prefixes, instead of those of the family
    document_prefix: str = ""
    net: NetConfig = field(default_factory=NetConfig)


@dataclass
class Connection:
    llm: ServiceConfig
    embeddings: ServiceConfig
    file: str = ""               # path of the loaded file ("" if none)


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
            raise ConfigError('ca_cert = "system" needs: pip install truststore') from None
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        ctx = ssl.create_default_context()      # on Windows it already includes the system store
        if sys.version_info >= (3, 13) and hasattr(ssl, "VERIFY_X509_STRICT"):
            # 3.13 turns on the strict X.509 checks of OpenSSL, which reject certificates
            # without an Authority Key Identifier, such as those of some proxies that inspect
            # TLS. Off, as in 3.12: the chain and the host name are still verified.
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            log.debug("TLS: VERIFY_X509_STRICT off (as in Python 3.12); the chain and the host name "
                      "are still verified")
        try:
            import certifi
            ctx.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
        if net.ca_cert:
            path = Path(net.ca_cert).expanduser()
            if not path.exists():
                raise ConfigError(f"CA certificate {path} does not exist")
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


_LOGGER = logging.getLogger("atlas.llm")


def _log(msg: str) -> None:
    """A notice of the limiter or of the retries: a warning of atlas.llm, which [project]
    log_level governs and the log file keeps."""
    _LOGGER.warning(msg)


def limits_dir(net: NetConfig | None = None) -> Path | None:
    """The folder of the windows that the limiter shares between the processes of the
    workspace: ATLAS_LIMITS_DIR, or the limits_dir of `net` once atlas.llm.from_config made
    it absolute (<workspace>/limits by default); None otherwise (a NetConfig made by hand,
    or limits_dir = ""): nothing is written outside the workspace unless asked."""
    if os.environ.get("ATLAS_LIMITS_DIR"):
        return Path(os.environ["ATLAS_LIMITS_DIR"]).expanduser()
    folder = Path(net.limits_dir).expanduser() if net is not None and net.limits_dir else None
    return folder if folder is not None and folder.is_absolute() else None


def limit_file(cfg: "ServiceConfig") -> Path | None:
    """<limits_dir>/<host>-<port>.json: one window per destination; None without a folder.
    Neither the name nor the content has anything of the key (docs/seguridad.md: no hash of
    a secret on disk)."""
    folder = limits_dir(cfg.net)
    if folder is None:
        return None
    u = urlparse(cfg.base_url)
    host = re.sub(r"[^A-Za-z0-9._-]", "_", u.hostname or "server")
    return folder / f"{host}-{u.port or (443 if u.scheme == 'https' else 80)}.json"


class SharedWindow:
    """The window of a destination shared by the processes of the workspace (atlas serve,
    a long atlas run, atlas develop): a JSON file with the times of the sends of the last
    minute and the adapted pace, read and written under the operating system's lock on a
    file beside it (flock on POSIX, msvcrt.locking on Windows), as the workspace lock of
    atlas.pipeline. The lock is only held while one send is decided, never during a request.

    A broken file is written again from scratch; a window nobody touched for STALE seconds
    starts afresh (a pace lowered yesterday does not hold today)."""

    LOCK_WAIT = 2.0      # seconds trying to lock before pacing alone
    STALE = 600.0

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self._fd: int | None = None

    def _try_lock(self, fd: int) -> bool:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self) -> dict:
        """Locks and returns the state ({} if there is none, it is broken or stale). Raises
        OSError if the file cannot be made or locked within LOCK_WAIT seconds."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.LOCK_WAIT
        while not self._try_lock(fd):
            if time.monotonic() > deadline:
                os.close(fd)
                raise OSError(f"{self.lock_path} stayed locked for {self.LOCK_WAIT:g} s")
            time.sleep(0.01)
        self._fd = fd
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("stamps", []), list):
                raise ValueError("not a window")
            [float(t) for t in state.get("stamps", [])]
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError):
            _log(f"[limits] {self.path} was broken: it is rewritten")
            return {}
        if time.time() - float(state.get("updated", 0) or 0) > self.STALE:
            return {}
        return state

    def release(self, state: dict | None) -> None:
        """Writes `state` (None: nothing changed) and unlocks."""
        fd, self._fd = self._fd, None
        try:
            if state is not None:
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps({**state, "updated": time.time()}), encoding="utf-8")
                os.replace(tmp, self.path)
        finally:
            if fd is not None:
                if os.name == "nt":
                    try:
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                os.close(fd)             # on POSIX this releases the flock


class RateLimiter:
    """Pacing shared by everything that goes to the same server with the same key.

    - Requests per minute, in a sliding window AND evenly spaced (60/rpm s between
      sends): this also holds if the server measures with a "bucket" or per second
      instead of per full minute.
    - Tokens per minute (approximate), for volume limits.
    - Adaptive: each rate-limit rejection lowers the pace by 30 %; after a minute without
      rejections it slowly recovers. If no rpm is configured, it discovers it.
    - Shared pause: after a rejection, or if the server says no requests are left, all
      threads wait.
    - With `shared` (a SharedWindow), the window, the adapted pace and the pauses are those
      of every process of the workspace that goes to the same destination; if the file
      cannot be locked, this process paces on its own, in memory."""

    def __init__(self, net: NetConfig, label: str, shared: "SharedWindow | None" = None):
        self.label = label
        self.rpm_conf = net.requests_per_minute
        self.rpm = float(net.requests_per_minute)
        self.tpm = net.tokens_per_minute
        self.adaptive = net.adaptive
        self.sem = threading.BoundedSemaphore(net.max_concurrent) if net.max_concurrent > 0 else None
        self.lock = threading.Lock()
        self.shared = shared
        self.clock = time.time if shared else time.monotonic     # the shared window is in epoch seconds
        self.stamps: deque[float] = deque()
        self.tokens: deque[tuple[float, int]] = deque()
        self.last_send = 0.0
        self.paused_until = 0.0
        self.last_rejection = 0.0
        self.last_increase = 0.0
        self.rejections = 0
        self.diagnosed = False

    # -- the shared window (with self.lock held) ------------------------------------------
    def _load(self) -> bool:
        """Takes the state of the shared window; False if there is none (or it failed)."""
        if self.shared is None:
            return False
        try:
            st = self.shared.acquire()
        except OSError as e:
            _log(f"[API {self.label}] the window shared with other processes ({self.shared.path}) cannot be "
                 f"used ({e}): this process paces on its own")
            self.shared = None
            return False
        self.stamps = deque(sorted(float(t) for t in st.get("stamps", [])))
        self.tokens = deque((float(t), int(n)) for t, n in st.get("tokens", []))
        for name in ("last_send", "paused_until", "last_rejection", "last_increase"):
            setattr(self, name, float(st.get(name, 0) or 0))
        self.rejections = int(st.get("rejections", 0) or 0)
        adapted = float(st.get("rpm", 0) or 0)
        self.rpm = min(adapted, self.rpm_conf) if adapted and self.rpm_conf else (adapted or float(self.rpm_conf))
        return True

    def _save(self, changed: bool = True) -> None:
        if self.shared is None:
            return
        self.shared.release({"stamps": list(self.stamps), "tokens": [list(t) for t in self.tokens],
                             "rpm": self.rpm if self.rejections else 0, "rejections": self.rejections,
                             "last_send": self.last_send, "paused_until": self.paused_until,
                             "last_rejection": self.last_rejection, "last_increase": self.last_increase}
                            if changed else None)

    def _purge(self, now: float) -> None:
        while self.stamps and now - self.stamps[0] >= WINDOW:
            self.stamps.popleft()
        while self.tokens and now - self.tokens[0][0] >= WINDOW:
            self.tokens.popleft()

    def _recover(self, now: float) -> bool:
        """Raises the pace by +1 req/min every 30 s without rejections, up to the configured one."""
        if not self.adaptive or not self.rejections or not self.rpm:
            return False
        if now - self.last_rejection < WINDOW or now - self.last_increase < WINDOW / 2:
            return False
        if self.rpm_conf and self.rpm >= self.rpm_conf:
            return False
        self.rpm = min(self.rpm + 1, self.rpm_conf or self.rpm + 1)
        self.last_increase = now
        return True

    def wait(self, tokens: int = 0) -> None:
        while True:
            with self.lock:
                shared = self._load()
                try:
                    now = self.clock()
                    self._purge(now)
                    recovered = self._recover(now)
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
                finally:
                    if shared:
                        self._save(changed=delay <= 0 or recovered)
                if delay <= 0:
                    return
            time.sleep(min(delay, 2.0) + 0.01)

    def pause(self, seconds: float) -> None:
        with self.lock:
            shared = self._load()
            self.paused_until = max(self.paused_until, self.clock() + seconds)
            if shared:
                self._save()

    def rejected(self) -> None:
        """The server rejected for rate limit: lower the pace (at most once every 10 s, so
        that several threads, or processes, rejected at the same time do not sink it)."""
        with self.lock:
            shared = self._load()
            try:
                now = self.clock()
                self.rejections += 1
                if not self.adaptive or now - self.last_rejection < 10:
                    return
                self._purge(now)
                base = self.rpm or max(len(self.stamps), 2)
                self.rpm = max(1.0, base * 0.7)
                self.last_rejection = now
            finally:
                if shared:
                    self._save()
        _log(f"[API {self.label}] rate-limit rejection: pace lowered to {self.rpm:.0f} req/min "
             f"(it recovers on its own once rejections stop)")


_LIMITERS: dict[tuple, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def rate_limiter_for(cfg: ServiceConfig) -> RateLimiter:
    """The limiter of the destination of `cfg` in this process (one per server and key) and,
    with [network] shared_limit and a limit, on the window that the processes of the
    workspace share (SharedWindow)."""
    u = urlparse(cfg.base_url)
    key = (u.scheme, u.hostname, u.port, cfg.api_key)
    n = cfg.net
    with _LIMITERS_LOCK:
        lim = _LIMITERS.get(key)
        if lim is None:
            path = limit_file(cfg) if n.shared_limit and (
                n.requests_per_minute > 0 or n.tokens_per_minute > 0) else None
            shared = SharedWindow(path) if path else None
            lim = _LIMITERS[key] = RateLimiter(n, u.hostname or cfg.base_url, shared)
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
                            "wait_seconds": 0.0, "api_seconds": 0.0, "chat_seconds": 0.0, "embeddings_seconds": 0.0,
                            "other_seconds": 0.0, "sent_kb": 0.0,
                            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
                            "embedding_tokens": 0, "cost": 0.0}
_USAGE_LOCK = threading.Lock()
_LABEL = ["", ""]       # (command, step or agent): see set_usage_label


def _count(**deltas: float) -> None:
    with _USAGE_LOCK:
        for k, v in deltas.items():
            _USAGE[k] += v


def usage() -> dict[str, float]:
    """Cumulative counters of the requests of this process to the APIs: requests sent by
    endpoint (`chat`, `embeddings`, `other`), rate-limit rejections (`rejected`), retries
    after 5xx or connection errors (`retried`), seconds waiting for the pace or a pause of
    the limiter (`wait_seconds`) and for the server (`api_seconds`, and by endpoint
    `chat_seconds`, `embeddings_seconds`, `other_seconds`: divided by the requests, the
    latency), kilobytes sent (`sent_kb`), and what the servers said each answer used (see
    note_usage): tokens of the chat prompts (`prompt_tokens`, of them `cached_tokens`), of
    its answers (`completion_tokens`, of them `reasoning_tokens`), of the texts embedded
    (`embedding_tokens`), and `cost` when the server gives it (OpenRouter)."""
    with _USAGE_LOCK:
        return dict(_USAGE)


def usage_since(before: dict[str, float]) -> dict[str, float]:
    """What usage() grew since `before` (an earlier usage()), without the counters that did
    not move."""
    return {k: round(v - before.get(k, 0), 6) for k, v in usage().items() if v != before.get(k, 0)}


def _number(obj, name: str) -> float:
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def note_usage(response, endpoint: str = "chat") -> dict[str, float]:
    """Adds to usage() what the `usage` of an answer says: prompt and completion tokens,
    reasoning tokens (completion_tokens_details), cached prompt tokens (prompt_tokens_details)
    and the cost when the server gives it. The prompt tokens of an `embeddings` answer go to
    `embedding_tokens`. Returns what it added; nothing if the answer has no usage. Called by
    every place that gets an answer: JsonLlm, Embedder, AtlasLlm, check and probe."""
    u = getattr(response, "usage", None)
    if u is None:
        return {}
    field = lambda name: u.get(name) if isinstance(u, dict) else getattr(u, name, None)   # a streamed answer: a dict
    grown = {"prompt_tokens": _number(u, "prompt_tokens"), "completion_tokens": _number(u, "completion_tokens"),
             "reasoning_tokens": _number(field("completion_tokens_details"), "reasoning_tokens"),
             "cached_tokens": _number(field("prompt_tokens_details"), "cached_tokens"),
             "cost": _number(u, "cost")}
    if endpoint == "embeddings":
        grown = {"embedding_tokens": grown["prompt_tokens"], "cost": grown["cost"]}
    grown = {k: v for k, v in grown.items() if v}
    if grown:
        _count(**grown)
    return grown


def set_usage_label(command: str, step_or_agent: str = "") -> None:
    """What this process is doing: the command (atlas.cli sets it when it starts: run, ask,
    serve...) and its step or agent (the Runner sets it for each step). It goes in the lines
    of the log file and in the usage log of the workspace."""
    _LABEL[:] = [command, step_or_agent]


def usage_label() -> tuple[str, str]:
    """(command, step or agent) of set_usage_label."""
    return _LABEL[0], _LABEL[1]
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
                        took = time.monotonic() - sent
                        _count(api_seconds=took, **{f"{endpoint}_seconds": took})
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
        raise ConfigError("Install the client: pip install openai") from None
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
    if any(m in text for m in ("Missing Authority Key Identifier", "Missing Subject Key Identifier", "X509_V_FLAG")):
        return ("the server certificate (or that of a proxy that inspects TLS) does not pass the strict "
                "X.509 checks of OpenSSL: it lacks an extension that Python 3.13 requires. Set ca_cert = "
                "\"system\" (the certificate store of the system), or use Python 3.12.")
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
            + (f"  context_tokens={cfg.context_tokens}" if cfg.context_tokens else "")
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
            + (", adaptive pace" if n.adaptive else "")
            + (f"; shared with the other processes of this workspace ({limit_file(cfg)})"
               if n.shared_limit and (n.requests_per_minute or n.tokens_per_minute) and limit_file(cfg) else ""))


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


def served_context(cfg: ServiceConfig, client) -> tuple[int, str]:
    """The context of the model of `cfg` in tokens, as its server publishes it, and where it
    says so: GET /models with `context_length` (OpenRouter) or `max_model_len` (vLLM), or
    Ollama's /api/show (outside /v1) with the `num_ctx` of the model. (0, why) when it says
    nothing usable: Ollama without num_ctx only says what the model admits, not what the
    server gives it (OLLAMA_CONTEXT_LENGTH)."""
    try:
        listed = client.get("/models", cast_to=object)
    except Exception:
        listed = None
    for m in (listed.get("data") or []) if isinstance(listed, dict) else []:
        if not isinstance(m, dict) or m.get("id") != cfg.model:
            continue
        for key, where in (("context_length", "context_length of GET /models, as OpenRouter publishes it"),
                           ("max_model_len", "max_model_len of GET /models, as vLLM publishes it")):
            if isinstance(m.get(key), int) and not isinstance(m[key], bool) and m[key] > 0:
                return m[key], where
    root = re.sub(r"/v1/?$", "", cfg.base_url.rstrip("/"))
    try:
        shown = client.with_options(base_url=root).post("/api/show", body={"model": cfg.model}, cast_to=object)
    except Exception:
        return 0, ""
    if not isinstance(shown, dict):
        return 0, ""
    found = re.search(r"\bnum_ctx\s+(\d+)", str(shown.get("parameters") or ""))
    if found:
        return int(found[1]), "num_ctx of the model in Ollama (/api/show)"
    admits = next((v for k, v in (shown.get("model_info") or {}).items() if k.endswith(".context_length")), 0)
    return 0, (f"Ollama says the model admits {admits} tokens, but it gives it OLLAMA_CONTEXT_LENGTH (or the num_ctx "
               "of a Modelfile): set [llm] context_tokens to that" if admits else "")


def _check_context(cfg: ServiceConfig, client) -> str:
    """The line of `check` about the context of the model: the one the server publishes,
    proposed for [llm] context_tokens or compared with it."""
    tokens, where = served_context(cfg, client)
    mine = cfg.context_tokens
    if not tokens:
        return (f"  context: {where}" if where else "  context: the server does not publish it") + \
            (f"; [llm] context_tokens = {mine}" if mine else "; set [llm] context_tokens by hand (0 = unknown)")
    if not mine:
        return f"  context: {tokens} tokens ({where}): set [llm] context_tokens = {tokens}"
    if mine != tokens:
        return f"  WARNING context: [llm] context_tokens = {mine}, but the server says {tokens} ({where})"
    return f"  context: {tokens} tokens, as [llm] context_tokens ({where})"


def _structured_output(cfg: ServiceConfig) -> str:
    """The response_format tier the server accepts (json_schema | json_object | none), with
    one small request per tier it rejects."""
    from atlas.llm.structured import JsonLlm
    try:
        return JsonLlm(cfg).probe_structured()
    except Exception as e:
        return f"unknown ({diagnose(e)})"


def check_service(name: str, cfg: ServiceConfig) -> dict:
    """One real request to a service ("LLM": a short chat; otherwise an embedding):
    {"ok", "detail", "seconds"}, and "warning" when the chat model reasons and
    reasoning_effort does not say anything. A failure comes with the diagnosis in "detail"."""
    t0, warning = time.time(), ""
    try:
        client, _ = create_client(cfg)
        if name == "LLM":
            r = client.chat.completions.create(
                model=cfg.model, max_tokens=CHECK_MAX_TOKENS,
                messages=[{"role": "user", "content": "Reply with: ok"}],
                **({"reasoning_effort": cfg.reasoning_effort} if cfg.reasoning_effort else {}),
                **({"extra_body": cfg.extra_body} if cfg.extra_body else {}))
            note_usage(r)
            ok, detail = _check_chat(r)
            if _reasoning(r, r.choices[0].message) and not cfg.reasoning_effort:
                warning = ("the model reasons and reasoning_effort is empty: documents will be analysed more "
                           "slowly. Set reasoning_effort = \"none\" in [llm] (an agent that needs reasoning "
                           "turns it on in its file)")
        else:
            r = client.embeddings.create(model=cfg.model, input=["test"])
            note_usage(r, "embeddings")
            ok, detail = True, f"dimension {len(r.data[0].embedding)}"
    except ConfigError:
        raise
    except Exception as e:
        return {"ok": False, "detail": diagnose(e), "seconds": time.time() - t0}
    return {"ok": ok, "detail": detail, "seconds": time.time() - t0, **({"warning": warning} if warning else {})}


def check(conn: Connection, notes: dict[str, list[str]] | None = None) -> int:
    """Real chat and embeddings request. Returns 0 if everything works. `notes`: lines
    printed under a service ("LLM", "Embeddings") after its result."""
    print(f"configuration: {conn.file or 'no file (defaults + environment + arguments)'}")
    failures = 0
    for name, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{name}: {describe(cfg)}")
        res = check_service(name, cfg)
        print(f"  {'OK' if res['ok'] else 'FAILED'} ({res['seconds']:.1f}s): {res['detail']}")
        if res.get("warning"):
            print(f"  [warning] {res['warning']}")
        failures += not res["ok"]
        if res["ok"] and name == "LLM":
            print(f"  structured output: {_structured_output(cfg)}")
            print(_check_context(cfg, create_client(cfg)[0]))
        if res["ok"]:
            for line in (notes or {}).get(name, []):
                print(f"  {line}")
    if conn.llm.net.insecure or conn.embeddings.net.insecure:
        print("\n[warning] insecure turns TLS verification off; use it only for diagnosis.")
    return 1 if failures else 0


def show(conn: Connection) -> None:
    print(f"configuration: {conn.file or 'no file (defaults + environment + arguments)'}")
    for name, cfg in (("LLM", conn.llm), ("Embeddings", conn.embeddings)):
        print(f"\n{name}: {describe(cfg)}")
        if cfg.net.no_proxy:
            print(f"  no_proxy: {cfg.net.no_proxy}")
