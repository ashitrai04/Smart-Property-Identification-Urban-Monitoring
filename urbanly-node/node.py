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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import node_api

# Bumped whenever this file changes in a way the other laptop should pick up.
# A number beats comparing timestamps: a file extracted from a zip carries
# whatever date the zip recorded, so "newer" was a coin toss in exactly the
# situation this exists for.
__version__ = 8

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


HEIGHT_PORT = int(os.environ.get("UN_HEIGHT_PORT", "8900"))


def sniff_height_server():
    """Find the machine holding the DSM, the same way peers are found.

    The height server is a separate package on whichever laptop holds the
    elevation data, so its address is one more thing that would otherwise have
    to be copied between machines by hand and corrected whenever the network
    changed.
    """
    explicit = os.environ.get("UN_HEIGHT_SERVER", "").strip()
    if explicit:
        return explicit.rstrip("/")
    rq = _requests()
    for _, ip in [("self", "127.0.0.1")] + tailscale_peers():
        url = f"http://{ip}:{HEIGHT_PORT}"
        try:
            r = rq.get(f"{url}/health", timeout=3)
            if r.ok and (r.json() or {}).get("service") == "building-height":
                return url
        except Exception:
            continue
    return None


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


PROJECT_FOLDER = "Smart-Property-Identification-Urban-Monitoring"


def sniff_project():
    """Find a project installed by SETUP SERVER.bat, whatever config.env says.

    config.env gets written twice - once by autoconfig when this package first
    runs on its own, once by setup when the project lands - and whichever ran
    last wins. Get that order wrong, or lose the file, and the package looks
    for the servers inside its own folder, reports them "not installed here",
    and puts the weights somewhere nothing will read them, while the real
    install sits a few directories away.
    """
    here = Path(__file__).resolve().parent
    bases = [Path(d) for d in (r"C:\Urbanly", r"D:\Urbanly", r"E:\Urbanly")]
    bases += [Path.home() / "Urbanly", here.parent, here.parent.parent]
    for base in bases:
        for cand in (base / PROJECT_FOLDER, base):
            try:
                if (cand / "gpu-server" / "app" / "main.py").is_file():
                    return cand
            except OSError:
                continue
    return None


def persist_env(values):
    """Write settings into config.env, replacing any line that sets them."""
    cfg = Path(__file__).resolve().parent / "config.env"
    try:
        txt = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
        lines = [ln for ln in txt.splitlines()
                 if ln.split("=", 1)[0].strip() not in values]
        lines += ["", "# Written automatically - where the project was found."]
        lines += [f"{k}={v}" for k, v in values.items()]
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def repair_paths():
    """Re-point this package at the project, and bring the weights with it.

    Re-downloading 1.5 GB because a settings file lost three lines is not a
    reasonable thing to ask of anyone, so weights already pulled into the
    wrong folder are moved rather than fetched again.
    """
    if (config.GPU_SERVER_DIR / "app" / "main.py").is_file():
        return None
    proj = sniff_project()
    if proj is None:
        return None

    old_w = config.WEIGHTS_DIR
    config.GPU_SERVER_DIR = proj / "gpu-server"
    config.DATA_SERVER_DIR = proj / "local-server"
    config.WEIGHTS_DIR = proj / "gpu-server" / "weights"

    moved = 0
    try:
        if old_w.is_dir() and old_w.resolve() != config.WEIGHTS_DIR.resolve():
            config.WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
            for f in sorted(old_w.iterdir()):
                if not f.is_file():
                    continue
                dst = config.WEIGHTS_DIR / f.name
                if dst.exists() and dst.stat().st_size == f.stat().st_size:
                    continue
                shutil.move(str(f), str(dst))
                moved += 1
    except OSError:
        pass

    persist_env({"UN_GPU_SERVER_DIR": str(config.GPU_SERVER_DIR),
                 "UN_DATA_SERVER_DIR": str(config.DATA_SERVER_DIR),
                 "UN_WEIGHTS_DIR": str(config.WEIGHTS_DIR)})
    return proj, moved


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


