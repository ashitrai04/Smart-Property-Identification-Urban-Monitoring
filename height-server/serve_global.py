r"""Serve the height API to the whole internet, and publish where it is.

Same shape as the GPU server's tunnel.sh / publish_url.py, for the same
reasons: cloudflared makes only an OUTBOUND connection to Cloudflare, so
there are no open ports, no router changes, no admin rights, and it works
behind a university or hotel firewall that blocks inbound traffic entirely.

The free quick tunnel hands out a new hostname on every restart, which is
why the address is published to R2 (runtime/height_server.json) rather than
written down. The HiSup machine reads it from there, so HEIGHT_SERVER=auto
keeps working across restarts of either laptop.

    python serve_global.py                 quick tunnel, random hostname
    python serve_global.py --no-tunnel     LAN only, same as start.bat
    CF_TUNNEL_TOKEN=...  python serve_global.py     fixed hostname

ONE THING TO UNDERSTAND. On the LAN, leaving the API open is fine. On the
open internet it is not: a trycloudflare hostname is not a secret - it
appears in TLS SNI and in certificate logs, and those get scanned - and an
open endpoint here lets a stranger read your elevation data and hammer the
disk the rasters live on. So an API key is REQUIRED in tunnel mode. If you
have not set one, this generates it, saves it, prints it, and publishes it
alongside the URL so the HiSup machine picks it up without you copying it.
"""
import argparse
import json
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

R2_KEY = "runtime/height_server.json"
CF_PATHS = [r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
            "/usr/local/bin/cloudflared", "/opt/homebrew/bin/cloudflared"]
CF_RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def env():
    """Environment plus config.env next to this script, for the R2 credentials."""
    out = dict(os.environ)
    p = HERE / "config.env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.split(" #", 1)[0].strip())
    return out


# ── the API key ─────────────────────────────────────────────────────
def load_or_make_key(e):
    """A key that survives restarts, so the HiSup side is not reconfigured daily."""
    if e.get("HS_API_KEY"):
        return e["HS_API_KEY"], "from HS_API_KEY"
    store = HERE / "cache" / "api_key.txt"
    if store.exists():
        k = store.read_text(encoding="utf-8").strip()
        if k:
            return k, f"reused from {store.name}"
    k = secrets.token_urlsafe(24)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(k, encoding="utf-8")
    try:    # best effort on Windows; on POSIX this actually restricts it
        store.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return k, f"generated and saved to {store.name}"


# ── cloudflared ─────────────────────────────────────────────────────
def cloudflared_path():
    exe = shutil.which("cloudflared") or next((p for p in CF_PATHS if os.path.isfile(p)), None)
    if exe:
        return exe
    local = HERE / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local.is_file():
        return str(local)

    sysname, mach = platform.system().lower(), platform.machine().lower()
    arch = "arm64" if mach in ("arm64", "aarch64") else "amd64"
    if sysname == "windows":
        asset = f"cloudflared-windows-{arch}.exe"
    elif sysname == "darwin":
        asset = f"cloudflared-darwin-{arch}.tgz"
    else:
        asset = f"cloudflared-linux-{arch}"
    if asset.endswith(".tgz"):
        print("  ! install cloudflared first:  brew install cloudflared")
        return None

    print(f"  downloading cloudflared ({asset}) ...")
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(CF_RELEASE + asset, timeout=120) as r, \
                open(local, "wb") as f:
            shutil.copyfileobj(r, f)
        local.chmod(local.stat().st_mode | stat.S_IEXEC)
        return str(local)
    except Exception as ex:
        print(f"  ! could not download cloudflared: {str(ex)[:140]}")
        return None


def start_tunnel(port, token=None):
    """-> (process, https url). A named tunnel has no URL to scrape."""
    exe = cloudflared_path()
    if not exe:
        return None, None
    if token:
        proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "run", "--token", token],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=lambda: [l for l in iter(proc.stdout.readline, "")],
                         daemon=True).start()
        return proc, None

    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    url, log = None, []
    t0 = time.time()
    while time.time() - t0 < 60:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        log.append(line)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
    if not url:
        print("  ! tunnel did not report a URL:\n   " + "   ".join(log[-6:]))
        return proc, None
    # Drain the pipe forever, or cloudflared blocks once the buffer fills.
    threading.Thread(target=lambda: [l for l in iter(proc.stdout.readline, "")],
                     daemon=True).start()
    return proc, url


