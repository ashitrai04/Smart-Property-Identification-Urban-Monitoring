"""Ask the survey: a question in English, an answer computed from the data.

Same split as sentinel-command-center/pipeline/ask: the language model turns the
sentence into a FIXED JSON PLAN and nothing else. It never sees the road table,
never writes SQL, and never produces a number. Every figure in the answer is
computed here from the knowledge base, so the model cannot invent a pothole
count — the worst it can do is pick the wrong sort order, which is visible.

Qwen2.5-VL 3B through Ollama, locally. If Ollama is down a rule parser takes
over, so the box still answers the common questions.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("ASK_MODEL", "qwen2.5vl:3b")
SEED = 7          # Ollama seeds randomly per request; same question, same plan
NUM_CTX = 8192

METRICS = ["severity", "potholes", "density", "length", "width", "area"]
GRADES = ["good", "fair", "poor", "bad", "critical"]
INTENTS = ["rank", "detail", "count", "list", "summary", "unknown"]

SYSTEM = """You turn a question about a city road survey into a JSON query plan.
Reply with ONLY the JSON object, no prose, no markdown.

{"intent":"rank|detail|count|list|summary|unknown",
 "metric":"severity|potholes|density|length|width|area|none",
 "order":"desc|asc",
 "limit":1,
 "road":"",
 "grade":"",
 "min_confidence":0}

intent:
  rank    - "worst", "most damaged", "best", "top 5", "least damaged"
  detail  - facts about ONE road the user names
  count   - "how many potholes", "how many roads"
  list    - "show all roads that are critical"
  summary - "overview", "summarise the survey", "how bad is it overall"
metric: what to sort or count by. density = potholes per km. Use "severity" for
        vague damage words like worst/bad/condition.
