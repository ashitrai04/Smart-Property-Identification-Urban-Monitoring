"""Turn the local server on: serve the data folder, expose it over HTTPS, publish
the address so the deployed webapp finds it by itself.

  python run.py                 serve + Cloudflare tunnel + publish  (normal use)
  python run.py --no-tunnel     serve on localhost only
  python run.py --port 8777     different port

The webapp asks the backend for /api/gpu-server, which reads runtime/gpu_server.json
from R2 — the same key gpu-server/publish_url.py writes. So publishing the tunnel
address here is all it takes: no Vercel redeploy, no .env change.
Ctrl+C marks the server offline again.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
R2_KEY = "runtime/gpu_server.json"
CF_PATHS = [r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe"]


def env():
    """Environment plus the repo .env (where the R2 credentials already live)."""
    out = dict(os.environ)
    for name in (".env.local", ".env"):
        p = ROOT / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.split(" #", 1)[0].strip())
    return out


def publish(url, e, clear=False):
    """Write the current public address to R2 so the webapp can discover it."""
    acc = (e.get("R2_ACCOUNT_ID", "").replace("https://", "")
           .replace(".r2.cloudflarestorage.com", "").replace("/", "").strip())
    if not (acc and e.get("R2_ACCESS_KEY_ID") and e.get("R2_SECRET_ACCESS_KEY") and e.get("R2_BUCKET_NAME")):
        print("  ! R2 not configured — set VITE_GPU_API in Vercel instead")
        return False
    try:
        import boto3
        s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                          aws_access_key_id=e["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"], region_name="auto")
        body = {"url": None if clear else url.rstrip("/"),
                "updated": int(time.time()), "online": not clear}
        s3.put_object(Bucket=e["R2_BUCKET_NAME"], Key=R2_KEY,
                      Body=json.dumps(body).encode(), ContentType="application/json")
        print("  offline — webapp will stop using it" if clear else f"  published: {url}")
        return True
    except Exception as ex:
        print(f"  ! publish failed: {str(ex)[:120]}")
        return False


def start_tunnel(port):
    """Cloudflare quick tunnel -> (process, https url). Free, no account, no open ports."""
    exe = shutil.which("cloudflared") or next((p for p in CF_PATHS if os.path.isfile(p)), None)
    if not exe:
        print("  ! cloudflared not found — install it or use --no-tunnel")
        return None, None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    url, log = None, []
    t0 = time.time()
    while time.time() - t0 < 45:
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
        print("  ! tunnel did not report a URL:\n   " + "   ".join(log[-5:]))
        return proc, None
    threading.Thread(target=lambda: [l for l in iter(proc.stdout.readline, "")],
                     daemon=True).start()        # drain, so the pipe never blocks
    return proc, url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8777)))
    ap.add_argument("--data", default=os.environ.get("GEO_DATA_DIR", r"D:\GEO_SERVE"))
    ap.add_argument("--no-tunnel", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    a = ap.parse_args()

    # Without this, printing to a redirected log is block-buffered and the
    # address never appears until the process ends.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    data = Path(a.data)
    data.mkdir(parents=True, exist_ok=True)
    os.environ["GEO_DATA_DIR"] = str(data)
    e = env()

    sys.path.insert(0, str(HERE))
    import uvicorn
    from server import app, refresh

    cat = refresh(force=True)
    print(f"\n  data folder : {data}")
    print(f"  imagery     : {len(cat['imagery'])}")
    print(f"  result layers: {sum(len(j['layers']) for j in cat['jobs'])}")
    for u in cat["unmapped"]:
        print("   ! " + u)
    if not cat["imagery"]:
        print(f"\n  Folder is empty — copy or link your .tif / .gpkg files into {data}")
        print("  (they are picked up automatically, no restart needed)")

    cfg = uvicorn.Config(app, host="0.0.0.0", port=a.port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            break
        time.sleep(0.1)
    print(f"\n  local       : http://localhost:{a.port}")
    print(f"  catalog     : http://localhost:{a.port}/catalog")

    proc, url = (None, None) if a.no_tunnel else start_tunnel(a.port)
    if url:
        print(f"  public      : {url}")
        publish(url, e)
        print("\n  The deployed webapp will pick this up within a minute.")
    elif a.no_tunnel and not a.no_publish:
        # Fastest path: publish localhost itself. The browser then talks straight
        # to this machine with no tunnel hop — a tile is milliseconds instead of
        # a round trip through Cloudflare. Works because http://localhost counts
        # as a secure origin, so an https:// page is allowed to call it.
        # Only works while viewing on THIS machine; use the tunnel to share.
        local = f"http://localhost:{a.port}"
        print(f"  publishing  : {local}  (direct, no tunnel — fastest)")
        publish(local, e)
        print("\n  The deployed webapp will use it within a minute,")
        print("  as long as you browse from this machine.")

    print("\n  Running. Press Ctrl+C to stop.\n")
    stop = threading.Event()

    def bye(*_):
        stop.set()
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    try:
        while not stop.is_set():
            stop.wait(1)
    finally:
        print("\n  stopping…")
        if url or (a.no_tunnel and not a.no_publish):
            publish(None, e, clear=True)
        if proc:
            proc.terminate()
        srv.should_exit = True
        time.sleep(0.6)
        print("  stopped.")


if __name__ == "__main__":
    main()