def server_python():
    r"""The interpreter the servers must run under - not the one running this.

    SETUP SERVER.bat builds a virtual environment beside the project and puts
    PyTorch in it. The script you double-click may well be running under a
    bare system Python that has none of that: on the second laptop the project
    is installed in C:\Urbanly while the node package sits wherever it was
    copied, so the venv is nowhere near `..\gpu-server`. Spawning the model
    server with the wrong interpreter starts a process that dies on
    `import torch`, which shows up only as "did not answer in 60s" and gives
    no hint as to why.
    """
    here = Path(__file__).resolve().parent
    for c in [config.GPU_SERVER_DIR / ".venv" / "Scripts" / "python.exe",
              config.GPU_SERVER_DIR.parent / ".venv" / "Scripts" / "python.exe",
              here.parent / "gpu-server" / ".venv" / "Scripts" / "python.exe",
              config.GPU_SERVER_DIR / ".venv" / "bin" / "python"]:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return sys.executable


def has_module(py, mod):
    try:
        return subprocess.run([py, "-c", "import " + mod], timeout=120,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


LOG_DIR = Path(__file__).resolve().parent / "logs"


def spawn(title, cwd, args, env=None):
    """Start a server so that it outlives this script and its own crashes.

    Not `cmd /c start`: that needs an interactive desktop to put a window on,
    and where there is none - a scheduled task, a remote session, a script
    driving the start - the window never appears and the server silently never
    runs. Asking Windows for the console directly works in both cases.

    Whatever happens, the output goes to a file as well, because a server that
    dies in its own window takes the reason with it when the window closes.
    """
    LOG_DIR.mkdir(exist_ok=True)
    log = LOG_DIR / (title.replace(" ", "_").replace("-", "") + ".log")
    fh = open(log, "ab", buffering=0)
    if os.name != "nt":
        return subprocess.Popen(args, cwd=str(cwd), stdout=fh, stderr=fh,
                                env=env)

    CREATE_NEW_CONSOLE = 0x00000010
    DETACHED_PROCESS = 0x00000008
    try:
        p = subprocess.Popen(args, cwd=str(cwd), env=env,
                             creationflags=CREATE_NEW_CONSOLE)
        fh.close()
        return p
    except OSError:
        # No desktop to open a console on; run it detached and keep the log.
        return subprocess.Popen(args, cwd=str(cwd), stdout=fh, stderr=fh,
                                env=env, creationflags=DETACHED_PROCESS)


ASK_MODEL = os.environ.get("ASK_MODEL", "qwen2.5vl:3b")
OLLAMA_PORT = 11434


def start_ask_service(py):
    """Ollama, which answers the webapp's "Ask the survey" box.

    Warmed with a throwaway question, because the first request to a cold
    model loads several gigabytes from disk and otherwise that cost lands on
    whoever asks the first real question.
    """
    if port_open(OLLAMA_PORT):
        print(f"      already running on :{OLLAMA_PORT}")
    elif not shutil.which("ollama"):
        print("      ollama not installed - the chat box will be offline")
        print("      (everything else works; install it from ollama.com if wanted)")
        return
    else:
        try:
            subprocess.Popen(["ollama", "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"      could not start ollama: {str(e)[:70]}")
            return
        for _ in range(30):
            if port_open(OLLAMA_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(OLLAMA_PORT) else 'did not answer in 30s'}"
              f" on :{OLLAMA_PORT}")
    if not port_open(OLLAMA_PORT):
        return
    try:
        rq = _requests()
        rq.post(f"http://127.0.0.1:{OLLAMA_PORT}/api/chat", timeout=90,
                json={"model": ASK_MODEL, "stream": False,
                      "messages": [{"role": "user", "content": "ok"}]})
        print(f"      {ASK_MODEL} warmed")
    except Exception:
        # Loading several GB can outlast the wait. The model keeps loading in
        # the background, so this is a note, not a failure.
        print(f"      {ASK_MODEL} still loading - the chat box will work shortly")
        print(f"      (if it never does:  ollama pull {ASK_MODEL})")


QGIS_PROFILES = Path(os.path.expanduser(r"~\AppData\Roaming\QGIS\QGIS3\profiles"))
PLUGIN_NAME = "building_delineator"


def qgis_profiles():
    """Every QGIS profile on this machine, not just "default".

    A machine that has had QGIS reinstalled, or that someone set a second
    profile up on, loads plugins from the profile in use - which is not
    necessarily the one named default. Installing into all of them costs
    nothing and removes a failure that looks like the plugin never installed.
    """
    if not QGIS_PROFILES.is_dir():
        return []
    return [d for d in QGIS_PROFILES.iterdir() if d.is_dir()]


QGIS_PLUGIN_DIRS = [p / "python" / "plugins" / PLUGIN_NAME for p in qgis_profiles()]


def enable_in_qgis_ini(ini, name=PLUGIN_NAME):
    """Tick the plugin's box in QGIS's own settings file.

    A copied folder is an installed plugin but a disabled one: QGIS keeps the
    enabled list in QGIS3.ini and will not load anything missing from it, so
    without this the plugin is present and still does nothing.
    """
    if not ini.is_file():
        return
    txt = ini.read_text(encoding="utf-8", errors="ignore")
    if name + "=true" in txt:
        return
    lines = [ln for ln in txt.splitlines() if not ln.startswith(name + "=")]
    if "[PythonPlugins]" in lines:
        lines.insert(lines.index("[PythonPlugins]") + 1, name + "=true")
    else:
        lines += ["", "[PythonPlugins]", name + "=true"]
    ini.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_qgis_plugin(src):
    """Copy the plugin into every QGIS profile here and enable it.

    Handing someone a zip to install by hand is one more step that can be
    skipped, and a skipped step looks exactly like a broken server - the
    plugin is simply absent from the menu. Copying the folder is what QGIS's
    own installer does with the zip, so there is nothing to build first.
    """
    profiles = qgis_profiles()
    if not profiles:
        return [], "QGIS is not installed here"
    done = []
    for prof in profiles:
        dest = prof / "python" / "plugins" / src.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.zip"))
            enable_in_qgis_ini(prof / "QGIS" / "QGIS3.ini", src.name)
            done.append(dest)
        except Exception as e:
            return done, str(e)[:60]
    return done, None


FIREWALL_RULE = "Urbanly (Tailscale)"


def firewall_ok():
    """Whether inbound connections from the tailnet are actually allowed.

    Tailscale's adapter lands in the Private firewall profile, while the
    allow-rule Python gets from the prompt someone clicked through usually
    covers Public only. A machine like that can reach its peer and cannot be
    reached by it, which looks from the other side exactly like the peer being
    switched off - and that is the whole of the "offline" in a failed sync.
    """
    if os.name != "nt":
        return True
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            f"name={FIREWALL_RULE}"], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return True


def report_models():
    """What the model server is actually serving, once it is up.

    HiSup is not a separate process - it lives inside the model server - so
    "the model server started" does not tell you the 1.19 GB checkpoint
    loaded. This asks.
    """
    if not port_open(config.GPU_PORT):
        print("      model server is not up, so nothing to report")
        return
    rq = _requests()
    base = f"http://127.0.0.1:{config.GPU_PORT}"
    try:
        h = rq.get(f"{base}/hisup/health", timeout=30).json()
        loaded = (h.get("load") or {}).get("loaded")
        state = "loaded" if loaded else ("ready, loads on first use"
                                         if h.get("weights_present") else "WEIGHTS MISSING")
        print(f"      HiSup buildings : {state} on {h.get('device', '?')}")
        if h.get("load", {}).get("matched"):
            print(f"                        {h['load']['matched']} tensors matched")
    except Exception as e:
        print(f"      HiSup buildings : could not ask ({str(e)[:50]})")
    try:
        a = rq.get(f"{base}/api/health", timeout=20).json()
        caps = a.get("capabilities") or []
        print(f"      also serving    : {', '.join(c for c in caps if c != 'hisup')}")
    except Exception:
        pass
    try:
        hh = rq.get(f"{base}/height/health", timeout=25).json()
        if hh.get("ok"):
            idx = hh.get("index") or {}
            print(f"      building height : ready via {hh.get('server')} "
                  f"({(idx.get('dsm') or {}).get('count', '?')} DSM clips)")
        else:
            print(f"      building height : not available - {str(hh.get('reason', ''))[:48]}")
    except Exception:
        print("      building height : not available")


def report_qgis():
    """Whether the QGIS plugin is installed here and current.

    The plugin is not a service and nothing starts it, but it is part of the
    stack and the usual failure is an installed copy older than the server it
    talks to - which is invisible until a job fails.
    """
    here = Path(__file__).resolve().parent
    source = None
    # GPU_SERVER_DIR.parent first: on a machine set up by SETUP SERVER.bat the
    # project lives in its own folder and this package sits wherever it was
    # copied, so walking up from here never reaches the plugin source.
    bases = [config.GPU_SERVER_DIR.parent, here, *list(here.parents)[:4]]
    for base in bases:
        c = base / "qgis-plugin" / "building_delineator" / "client.py"
        try:
            if c.is_file():
                source = c
                break
        except OSError:
            continue

    dirs = [q / "python" / "plugins" / PLUGIN_NAME for q in qgis_profiles()]
    installed = next((d for d in dirs if (d / "client.py").is_file()), None)
    stale = bool(source and installed
                 and source.read_bytes() != (installed / "client.py").read_bytes())

    if source and (installed is None or stale):
        print("      not installed in QGIS" if installed is None
              else "      older than this project")
        done, why = install_qgis_plugin(source.parent)
        for d in done:
            print(f"      installed -> {d}")
        if why:
            print(f"      could not install automatically: {why}")
            print(f"      Install this folder by hand in QGIS: {source.parent}")
        elif done:
            print("      enabled - restart QGIS if it is open")
            installed = done[0]
    elif installed is None:
        print("      plugin source not found next to this package")
        return
    else:
        print(f"      installed at {installed}")
        print("      up to date with this project")

    if installed is None:
        return
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_bd_client", installed / "client.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        url = m.resolve("auto")
        print(f"      it will use {url}")
    except Exception as e:
        print(f"      could not check which server it would use ({str(e)[:40]})")


def bring_up(skip_sync=False, no_tunnel=False, no_ask=False):
    py = sys.executable
    print("\n[0/7] settings")
    autoconfig()
    fixed = repair_paths()
    if fixed:
        proj, moved = fixed
        print(f"      found the project at {proj}")
        if moved:
            print(f"      moved {moved} weight file(s) into its weights folder")
        print("      config.env updated - this will not need doing again")
    # After repair_paths, never before it: the environment holding PyTorch is
    # inside the project, so asking where it is while the project is still
    # being located gets the answer "nowhere" and quietly falls back to a
    # system Python that has none of the model packages.
    srv = server_python()
    refresh_peers()
    if not config.PEERS:
        print("      no peer found - running standalone, which is fine")
    if not firewall_ok():
        print("      [!] inbound connections are blocked by Windows Firewall,")
        print("          so the other laptop will see this one as offline.")
        print("          Right-click ALLOW TAILSCALE.bat -> Run as administrator")
        print("          (once per machine; it opens the ports to the tailnet only)")
    if not skip_sync:
        print("\n[1/7] syncing with peers")
        do_sync()
    else:
        print("\n[1/7] sync skipped")

    print("\n[2/7] chat service")
    start_ask_service(py)

    print("\n[2b]  height server")
    height = sniff_height_server()
    env = dict(os.environ)
    if height:
        env["HEIGHT_SERVER"] = height
        print(f"      height server found at {height}")
    else:
        print("      no height server found - buildings will have no heights")
        print("      (start height-server\\start.bat on the machine with the DSM)")

    print("\n[3/7] model server")
    if srv != py:
        print(f"      project environment: {srv}")
    if port_open(config.GPU_PORT):
        print(f"      already running on :{config.GPU_PORT}")
    elif not (config.GPU_SERVER_DIR / "app" / "main.py").exists():
        print(f"      not installed here ({config.GPU_SERVER_DIR})")
        print("      Run SETUP SERVER.bat once to install it, or leave this")
        print("      machine as a data peer and run models on the other one.")
    else:
        if not has_module(srv, "torch"):
            print("      PyTorch is missing from the environment this would use,")
            print("      so the model server would start and immediately die.")
            print(f"      Run SETUP SERVER.bat once.  ({srv})")
        else:
            spawn("Urbanly - models", config.GPU_SERVER_DIR,
                  [srv, "-m", "uvicorn", "main:app", "--app-dir", "app",
                   "--host", "0.0.0.0", "--port", str(config.GPU_PORT)], env=env)
            for _ in range(90):
                if port_open(config.GPU_PORT):
                    break
                time.sleep(1)
            if port_open(config.GPU_PORT):
                print(f"      ready on :{config.GPU_PORT}")
            else:
                print("      did not answer in 90s - the reason is in")
                print(f"      {LOG_DIR / 'Urbanly__models.log'}")

    print("\n[4/7] data server")
    print(f"      serving {config.DATA_DIR}"
          f"{'' if config.DATA_DIR.is_dir() else '   [!] no such folder'}")
    if port_open(config.DATA_PORT):
        print(f"      already running on :{config.DATA_PORT}")
    elif not (config.DATA_SERVER_DIR / "run.py").exists():
        print(f"      not installed here ({config.DATA_SERVER_DIR})")
        print("      Comes with SETUP SERVER.bat, same as the model server.")
    else:
        # --data explicitly: run.py falls back to a hardcoded path that
        # is this laptop's layout and nobody else's. Without it the second
        # machine starts a data server over an empty folder, and both the
        # webapp and the QGIS plugin show no layers at all.
        args = [srv, "-u", "run.py", "--port", str(config.DATA_PORT),
                "--data", str(config.DATA_DIR)]
        if no_tunnel:
            args.append("--no-tunnel")
        denv = dict(env)
        denv["GEO_DATA_DIR"] = str(config.DATA_DIR)
        spawn("Urbanly - data", config.DATA_SERVER_DIR, args, env=denv)
        for _ in range(90):
            if port_open(config.DATA_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(config.DATA_PORT) else 'did not answer in 90s'}"
              f" on :{config.DATA_PORT}")

    print("\n[5/7] node service")
    if port_open(config.NODE_PORT):
        print(f"      already running on :{config.NODE_PORT}")
    else:
        spawn("Urbanly - node", Path(__file__).resolve().parent,
              [srv if has_module(srv, "fastapi") else py,
               "-m", "uvicorn", "node_api:app", "--host", "0.0.0.0",
               "--port", str(config.NODE_PORT)])
        for _ in range(40):
            if port_open(config.NODE_PORT):
                break
            time.sleep(1)
        print(f"      {'ready' if port_open(config.NODE_PORT) else 'did not answer'}"
              f" on :{config.NODE_PORT}")

    print("\n[6/7] models")
    report_models()

    print("\n[7/7] QGIS plugin")
    report_qgis()

    print()
    show_status()
    addr, how = reachable_address()
    print("\n  The other laptop reaches this one at:")
    print(f"      UN_PEERS={addr}")
    print(f"      ({how})")
    print("\n  Leave the server windows open. Closing one stops that service.\n")