order:  desc for worst/most, asc for best/least.
limit:  how many roads to return, 1 for "the worst road", else 1-20.
road:   the road name if the user names one, else "".
grade:  one of good, fair, poor, bad, critical if the user names a condition.
min_confidence: only if the user asks for a confidence threshold, else 0."""

_kb = {"path": None, "mtime": 0, "data": None, "geo": None}


# ── knowledge base ───────────────────────────────────────────────
def kb(data_dir):
    """Road table + geometry, reloaded when the build script rewrites it."""
    p = Path(data_dir) / "_kb" / "guntur_roads.json"
    if not p.is_file():
        return None
    mt = p.stat().st_mtime
    if _kb["data"] is None or _kb["mtime"] != mt:
        with open(p, encoding="utf-8") as f:
            _kb["data"] = json.load(f)
        _kb["mtime"] = mt
        _kb["geo"] = None
        print(f"[ask] loaded {len(_kb['data']['roads'])} roads", flush=True)
    return _kb["data"]


def geometry_for(data_dir, names):
    """GeoJSON for the named roads: map pins and the story-mode path."""
    import geopandas as gpd
    g = _kb.get("geo")
    if g is None:
        p = Path(data_dir) / "guntur_roads_kb.gpkg"
        if not p.is_file():
            return {}
        g = _kb["geo"] = gpd.read_file(p, layer="road_kb").to_crs(4326)
    sub = g[g["road"].isin(list(names))]
    out = {}
    for _, r in sub.iterrows():
        geom = r.geometry
        if geom is None:
            continue
        coords = []
        if geom.geom_type == "LineString":
            coords = [[round(x, 6), round(y, 6)] for x, y in geom.coords]
        elif geom.geom_type == "MultiLineString":
            best = max(geom.geoms, key=lambda s: s.length)
            coords = [[round(x, 6), round(y, 6)] for x, y in best.coords]
        if coords:
            out[r["road"]] = coords
    return out


# ── prompt -> plan ───────────────────────────────────────────────
def ollama_up():
    try:
        urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3)
        return True
    except Exception:
        return False


def _chat(prompt, timeout=120):
    body = json.dumps({
        "model": MODEL, "format": "json", "stream": False,
        "options": {"temperature": 0, "seed": SEED, "num_ctx": NUM_CTX, "num_predict": 300},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"content-type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return (d.get("message") or {}).get("content") or ""


WORST = re.compile(r"\b(worst|most|highest|damag|bad|critical|poor|danger|severe)\w*", re.I)
BEST = re.compile(r"\b(best|least|fewest|good|cleanest|repair\w*|smooth)\w*", re.I)
COUNTW = re.compile(r"\b(how many|count|number of|total)\b", re.I)
LISTW = re.compile(r"\b(list|show all|all roads|which roads)\b", re.I)
SUMW = re.compile(r"\b(summar|overview|overall|how bad|report)\w*", re.I)
NUM = re.compile(r"\b(?:top|first|best|worst)\s+(\d{1,2})\b|\b(\d{1,2})\s+(?:roads|worst|best)\b", re.I)
CONF = re.compile(r"(?:above|over|>|confidence of)\s*(0?\.\d+|\d{1,3}\s*%)", re.I)
KM = re.compile(r"\b(km|kilomet|length|long)\w*", re.I)
DENS = re.compile(r"\bper\s*km\b|\bdensity\b", re.I)
# "widest" / "longest" are superlatives the model kept reading as ascending,
# so "what is the widest road" came back with the narrowest one.
MAXW = re.compile(r"\b(widest|longest|largest|biggest|highest|most|maximum|max|worst)\b", re.I)
MINW = re.compile(r"\b(narrowest|shortest|smallest|lowest|fewest|least|minimum|min|best)\b", re.I)


def rules(q):
    """Fallback plan, and the sanity check the model's plan is repaired against."""
    p = {"intent": "unknown", "metric": "severity", "order": "desc", "limit": 1,
         "road": "", "grade": "", "min_confidence": 0.0}
    if SUMW.search(q):
        p["intent"] = "summary"
    elif COUNTW.search(q):
        p["intent"] = "count"
        p["metric"] = "potholes"
    elif LISTW.search(q):
        p["intent"] = "list"
    elif WORST.search(q) or BEST.search(q):
        p["intent"] = "rank"
    if BEST.search(q) and not WORST.search(q):
        p["order"] = "asc"
    for g in GRADES:
        if re.search(rf"\b{g}\b", q, re.I):
            p["grade"] = g
    m = NUM.search(q)
    if m:
        p["limit"] = int(m.group(1) or m.group(2))
    elif p["intent"] in ("rank", "list"):
        p["limit"] = 5 if LISTW.search(q) else 1
    if DENS.search(q):
        p["metric"] = "density"
    elif KM.search(q):
        p["metric"] = "length"
    c = CONF.search(q)
    if c:
        v = c.group(1).strip()
        p["min_confidence"] = float(v.rstrip("% ").strip()) / (100 if "%" in v else 1)
    return p


