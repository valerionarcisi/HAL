"""Turso (libSQL) via HTTP pipeline API. Nessuna dipendenza esterna."""
import json
import os
import urllib.request

_URL = os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
_TOKEN = os.environ["TURSO_AUTH_TOKEN"]


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _val(c):
    t, v = c.get("type"), c.get("value")
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


def batch(stmts):
    """stmts: lista di (sql, args). Ritorna una lista di risultati (lista di dict per riga)."""
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in (args or [])]}} for s, args in stmts]
    body = json.dumps({"requests": reqs + [{"type": "close"}]}).encode()
    req = urllib.request.Request(_URL, data=body, headers={"Authorization": "Bearer " + _TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    results = []
    for res in out["results"][: len(stmts)]:
        if res["type"] != "ok":
            raise RuntimeError(f"Turso: {res.get('error', res)}")
        rs = res["response"]["result"]
        cols = [c["name"] for c in rs["cols"]]
        results.append([dict(zip(cols, [_val(c) for c in row])) for row in rs["rows"]])
    return results


def q(sql, *args):
    return batch([(sql, list(args))])[0]


def one(sql, *args):
    rows = q(sql, *args)
    return rows[0] if rows else None


def run(sql, *args):
    batch([(sql, list(args))])
