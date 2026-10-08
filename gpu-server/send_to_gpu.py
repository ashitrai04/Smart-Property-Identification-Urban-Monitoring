#!/usr/bin/env python3
"""
Send a very large drone / satellite image (tens of GB) straight to the Smart Property
GPU server - no Cloudflare R2, no size limit except the server's disk.

    python send_to_gpu.py "D:\\Drone\\ongole_full.tif" --key YOUR_SP_API_KEY --name "Ongole full drone" --district ongole

* Pure Python standard library - nothing to pip install (Python 3.8+).
* Sends 90 MB pieces (the free Cloudflare tunnel allows 100 MB per request).
* Resumable: stop it, lose Wi-Fi, or let the tunnel restart - run the same command
  again and it continues from the last byte the server has.
* Finds the server's current address by itself (it changes when the tunnel restarts).
* When the last piece arrives, the server registers the image for the map.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BACKEND = "https://asashit-smart-property-backend.hf.space"   # tells us where the GPU server is now
PIECE = 90 * 1024 * 1024


def gpu_url(fixed=None):
    if fixed:
        return fixed.rstrip("/")
    with urllib.request.urlopen(f"{BACKEND}/api/gpu-server", timeout=30) as r:
        d = json.load(r)
    if not d.get("online") or not d.get("url"):
        raise RuntimeError("GPU server is offline (run start.sh on the server)")
    return d["url"].rstrip("/")


def call(method, url, key, data=None, timeout=600, ctype="application/octet-stream"):
    req = urllib.request.Request(url, data=data, method=method, headers={"X-API-Key": key})
    if data is not None:
        req.add_header("Content-Type", ctype)
        req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"detail": body[:300]}


def fmt(n):
    return f"{n / 2**30:.2f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--key", required=True, help="SP_API_KEY from the server's config.env")
    ap.add_argument("--name", help="display name on the map")
    ap.add_argument("--kind", default="drone", choices=["drone", "satellite"])
    ap.add_argument("--district", default="ongole")
    ap.add_argument("--captured", help="capture date / year shown on the map, e.g. 2017")
    ap.add_argument("--server", help="GPU server URL (default: discover automatically)")
    ap.add_argument("--as", dest="remote", help="file name on the server (default: same as local)")
    ap.add_argument("--upload-only", action="store_true", help="only send the file; import it later on the server")
    a = ap.parse_args()

    path = a.file
    total = os.path.getsize(path)
    remote = a.remote or os.path.basename(path)
    q = urllib.parse.quote(remote)
    print(f"Sending {path}  ({fmt(total)})  as  {remote}")

    base, done, t0, sent_this_run, fails = None, None, time.time(), 0, 0
    with open(path, "rb") as fh:
        while True:
            try:
                if base is None:
                    base = gpu_url(a.server)
                    print("GPU server:", base)
                if done is None:
                    code, st = call("GET", f"{base}/uploads/{q}", a.key, timeout=60)
                    if code == 401:
                        sys.exit("Wrong --key (use SP_API_KEY from the server's config.env)")
                    if code != 200:
                        raise RuntimeError(f"status {code}: {st}")
                    done = st["bytes"]
                    if st.get("registered"):
                        print(f"Already uploaded and registered on the server ({st['registered']}). Nothing to do.")
                        return
                    if st.get("complete"):
                        print("Already complete on the server.")
                        break
                    if done:
                        print(f"Resuming at {fmt(done)} ({100 * done / total:.1f}%)")
                fh.seek(done)
                data = fh.read(min(PIECE, total - done))
                code, st = call("PUT", f"{base}/uploads/{q}?offset={done}&total={total}", a.key, data)
                if code == 409:                       # out of step: server tells us where to resume
                    done = st.get("resume_from", st.get("bytes", 0))
                    continue
                if code != 200:
                    raise RuntimeError(f"upload status {code}: {st}")
                sent_this_run += len(data)
                done = st["bytes"]
                fails = 0
                el = time.time() - t0
                speed = sent_this_run / max(el, 1)
                eta = (total - done) / speed if speed else 0
                print(f"  {fmt(done):>10} / {fmt(total)}  {100 * done / total:5.1f}%   "
                      f"{speed / 2**20:5.1f} MB/s   ~{eta / 3600:4.1f} h left", flush=True)
                if st.get("complete"):
                    print("Upload complete.")
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError, OSError) as e:
                fails += 1
                wait = min(300, 10 * fails)
                print(f"  connection problem ({e}); retrying in {wait}s - progress is kept", flush=True)
                time.sleep(wait)
                base, done = None, None              # rediscover the server (its URL may have changed) and resume

    if a.upload_only:
        print("Done. The file is on the GPU server - import it there with ingest_now.py.")
        return
    code, r = call("POST", f"{gpu_url(a.server)}/uploads/{q}/ingest", a.key,
                   data=json.dumps({"name": a.name or os.path.splitext(remote)[0], "kind": a.kind,
                                    "district": a.district, "captured": a.captured}).encode(), timeout=120,
                   ctype="application/json")
    print("Registering on the server:", code, r)
    print("Done. It appears in the webapp under Mapping > Server imagery once registered.")


if __name__ == "__main__":
    main()