def clean(raw, q):
    """Repair the model's plan. A 3B model emits out-of-vocabulary values and
    'helpful' defaults — min_confidence 1 came back for a question that never
    mentioned confidence, which would have filtered out every detection."""
    r = rules(q)
    p = dict(r)
    if isinstance(raw, dict):
        v = str(raw.get("intent", "")).lower().strip()
        if v in INTENTS and v != "unknown":
            p["intent"] = v
        v = str(raw.get("metric", "")).lower().strip()
        if v in METRICS:
            p["metric"] = v
        v = str(raw.get("order", "")).lower().strip()
        if v in ("desc", "asc"):
            p["order"] = v
        try:
            n = int(raw.get("limit", 0))
            if 1 <= n <= 20:
                p["limit"] = n
        except (TypeError, ValueError):
            pass
        v = str(raw.get("road", "")).strip()
        if v and v.lower() not in ("none", "null", "n/a", ""):
            p["road"] = v
        v = str(raw.get("grade", "")).lower().strip()
        if v in GRADES:
            p["grade"] = v
        try:
            c = float(raw.get("min_confidence", 0))
            # only trust a threshold the question actually asked for
            if 0 < c < 1 and CONF.search(q):
                p["min_confidence"] = c
        except (TypeError, ValueError):
            pass
    # The model reaches for "summary" far too readily — it answered "how long is
    # Dharma Main Road" and "which roads are in good condition" with a survey
    # overview. Only honour summary when the question actually asks for one.
    # Same with "count": it answered "top 3 roads with highest pothole density"
    # with a survey total. A count needs the question to actually ask for one.
    if p["intent"] == "count" and not COUNTW.search(q):
        p["intent"] = "detail" if p["road"] else "rank"
    # an explicit "density" / "per km" in the question beats whatever the model
    # guessed the metric was
    if DENS.search(q):
        p["metric"] = "density"
    if p["intent"] == "summary" and not SUMW.search(q):
        p["intent"] = r["intent"] if r["intent"] != "unknown" else "rank"
    # a named road always means detail, whatever the model said
    if p["road"] and p["intent"] in ("unknown", "rank", "list", "summary"):
        p["intent"] = "detail"
    if p["grade"] and p["intent"] in ("unknown", "summary"):
        p["intent"] = "list"
    if p["intent"] == "unknown":
        p["intent"] = "rank"
    if BEST.search(q) and not WORST.search(q):
        p["order"] = "asc"
    if MAXW.search(q) and not MINW.search(q):
        p["order"] = "desc"
    elif MINW.search(q) and not MAXW.search(q):
        p["order"] = "asc"
    return p


def plan_for(q):
    t0 = time.time()
    src = "rules"
    raw = None
    if ollama_up():
        try:
            txt = _chat(q)
            if txt.strip():
                raw = json.loads(txt)
                src = "qwen"
        except Exception as e:
            print(f"[ask] model: {str(e)[:70]}", flush=True)
    p = clean(raw if isinstance(raw, dict) else None, q)
    p["_source"] = src
    p["_ms"] = int((time.time() - t0) * 1000)
    return p


# ── plan -> answer ───────────────────────────────────────────────
def _match(roads, name):
    """Find a road by a name the user typed, tolerating 'peta'/'pet' and case."""
    n = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not n:
        return []
    exact = [r for r in roads if re.sub(r"[^a-z0-9]+", "", r["road"].lower()) == n]
    if exact:
        return exact
    part = [r for r in roads if n in re.sub(r"[^a-z0-9]+", "", r["road"].lower())]
    if part:
        return part
    loose = n.replace("peta", "pet")
    return [r for r in roads
            if loose in re.sub(r"[^a-z0-9]+", "", r["road"].lower()).replace("peta", "pet")]


SORT_KEY = {"severity": "severity", "potholes": "potholes", "density": "per_km",
            "length": "length_m", "width": "width_m", "area": "area_m2"}


