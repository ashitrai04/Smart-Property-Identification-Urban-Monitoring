"""Bring this laptop up as an Urbanly node, and keep it in step with the other.

    python node.py status      what I have, and which peers are reachable
    python node.py sync        pull anything newer from a peer
    python node.py up          sync, then start every server, then report

Both laptops run exactly this. Neither is the master: whichever is running
serves, and whichever has the newer file wins on sync. That is why the command
is the same on both and why nothing here is called "client".
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import node_api

BAR = "=" * 66


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _requests():
    import requests
    return requests


# ── peers ───────────────────────────────────────────────────────────
def probe(url, timeout=None):
    """Ask a peer what it is. Never raises - a peer being off is normal."""
    rq = _requests()
    t0 = time.time()
    try:
        r = rq.get(f"{url}/node/status", params={"full": "true"},
                   headers={"X-API-Key": config.API_KEY} if config.API_KEY else {},
                   timeout=timeout or config.PROBE_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        d["url"] = url
        d["online"] = True
        d["ms"] = int((time.time() - t0) * 1000)
        return d
    except Exception as e:
        return {"url": url, "online": False, "reason": str(e)[:90]}


def peer_line(p):
    if not p["online"]:
        return f"  offline  {p['url']}\n           {p.get('reason', '')}"
    g = p.get("gpu") or {}
    gpu = (f"{g.get('name', '?')} - {g.get('free_gb', '?')} GB free"
           if g.get("available") else "no GPU (CPU only)")
    c = p.get("counts") or {}
    bits = f"{c.get('layers', '?')} layers, {c.get('weights', '?')} weights, {c.get('rasters', '?')} rasters"
    auth = "" if p.get("authenticated", True) else "   [no key — manifest hidden]"
    return (f"  ONLINE   {p.get('node', '?')} at {p['url']}  ({p['ms']} ms){auth}\n"
            f"           {gpu}\n"
            f"           {bits}")


# ── sync ────────────────────────────────────────────────────────────
def plan(local, remote):
    """What to pull: anything the peer has that we lack or that is newer.

    Compared by fingerprint first and mtime second. Two files with the same
    fingerprint are the same file however their timestamps drifted, which
    matters because copying between machines rarely preserves mtime.
    """
    jobs = []
    for kind in ("weights", "layers"):
        mine = {e["name"]: e for e in local.get(kind, [])}
        theirs = {e["name"]: e for e in (remote.get("manifest") or {}).get(kind, [])}
        for name, r in theirs.items():
            m = mine.get(name)
            if m is None:
                jobs.append((kind, r, "new here"))
            elif m["fingerprint"] != r["fingerprint"] and r["mtime"] > m["mtime"]:
                jobs.append((kind, r, f"newer by {(r['mtime'] - m['mtime']) // 60} min"))
    return jobs


def download(url, kind, entry, dest_root, dry=False):
    """Fetch one file to a temporary name, then move it into place.

    Never written directly over the live file: a half-downloaded checkpoint
    that happens to be the right name is far worse than no checkpoint, and the
    model server may be reading it at the time.
    """
    target = dest_root / entry["name"]
    if dry:
        return True, f"would fetch {entry['name']} ({human(entry['bytes'])})"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    rq = _requests()
    singular = "layer" if kind == "layers" else "weight"
    try:
        with rq.get(f"{url}/node/file", params={"kind": singular, "name": entry["name"]},
                    headers={"X-API-Key": config.API_KEY} if config.API_KEY else {},
                    stream=True, timeout=config.SYNC_TIMEOUT) as r:
            r.raise_for_status()
            got = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
                    got += len(chunk)
        if entry["bytes"] and got != entry["bytes"]:
            tmp.unlink(missing_ok=True)
            return False, f"{entry['name']}: got {human(got)} of {human(entry['bytes'])}"
        if target.exists():
            keep = target.with_suffix(target.suffix + ".prev")
            keep.unlink(missing_ok=True)
            shutil.move(str(target), str(keep))
        shutil.move(str(tmp), str(target))
        return True, f"{entry['name']} ({human(got)})"
    except Exception as e:
        Path(tmp).unlink(missing_ok=True)
        return False, f"{entry['name']}: {str(e)[:90]}"


def do_sync(dry=False):
    local = node_api.manifest()
    if not config.PEERS:
        print("  no peers configured — set UN_PEERS in config.env")
        return 0
    pulled = failed = 0
    for url in config.PEERS:
        p = probe(url)
        print(peer_line(p))
        if not p["online"]:
            continue
        if not p.get("authenticated", True):
            print("           cannot sync without a matching UN_API_KEY")
            continue
        jobs = plan(local, p)
        if not jobs:
            print("           already in step — nothing to pull")
            continue
        total = sum(e["bytes"] for _, e, _ in jobs)
        print(f"           {len(jobs)} file(s) to pull, {human(total)}")
        for kind, entry, why in jobs:
            root = config.DATA_DIR if kind == "layers" else config.WEIGHTS_DIR
            ok, msg = download(url, kind, entry, root, dry=dry)
            print(f"             {'ok  ' if ok else 'FAIL'} {msg}   [{why}]")
            pulled += ok
            failed += not ok
        local = node_api.manifest()
    print(f"\n  pulled {pulled}, failed {failed}")
    return failed


# ── status ──────────────────────────────────────────────────────────
def show_status():
    g = node_api.gpu_info()
    m = node_api.manifest()
    print(BAR)
    print(f"  THIS NODE   {config.NODE_NAME}")
    print(f"  address     {node_api.lan_ip()}:{config.NODE_PORT}")
    if g.get("available"):
        print(f"  GPU         {g['name']} - {g['free_gb']} of {g['total_gb']} GB free")
    else:
        print(f"  GPU         none detected — CPU only ({g.get('device')})")
    print(f"  data        {config.DATA_DIR}"
          + ("" if config.DATA_DIR.exists() else "   [MISSING]"))
    print(f"  weights     {config.WEIGHTS_DIR}"
          + ("" if config.WEIGHTS_DIR.exists() else "   [MISSING]"))
    print(f"  holds       {len(m['layers'])} layers ({human(sum(e['bytes'] for e in m['layers']))}), "
          f"{len(m['weights'])} weights ({human(sum(e['bytes'] for e in m['weights']))}), "
          f"{len(m['rasters'])} rasters ({human(sum(e['bytes'] for e in m['rasters']))})")
    print(BAR)
    if not config.PEERS:
        print("  PEERS       none configured (UN_PEERS in config.env)")
    else:
        print("  PEERS")
        for url in config.PEERS:
            print(peer_line(probe(url)))
    print(BAR)


# ── bringing the stack up ───────────────────────────────────────────
def port_open(port, host="127.0.0.1", timeout=1.0):
    import socket
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except Exception:
        return False


def spawn(title, cwd, args):
    """Start a server in its own console so one crash does not take the rest."""
    if os.name == "nt":
        return subprocess.Popen(["cmd", "/c", "start", title, "cmd", "/k",
                                 "cd", "/d", str(cwd), "&&", *args])
    return subprocess.Popen(args, cwd=str(cwd))


def bring_up(skip_sync=False, no_tunnel=False):
    py = sys.executable
    if not skip_sync:
        print("\n[1/4] syncing with peers")
        do_sync()
    else:
        print("\n[1/4] sync skipped")

    print("\n[2/4] model server")
    if port_open(config.GPU_PORT):
        print(f"      already running on :{config.GPU_PORT}")
    elif not (config.GPU_SERVER_DIR / "app" / "main.py").exists():
        print(f"      not found at {config.GPU_SERVER_DIR} — skipped")
    else:
        spawn("Urbanly - models", config.GPU_SERVER_DIR,
              [py, "-m", "uvicorn", "main:app", "--app-dir", "app",
               "--host", "0.0.0.0", "--port", str(config.GPU_PORT)])
        for _ in range(60):
            if port_open(config.GPU_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(config.GPU_PORT) else 'did not answer in 60s'}"
              f" on :{config.GPU_PORT}")

    print("\n[3/4] data server")
    if port_open(config.DATA_PORT):
        print(f"      already running on :{config.DATA_PORT}")
    elif not (config.DATA_SERVER_DIR / "run.py").exists():
        print(f"      not found at {config.DATA_SERVER_DIR} — skipped")
    else:
        args = [py, "-u", "run.py", "--port", str(config.DATA_PORT)]
        if no_tunnel:
            args.append("--no-tunnel")
        spawn("Urbanly - data", config.DATA_SERVER_DIR, args)
        for _ in range(90):
            if port_open(config.DATA_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(config.DATA_PORT) else 'did not answer in 90s'}"
              f" on :{config.DATA_PORT}")

    print("\n[4/4] node service")
    if port_open(config.NODE_PORT):
        print(f"      already running on :{config.NODE_PORT}")
    else:
        spawn("Urbanly - node", Path(__file__).resolve().parent,
              [py, "-m", "uvicorn", "node_api:app", "--host", "0.0.0.0",
               "--port", str(config.NODE_PORT)])
        for _ in range(40):
            if port_open(config.NODE_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(config.NODE_PORT) else 'did not answer'}"
              f" on :{config.NODE_PORT}")

    print()
    show_status()
    print("\n  The other laptop reaches this one at:")
    print(f"      UN_PEERS={node_api.lan_ip()}:{config.NODE_PORT}".replace(
        "UN_PEERS=", "UN_PEERS=http://"))
    print("\n  Leave the server windows open. Closing one stops that service.\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "sync", "up", "serve"])
    ap.add_argument("--dry-run", action="store_true", help="sync: list, do not fetch")
    ap.add_argument("--no-sync", action="store_true", help="up: start without syncing")
    ap.add_argument("--no-tunnel", action="store_true", help="up: no public address")
    a = ap.parse_args()

    if a.command == "status":
        show_status()
    elif a.command == "sync":
        sys.exit(1 if do_sync(dry=a.dry_run) else 0)
    elif a.command == "serve":
        import uvicorn
        uvicorn.run("node_api:app", host="0.0.0.0", port=config.NODE_PORT)
    else:
        bring_up(skip_sync=a.no_sync, no_tunnel=a.no_tunnel)


if __name__ == "__main__":
    main()
