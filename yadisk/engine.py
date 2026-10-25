"""
Yandex Disk shadow-files engine.

Design: rclone is the FUSE engine (mature streaming / random access / upload
path). This module adds everything rclone does NOT have on its own:

  * per-file cloud<->local state (does the VFS cache hold the bytes?)
  * "keep offline" pins that survive cache eviction (enforced by a timer)
  * "available online only" (drop the local bytes and the offline flag)
  * mount lifecycle + unit rendering + diagnostics

Layout
  ~/.local/share/yadisk/config.json      settings + pin list
  ~/.local/share/yadisk/rclone.conf      yandex remote (token, mode 600)
  ~/.local/share/yadisk/logs/            rclone + pin-watcher logs
  ~/.cache/yadisk/vfs/<remote>/<path>    rclone VFS cache = the "local" bytes
  ~/YandexDisk                           the mountpoint (shadow view)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
BASE = os.environ.get("YADISK_BASE", os.path.join(HOME, ".local/share/yadisk"))
LOGS = os.path.join(BASE, "logs")
CFG_PATH = os.environ.get("YADISK_CONFIG", os.path.join(BASE, "config.json"))
RCLONE_CONF = os.environ.get("YADISK_RCLONE_CONF", os.path.join(BASE, "rclone.conf"))
UNIT_NAME = "yadisk-mount.service"
UNIT_PATH = os.path.join(HOME, ".config/systemd/user", UNIT_NAME)
PIN_UNIT = "yadisk-pins.service"
PIN_TIMER = "yadisk-pins.timer"
TIDY_UNIT = "yadisk-tidy.service"
TIDY_TIMER = "yadisk-tidy.timer"
RCLONE = shutil.which("rclone") or "/usr/bin/rclone"
FUSERMOUNT = shutil.which("fusermount3") or "/usr/bin/fusermount3"

DEFAULTS = {
    "remote": "yandex",
    "remote_path": "",            # subfolder of the Disk to expose ("" = whole disk)
    "mountpoint": "~/YandexDisk",
    "cache_root": "~/.cache/yadisk",
    "store_max_size": "20G",      # safety cap for the local store (rclone hard limit)
    "cache_max_age": "8760h",     # never expire by age; only by size (LRU)
    "dir_cache_time": "24h",
    "attr_timeout": "10m",
    "vfs_read_chunk_size": "32M",
    "buffer_size": "32M",
    "transfers": 4,
    "rc_port": 5572,
    "pins": [],
    "pin_watch": True,
    "tidy_grace_min": 10,         # how long a merely-opened file may sit on disk
    "mount_at_login": True,
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _expand(p: str) -> str:
    return os.path.expanduser(p)


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    raw: dict = {}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
    except FileNotFoundError:
        pass
    except Exception as e:                      # corrupt file must not brick the app
        print(f"warning: cannot read {CFG_PATH}: {e}", file=__import__("sys").stderr)
    if "cache_max_size" in raw and "store_max_size" not in raw:
        raw["store_max_size"] = raw.pop("cache_max_size")   # pre-tidy key name
    cfg.update(raw)
    cfg.pop("cache_max_size", None)
    return cfg


def save_cfg(cfg: dict) -> None:
    os.makedirs(BASE, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CFG_PATH)


def mountpoint(cfg=None) -> str:
    return _expand((cfg or load_cfg())["mountpoint"])


def cache_root(cfg=None) -> str:
    return _expand((cfg or load_cfg())["cache_root"])


def remote_spec(cfg=None) -> str:
    cfg = cfg or load_cfg()
    rp = (cfg.get("remote_path") or "").strip("/")
    return f"{cfg['remote']}:" + (rp if rp else "")


def ensure_dirs(cfg=None) -> None:
    cfg = cfg or load_cfg()
    for d in (BASE, LOGS, mountpoint(cfg), os.path.join(cache_root(cfg), "vfs")):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------

def rel_of(path: str) -> str:
    """Accept absolute path inside the mount, a 'mount:path' or a plain rel path."""
    mp = os.path.realpath(mountpoint())
    p = path
    if p.startswith(mp):
        p = os.path.relpath(p, mp)
    p = p.strip()
    if p in (".", ""):
        return ""
    return p.lstrip("/")


def mount_path(rel: str) -> str:
    rel = rel.lstrip("/")
    return os.path.join(mountpoint(), rel) if rel else mountpoint()


# rclone escapes path segments that are "." or empty with a fullwidth full stop,
# so the cache dir for an empty remote root is literally ".../vfs/<remote>/．".
_ESCAPED_DOT = "\uff0e"


def _escaped_root(cfg) -> str:
    rp = (cfg.get("remote_path") or "").strip("/")
    if not rp:
        return _ESCAPED_DOT
    return "/".join(_ESCAPED_DOT if p in ("", ".", "..") else p for p in rp.split("/"))


_BASEDIR_MEMO: dict[tuple, tuple[float, str]] = {}
_BASEDIR_TTL = 20.0


def forget_cache_basedir() -> None:
    """Drop the memo — call after mounting/unmounting or changing the remote."""
    _BASEDIR_MEMO.clear()


def cache_basedir(cfg=None) -> str:
    """The payload root rclone actually uses: <cache>/vfs/<remote>/[<escaped root>].

    The layout depends on how the mount was started (a blank remote root may or
    may not get an escaped "．" segment), so ask the running daemon first and
    only fall back to guessing when it is down. The answer is memoised for a few
    seconds: a listing asks for it once per file, and every rc round-trip is a
    real HTTP request (which is what made `yadisk status --counts` take a minute).
    """
    import time as _t
    cfg = cfg or load_cfg()
    key = (cache_root(cfg), cfg.get("remote"), cfg.get("remote_path"))
    now = _t.monotonic()
    hit = _BASEDIR_MEMO.get(key)
    if hit and now - hit[0] < _BASEDIR_TTL:
        return hit[1]
    try:
        st = rc("vfs/stats", timeout=5)
        p = ((st or {}).get("diskCache") or {}).get("path")
        if p and os.path.isdir(p):
            _BASEDIR_MEMO[key] = (now, p.rstrip("/"))
            return p.rstrip("/")
    except Exception:
        pass

    root = os.path.join(cache_root(cfg), "vfs", cfg["remote"])
    want = os.path.join(root, _escaped_root(cfg))
    if os.path.isdir(want):
        return want
    if os.path.isdir(root):
        try:
            subs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
        except OSError:
            subs = []
        # an escaped-root directory is a single segment carrying "．"
        esc = [d for d in subs if _ESCAPED_DOT in d]
        if len(esc) == 1:
            return os.path.join(root, esc[0])
        return root
    return want


def cache_meta_basedir(cfg=None) -> str:
    base = cache_basedir(cfg)
    root = os.path.join(cache_root(cfg or load_cfg()), "vfs")
    if base.startswith(root):
        return os.path.join(os.path.join(cache_root(cfg or load_cfg()), "vfsMeta"),
                            base[len(root):].lstrip("/"))
    return base


def _find_in_cache(base: str, rel: str, limit: int = 20000):
    """Shallow search for a payload whose tail matches rel (odd-char escaping)."""
    if not os.path.isdir(base) or not rel:
        return None
    seen = 0
    for dirpath, dirnames, filenames in os.walk(base):
        for name in filenames:
            seen += 1
            if seen > limit:
                return None
            p = os.path.join(dirpath, name)
            if os.path.relpath(p, base) == rel:
                return p
    return None


def cache_file(rel: str, cfg=None) -> str:
    cfg = cfg or load_cfg()
    rel = rel.lstrip("/")
    base = cache_basedir(cfg)
    p = os.path.join(base, rel) if rel else base
    if not rel or os.path.exists(p):
        return p
    return _find_in_cache(base, rel) or p


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


human_bytes = _fmt_bytes


# --------------------------------------------------------------------------
# rclone remote / token
# --------------------------------------------------------------------------

def has_token() -> bool:
    if not os.path.exists(RCLONE_CONF):
        return False
    txt = open(RCLONE_CONF, encoding="utf-8").read()
    return "access_token" in txt


def remote_conf() -> dict:
    """Read the [remote] section of rclone.conf into a dict."""
    out = {}
    try:
        txt = open(RCLONE_CONF, encoding="utf-8").read()
    except OSError:
        return out
    for line in txt.splitlines():
        if "=" in line and not line.strip().startswith("["):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_remote(token_json: str, cfg=None, client_id: str | None = None,
                 client_secret: str | None = None) -> None:
    """token_json: rclone token blob (from `rclone authorize`) or a bare token.

    client_id/client_secret are remembered so Yandex refreshes work and so a
    custom OAuth app (needed since Yandex blocked rclone's own client id) is
    kept across re-authorizations.
    """
    cfg = cfg or load_cfg()
    tok = token_json.strip()
    if tok.startswith("{"):
        blob = tok
        try:
            json.loads(blob)                    # validate
        except Exception as e:
            raise SystemExit(f"token is not valid JSON: {e}")
    else:
        blob = json.dumps({"access_token": tok, "token_type": "Bearer"})

    prev = remote_conf()
    cid = client_id or prev.get("client_id", "")
    csec = client_secret if client_secret is not None else prev.get("client_secret", "")

    os.makedirs(BASE, exist_ok=True)
    body = f"[{cfg['remote']}]\ntype = yandex\ntoken = {blob}\n"
    if cid:
        body += f"client_id = {cid}\n"
    if csec:
        body += f"client_secret = {csec}\n"
    with open(RCLONE_CONF, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(RCLONE_CONF, 0o600)


def authorize_flow(client_id: str = "", client_secret: str = "", timeout: int = 900,
                   url_cb=None) -> tuple[bool, str]:
    """Run `rclone authorize` against our own OAuth app and install the token.

    Yandex returns 410 Gone for file operations on tokens issued to rclone's
    built-in client id (blocked upstream), so a custom app is required:
      https://oauth.yandex.ru/client/new  with redirect http://127.0.0.1:53682/
    """
    import re
    import tempfile
    cfg = load_cfg()
    prev = remote_conf()
    cid = client_id or prev.get("client_id", "")
    csec = client_secret or prev.get("client_secret", "")

    tf = tempfile.NamedTemporaryFile(prefix="yadisk-auth-", suffix=".log", delete=False)
    logpath = tf.name
    tf.close()
    cmd = [RCLONE, "authorize", cfg["remote"], "--auth-no-open-browser"]
    if cid and csec:
        cmd += [cid, csec]
    with open(logpath, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    url = None
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            try:
                txt = open(logpath, encoding="utf-8", errors="replace").read()
            except OSError:
                txt = ""
            if url is None:
                m = re.search(r"(https?://\S*/auth\?state=\S+)", txt)
                if m:
                    url = m.group(1)
                    if url_cb:
                        url_cb(url)
                    subprocess.run(["xdg-open", url], capture_output=True)
            tok = extract_token_from_log(logpath)
            if tok:
                write_remote(tok, cfg, cid, csec)
                return True, "token installed"
            if proc.poll() is not None:
                return False, "authorize exited: " + txt.strip()[-300:]
        return False, "timed out waiting for authorization"
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            os.unlink(logpath)
        except OSError:
            pass


YANDEX_AUTH_URL = "https://oauth.yandex.com/authorize"
YANDEX_TOKEN_URL = "https://oauth.yandex.com/token"
YANDEX_VERIFY_REDIRECT = "https://oauth.yandex.com/verification_code"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/150.0 Safari/537.36")


def code_authorize_url(client_id: str, redirect: str | None = None,
                       state: str = "yadisk") -> str:
    """Authorize URL for the confirmation-code flow.

    redirect=None lets Yandex use the app's *first registered* Callback URL,
    which is the only value it ever accepts — guessing a different one is what
    produces "redirect_uri does not match the Callback URL defined for the client".
    """
    from urllib.parse import urlencode
    q = {
        "response_type": "code",
        "client_id": client_id,
        "state": state,
        "force_confirm": "yes",
    }
    if redirect:
        q["redirect_uri"] = redirect
    return f"{YANDEX_AUTH_URL}?{urlencode(q)}"


def parse_code(text: str) -> str:
    """Accept a bare code, a `code=…` fragment or a whole redirect URL."""
    from urllib.parse import parse_qs, urlparse
    t = (text or "").strip()
    if not t:
        return ""
    if "code=" in t:
        q = urlparse(t).query or t.split("?", 1)[-1]
        vals = parse_qs(q).get("code")
        if vals:
            t = vals[0]
    return re.sub(r"\s+", "", t)


def exchange_code(client_id: str, client_secret: str, code: str,
                  redirect: str | None = None, timeout: int = 30) -> dict:
    """Trade a confirmation code for a token and install it into rclone.conf."""
    from urllib.parse import urlencode
    code = parse_code(code)
    if not re.fullmatch(r"[0-9a-zA-Z_.-]{4,64}", code):
        raise ValueError("that does not look like a confirmation code")
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if redirect:
        fields["redirect_uri"] = redirect
    data = urlencode(fields).encode()
    req = urllib.request.Request(
        YANDEX_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "yadisk/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            err = f"{j.get('error', e.code)}: {j.get('error_description', body[:200])}"
        except json.JSONDecodeError:
            err = f"HTTP {e.code}: {body[:200]}"
        if "invalid_grant" in err:
            err += "  (the code expires after 10 minutes — request a fresh one)"
        raise RuntimeError(err)
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach oauth.yandex.com: {e.reason}")
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"unexpected answer: {raw[:200]}")
    if "access_token" not in j:
        raise RuntimeError(j.get("error_description") or j.get("error") or raw[:200])

    expires_in = int(j.get("expires_in") or 31536000)
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
    token = {
        "access_token": j["access_token"],
        "token_type": j.get("token_type", "bearer"),
        "expiry": exp.isoformat().replace("+00:00", "Z"),
    }
    if j.get("refresh_token"):
        token["refresh_token"] = j["refresh_token"]
    write_remote(json.dumps(token), None, client_id, client_secret)
    return token


def extract_token_from_log(path: str) -> str | None:
    """Pull the token JSON out of an `rclone authorize` log/stdout file."""
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    start = txt.rfind('{"access_token"')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(txt)):
        if txt[i] == "{":
            depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0:
                return txt[start:i + 1]
    return None


# --------------------------------------------------------------------------
# systemd user unit
# --------------------------------------------------------------------------

def render_unit(cfg=None) -> str:
    cfg = cfg or load_cfg()
    mp, cr = mountpoint(cfg), cache_root(cfg)
    args = [
        RCLONE, "mount", remote_spec(cfg), mp,
        "--config", RCLONE_CONF,
        "--cache-dir", cr,
        "--vfs-cache-mode", "full",
        "--vfs-cache-max-size", str(cfg["store_max_size"]),
        "--vfs-cache-max-age", str(cfg["cache_max_age"]),
        "--vfs-cache-poll-interval", "1m",
        "--dir-cache-time", str(cfg["dir_cache_time"]),
        "--attr-timeout", str(cfg["attr_timeout"]),
        "--vfs-read-chunk-size", str(cfg["vfs_read_chunk_size"]),
        "--vfs-read-chunk-size-limit", "off",
        "--buffer-size", str(cfg["buffer_size"]),
        "--vfs-write-back", "5s",
        "--transfers", str(cfg["transfers"]),
        "--checkers", "8",
        "--umask", "022",
        "--poll-interval", "0",
        "--rc", "--rc-addr", f"127.0.0.1:{cfg['rc_port']}", "--rc-no-auth",
        "--log-level", "INFO",
        "--log-file", os.path.join(LOGS, "rclone.log"),
    ]
    exec_start = " ".join(_systemd_quote(a) for a in args)
    return f"""[Unit]
Description=Yandex Disk shadow-files mount (rclone FUSE)
Documentation=man:rclone(1)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart={exec_start}
ExecStop={FUSERMOUNT} -uz {_systemd_quote(mp)}
Restart=on-failure
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""


def _systemd_quote(s: str) -> str:
    if not any(c in s for c in ' \t"\'\\'):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_pin_units(cfg=None) -> tuple[str, str]:
    exe = shutil.which("yadisk") or os.path.join(HOME, ".local/bin/yadisk")
    svc = f"""[Unit]
Description=Yandex Disk: re-hydrate pinned (keep-offline) files
After={UNIT_NAME}

[Service]
Type=oneshot
ExecStart={exe} pins --enforce
"""
    timer = f"""[Unit]
Description=Periodically enforce Yandex Disk keep-offline pins

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Unit={PIN_UNIT}

[Install]
WantedBy=timers.target
"""
    return svc, timer


def render_tidy_units(cfg=None) -> tuple[str, str]:
    exe = shutil.which("yadisk") or os.path.join(HOME, ".local/bin/yadisk")
    svc = f"""[Unit]
Description=Yandex Disk: drop cached bytes that are not kept offline
After={UNIT_NAME}

[Service]
Type=oneshot
ExecStart={exe} tidy --quiet
"""
    timer = f"""[Unit]
Description=Periodically free Yandex Disk cache leftovers

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit={TIDY_UNIT}

[Install]
WantedBy=timers.target
"""
    return svc, timer


def write_units(cfg=None) -> list[str]:
    cfg = cfg or load_cfg()
    os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
    with open(UNIT_PATH, "w", encoding="utf-8") as f:
        f.write(render_unit(cfg))
    svc, timer = render_pin_units(cfg)
    pin_dir = os.path.dirname(UNIT_PATH)
    with open(os.path.join(pin_dir, PIN_UNIT), "w", encoding="utf-8") as f:
        f.write(svc)
    with open(os.path.join(pin_dir, PIN_TIMER), "w", encoding="utf-8") as f:
        f.write(timer)
    tsvc, ttimer = render_tidy_units(cfg)
    with open(os.path.join(pin_dir, TIDY_UNIT), "w", encoding="utf-8") as f:
        f.write(tsvc)
    with open(os.path.join(pin_dir, TIDY_TIMER), "w", encoding="utf-8") as f:
        f.write(ttimer)
    systemctl("--user", "daemon-reload", check=False)
    return [UNIT_PATH, os.path.join(pin_dir, PIN_UNIT), os.path.join(pin_dir, PIN_TIMER),
            os.path.join(pin_dir, TIDY_UNIT), os.path.join(pin_dir, TIDY_TIMER)]


def systemctl(*args, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, check=check)


def reload_mount(cfg=None) -> str:
    """Re-start the mount so it picks up a freshly installed token.

    rclone reads rclone.conf once at startup, so a new token needs a restart —
    a running mount keeps answering 410 Gone until then.
    """
    cfg = cfg or load_cfg()
    was = is_mounted(cfg)
    if was:
        unmount(cfg)
    mount(cfg)
    return "remounted" if was else "mounted"


def is_mounted(cfg=None) -> bool:
    mp = mountpoint(cfg)
    try:
        return os.path.ismount(mp)
    except OSError:
        return False


def mount_healthy(cfg=None) -> tuple[bool, str]:
    """Mounted AND the root is readable (a mounted-but-410 FUSE dir is not usable)."""
    cfg = cfg or load_cfg()
    if not is_mounted(cfg):
        return False, "not mounted"
    try:
        os.listdir(mountpoint(cfg))
        return True, "ok"
    except OSError as e:
        try:
            log = open(os.path.join(LOGS, "rclone.log"), encoding="utf-8", errors="replace").read()
            tail = "\n".join(log.strip().splitlines()[-3:])
        except OSError:
            tail = ""
        if "410" in tail:
            return False, ("Yandex rejects file access (410 Gone) for this token — "
                           "rclone's shared client id is blocked; run `yadisk auth`")
        return False, f"mount unreadable: {e}"


def remote_access(cfg=None, timeout: int = 90) -> tuple[bool, str]:
    """One real API round-trip: is the token allowed to see files at all?"""
    cfg = cfg or load_cfg()
    try:
        r = subprocess.run([RCLONE, "lsf", remote_spec(cfg), "--max-depth", "1",
                            "--config", RCLONE_CONF],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"listing timed out after {timeout}s"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        msg = msg[-1] if msg else "unknown error"
        if "410" in msg:
            return False, "410 Gone — token has no file access (blocked client id)"
        return False, msg
    return True, f"{len(r.stdout.splitlines())} entries at the disk root"


def mount(cfg=None) -> None:
    cfg = cfg or load_cfg()
    ensure_dirs(cfg)
    if is_mounted(cfg):
        return
    if not has_token():
        raise RuntimeError("no OAuth token configured - run `yadisk setup` first")
    write_units(cfg)
    r = systemctl("--user", "start", UNIT_NAME, check=False)
    forget_cache_basedir()
    for _ in range(60):
        if is_mounted(cfg):
            return
        time.sleep(0.5)
    log = ""
    try:
        log = open(os.path.join(LOGS, "rclone.log"), encoding="utf-8", errors="replace").read()[-1200:]
    except OSError:
        pass
    raise RuntimeError(f"mount did not come up.\n{r.stderr.strip()}\n{log}")


def unmount(cfg=None) -> None:
    cfg = cfg or load_cfg()
    systemctl("--user", "stop", UNIT_NAME, check=False)
    forget_cache_basedir()
    for _ in range(20):
        if not is_mounted(cfg):
            return
        time.sleep(0.25)
    subprocess.run([FUSERMOUNT, "-uz", mountpoint(cfg)], capture_output=True, text=True)


def enable_autostart(on: bool, cfg=None) -> None:
    cfg = cfg or load_cfg()
    write_units(cfg)
    if on:
        systemctl("--user", "enable", UNIT_NAME, check=False)
        systemctl("--user", "enable", "--now", PIN_TIMER, check=False)
        systemctl("--user", "enable", "--now", TIDY_TIMER, check=False)
    else:
        systemctl("--user", "disable", UNIT_NAME, check=False)
        systemctl("--user", "disable", "--now", PIN_TIMER, check=False)
        systemctl("--user", "disable", "--now", TIDY_TIMER, check=False)
    cfg["mount_at_login"] = bool(on)
    save_cfg(cfg)


def unit_enabled() -> bool:
    r = systemctl("--user", "is-enabled", UNIT_NAME, check=False)
    return r.stdout.strip() == "enabled"


# --------------------------------------------------------------------------
# rclone remote-control (mounted daemon)
# --------------------------------------------------------------------------

_OPENER = None


def _local_opener():
    """urllib opener with proxies disabled — 127.0.0.1 must never go via HTTP_PROXY."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def rc(cmd: str, timeout: float = 30, **params):
    cfg = load_cfg()
    url = f"http://127.0.0.1:{cfg['rc_port']}/{cmd}"
    data = json.dumps(params).encode() if params else b"{}"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _local_opener().open(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body or b"{}")


def rc_available() -> bool:
    try:
        rc("rc/noop")
        return True
    except Exception:
        return False


def stats() -> dict:
    """Cache usage from the running daemon (falls back to du of cache dir)."""
    out = {"mounted": is_mounted(), "cache_used": 0, "objects_local": 0}
    try:
        s = rc("vfs/stats")
        dc = (s.get("diskCache") or {})
        out["cache_used"] = int(dc.get("bytesUsed") or 0)
        out["raw"] = s
        return out
    except Exception:
        pass
    out["cache_used"] = du_cache()
    return out


def local_files() -> list[str]:
    """Files that really have bytes on this PC, read straight from the cache.

    Walking the cache is instant and needs no network, unlike walking the whole
    remote through FUSE.
    """
    cfg = load_cfg()
    base = cache_basedir(cfg)
    out = []
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            if f.startswith(".~") or ".partial" in f:
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, base)
            if rel.startswith(".."):
                continue
            try:
                if os.path.getsize(full) <= 0:
                    continue
            except OSError:
                continue
            out.append(rel)
    return sorted(out)


# --------------------------------------------------------------------------
# state export - feeds the Dolphin indicator plugin
# --------------------------------------------------------------------------

STATE_DIR = os.path.join(BASE, "state")


def _write_lines(path: str, lines) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(str(ln).replace("\n", "") + "\n")
    os.replace(tmp, path)


def export_state(cfg=None) -> dict:
    """Publish "which paths hold real bytes" for the Dolphin VCS plugin.

    Two plain-text lists and the mountpoint, nothing more: the plugin only
    re-reads them when their mtime changes, so this may run every minute.
    """
    cfg = cfg or load_cfg()
    mp = os.path.realpath(mountpoint(cfg))
    os.makedirs(STATE_DIR, exist_ok=True)
    local = local_files()
    pinned = pins(cfg)
    _write_lines(os.path.join(STATE_DIR, "local.txt"), local)
    _write_lines(os.path.join(STATE_DIR, "pins.txt"), pinned)
    _write_lines(os.path.join(STATE_DIR, "mount.txt"), [mp])
    return {"mount": mp, "local": len(local), "pins": len(pinned)}


def du_cache() -> int:
    root = os.path.join(cache_root(), "vfs")
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def on_device() -> tuple[int, int]:
    """(files, bytes) currently sitting on this PC - one cache walk."""
    base = cache_basedir()
    n = 0
    total = 0
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            if f.startswith(".~") or ".partial" in f:
                continue
            try:
                sz = os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                continue
            if sz <= 0:
                continue
            n += 1
            total += sz
    return n, total


def open_cache_paths() -> set[str]:
    """Cache files some process is holding open right now (never tidy those)."""
    base = os.path.realpath(cache_basedir())
    held: set[str] = set()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return held
    for pid in pids:
        if not pid.isdigit():
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                tgt = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if tgt.startswith(base):
                held.add(tgt)
    return held


def tidy(grace_min: int | None = None, dry_run: bool = False) -> tuple[list[str], int]:
    """Drop cached bytes nobody asked to keep.

    The app only has two states, so anything on disk that is not *kept offline*
    is leftovers: bytes pulled in because a file was opened, previewed or
    scanned. They are dropped once nobody has the file open and the grace
    window has passed - which is what makes "cloud" mean zero bytes again.

    Kept: pins, files open right now, in-flight .partial uploads.
    """
    cfg = load_cfg()
    grace = int(cfg.get("tidy_grace_min", 10) if grace_min is None else grace_min)
    base = cache_basedir(cfg)
    held = open_cache_paths()
    now = time.time()
    dropped: list[str] = []
    freed = 0
    for rel in local_files():
        if is_pinned(rel, cfg):
            continue
        full = os.path.join(base, rel)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if now - st.st_mtime < grace * 60:
            continue
        if any(h == full or h.startswith(full + os.sep) or full.startswith(h + os.sep)
               for h in held):
            continue
        dropped.append(rel)
        freed += st.st_size
        if not dry_run:
            try:
                evict(rel)
            except Exception:
                pass
    return dropped, freed


# --------------------------------------------------------------------------
# state: cloud-only vs local (shadow files)
# --------------------------------------------------------------------------

def file_state(rel: str) -> str:
    """'local' | 'cloud' | 'partial' | 'missing'."""
    mp = mount_path(rel)
    try:
        st = os.stat(mp)
    except OSError:
        return "missing"
    cf = cache_file(rel)
    try:
        cst = os.stat(cf)
    except OSError:
        return "partial" if any(_partial_neighbours(cf)) else "cloud"
    if st.st_size == 0:
        return "local"
    if cst.st_size >= st.st_size:
        return "local"
    return "partial"


def _partial_neighbours(cf: str):
    d, base = os.path.split(cf)
    try:
        for name in os.listdir(d):
            if name.startswith(base + ".") and ("partial" in name or "vfs" in name):
                yield name
    except OSError:
        return


def is_local(rel: str) -> bool:
    return file_state(rel) == "local"


def local_bytes(rel: str) -> int:
    try:
        return os.path.getsize(cache_file(rel))
    except OSError:
        return 0


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def _entry(rel: str, name: str, rel_dir: str, mp: str) -> dict | None:
    try:
        st = os.stat(mp)
    except OSError:
        return None
    return {
        "rel": rel,
        "name": name,
        "dir": rel_dir,
        "dir_flag": os.path.isdir(mp),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "state": file_state(rel),
        "local_bytes": local_bytes(rel),
    }


def scan(rel_root: str = "", max_entries: int = 0, cancel=None, recursive: bool = True):
    """Walk the shadow tree. Yields dicts: rel, name, size, mtime, state, local_bytes.

    recursive=False scans one directory only — the right thing for a real disk
    (walking 9.9 TB just to render the root would take minutes).
    """
    root = mount_path(rel_root)
    n = 0
    if os.path.isfile(root):
        rel = rel_root.strip("/")
        st = os.stat(root)
        yield {
            "rel": rel,
            "name": os.path.basename(root),
            "dir": os.path.dirname(rel),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "state": file_state(rel),
            "local_bytes": local_bytes(rel),
        }
        return
    base = rel_root.strip().strip("/")
    if not recursive:
        try:
            names = sorted(os.listdir(root), key=str.lower)
        except OSError:
            return
        # folders first, then files — the order every file manager uses
        dirs = [n for n in names if os.path.isdir(os.path.join(root, n))]
        names = dirs + [n for n in names if n not in set(dirs)]
        for name in names:
            if cancel and cancel():
                return
            rel = os.path.join(base, name) if base else name
            e = _entry(rel, name, base, os.path.join(root, name))
            if not e:
                continue
            yield e
            n += 1
            if max_entries and n >= max_entries:
                return
        return

    for dirpath, dirnames, filenames in os.walk(root):
        if cancel and cancel():
            return
        dirnames.sort(key=str.lower)
        rel_dir = os.path.relpath(dirpath, mountpoint())
        rel_dir = "" if rel_dir == "." else rel_dir
        for name in sorted(dirnames, key=str.lower):
            rel = os.path.join(rel_dir, name) if rel_dir else name
            e = _entry(rel, name, rel_dir, os.path.join(dirpath, name))
            if e:
                yield e
                n += 1
                if max_entries and n >= max_entries:
                    return
        for name in sorted(filenames, key=str.lower):
            if cancel and cancel():
                return
            rel = os.path.join(rel_dir, name) if rel_dir else name
            e = _entry(rel, name, rel_dir, os.path.join(dirpath, name))
            if not e:
                continue
            yield e
            n += 1
            if max_entries and n >= max_entries:
                return


# --------------------------------------------------------------------------
# hydrate / evict
# --------------------------------------------------------------------------

class Cancelled(Exception):
    pass


def hydrate(rel: str, progress=None, cancel=None) -> int:
    """Force the bytes to land in the VFS cache (read-through). Returns bytes read."""
    mp = mount_path(rel)
    if os.path.isdir(mp):
        total = 0
        for entry in scan(rel):
            total += hydrate(entry["rel"], progress, cancel)
        return total
    size = os.path.getsize(mp)
    if size == 0:
        open(mp, "rb").close()
        return 0
    done = 0
    with open(mp, "rb", buffering=0) as f:
        while True:
            if cancel and cancel():
                raise Cancelled()
            chunk = f.read(4 << 20)
            if not chunk:
                break
            done += len(chunk)
            if progress:
                progress(done, size)
    return done


def hydrate_many(rels, progress=None, cancel=None, workers=None) -> tuple[int, list]:
    cfg = load_cfg()
    workers = workers or max(1, int(cfg.get("transfers", 4)))
    done = [0]
    lock = __import__("threading").Lock()
    total = 0

    def _size(mp):
        if os.path.isdir(mp):
            acc = 0
            for d, _dn, fn in os.walk(mp):
                for name in fn:
                    try:
                        acc += os.path.getsize(os.path.join(d, name))
                    except OSError:
                        pass
            return acc
        try:
            return os.path.getsize(mp)
        except OSError:
            return 0

    for rel in rels:
        total += _size(mount_path(rel))

    def one(rel):
        if cancel and cancel():
            raise Cancelled()
        n = hydrate(rel, None, cancel)
        with lock:
            done[0] += n
            if progress:
                progress(min(done[0], total or done[0]), total or done[0])
        return rel

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, r): r for r in rels}
        for fut in futs:
            try:
                fut.result()
            except Cancelled:
                raise
            except Exception as e:
                errors.append((futs[fut], str(e)))
    return done[0], errors


def evict(rel: str) -> None:
    """Drop local bytes for a file/folder -> back to cloud-only (shadow)."""
    rel = rel.strip("/")
    cfg = load_cfg()
    # 1) tell the running daemon to drop its index entry (keeps listing fast & consistent)
    try:
        rc("vfs/forget", dir=rel)
    except Exception:
        try:
            rc("vfs/forget", file=rel)
        except Exception:
            pass
    # 2) belt and braces: delete the payload ourselves (daemon may be down)
    cf = cache_file(rel, cfg)
    if os.path.isdir(cf):
        shutil.rmtree(cf, ignore_errors=True)
    elif os.path.isfile(cf):
        try:
            os.remove(cf)
        except OSError:
            pass
    # stray rclone partial files next to it
    d, base = os.path.split(cf)
    try:
        for name in os.listdir(d):
            if name.startswith(base + ".") or name.startswith(".~" + base):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    except OSError:
        pass
    # 3) metadata sidecar
    mbase = cache_meta_basedir(cfg)
    meta = os.path.join(mbase, rel)
    for m in (meta, _find_in_cache(os.path.join(cache_root(cfg), "vfsMeta", cfg["remote"]), rel)):
        if m and os.path.isfile(m):
            try:
                os.remove(m)
            except OSError:
                pass


def evict_many(rels) -> int:
    n = 0
    for r in rels:
        evict(r)
        n += 1
    return n


# --------------------------------------------------------------------------
# pins ("keep offline")
# --------------------------------------------------------------------------

def pins(cfg=None) -> list[str]:
    return list((cfg or load_cfg()).get("pins") or [])


def _set_pins(lst, cfg=None) -> None:
    cfg = cfg or load_cfg()
    cfg["pins"] = sorted(set(lst))
    save_cfg(cfg)


def is_pinned(rel: str, cfg=None) -> bool:
    rel = rel.strip("/")
    for p in pins(cfg):
        if rel == p or rel.startswith(p.rstrip("/") + "/"):
            return True
    return False


def pin(rels, progress=None, cancel=None) -> tuple[list, list]:
    """Hydrate + remember, so the pin is restored after eviction."""
    cfg = load_cfg()
    cur = pins(cfg)
    added, errs = [], []
    expand = []
    for r in rels:
        r = r.strip("/")
        mp = mount_path(r)
        if not os.path.exists(mp):
            errs.append((r, "not found"))
            continue
        if os.path.isdir(mp):
            cur = cur + [r]
            expand += [e["rel"] for e in scan(r)]
        else:
            expand.append(r)
            cur.append(r)
    _set_pins(cur)
    try:
        hydrate_many(expand, progress, cancel)
    except Cancelled:
        errs.append(("<cancelled>", "cancelled"))
    for r in rels:
        r = r.strip("/")
        if os.path.isfile(mount_path(r)) and not is_local(r):
            errs.append((r, "not cached after hydrate"))
        elif os.path.isfile(mount_path(r)):
            added.append(r)
    return added, errs


def unpin(rels) -> int:
    cfg = load_cfg()
    cur = pins(cfg)
    n = 0
    for r in rels:
        r = r.strip("/")
        new = [p for p in cur if not (p == r or p.startswith(r + "/") or r.startswith(p.rstrip("/") + "/"))]
        n += len(cur) - len(new)
        cur = new
    _set_pins(cur)
    return n


def enforce_pins(log=print) -> int:
    """Re-hydrate pins that the LRU cache dropped. Run from a systemd timer."""
    cfg = load_cfg()
    if not cfg.get("pin_watch"):
        return 0
    if not is_mounted(cfg):
        try:
            mount(cfg)
        except Exception as e:
            log(f"pin-enforce: cannot mount: {e}")
            return 0
    fixed = 0
    for rel in pins(cfg):
        mp = mount_path(rel)
        if not os.path.exists(mp):
            log(f"pin-enforce: missing on disk, keeping pin: {rel}")
            continue
        targets = [rel] if os.path.isfile(mp) else [e["rel"] for e in scan(rel)]
        for t in targets:
            if not is_local(t):
                try:
                    hydrate(t)
                    fixed += 1
                    log(f"pin-enforce: re-hydrated {t}")
                except Exception as e:
                    log(f"pin-enforce: failed {t}: {e}")
    return fixed


# --------------------------------------------------------------------------
# desktop integration
# --------------------------------------------------------------------------

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#4da3ff"/><stop offset="1" stop-color="#1a6fd4"/>
  </linearGradient></defs>
  <path d="M18 46h28a9 9 0 0 0 1.6-17.9A13 13 0 0 0 22.4 26A9 9 0 0 0 18 46z"
        fill="url(#g)" opacity="0.95"/>
  <path d="M32 24v12m0 0l-5-5m5 5l5-5" stroke="#fff" stroke-width="3.2"
        stroke-linecap="round" stroke-linejoin="round" fill="none"/>
</svg>"""

APP_DESKTOP = """[Desktop Entry]
Type=Application
Name=Yandex Disk
Name[ru]=Яндекс Диск
Comment=Cloud files with shadow files (cloud-only placeholders)
Comment[ru]=Облачные файлы с теневыми файлами
Exec={exe} gui
Icon=yadisk
Terminal=false
Categories=Network;FileTransfer;Utility;
Keywords=yandex;disk;cloud;shadow;
StartupWMClass=yadisk
"""

SERVICEMENU = """[Desktop Entry]
Type=Service
Name=Yandex Disk
ServiceTypes=KonqPopupMenu/Plugin
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
MimeType=all/all;
Actions=keepOffline;freeSpace;
X-KDE-Priority=TopLevel
X-KDE-Submenu=Yandex Disk

[Desktop Action keepOffline]
Name=Keep on this computer (offline)
Name[ru]=Хранить на этом компьютере (офлайн)
Icon=cloud-download
Exec={exe} pin %F

[Desktop Action freeSpace]
Name=Available online only (remove from this PC)
Name[ru]=Только в облаке (убрать с этого компьютера)
Icon=cloud-check
Exec={exe} evict %F
"""


def install_assets(quiet: bool = False) -> list[str]:
    exe = shutil.which("yadisk") or os.path.join(HOME, ".local/bin/yadisk")
    out = []

    icon_dir = os.path.join(HOME, ".local/share/icons/hicolor/scalable/apps")
    os.makedirs(icon_dir, exist_ok=True)
    icon = os.path.join(icon_dir, "yadisk.svg")
    with open(icon, "w", encoding="utf-8") as f:
        f.write(ICON_SVG)
    out.append(icon)

    apps = os.path.join(HOME, ".local/share/applications")
    os.makedirs(apps, exist_ok=True)
    desk = os.path.join(apps, "yadisk.desktop")
    with open(desk, "w", encoding="utf-8") as f:
        f.write(APP_DESKTOP.format(exe=exe))
    out.append(desk)

    sm_dir = os.path.join(HOME, ".local/share/kio/servicemenus")
    os.makedirs(sm_dir, exist_ok=True)
    sm = os.path.join(sm_dir, "yadisk.desktop")
    with open(sm, "w", encoding="utf-8") as f:
        f.write(SERVICEMENU.format(exe=exe))
    os.chmod(sm, 0o755)
    out.append(sm)

    for tool in ("update-desktop-database", "gtk-update-icon-cache"):
        if shutil.which(tool) and tool == "update-desktop-database":
            subprocess.run([tool, apps], capture_output=True)
    if not quiet:
        pass
    return out


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Yandex Disk", "-i", "yadisk", title, body],
                       capture_output=True)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def doctor() -> list[tuple[bool, str]]:
    cfg = load_cfg()
    checks = []
    ok = shutil.which("rclone") is not None
    ver = subprocess.run([RCLONE, "version"], capture_output=True, text=True).stdout.splitlines()
    checks.append((ok, f"rclone: {ver[0] if ver else 'missing'}"))
    checks.append((os.path.exists("/dev/fuse"), "/dev/fuse present"))
    checks.append((shutil.which("fusermount3") is not None, "fusermount3 present"))
    checks.append((has_token(), f"OAuth token in {RCLONE_CONF}"))
    checks.append((os.path.isdir(mountpoint(cfg)), f"mountpoint {mountpoint(cfg)}"))
    m = is_mounted(cfg)
    checks.append((m, "mounted" if m else "NOT mounted"))
    if m:
        checks.append((rc_available(), f"remote control on 127.0.0.1:{cfg['rc_port']}"))
        healthy, why = mount_healthy(cfg)
        checks.append((healthy, f"file access: {why}"))
        if not healthy:
            ok2, msg = remote_access(cfg)
            checks.append((ok2, f"api listing: {msg}"))
    elif has_token():
        # unmounted, but we can still ask the API: tells us if the token is blocked
        ok2, msg = remote_access(cfg, timeout=40)
        checks.append((ok2, f"api listing (token health): {msg}"))
    nloc, used = on_device()
    checks.append((True, f"on this PC: {nloc} file(s) {human_bytes(used)} "
                         f"({len(pins(cfg))} kept offline, tidy runs every 10 min)"))
    checks.append((unit_enabled(), "autostart " + ("enabled" if unit_enabled() else "disabled")))
    return checks
