"""Talk to the Urbanly model server.

Deliberately free of any QGIS import so it can be exercised from a plain
Python prompt - the networking is the part most likely to break in the field,
and debugging it through the QGIS Python console is miserable.

urllib only, no requests: QGIS ships its own Python and the set of third-party
packages present varies by install and platform. The one thing a plugin cannot
do is ask the user to pip install into it.
"""
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = os.environ.get("URBANLY_SERVER", "auto")
LOCAL_SERVER = "http://127.0.0.1:8777"

# Where the server publishes its own current address. The free Cloudflare
# tunnel gets a new random hostname every restart, so a URL written down today
# is wrong tomorrow; the server writes its address here and the webapp reads
# it. The plugin does the same, which is what lets it work from a laptop that
# has never been told anything.
DISCOVERY = os.environ.get("URBANLY_DISCOVERY",
                           "https://asashit-smart-property-backend.hf.space/api/gpu-server")
TIMEOUT = 900


class ServerError(RuntimeError):
    pass


def _url(server, path):
    return server.rstrip("/") + path


def _read(resp):
    body = resp.read()
    if resp.headers.get("content-encoding") == "gzip":
        import gzip
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def _request(server, path, data=None, method=None, timeout=TIMEOUT, headers=None):
    req = urllib.request.Request(_url(server, path), data=data, method=method)
    # Ask for no compression: urllib does not transparently decode gzip, and a
    # plugin has no business re-implementing content negotiation.
    req.add_header("Accept-Encoding", "identity")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _read(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        raise ServerError(f"{e.code} from {path}: {detail}") from None
    except urllib.error.URLError as e:
        raise ServerError(f"cannot reach {server}: {e.reason}") from None


def _multipart(fields, files):
    """Encode a form the way the server's /hisup/predict expects."""
    boundary = "----urbanly" + os.urandom(8).hex()
    out = []
    for k, v in fields.items():
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, path in files.items():
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            blob = fh.read()
        out.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ── the two ways to run a model ──────────────────────────────────
def discover(timeout=8):
    """The server's current public address, or None.

    Never raises: discovery failing is normal (no internet, backend asleep)
    and the caller still has the local address to fall back on.
    """
    try:
        req = urllib.request.Request(DISCOVERY)
        req.add_header("Accept-Encoding", "identity")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        url = (d or {}).get("url") or ""
        return url.rstrip("/") if d.get("online") and url else None
    except Exception:
        return None


def alive(base, timeout=2.5):
    """Does this address answer right now? Never raises."""
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/health")
        req.add_header("Accept-Encoding", "identity")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def resolve(server=None, timeout=8):
    """Turn whatever the user typed into a URL to call.

    "auto" (the default) works on the machine running the server and on a
    laptop across the country, without being configured either way.

    Order matters, and it is not the obvious one:

    1. Localhost first. When QGIS is on the same machine as the server this is
       both faster - no round trip through Cloudflare - and immune to the
       tunnel churn below.
    2. Then the published address, but only if it actually answers.

    The published address is checked rather than trusted because the free
    Cloudflare tunnel takes a new hostname on every restart, and the old one
    stops resolving at all. A stale entry used to be returned as-is and the
    run died on "getaddrinfo failed" - with a working server sitting on
    localhost the whole time.
    """
    server = (server or "").strip()
    if server and server.lower() != "auto":
        return server.rstrip("/")
    if alive(LOCAL_SERVER):
        return LOCAL_SERVER
    found = discover(timeout=timeout)
    if found and alive(found, timeout=min(timeout, 6)):
        return found
    # Nothing answered. Hand back localhost so the error names something the
    # user can act on, rather than a hostname that no longer exists.
    return LOCAL_SERVER


def health(server=DEFAULT_SERVER, timeout=10):
    return _request(server, "/api/health", timeout=timeout)


def tasks(server=DEFAULT_SERVER, timeout=15):
    return _request(server, "/area/tasks", timeout=timeout).get("tasks", [])


def predict_file(path, server=DEFAULT_SERVER, gsd=None, timeout=TIMEOUT):
    """Delineate one image. A GeoTIFF carries its own resolution and location,
    so the result comes back in EPSG:4326 under `geo` and needs no help."""
    fields = {}
    if gsd:
        fields["gsd"] = str(gsd)
    body, ctype = _multipart(fields, {"file": path})
    return _request(server, "/hisup/predict", data=body, method="POST",
                    timeout=timeout, headers={"Content-Type": ctype})


def start_area(aoi_geojson, task="hisup", server=DEFAULT_SERVER, **params):
    payload = {"task": task, "aoi": aoi_geojson}
    payload.update({k: v for k, v in params.items() if v not in (None, "", [])})
    return _request(server, "/area", data=json.dumps(payload).encode(), method="POST",
                    timeout=60, headers={"Content-Type": "application/json"})


def area_status(jid, server=DEFAULT_SERVER):
    return _request(server, f"/area/{jid}", timeout=30)


def area_live(jid, since=0, server=DEFAULT_SERVER):
    """Buildings found so far, from `since` onwards.

    The server appends each window's results as it finishes, so this is what
    lets the panel draw the work as it happens rather than at the end. Pass
    back the `next` it returns to get only what is new.
    """
    return _request(server, f"/area/{jid}/live?since={int(since)}", timeout=60)


def area_cancel(jid, server=DEFAULT_SERVER):
    return _request(server, f"/area/{jid}/cancel", data=b"", method="POST", timeout=30)


def area_result(jid, server=DEFAULT_SERVER):
    return _request(server, f"/area/{jid}/result.geojson", timeout=TIMEOUT)


def run_area(aoi_geojson, task="hisup", server=DEFAULT_SERVER,
             on_progress=None, is_cancelled=None, poll=2.0, **params):
    """Start an area job and wait, reporting progress.

    `on_progress(percent, message)` and `is_cancelled()` map onto QGIS's
    QgsProcessingFeedback without this module needing to know it exists.
    """
    job = start_area(aoi_geojson, task=task, server=server, **params)
    jid = job["id"]
    last = -1
    while True:
        if is_cancelled and is_cancelled():
            area_cancel(jid, server=server)
            raise ServerError("cancelled")
        st = area_status(jid, server=server)
        pct = st.get("percent") or 0
        if on_progress and pct != last:
            eta = st.get("eta_s")
            on_progress(pct, f"{pct:.0f}%" + (f", about {int(eta)}s left" if eta else ""))
            last = pct
        status = st.get("status")
        if status == "done":
            return area_result(jid, server=server), st
        if status in ("failed", "cancelled"):
            raise ServerError(st.get("message") or f"job {status}")
        time.sleep(poll)
