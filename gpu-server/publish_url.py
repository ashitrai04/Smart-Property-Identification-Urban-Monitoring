#!/usr/bin/env python3
"""Publish the GPU server's current public URL so the webapp finds it on its own.

The free Cloudflare quick tunnel gets a new random address every time it starts.
tunnel.sh calls this with the new address; it is written to the shared R2 bucket
(runtime/gpu_server.json), and the data backend serves it at /api/gpu-server,
which the webapp reads — so a restart needs no .env / Vercel change.

  .venv/bin/python publish_url.py https://xxxx.trycloudflare.com
  .venv/bin/python publish_url.py --clear          # mark the server offline (stop.sh)
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY = "runtime/gpu_server.json"


def env():
    out = dict(os.environ)
    p = HERE / "config.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out.setdefault(k.strip(), v.split("#", 1)[0].strip())
    return out


def main():
    e = env()
    acc = e.get("R2_ACCOUNT_ID", "").replace("https://", "").replace(".r2.cloudflarestorage.com", "").replace("/", "").strip()
    if not (acc and e.get("R2_ACCESS_KEY_ID") and e.get("R2_SECRET_ACCESS_KEY") and e.get("R2_BUCKET_NAME")):
        print("publish_url: R2 not configured in config.env — skipping (webapp will use VITE_GPU_API)")
        return
    import boto3
    s3 = boto3.client("s3", endpoint_url=f"https://{acc}.r2.cloudflarestorage.com",
                      aws_access_key_id=e["R2_ACCESS_KEY_ID"], aws_secret_access_key=e["R2_SECRET_ACCESS_KEY"],
                      region_name="auto")
    clear = len(sys.argv) > 1 and sys.argv[1] == "--clear"
    url = None if clear else (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "")
    body = {"url": url, "updated": int(time.time()), "online": not clear}
    s3.put_object(Bucket=e["R2_BUCKET_NAME"], Key=KEY, Body=json.dumps(body).encode(), ContentType="application/json")
    print("publish_url:", "cleared" if clear else f"published {url}")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:   # never block the tunnel on this
        print("publish_url: failed:", ex)
