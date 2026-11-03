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


# ── finding things without being told ───────────────────────────────
TAILSCALE = [
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/usr/bin/tailscale", "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
]


def tailscale_peers():
    """Online machines on this tailnet, as (name, ip).

    Tailscale already knows which of your machines are up and what address
    each has, and it is the address that keeps working when the two laptops
    are on different networks. Asking it is better than making someone copy an
    IP between two config files and update it whenever the network changes.
    """
    exe = shutil.which("tailscale") or next((t for t in TAILSCALE if os.path.isfile(t)), None)
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode != 0:
            return []
        import json as _json
        d = _json.loads(out.stdout)
    except Exception:
        return []
    found = []
    for v in (d.get("Peer") or {}).values():
        if not v.get("Online"):
            continue
        ips = [i for i in (v.get("TailscaleIPs") or []) if ":" not in i]
        if ips:
            found.append((v.get("HostName", "peer"), ips[0]))
    return found


def tailscale_self_ip():
    """This machine's own tailnet address, or None.

    The address to hand a peer is this one, not the LAN address: the two
    laptops are regularly on different networks, where a 192.168 address
    reaches nothing. Printing the LAN address as the thing to copy sent
    someone to 192.168.88.7 from a machine on a phone hotspot.
    """
    exe = shutil.which("tailscale") or next((t for t in TAILSCALE if os.path.isfile(t)), None)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode != 0:
            return None
        import json as _json
        ips = (_json.loads(out.stdout).get("Self") or {}).get("TailscaleIPs") or []
        return next((i for i in ips if ":" not in i), None)
    except Exception:
        return None


def reachable_address():
    """Where a peer should be told to find this node."""
    ts = tailscale_self_ip()
    if ts:
        return f"http://{ts}:{config.NODE_PORT}", "Tailscale - works from any network"
    import node_api
    return (f"http://{node_api.lan_ip()}:{config.NODE_PORT}",
            "LAN only - both machines must share a router")


def sniff_peers():
    """Which online tailnet machines are actually running a node."""
    hits = []
    for name, ip in tailscale_peers():
        url = f"http://{ip}:{config.NODE_PORT}"
        p = probe(url, timeout=3)
        if p["online"]:
            hits.append((name, url))
    return hits


DATA_CANDIDATES = [
    r"D:\GEO_SERVE", r"E:\GEO_SERVE", r"C:\GEO_SERVE",
    os.path.expanduser(r"~\Downloads\GEO_SERVE"),
    os.path.expanduser(r"~\GEO_SERVE"),
    os.path.expanduser(r"~\Downloads\GEO SERVE"),
]


def sniff_data_dir():
    """The survey folder, preferring one that actually has survey data in it."""
    best, best_score = None, -1
    for c in DATA_CANDIDATES:
        d = Path(c)
        if not d.is_dir():
            continue
        try:
            score = sum(1 for x in d.iterdir()
                        if x.is_file() and x.suffix.lower() in (".gpkg", ".tif"))
        except Exception:
            score = 0
        if score > best_score:
            best, best_score = d, score
    return best


def sniff_weights_dir():
    """The model folder, which is not always beside this package.

    The zip can be extracted anywhere - next to the repository, or on its own
    on a laptop that has no checkout at all. Walk up a few levels looking for
    a real gpu-server/weights, and otherwise keep a local one so there is
    always somewhere for a synced checkpoint to land.
    """
    here = Path(__file__).resolve().parent
    for base in [here, *here.parents][:5]:
        c = base / "gpu-server" / "weights"
        if c.is_dir():
            return c
    # No absolute path from any one machine belongs here: the same folder runs
    # on both laptops, and a path that happens to exist on one is a trap on
    # the other.
    local = here / "weights"
    local.mkdir(parents=True, exist_ok=True)
    return local


def autoconfig(force=False):
    """Write a config.env this machine can run with, without being edited."""
    cfg = Path(__file__).resolve().parent / "config.env"
    if cfg.exists() and not force:
        return cfg

    data = sniff_data_dir()
    weights = sniff_weights_dir()
    print("  detecting settings for this machine")
    print(f"    name   : {config.NODE_NAME}")
    print(f"    data   : {data if data else 'not found - set UN_DATA_DIR by hand'}")
    print(f"    weights: {weights}")

    peers = sniff_peers()
    if peers:
        for n, u in peers:
            print(f"    peer   : {n} at {u}")
    else:
        found = tailscale_peers()
        if found:
            print(f"    peer   : none running a node yet "
                  f"({len(found)} machine(s) online on the tailnet)")
        else:
            print("    peer   : none found - add UN_PEERS later if you want sync")

    lines = [
        "# Written automatically on first run. Edit freely.",
        f"UN_NODE_NAME={config.NODE_NAME}",
        f"UN_DATA_DIR={data if data else r'D:\GEO_SERVE'}",
        f"UN_WEIGHTS_DIR={weights}",
        "",
        "# The other laptop running this package. Filled in automatically when",
        "# it is online and reachable over Tailscale; set it by hand otherwise.",
        f"UN_PEERS={','.join(u for _, u in peers)}",
        "",
        "# Leave empty on a private tailnet. If you ever expose the node port",
        "# publicly, set the SAME long random string on both laptops.",
        "UN_API_KEY=",
        "",
        f"UN_NODE_PORT={config.NODE_PORT}",
        f"UN_GPU_PORT={config.GPU_PORT}",
        f"UN_DATA_PORT={config.DATA_PORT}",
        "",
    ]
    cfg.write_text("\n".join(lines), encoding="utf-8")
    print(f"    written: {cfg.name}")
    # config.py read its values at import, before this file existed, so apply
    # the detected ones now rather than making the first run the odd one out.
    if data:
        config.DATA_DIR = Path(data)
    config.WEIGHTS_DIR = Path(weights)
    if peers:
        config.PEERS = [u for _, u in peers]
    return cfg


