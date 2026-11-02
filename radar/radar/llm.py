"""Claude via OpenRouter, con risposta JSON. Usato solo per scheda, punteggio e bozze.

Tetto giornaliero: i lavori automatici si fermano quando raggiungono `llm_daily_calls` chiamate al giorno
(impostazione modificabile dalla pagina Motore). Le richieste fatte a mano dall'admin non sono bloccate dal tetto,
ma contano.
"""
import json
import os
import re
import urllib.request

from . import db

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("RADAR_MODEL", "anthropic/claude-haiku-4.5")


class BudgetExceeded(Exception):
    """Tetto giornaliero di chiamate raggiunto: non è un errore del servizio, il lavoro riparte domani."""


def _keys(budget):
    """Il censimento ha il suo tetto (census_llm_daily_calls) e il suo contatore, separati dai lavori automatici."""
    p = "census_" if budget == "census" else ""
    return f"{p}llm_daily_calls", f"{p}llm_day", f"{p}llm_calls_today"


def _check_budget(budget=True):
    cap_k, day_k, n_k = _keys(budget)
    row = db.one("""SELECT date('now') AS today,
                           (SELECT value FROM radar_settings WHERE key = ?) AS cap,
                           (SELECT value FROM radar_settings WHERE key = ?) AS day,
                           (SELECT value FROM radar_settings WHERE key = ?) AS n""", cap_k, day_k, n_k)
    n = int(row["n"] or 0) if row["day"] == row["today"] else 0
    cap = int(row["cap"] or 0)
    if cap and n >= cap:
        raise BudgetExceeded(f"tetto giornaliero raggiunto ({n}/{cap} chiamate al modello)")
    return row["today"], n


def _count_call(today, n, budget=True):
    _, day_k, n_k = _keys(budget)
    for key, value in ((day_k, today), (n_k, str(n + 1))):
        db.run("INSERT INTO radar_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


def ask_json(system, user, max_tokens=1200, budget=True):
    """budget: True = tetto dei lavori automatici, "census" = tetto del censimento, False = nessun tetto (conta comunque)."""
    today, n = _check_budget(budget) if budget else (None, 0)
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
        "Content-Type": "application/json",
        "X-Title": "radar-valerio",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.load(r)
    if today is None:
        _, day_k, n_k = _keys(budget)
        row = db.one("SELECT date('now') AS today, (SELECT value FROM radar_settings WHERE key = ?) AS day, "
                     "(SELECT value FROM radar_settings WHERE key = ?) AS n", day_k, n_k)
        today, n = row["today"], int(row["n"] or 0) if row["day"] == row["today"] else 0
    _count_call(today, n, budget)
    txt = out["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError("risposta senza JSON: " + txt[:200])
    return json.loads(m.group(0))


def balance():
    """Credito reale su OpenRouter: (residuo account, residuo del limite della chiave) o None se non risponde."""
    headers = {"Authorization": "Bearer " + os.environ.get("OPENROUTER_API_KEY", "")}
    try:
        with urllib.request.urlopen(urllib.request.Request("https://openrouter.ai/api/v1/credits", headers=headers), timeout=15) as r:
            c = json.load(r)["data"]
        with urllib.request.urlopen(urllib.request.Request("https://openrouter.ai/api/v1/key", headers=headers), timeout=15) as r:
            k = json.load(r)["data"]
        return c["total_credits"] - c["total_usage"], k.get("limit_remaining")
    except Exception:
        return None
