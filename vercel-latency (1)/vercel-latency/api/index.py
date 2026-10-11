import json
import math
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()


# ---------------------------------------------------------------- CORS
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": request.headers.get(
            "access-control-request-headers", "*"
        ),
        "Access-Control-Max-Age": "86400",
    }
    if request.method == "OPTIONS":  # preflight
        return Response(status_code=204, headers=headers)
    try:
        response = await call_next(request)
    except Exception as exc:  # keep CORS headers on crashes too
        response = JSONResponse(
            {"detail": f"Server error: {type(exc).__name__}: {exc}"}, status_code=500
        )
    for k, v in headers.items():
        response.headers[k] = v
    return response


# ---------------------------------------------------------------- data
_DATA = None
_SOURCE = None
_SKIP = {"vercel.json", "package.json", "package-lock.json", "tsconfig.json"}


def _find_file():
    here = Path(__file__).resolve().parent
    searched = []
    for d in (here, here.parent, Path.cwd()):
        if d in searched:
            continue
        searched.append(d)
        files = [f for f in sorted(d.glob("*.json")) if f.name not in _SKIP]
        files.sort(key=lambda f: "latency" not in f.name.lower())
        if files:
            return files[0]
    raise HTTPException(
        500,
        "Telemetry JSON not found. Searched: " + ", ".join(str(s) for s in searched)
        + ". Put q-vercel-latency.json inside the api/ folder of the repo.",
    )


def _load():
    global _DATA, _SOURCE
    if _DATA is not None:
        return _DATA
    f = _find_file()
    raw = json.loads(f.read_text(encoding="utf-8-sig"))
    records = []
    if isinstance(raw, list):
        records = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, list):
                for r in val:
                    if isinstance(r, dict):
                        r = dict(r)
                        r.setdefault("region", key)
                        records.append(r)
    _DATA, _SOURCE = records, str(f)
    return _DATA


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(rec, preferred, contains):
    for k in preferred:
        if k in rec:
            v = _to_float(rec[k])
            if v is not None:
                return v
    for k, v in rec.items():
        if contains in k.lower():
            v = _to_float(v)
            if v is not None:
                return v
    return None


def _percentile(values, p):
    """Linear-interpolation percentile (same as numpy.percentile default)."""
    s = sorted(values)
    if not s:
        return None
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def compute(records, regions, threshold):
    out = {}
    for region in regions:
        rows = [r for r in records if str(r.get("region", "")).lower() == region.lower()]
        lat = [v for v in (_num(r, ("latency_ms", "latency"), "latency") for r in rows) if v is not None]
        up = [v for v in (_num(r, ("uptime_pct", "uptime", "uptime_percent"), "uptime") for r in rows) if v is not None]
        out[region] = {
            "avg_latency": sum(lat) / len(lat) if lat else None,
            "p95_latency": _percentile(lat, 95),
            "avg_uptime": sum(up) / len(up) if up else None,
            "breaches": sum(1 for v in lat if v > threshold),
        }
    return out


# ---------------------------------------------------------------- routes
class Query(BaseModel):
    regions: List[str]
    threshold_ms: float


@app.get("/")
@app.get("/api")
@app.get("/api/latency")
def health():
    """Open this URL in a browser to see whether the data file was found."""
    info = {"status": "ok", "usage": "POST /api/latency with {regions: [...], threshold_ms: 180}"}
    try:
        data = _load()
        info["data_file"] = _SOURCE
        info["records"] = len(data)
        info["regions_found"] = sorted({str(r.get("region")) for r in data})
        info["sample_record"] = data[0] if data else None
    except HTTPException as e:
        info["status"] = "error"
        info["detail"] = e.detail
    except Exception as e:
        info["status"] = "error"
        info["detail"] = f"{type(e).__name__}: {e}"
    return info


@app.post("/")
@app.post("/api")
@app.post("/api/latency")
def latency(q: Query):
    result = compute(_load(), q.regions, q.threshold_ms)
    return {**result, "regions": result}