def refresh_peers():
    """If no peer is configured, look once more before giving up on sync."""
    if config.PEERS:
        return
    hits = sniff_peers()
    if not hits:
        return
    config.PEERS = [u for _, u in hits]
    print(f"  found {len(hits)} peer(s) on the tailnet: "
          + ", ".join(n for n, _ in hits))
    cfg = Path(__file__).resolve().parent / "config.env"
    if cfg.exists():
        txt = cfg.read_text(encoding="utf-8")
        if "\nUN_PEERS=" in txt:
            out = []
            for line in txt.splitlines():
                out.append(f"UN_PEERS={','.join(config.PEERS)}"
                           if line.startswith("UN_PEERS=") else line)
            cfg.write_text("\n".join(out) + "\n", encoding="utf-8")


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
           if g.get("available")
           else ("models not installed there yet" if g.get("device") == "no-pytorch"
                 else "no usable GPU"))
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
    addr, how = reachable_address()
    print(f"  address     {addr}")
    print(f"              {how}")
    if g.get("available"):
        print(f"  GPU         {g['name']} - {g['free_gb']} of {g['total_gb']} GB free")
    else:
        if g.get("device") == "no-pytorch":
            print("  GPU         not checked yet - PyTorch is not installed here")
            print("              This machine cannot run models until you run")
            print("              SETUP SERVER.bat once. It says nothing about")
            print("              whether the laptop has a graphics card.")
        else:
            print(f"  GPU         none usable - {g.get('reason', g.get('device'))}")
    print(f"  data        {config.DATA_DIR}"
          + ("" if config.DATA_DIR.exists() else "   [MISSING]"))
    print(f"  weights     {config.WEIGHTS_DIR}"
          + ("" if config.WEIGHTS_DIR.exists() else "   [MISSING]"))
    print(f"  holds       {len(m['layers'])} layers ({human(sum(e['bytes'] for e in m['layers']))}), "
          f"{len(m['weights'])} weights ({human(sum(e['bytes'] for e in m['weights']))}), "
          f"{len(m['rasters'])} rasters ({human(sum(e['bytes'] for e in m['rasters']))})")
    print(BAR)
    if not config.PEERS:
        print("  PEERS       none found")
        print("              Nothing to sync with yet. Start Urbanly on the")
        print("              other laptop and it will be picked up by itself,")
        print("              or set UN_PEERS in config.env by hand.")
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
    print("\n[0/4] settings")
    autoconfig()
    refresh_peers()
    if not config.PEERS:
        print("      no peer found - running standalone, which is fine")
    if not skip_sync:
        print("\n[1/4] syncing with peers")
        do_sync()
    else:
        print("\n[1/4] sync skipped")

    print("\n[2/4] model server")
    if port_open(config.GPU_PORT):
        print(f"      already running on :{config.GPU_PORT}")
    elif not (config.GPU_SERVER_DIR / "app" / "main.py").exists():
        print(f"      not installed here ({config.GPU_SERVER_DIR})")
        print("      Run SETUP SERVER.bat once to install it, or leave this")
        print("      machine as a data peer and run models on the other one.")
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
        print(f"      not installed here ({config.DATA_SERVER_DIR})")
        print("      Comes with SETUP SERVER.bat, same as the model server.")
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
    addr, how = reachable_address()
    print("\n  The other laptop reaches this one at:")
    print(f"      UN_PEERS={addr}")
    print(f"      ({how})")
    print("\n  Leave the server windows open. Closing one stops that service.\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="up",
                choices=["status", "sync", "up", "serve", "autoconfig"])
    ap.add_argument("--dry-run", action="store_true", help="sync: list, do not fetch")
    ap.add_argument("--no-sync", action="store_true", help="up: start without syncing")
    ap.add_argument("--no-tunnel", action="store_true", help="up: no public address")
    a = ap.parse_args()

    if a.command == "autoconfig":
        autoconfig(force=True)
    elif a.command == "status":
        autoconfig()
        refresh_peers()
        show_status()
    elif a.command == "sync":
        refresh_peers()
        sys.exit(1 if do_sync(dry=a.dry_run) else 0)
    elif a.command == "serve":
        import uvicorn
        uvicorn.run("node_api:app", host="0.0.0.0", port=config.NODE_PORT)
    else:
        bring_up(skip_sync=a.no_sync, no_tunnel=a.no_tunnel)


if __name__ == "__main__":
    main()
