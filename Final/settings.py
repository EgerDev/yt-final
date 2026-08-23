"""Opt-in network settings (default proxy) for yt_final.

Pure helpers: config load/save (no-clobber [network] table) plus proxy
validation and the per-job proxy-resolution rule. No server/engine imports.
"""
from __future__ import annotations
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

# NOTE: 'https' proxies (proxy reached over TLS) are intentionally NOT allowed — the managed
# yt-dlp lacks the requests/curl_cffi backend they require, so they fail at download time. http
# and socks4/5/5h all work natively.
# Sampled ONCE at import — on the main thread, before any ThreadingHTTPServer exists, so it has
# none of the cross-thread hazard of probing os.umask(0) per request. Used to reproduce the
# baseline first-write mode (open() would have applied the umask; os.fchmod does not).
try:
    _UMASK = os.umask(0)
    os.umask(_UMASK)
except Exception:                       # pragma: no cover
    _UMASK = 0o022

_ALLOWED_SCHEMES = ("http", "socks4", "socks5", "socks5h")
_EXAMPLE = "use e.g. socks5://127.0.0.1:1080"


def validate_proxy(url: str) -> tuple[bool, str]:
    """(ok, error). Empty string is valid (clear/direct). Passwordless only."""
    url = (url or "").strip()
    if not url:
        return True, ""
    # Reject control chars / quotes so the value can't inject TOML when written to config.toml
    # (urlparse silently strips \r\n\t, so these must be caught before parsing).
    if any(ord(c) < 0x20 for c in url) or any(c in url for c in '"\'\\'):
        return False, f"proxy contains invalid characters — {_EXAMPLE}"
    p = urlparse(url)
    if p.scheme not in _ALLOWED_SCHEMES:
        return False, f"proxy scheme must be one of {', '.join(_ALLOWED_SCHEMES)}"
    if p.username or p.password or "@" in (p.netloc or ""):
        return False, f"passwordless proxies only — {_EXAMPLE}"
    if not p.hostname:
        return False, f"proxy needs a host — {_EXAMPLE}"
    try:
        port = p.port
    except ValueError:
        port = None
    if port is None:
        return False, f"proxy needs a port — {_EXAMPLE}"
    return True, ""


def _config_path(config_dir: Path | None) -> Path:
    if config_dir is None:
        from cookies_auth import auth_config_dir
        config_dir = auth_config_dir()
    return Path(config_dir) / "config.toml"


# Local DPI-desync bypass modes (must match desync_proxy.py). Whitelisted so a config/API value can
# never inject arbitrary tokens into the child's argv.
DESYNC_MODES = ("oob", "frag", "frag_oob", "tlsrec")
DEFAULT_DESYNC_MODE = "oob"


def normalize_desync_mode(mode) -> str:
    m = str(mode or "").strip().lower()
    return m if m in DESYNC_MODES else DEFAULT_DESYNC_MODE


def load_network_settings(config_dir: Path | None = None) -> dict:
    out = {"enabled": False, "proxy": "", "desync": False, "desync_mode": DEFAULT_DESYNC_MODE, "doh": False}
    path = _config_path(config_dir)
    try:
        import tomllib
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            net = data.get("network", {})
            if isinstance(net, dict):
                out["enabled"] = bool(net.get("enabled", False))
                out["proxy"] = str(net.get("proxy", "") or "")
                out["desync"] = bool(net.get("desync", False))
                out["desync_mode"] = normalize_desync_mode(net.get("desync_mode"))
                out["doh"] = bool(net.get("doh", False))
    except Exception:
        pass  # never fatal — fall back to defaults
    return out


def save_network_settings(enabled: bool, proxy: str, config_dir: Path | None = None,
                          desync: bool = False, desync_mode: str = DEFAULT_DESYNC_MODE,
                          doh: bool = False) -> None:
    """Rewrite ONLY the [network] table; leave every other table intact."""
    path = _config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    # Strip an existing [network] table: its header line plus following lines
    # up to (not including) the next "[table]" header or EOF. No DOTALL.
    # `\n?` after the header, not `\n`: a config ending in a bare "[network]" with NO trailing
    # newline was left in place, so the append below produced a DUPLICATE table and tomllib then
    # failed on every load — silently reverting both [network] and [auth] to defaults, since both
    # loaders swallow the parse error.
    text = re.sub(r"(?m)^\[network\][^\n]*\n?(?:(?!\[)[^\n]*\n?)*", "", text)
    text = text.rstrip("\n")
    block = (f"[network]\nenabled = {'true' if enabled else 'false'}\n"
             f"desync = {'true' if desync else 'false'}\n"
             f'desync_mode = "{normalize_desync_mode(desync_mode)}"\n'
             f"doh = {'true' if doh else 'false'}\n")
    if proxy:
        block += f'proxy = "{proxy}"\n'
    text = (text + "\n\n" + block) if text else block
    # Write atomically (temp + os.replace) so a concurrent reader/writer of config.toml never sees a
    # half-written file. os.replace is atomic on the same filesystem.
    # Unique temp name: a fixed ".tmp" would collide if two writers (CLI + web console) overlapped.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # fdopen takes ownership immediately, so the fd cannot leak if the mode copy below raises.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # mkstemp forces 0600 and os.replace carries the SOURCE inode's mode onto the
            # destination, which would silently tighten config.toml. Copy the EXISTING file's mode;
            # on a first write reproduce what open() would have produced (0666 & ~umask), using the
            # umask sampled at import rather than mutating process-wide state on a request thread.
            # Windows ignores these bits, so it is skipped there.
            if os.name != 'nt':
                try:
                    os.fchmod(f.fileno(), stat.S_IMODE(os.stat(path).st_mode))
                except FileNotFoundError:
                    os.fchmod(f.fileno(), 0o666 & ~_UMASK)
                except OSError:
                    pass                     # mode copy is best-effort; never lose the write
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows raises PermissionError when the destination is held open by another process
        # (the web console reading config.toml, AV, a sync agent). Retry like the job store does;
        # on POSIX os.replace does not fail for a concurrent reader, so this is effectively a no-op there.
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def resolve_job_proxy(per_job: str | None, enabled: bool, default_proxy: str | None) -> str | None:
    """Final proxy for one job. 'direct' (any case) forces None."""
    v = (per_job or "").strip()
    if v.lower() == "direct":
        return None
    if v:
        return v
    if enabled and default_proxy:
        return default_proxy
    return None