# ── publishing ──────────────────────────────────────────────────────
def publish(url, key, e, clear=False):
    """Write the current address to R2, where the HiSup machine looks for it.

    The key goes in the same object. That bucket is already the trust boundary
    for this pair of machines - it is where the GPU server publishes its own
    address - and both laptops hold its credentials, so putting the key there
    is what lets HEIGHT_SERVER=auto work with nothing copied by hand.
    """
    acc = (e.get("R2_ACCOUNT_ID", "").replace("https://", "")
           .replace(".r2.cloudflarestorage.com", "").replace("/", "").strip())
    if not (acc and e.get("R2_ACCESS_KEY_ID") and e.get("R2_SECRET_ACCESS_KEY")
            and e.get("R2_BUCKET_NAME")):
        if not clear:
            print("  ! R2 not configured, so the address cannot be published.")
            print("    Either copy R2_* from the HiSup laptop's .env into")
            print("    height-server/config.env, or set HEIGHT_SERVER by hand")
            print("    on the HiSup laptop every time this URL changes.")
        return False
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                          aws_access_key_id=e["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                          region_name="auto")
        body = {"url": None if clear else url.rstrip("/"),
                "api_key": None if clear else key,
                "service": "building-height",
                "updated": int(time.time()), "online": not clear}
        s3.put_object(Bucket=e["R2_BUCKET_NAME"], Key=R2_KEY,
                      Body=json.dumps(body).encode(), ContentType="application/json")
        print("  marked offline" if clear else f"  published: {url}")
        return True
    except Exception as ex:
        print(f"  ! publish failed: {str(ex)[:140]}")
        return False


# ── main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Serve the height API publicly.")
    ap.add_argument("--no-tunnel", action="store_true",
                    help="LAN only; no key is forced and nothing is published")
    ap.add_argument("--no-publish", action="store_true",
                    help="tunnel, but do not write the address to R2")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--dsm", default=None, help="DSM folder")
    ap.add_argument("--dtm", default=None, help="DTM folder")
    a = ap.parse_args()

    e = env()
    if a.dsm:
        os.environ["HS_DSM_DIR"] = e["HS_DSM_DIR"] = a.dsm
    if a.dtm:
        os.environ["HS_DTM_DIR"] = e["HS_DTM_DIR"] = a.dtm
    for k in ("HS_DSM_DIR", "HS_DTM_DIR", "HS_CACHE"):
        if e.get(k):
            os.environ.setdefault(k, e[k])
    port = a.port or int(e.get("HS_PORT", "8900"))
    os.environ["HS_PORT"] = str(port)

    key, how = (None, None)
    if not a.no_tunnel:
        # Set before the app is imported: config.py reads HS_API_KEY at import,
        # and uvicorn only imports the app inside run().
        key, how = load_or_make_key(e)
        os.environ["HS_API_KEY"] = key

    print("\n=== Building Height Server (global) ===\n")
    print("[*] checking the elevation data")
    import selfcheck
    if selfcheck.main() != 0:
        print("\n[!] refusing to serve bad data - fix the above and retry.")
        return 1

    import uvicorn
    cfg = uvicorn.Config("app.main:app", host="0.0.0.0", port=port,
                         log_level="info", access_log=False)
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        if server.started:
            break
        time.sleep(0.25)
    if not server.started:
        print("[!] the API did not start")
        return 1
    print(f"\n[+] API up on port {port}")

    proc = url = None
    if not a.no_tunnel:
        print("\n[*] opening the tunnel")
        token = e.get("CF_TUNNEL_TOKEN") or None
        proc, url = start_tunnel(port, token)
        if token:
            url = (e.get("HS_PUBLIC_URL") or "").rstrip("/") or None
            if url:
                print(f"  named tunnel -> {url}")
            else:
                print("  named tunnel started; set HS_PUBLIC_URL to your hostname "
                      "so it can be published")
        if url and not a.no_publish:
            publish(url, key, e)

    bar = "=" * 66
    print("\n" + bar)
    if url:
        print(f"  PUBLIC URL : {url}")
        print(f"  API KEY    : {key}       ({how})")
        print(bar)
        print("  On the HiSup laptop, once:")
        print("      set HEIGHT_SERVER=auto")
        print("  and it will find this address and key by itself, every restart.")
        print("  Without R2 configured, set them explicitly instead:")
        print(f"      set HEIGHT_SERVER={url}")
        print(f"      set HEIGHT_API_KEY={key}")
    elif a.no_tunnel:
        print("  LAN ONLY - no tunnel, no key required.")
        print(f"  Point the HiSup laptop at  http://<this-laptop-ip>:{port}")
    else:
        print("  The tunnel did not come up. The API is still reachable on the LAN:")
        print(f"      http://<this-laptop-ip>:{port}")
        print(f"  API KEY is set and required: {key}")
    print(bar)
    print("\nLeave this window open. Ctrl+C stops everything.\n")

    stop = threading.Event()

    def bye(*_):
        stop.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, bye)
        except Exception:
            pass
    try:
        while not stop.is_set():
            if proc is not None and proc.poll() is not None:
                print("[!] the tunnel exited; the API is still up on the LAN.")
                proc = None
            stop.wait(2)
    except KeyboardInterrupt:
        pass

    print("\n[*] shutting down")
    if url and not a.no_publish:
        publish(None, key, e, clear=True)   # so the HiSup side stops trying
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    server.should_exit = True
    time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
