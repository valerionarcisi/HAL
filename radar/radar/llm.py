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


def _check_budget():
    row = db.one("""SELECT date('now') AS today,
                           (SELECT value FROM radar_settings WHERE key = 'llm_daily_calls') AS cap,
                           (SELECT value FROM radar_settings WHERE key = 'llm_day') AS day,
                           (SELECT value FROM radar_settings WHERE key = 'llm_calls_today') AS n""")
    n = int(row["n"] or 0) if row["day"] == row["today"] else 0
    cap = int(row["cap"] or 0)
    if cap and n >= cap:
        raise BudgetExceeded(f"tetto giornaliero raggiunto ({n}/{cap} chiamate al modello)")
    return row["today"], n


def _count_call(today, n):
    for key, value in (("llm_day", today), ("llm_calls_today", str(n + 1))):
        db.run("INSERT INTO radar_settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


def ask_json(system, user, max_tokens=1200, budget=True):
    today, n = _check_budget() if budget else (None, 0)
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
        row = db.one("SELECT date('now') AS today, (SELECT value FROM radar_settings WHERE key = 'llm_day') AS day, "
                     "(SELECT value FROM radar_settings WHERE key = 'llm_calls_today') AS n")
        today, n = row["today"], int(row["n"] or 0) if row["day"] == row["today"] else 0
    _count_call(today, n)
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
