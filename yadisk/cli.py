"""Command line for the Yandex Disk shadow-files app."""

from __future__ import annotations

import argparse
import os
import sys

from . import engine as E


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _bar(done: int, total: int, prefix: str = "") -> None:
    if total <= 0:
        return
    pct = 100.0 * done / total
    width = 28
    filled = int(width * pct / 100)
    sys.stderr.write(f"\r{prefix}[{'#' * filled}{'.' * (width - filled)}] "
                     f"{pct:5.1f}%  {E.human_bytes(done)}/{E.human_bytes(total)}")
    if done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _state_tag(state: str) -> str:
    return {"local": "LOCAL", "cloud": "CLOUD", "partial": "PARTL", "missing": "  -- "}.get(state, state)


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def cmd_setup(a) -> int:
    cfg = E.load_cfg()

    cfg = E.load_cfg()
    cid = getattr(a, "client_id", "") or ""
    csec = getattr(a, "client_secret", None)

    if a.from_log:
        tok = E.extract_token_from_log(a.from_log)
        if not tok:
            print(f"error: no token JSON found in {a.from_log}", file=sys.stderr)
            return 1
        E.write_remote(tok, cfg, cid, csec)
        print("token: installed from", a.from_log)
    elif a.token:
        E.write_remote(a.token, cfg, cid, csec)
        print("token: installed")
    elif cid:
        prev = E.remote_conf()
        if prev.get("token"):
            E.write_remote(prev["token"], cfg, cid, csec)
            print("client credentials: saved")
    elif not E.has_token():
        print("error: no token yet.\n"
              "  easiest: yadisk auth   (needs a Yandex OAuth app - see `yadisk auth --help`)\n"
              "  or:      yadisk setup --from-log <file with `rclone authorize yandex` output>",
              file=sys.stderr)
        return 2

    cfg = E.load_cfg()
    if a.mountpoint:
        cfg["mountpoint"] = os.path.abspath(os.path.expanduser(a.mountpoint))
    store = getattr(a, "store_size", None) or getattr(a, "cache_size", None)
    if store:
        cfg["store_max_size"] = store
    E.save_cfg(cfg)
    E.ensure_dirs(cfg)
    E.write_units(cfg)
    E.install_assets(quiet=True)
    if a.autostart:
        E.enable_autostart(True, cfg)

    print("\nconfigured:")
    print(f"  remote      {E.remote_spec(cfg)}")
    print(f"  mountpoint  {E.mountpoint(cfg)}")
    print(f"  store       {E.cache_root(cfg)}  (kept-offline files only)")
    print(f"  unit        {E.UNIT_PATH}")
    print(f"  autostart   {'on' if E.unit_enabled() else 'off'}")
    print("\nnext: yadisk mount && yadisk status")
    return 0


AUTH_HELP = """no OAuth app for this tool yet.

Yandex blocked the client id that rclone ships with (every file request from it
answers "410 Gone"), so the app has to run on your own OAuth credentials.
One-time setup, ~2 minutes:

  1. open  https://oauth.yandex.ru/client/new
  2. app type: "For API access" (Для доступа к API)
     platform:  "Web services" (Веб-сервисы)
     Redirect URI:  http://127.0.0.1:53682/
  3. Data access — tick:
       cloud_api:disk.read       (read the whole Disk)
       cloud_api:disk.write      (write / upload, optional but recommended)
       cloud_api:disk.info       (quota info)
  4. copy "ClientID" + "Client secret", then either
       yadisk auth --client-id <ID> --client-secret <SECRET>
       yadisk auth --code --client-id <ID> --client-secret <SECRET>

If the Redirect URI field is missing or locked (that happens when the app was
created as "Mobile apps" or as an "authorization" app), use --code: it needs no
callback at all, you just paste the 7-digit code the browser shows you.
"""


def _verify_file_access() -> int:
    import subprocess
    cfg = E.load_cfg()
    print("verifying file access …")
    r = subprocess.run([E.RCLONE, "lsf", E.remote_spec(cfg), "--max-depth", "1",
                        "--config", E.RCLONE_CONF], capture_output=True, text=True)
    if r.returncode != 0:
        print("WARNING: file access still failing:\n" + (r.stderr or r.stdout).strip()[:600],
              file=sys.stderr)
        return 1
    print(f"file access OK — {len(r.stdout.splitlines())} entries at the disk root")
    try:
        print(f"{E.reload_mount(cfg)} at {E.mountpoint(cfg)}")
    except Exception as e:                                   # noqa: BLE001
        print(f"mount: {e}", file=sys.stderr)
    return 0


