#!/usr/bin/env python3
"""Command-line control for the GPU server (talks to the local API).

  .venv/bin/python spctl.py status
  .venv/bin/python spctl.py raw                                   # files waiting in data/raw
  .venv/bin/python spctl.py ingest data/raw/vja_drone.tif --name "Vijayawada drone Sep-26" --kind drone --district vijayawada
  .venv/bin/python spctl.py imagery                               # catalog
  .venv/bin/python spctl.py job img_xxx pothole --conf 0.25
  .venv/bin/python spctl.py job img_xxx segment --gsd 0.3         # optional coarser analysis grid (m)
  .venv/bin/python spctl.py job img_xxx fusion
  .venv/bin/python spctl.py jobs                                  # progress of every job
  .venv/bin/python spctl.py cancel job_xxx
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent


def _env():
    env = {}
    p = HERE / "config.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = _env()
BASE = f"http://127.0.0.1:{ENV.get('PORT', '8800')}"
HDR = {"X-API-Key": ENV.get("SP_API_KEY", "")}


def call(method, path, **kw):
    r = requests.request(method, BASE + path, headers=HDR, timeout=60, **kw)
    if r.status_code >= 400:
        sys.exit(f"{r.status_code}: {r.text[:400]}")
    return r.json()


def show(rows, cols):
    if not rows:
        print("(none)")
        return
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("raw")
    sub.add_parser("imagery")
    sub.add_parser("jobs")
    p = sub.add_parser("ingest"); p.add_argument("path"); p.add_argument("--name"); p.add_argument("--kind", default="drone", choices=["drone", "satellite"])
    p.add_argument("--district"); p.add_argument("--captured"); p.add_argument("--delete-source", action="store_true")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("job"); p.add_argument("imagery_id"); p.add_argument("task", choices=["segment", "fusion", "pothole"])
    p.add_argument("--conf", type=float); p.add_argument("--gsd", type=float, help="analysis grid in metres (segment/fusion)")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("cancel"); p.add_argument("job_id")
    a = ap.parse_args()

    if a.cmd == "status":
        print(json.dumps(call("GET", "/api/health"), indent=2))
    elif a.cmd == "raw":
        show(call("GET", "/imagery/raw/files"), ["name", "gb", "path"])
    elif a.cmd == "imagery":
        rows = call("GET", "/imagery")
        for r in rows:
            r["src_gb"] = round((r.get("source_bytes") or 0) / 2**30, 2)
            r["cog_gb"] = round((r.get("cog_bytes") or 0) / 2**30, 2)
        show(rows, ["id", "name", "kind", "district", "status", "gsd_m", "src_gb", "cog_gb", "error"])
    elif a.cmd == "jobs":
        rows = call("GET", "/jobs")
        for r in rows:
            r["pct"] = f"{100 * (r.get('progress') or 0):.0f}%"
            r["result"] = json.dumps(r.get("layers") or {})
        show(rows, ["id", "imagery_id", "task", "status", "pct", "result", "message"])
    elif a.cmd == "ingest":
        path = str(Path(a.path).resolve())
        r = call("POST", "/imagery/ingest", json={"path": path, "name": a.name, "kind": a.kind, "district": a.district,
                                                  "captured": a.captured, "delete_source": a.delete_source})
        print("ingesting:", r["id"])
        if a.wait:
            while True:
                s = call("GET", f"/imagery/{r['id']}")
                if s["status"] != "ingesting":
                    print(s["status"], s.get("error") or "")
                    break
                time.sleep(10)
    elif a.cmd == "job":
        params = {}
        if a.conf is not None:
            params["conf"] = a.conf
        if a.gsd is not None:
            params["target_gsd_m"] = a.gsd
        r = call("POST", "/jobs", json={"imagery_id": a.imagery_id, "task": a.task, "params": params})
        print("queued:", r["id"])
        if a.wait:
            while True:
                s = call("GET", f"/jobs/{r['id']}")
                print(f"\r{s['status']} {100 * (s.get('progress') or 0):5.1f}%", end="", flush=True)
                if s["status"] in ("done", "failed", "cancelled"):
                    print("\n", json.dumps({k: s.get(k) for k in ("layers", "stats", "message")}, indent=2))
                    break
                time.sleep(5)
    elif a.cmd == "cancel":
        print(call("POST", f"/jobs/{a.job_id}/cancel")["status"])


if __name__ == "__main__":
    main()