REPO_ZIP = ("https://codeload.github.com/ashitrai04/"
            "Smart-Property-Identification-Urban-Monitoring/zip/refs/heads/main")
SNAPSHOT_EVERY = 12 * 3600


def refresh_snapshot(repo):
    """Update a project that was installed from a zip rather than cloned.

    `git clone` is tried first at install time, but on a machine without git
    the project arrives as a zip: a snapshot with no way to pull. Left alone
    it stays frozen at whatever main looked like that day, which is how the
    second laptop came to run a startup script several versions behind the one
    it had just been given - and to report step [2/4] of a sequence that has
    had seven steps for a while.

    Code only. The virtual environment, the weights and config.env are left
    exactly as they are, so this costs a download and nothing else.
    """
    import tempfile
    import urllib.request
    import zipfile

    stamp = repo / "urbanly-node" / ".snapshot"
    try:
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < SNAPSHOT_EVERY:
            return 0
    except OSError:
        pass

    keep = {".venv", "weights", "config.env", "node_modules", ".git", "logs",
            "__pycache__", ".snapshot"}
    print("      refreshing the project (no git here, so it came as a zip)")
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            z = td / "repo.zip"
            with urllib.request.urlopen(REPO_ZIP, timeout=600) as r, open(z, "wb") as fh:
                shutil.copyfileobj(r, fh)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(td)
            root = next((d for d in td.iterdir()
                         if d.is_dir() and d.name.startswith("Smart-Property")), None)
            if root is None:
                return 0
            n = 0
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root)
                if any(part in keep for part in rel.parts):
                    continue
                dst = repo / rel
                try:
                    if dst.exists() and dst.read_bytes() == f.read_bytes():
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    n += 1
                except OSError:
                    continue
    except Exception as e:
        print(f"      could not refresh it: {str(e)[:60]}")
        return 0
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("", encoding="utf-8")
    except OSError:
        pass
    print(f"      {n} file(s) updated in the project")
    return n