def run(q, data_dir):
    data = kb(data_dir)
    if not data:
        return {"ok": False, "answer": "The road knowledge base has not been built yet.",
                "roads": [], "plan": None}
    roads = data["roads"]
    p = plan_for(q)
    tot = data["totals"]
    key = SORT_KEY.get(p["metric"], "severity")
    hits, answer = [], ""

    if p["intent"] == "summary":
        g = {}
        for r in roads:
            g[r["grade"]] = g.get(r["grade"], 0) + 1
        worst = sorted([r for r in roads if r["rankable"]],
                       key=lambda r: -r["severity"])[:3]
        answer = (f"The Guntur drone survey covers {tot['length_km']:.1f} km of road across "
                  f"{tot['roads']} roads, {tot['named']} of them named. "
                  f"{tot['potholes']} potholes are attached to a road. "
                  f"Condition: {g.get('critical',0)} critical, {g.get('bad',0)} bad, "
                  f"{g.get('poor',0)} poor, {g.get('fair',0)} fair, {g.get('good',0)} good. "
                  f"The worst is {worst[0]['road']} at severity {worst[0]['severity']:.0f}."
                  if worst else "")
        hits = worst

    elif p["intent"] == "detail" and p["road"]:
        m = _match(roads, p["road"])
        if not m:
            answer = (f'No road matching "{p["road"]}" is in the survey. '
                      f"It covers {tot['named']} named roads in central Guntur.")
        else:
            r = sorted(m, key=lambda r: -r["length_m"])[0]
            hits = [r]
            rank = (f"ranked #{r['rank']} worst of {sum(1 for x in roads if x['rankable'])} "
                    f"rankable roads" if r["rankable"] else "too short to rank")
            answer = (f"{r['road']} is {r['length_m']:.0f} m long "
                      f"({r['length_km']:.2f} km), about {r['width_m']:.1f} m wide, "
                      f"{r['area_m2']:,.0f} m² of surface across {r['segments']} segment(s). "
                      f"It has {r['potholes']} potholes ({r['per_km']:.1f} per km) and "
                      f"{r['defects']} defects in total, condition {r['grade'].upper()} "
                      f"(severity {r['severity']:.0f}/100), {rank}.")
            if r["defects"]:
                answer += (f" Largest defect {r['size_max']:.1f} m, "
                           f"peak detector confidence {r['conf_max']:.2f}.")

    elif p["intent"] == "count":
        if p["road"]:
            m = _match(roads, p["road"])
            if m:
                r = sorted(m, key=lambda r: -r["length_m"])[0]
                hits = [r]
                answer = (f"{r['road']} has {r['potholes']} potholes "
                          f"({r['defects']} defects including cracks) over "
                          f"{r['length_m']:.0f} m — {r['per_km']:.1f} per km.")
            else:
                answer = f'No road matching "{p["road"]}" is in the survey.'
        elif p["metric"] == "length":
            answer = (f"The survey covers {tot['length_km']:.1f} km of road "
                      f"across {tot['roads']} roads.")
        else:
            answer = (f"{tot['potholes']} potholes are attached to a road "
                      f"({tot['defects']} defects in total including cracks and patches), "
                      f"over {tot['length_km']:.1f} km.")

    else:  # rank / list
        pool = [r for r in roads if r["rankable"]]
        if p["grade"]:
            pool = [r for r in pool if r["grade"] == p["grade"]]
        rev = p["order"] == "desc"
        pool = sorted(pool, key=lambda r: r[key], reverse=rev)
        hits = pool[:max(1, p["limit"])]
        if not hits:
            answer = "No road in the survey matches that."
        else:
            what = {"severity": "condition", "potholes": "pothole count",
                    "density": "potholes per km", "length": "length",
                    "width": "width", "area": "surface area"}[p["metric"]]
            lead = ("Worst" if rev else "Best") if p["metric"] == "severity" else \
                   ("Highest" if rev else "Lowest")
            if len(hits) == 1:
                r = hits[0]
                sup = ({"severity": ("worst", "best")}.get(
                    p["metric"], ("highest", "lowest")))[0 if rev else 1]
                extra = {"width": f"{r['width_m']:.1f} m wide",
                         "length": f"{r['length_m']:.0f} m long",
                         "area": f"{r['area_m2']:,.0f} m² of surface"}.get(p["metric"])
                answer = (f"{r['road']} — {r['length_m']:.0f} m, {r['potholes']} potholes "
                          f"({r['per_km']:.1f}/km), condition {r['grade'].upper()}, "
                          f"severity {r['severity']:.0f}/100"
                          + (f", {extra}" if extra else "") +
                          f". That is the {sup} road by {what}.")
            else:
                answer = f"{lead} {len(hits)} roads by {what}:\n" + "\n".join(
                    f"{i}. {r['road']} — {r['potholes']} potholes, {r['per_km']:.1f}/km, "
                    f"{r['length_m']:.0f} m, {r['grade']}"
                    for i, r in enumerate(hits, 1))

    paths = geometry_for(data_dir, [h["road"] for h in hits]) if hits else {}
    for h in hits:
        h = h.setdefault("path", paths.get(h["road"], []))
    return {"ok": True, "answer": answer, "plan": p,
            "roads": [{**h, "path": paths.get(h["road"], [])} for h in hits],
            "totals": tot}
