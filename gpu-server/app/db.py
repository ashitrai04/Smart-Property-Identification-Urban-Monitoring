"""SQLite catalog: imagery, jobs and their results.

SQLite (WAL) needs no database server and no root, which suits a shared GPU box;
the heavy geometry lives in one GeoPackage per job (itself SQLite with an R-tree
index), so bbox queries for the map stay fast.
"""
import json
import sqlite3
import threading
import time
import uuid

from config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS imagery (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                -- drone | satellite
    district TEXT,
    captured TEXT,                     -- capture date (free text / ISO)
    source_path TEXT,
    cog_path TEXT,
    status TEXT NOT NULL,              -- ingesting | ready | failed
    error TEXT,
    bounds TEXT,                       -- JSON [w, s, e, n] in EPSG:4326
    crs TEXT,
    gsd_m REAL,
    width INTEGER, height INTEGER, bands INTEGER,
    source_bytes INTEGER, cog_bytes INTEGER,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    imagery_id TEXT NOT NULL REFERENCES imagery(id),
    task TEXT NOT NULL,                -- segment | fusion | pothole
    params TEXT,                       -- JSON
    status TEXT NOT NULL,              -- queued | running | done | failed | cancelled
    progress REAL DEFAULT 0,
    message TEXT,
    result_dir TEXT,
    layers TEXT,                       -- JSON {layer: feature_count}
    stats TEXT,                        -- JSON summary
    device TEXT,
    created REAL NOT NULL, started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS jobs_imagery ON jobs(imagery_id);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    conn().executescript(SCHEMA)
    # A restart interrupts running jobs; put them back in the queue.
    conn().execute("UPDATE jobs SET status='queued', message='requeued after restart' WHERE status='running'")
    conn().commit()


def _row(r, json_cols=("bounds", "params", "layers", "stats")):
    if r is None:
        return None
    d = dict(r)
    for k in json_cols:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ── imagery ─────────────────────────────────────────────────────
def add_imagery(**f):
    f.setdefault("id", new_id("img"))
    f.setdefault("created", time.time())
    if isinstance(f.get("bounds"), (list, tuple)):
        f["bounds"] = json.dumps(f["bounds"])
    cols = ",".join(f)
    conn().execute(f"INSERT INTO imagery ({cols}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
    conn().commit()
    return f["id"]


def update_imagery(img_id, **f):
    if isinstance(f.get("bounds"), (list, tuple)):
        f["bounds"] = json.dumps(f["bounds"])
    sets = ",".join(f"{k}=?" for k in f)
    conn().execute(f"UPDATE imagery SET {sets} WHERE id=?", (*f.values(), img_id))
    conn().commit()


def get_imagery(img_id):
    return _row(conn().execute("SELECT * FROM imagery WHERE id=?", (img_id,)).fetchone())


def list_imagery():
    return [_row(r) for r in conn().execute("SELECT * FROM imagery ORDER BY created DESC")]


def delete_imagery(img_id):
    conn().execute("DELETE FROM jobs WHERE imagery_id=?", (img_id,))
    conn().execute("DELETE FROM imagery WHERE id=?", (img_id,))
    conn().commit()


# ── jobs ────────────────────────────────────────────────────────
def add_job(imagery_id, task, params):
    jid = new_id("job")
    conn().execute(
        "INSERT INTO jobs (id, imagery_id, task, params, status, created) VALUES (?,?,?,?, 'queued', ?)",
        (jid, imagery_id, task, json.dumps(params or {}), time.time()))
    conn().commit()
    return jid


def update_job(jid, **f):
    for k in ("layers", "stats", "params"):
        if isinstance(f.get(k), (dict, list)):
            f[k] = json.dumps(f[k])
    sets = ",".join(f"{k}=?" for k in f)
    conn().execute(f"UPDATE jobs SET {sets} WHERE id=?", (*f.values(), jid))
    conn().commit()


def get_job(jid):
    return _row(conn().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def list_jobs(imagery_id=None):
    if imagery_id:
        rows = conn().execute("SELECT * FROM jobs WHERE imagery_id=? ORDER BY created DESC", (imagery_id,))
    else:
        rows = conn().execute("SELECT * FROM jobs ORDER BY created DESC")
    return [_row(r) for r in rows]


def next_queued():
    return _row(conn().execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone())