def file_version(path):
    """The __version__ another copy of this file declares, or 0."""
    try:
        m = re.search(r"^__version__ = (\d+)", path.read_text(encoding="utf-8"),
                      re.M)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def self_update():
    """Keep a handed-over copy of this package in step with the project.

    On a machine set up by SETUP SERVER.bat there are two copies of this
    folder: the one that was copied over by hand, which is what gets
    double-clicked, and the one inside the project, which is a git clone and
    can be pulled. Without this, every fix to the startup has to be carried
    across by hand - and the machine that needs the fix is the one nobody is
    sitting at.

    Does nothing when the package being run is already the project's own, so
    the machine the project is developed on is unaffected.

    Returns (restart_needed, names_changed).
    """
    here = Path(__file__).resolve().parent
    src = config.GPU_SERVER_DIR.parent / "urbanly-node"
    # config.env can be missing the line that says where the project went,
    # in which case the above resolves to this very folder and nothing ever
    # updates. Look for the project the same way the startup does.
    if not (src / "node.py").is_file():
        proj = sniff_project()
        if proj is not None:
            src = proj / "urbanly-node"
    try:
        if not src.is_dir() or src.resolve() == here:
            return False, []
    except OSError:
        return False, []

    if (src.parent / ".git").is_dir() and shutil.which("git"):
        try:
            subprocess.run(["git", "-C", str(src.parent), "pull", "--ff-only"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=240)
        except Exception:
            pass
    else:
        refresh_snapshot(src.parent)

    # A project installed from a zip can be well behind the copy someone has
    # just handed over, and copying it anyway replaces a working startup with
    # the broken one it was brought in to replace.
    if file_version(src / "node.py") <= __version__:
        return False, []

    changed = []
    for f in sorted(src.glob("*.py")) + sorted(src.glob("*.bat")):
        dst = here / f.name
        try:
            if dst.exists() and dst.read_bytes() == f.read_bytes():
                continue
            shutil.copy2(f, dst)
            changed.append(f.name)
        except OSError:
            continue
    return ("node.py" in changed or "config.py" in changed), changed


def main():
    # Progress that arrives only at the end is not progress. Python
    # block-buffers stdout whenever it is not a terminal, which is
    # exactly the case when this is run from a script or piped to a log.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="up",
                choices=["status", "sync", "up", "serve", "autoconfig"])
    ap.add_argument("--dry-run", action="store_true", help="sync: list, do not fetch")
    ap.add_argument("--no-sync", action="store_true", help="up: start without syncing")
    ap.add_argument("--no-tunnel", action="store_true", help="up: no public address")
    ap.add_argument("--no-ask", action="store_true",
                    help="up: skip the chat service")
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
        if not os.environ.get("UN_NO_SELF_UPDATE"):
            restart, changed = self_update()
            if changed:
                print(f"  updated from the project: {', '.join(changed)}")
            if restart:
                print("  restarting with the new version\n")
                os.environ["UN_NO_SELF_UPDATE"] = "1"
                os.execv(sys.executable,
                         [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])
        bring_up(skip_sync=a.no_sync, no_tunnel=a.no_tunnel, no_ask=a.no_ask)


if __name__ == "__main__":
    main()