def _auth_by_code(cid: str, csec: str, redirect: str | None = None) -> int:
    """Confirmation-code flow: no local listener, works with any registered callback."""
    import subprocess
    if redirect is None:
        print("no --redirect-uri given: Yandex will use the Callback URL registered\n"
              "for your app, so a mismatch is impossible.")
    url = E.code_authorize_url(cid, redirect)
    print("1. open this page and press Allow:")
    print("   " + url)
    try:
        subprocess.run(["xdg-open", url], capture_output=True)
    except Exception:                                        # noqa: BLE001
        pass
    print("2. then paste the code here. It is either shown on the page, or — if the\n"
          "   browser could not reach 127.0.0.1:53682 — it is the ?code=… part of\n"
          "   the address bar.")
    try:
        code = input("   code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("aborted", file=sys.stderr)
        return 2
    if not code:
        print("no code entered", file=sys.stderr)
        return 2
    code = E.parse_code(code)
    try:
        E.exchange_code(cid, csec, code, redirect)
    except Exception as e:                                   # noqa: BLE001
        print(f"exchange failed: {e}", file=sys.stderr)
        return 1
    print("token installed")
    return _verify_file_access()


def cmd_auth(a) -> int:
    prev = E.remote_conf()
    cid = a.client_id or prev.get("client_id", "")
    csec = a.client_secret or prev.get("client_secret", "")
    if not (cid and csec):
        print(AUTH_HELP, file=sys.stderr)
        return 2

    if getattr(a, "code", False):
        return _auth_by_code(cid, csec, getattr(a, "redirect_uri", None))

    print(f"authorizing with client_id {cid} (browser will open)")
    print("waiting for you to press Allow … (Ctrl-C to abort; nothing prints meanwhile, "
          "that is normal)")
    ok, msg = E.authorize_flow(
        cid, csec, timeout=a.timeout,
        url_cb=lambda u: print("\nopen this link if no browser appeared:\n  " + u + "\n"))
    if not ok:
        print(f"authorization failed: {msg}", file=sys.stderr)
        return 1
    print("token installed")
    return _verify_file_access()


# --------------------------------------------------------------------------
# mount lifecycle
# --------------------------------------------------------------------------

def cmd_mount(a) -> int:
    cfg = E.load_cfg()
    if E.is_mounted(cfg):
        print(f"already mounted at {E.mountpoint(cfg)}")
        return 0
    try:
        E.mount(cfg)
    except Exception as e:
        print(f"mount failed: {e}", file=sys.stderr)
        return 1
    print(f"mounted at {E.mountpoint(cfg)}")
    return 0


def cmd_unmount(a) -> int:
    E.unmount()
    print("unmounted")
    return 0


def cmd_restart(a) -> int:
    E.unmount()
    return cmd_mount(a)


def cmd_status(a) -> int:
    cfg = E.load_cfg()
    m = E.is_mounted(cfg)
    healthy, why = E.mount_healthy(cfg)
    ps = E.pins(cfg)
    state = "MOUNTED+OK" if healthy else ("MOUNTED!" if m else "not mounted")
    print(f"mount      {state:12}  {E.mountpoint(cfg)}")
    if m and not healthy:
        print(f"           ^ {why}")
    print(f"remote     {E.remote_spec(cfg)}   token: {'yes' if E.has_token() else 'NO'}")
    nloc, used = E.on_device()
    kept = sum(1 for r in E.local_files() if E.is_pinned(r, cfg)) if m else 0
    stray = max(0, nloc - kept)
    line = f"{nloc} file(s) · {E.human_bytes(used)}"
    if stray:
        line += f" ({kept} kept offline, {stray} cache leftover - tidied within 10 min)"
    else:
        line += f" ({kept} kept offline)"
    print(f"on device  {line}")
    print(f"rc         {'up on 127.0.0.1:%s' % cfg['rc_port'] if m and E.rc_available() else 'down'}")
    print(f"autostart  {'enabled' if E.unit_enabled() else 'disabled'}")
    print(f"pins       {len(ps)}")
    if m and a.counts:
        local = E.local_files()
        loc = len(local)
        cl = sum(1 for e in E.scan("", recursive=False) if e["state"] != "local")
        print(f"files      {loc} on this PC · {cl} cloud-only at the root")
    return 0 if m else 1


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------

def cmd_list(a) -> int:
    cfg = E.load_cfg()
    if not E.is_mounted(cfg):
        print("not mounted - run `yadisk mount`", file=sys.stderr)
        return 1
    rows = []
    for e in E.scan(E.rel_of(a.path or ""), recursive=a.recursive):
        if a.local_only and e["state"] != "local":
            continue
        if a.cloud_only and e["state"] == "local":
            continue
        if a.pinned and not E.is_pinned(e["rel"], cfg):
            continue
        rows.append(e)
        if a.limit and len(rows) >= a.limit:
            break
    if a.json:
        import json
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(nothing to show)")
        return 0
    print(f"{'STATE':6} {'SIZE':>10} {'LOCAL':>10}  PATH")
    for e in rows:
        tail = "/" if e.get("dir_flag") else ""
        print(f"{_state_tag(e['state']):6} {E.human_bytes(e['size']):>10} "
              f"{E.human_bytes(e['local_bytes']):>10}  {e['rel']}{tail}")
    loc = sum(1 for e in rows if e["state"] == "local")
    print(f"\n{len(rows)} files · {loc} local · {len(rows) - loc} cloud-only")
    return 0


# --------------------------------------------------------------------------
# pin / evict
# --------------------------------------------------------------------------

def cmd_pin(a) -> int:
    rels = [E.rel_of(p) for p in a.paths]
    added, errs = E.pin(rels, progress=lambda d, t: _bar(d, t, "keep offline "))
    E.export_state()
    for r in added:
        print(f"pinned {r}")
    for r, err in errs:
        print(f"failed {r}: {err}", file=sys.stderr)
    return 0 if added or not errs else 1


def cmd_unpin(a) -> int:
    n = E.unpin([E.rel_of(p) for p in a.paths])
    E.export_state()
    print(f"removed {n} pin(s) (bytes stay until the cache evicts them)")
    return 0


def cmd_state(a) -> int:
    """Publish the local/cloud state Dolphin's indicator plugin reads."""
    r = E.export_state()
    if not getattr(a, "quiet", False):
        print(f"state exported for {r['mount']}: "
              f"{r['local']} file(s) on this PC, {r['pins']} pin(s)")
        print(f"  {E.STATE_DIR}")
    return 0


def cmd_pins(a) -> int:
    if a.enforce:
        n = E.enforce_pins()
        print(f"re-hydrated {n} pinned file(s)")
        return 0
    ps = E.pins()
    if not ps:
        print("no pins")
        return 0
    for p in ps:
        mp = E.mount_path(p)
        kind = "dir " if os.path.isdir(mp) else "file"
        state = "ok" if (os.path.isdir(mp) or E.is_local(p)) else "MISSING-BYTES"
        print(f"{kind}  {state:14} {p}")
    return 0


def cmd_evict(a) -> int:
    """Available online only: drop the local bytes AND the offline flag.

    Same two-state model as the Windows client - a file is either kept on this
    PC or it lives in the cloud. Leaving a pin behind would be pointless: the
    30-minute pin enforcer would pull the bytes straight back.
    """
    cfg = E.load_cfg()
    if a.all_unpinned:
        targets = [r for r in E.local_files() if not E.is_pinned(r, cfg)]
    else:
        targets = []
        for p in a.paths:
            rel = E.rel_of(p)
            mp = E.mount_path(rel)
            if os.path.isdir(mp):
                targets += [e["rel"] for e in E.scan(rel) if e["state"] == "local"]
            else:
                targets.append(rel)
        cleared = E.unpin([E.rel_of(p) for p in a.paths])
        if cleared:
            print(f"stopped keeping {cleared} pin(s) offline")
    freed = 0
    for rel in targets:
        freed += E.local_bytes(rel)
    for rel in targets:
        E.evict(rel)
    E.export_state()
    print(f"freed ~{E.human_bytes(freed)} ({len(targets)} file(s)) - they stay visible, fetched on open")
    return 0


def cmd_tidy(a) -> int:
    dropped, freed = E.tidy(grace_min=a.grace, dry_run=a.dry_run)
    if a.dry_run:
        for r in dropped:
            print(f"would drop  {r}")
        print(f"would free ~{E.human_bytes(freed)} ({len(dropped)} file(s))")
    else:
        if dropped:
            E.export_state()
        if not a.quiet:
            print(f"freed ~{E.human_bytes(freed)} ({len(dropped)} cache leftover(s)); "
                  f"kept files stay on this PC")
    return 0


def cmd_hydrate(a) -> int:
    rels = [E.rel_of(p) for p in a.paths]
    done, errs = E.hydrate_many(rels, progress=lambda d, t: _bar(d, t, "fetch "))
    E.export_state()
    print(f"fetched {E.human_bytes(done)}")
    for r, err in errs:
        print(f"failed {r}: {err}", file=sys.stderr)
    return 0 if not errs else 1


# --------------------------------------------------------------------------
# gui / misc
# --------------------------------------------------------------------------

def cmd_gui(a) -> int:
    from . import gui
    return gui.main(tray_only=bool(a.tray))


def cmd_doctor(a) -> int:
    bad = 0
    for ok, msg in E.doctor():
        print(f"{'OK  ' if ok else 'FAIL'}  {msg}")
        bad += 0 if ok else 1
    return 0 if bad == 0 else 1


def cmd_desktop(a) -> int:
    files = E.install_assets()
    for f in files:
        print(f"installed {f}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yadisk", description="Yandex Disk with shadow files (cloud-only placeholders)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("setup", help="configure token, mountpoint, cache; install units")
    s.add_argument("--token", help="rclone token JSON blob or bare access token")
    s.add_argument("--from-log", help="file containing `rclone authorize yandex` output")
    s.add_argument("--client-id", help="your Yandex OAuth app client id")
    s.add_argument("--client-secret", help="your Yandex OAuth app client secret")
    s.add_argument("--mountpoint")
    s.add_argument("--store-size", dest="store_size",
                   help="safety cap for the local store, e.g. 20G (advanced; tidy keeps it "
                        "at kept-offline size anyway)")
    s.add_argument("--cache-size", dest="cache_size", help=argparse.SUPPRESS)
    s.add_argument("--autostart", action="store_true", help="mount at login")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("auth", help="authorize through your own Yandex OAuth app")
    s.add_argument("--client-id")
    s.add_argument("--client-secret")
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--redirect-uri",
                   help="Callback URL registered for your app; omit to let Yandex use it")
    s.add_argument("--code", action="store_true",
                   help="use the confirmation-code flow (no callback URL needed; "
                        "use this if the Redirect URI field is locked)")
    s.set_defaults(func=cmd_auth)

    s = sub.add_parser("mount"); s.set_defaults(func=cmd_mount)
    s = sub.add_parser("unmount"); s.set_defaults(func=cmd_unmount)
    s = sub.add_parser("restart"); s.set_defaults(func=cmd_restart)
    s = sub.add_parser("status", help="mount/d cache state")
    s.add_argument("--counts", action="store_true", help="walk the tree and count local/cloud files (slow)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list files with cloud/local state")
    s.add_argument("path", nargs="?")
    s.add_argument("--recursive", "-r", action="store_true",
                   help="walk subdirectories too (slow on a big disk)")
    s.add_argument("--local-only", action="store_true")
    s.add_argument("--cloud-only", action="store_true")
    s.add_argument("--pinned", action="store_true")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("pin", help="keep offline (download + protect from eviction)")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_pin)

    s = sub.add_parser("unpin")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_unpin)

    s = sub.add_parser("pins")
    s.add_argument("--enforce", action="store_true", help="re-download pins the cache dropped")
    s.set_defaults(func=cmd_pins)

    s = sub.add_parser("evict", help="available online only: drop local bytes (and the offline flag)")
    s.add_argument("paths", nargs="*")
    s.add_argument("--force", action="store_true", help="no-op, kept for compatibility")
    s.add_argument("--all-unpinned", action="store_true")
    s.set_defaults(func=cmd_evict)

    s = sub.add_parser("tidy", help="drop cached bytes that are not kept offline")
    s.add_argument("--grace", type=int, default=None,
                   help="minutes a merely-opened file may stay on disk (default 10)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_tidy)

    s = sub.add_parser("hydrate", help="download bytes without pinning")
    s.add_argument("paths", nargs="+")
    s.set_defaults(func=cmd_hydrate)

    s = sub.add_parser("state", help="publish local/cloud state for the Dolphin indicators")
    s.add_argument("--quiet", "-q", action="store_true")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("gui", help="open the window (default: window + tray)")
    s.add_argument("--tray", action="store_true", help="tray only, no window")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("doctor"); s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("install-desktop", help="write .desktop + Dolphin service menus")
    s.set_defaults(func=cmd_desktop)
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "cmd", None):
        a = p.parse_args(["gui"])
    try:
        return int(a.func(a) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
