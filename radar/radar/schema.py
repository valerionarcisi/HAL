"""Tabelle del motore. Solo CREATE IF NOT EXISTS: non tocca le tabelle di Hermes."""
from . import db

DDL = [
    # Un contatto può servire a più film: punteggio, motivo e shortlist per progetto.
    """CREATE TABLE IF NOT EXISTS contact_projects (
        contact_id INTEGER NOT NULL,
        project TEXT NOT NULL,
        score INTEGER,
        score_detail TEXT,
        reason TEXT,
        shortlist INTEGER DEFAULT 0,
        stage TEXT DEFAULT 'to-contact',
        scored_at TEXT,
        updated_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (contact_id, project)
    )""",
    # Bozze e coda invii. Il sito scrive solo status='approved'; spedisce il NAS.
    """CREATE TABLE IF NOT EXISTS radar_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL,
        project TEXT NOT NULL,
        kind TEXT DEFAULT 'first',
        to_email TEXT,
        subject TEXT,
        body TEXT,
        hook TEXT,
        lang TEXT DEFAULT 'it',
        status TEXT DEFAULT 'draft',
        error TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        approved_at TEXT,
        sent_at TEXT,
        message_id TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON radar_outbox(status)",
    """CREATE TABLE IF NOT EXISTS radar_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job TEXT NOT NULL,
        started_at TEXT DEFAULT (datetime('now')),
        finished_at TEXT,
        status TEXT DEFAULT 'running',
        found INTEGER DEFAULT 0,
        errors INTEGER DEFAULT 0,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS radar_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL,
        kind TEXT NOT NULL,
        project TEXT NOT NULL,
        company_type TEXT DEFAULT 'sales',
        country TEXT,
        active INTEGER DEFAULT 1,
        last_run TEXT,
        found_total INTEGER DEFAULT 0,
        emails_total INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS radar_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    # Richieste dall'admin e da HAL: il sito e il bot non chiamano il modello, accodano e il motore esegue.
    """CREATE TABLE IF NOT EXISTS radar_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        contact_id INTEGER,
        project TEXT,
        outbox_id INTEGER,
        url TEXT,
        status TEXT DEFAULT 'pending',
        note TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        done_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_requests_status ON radar_requests(status)",
    # Una fotografia del database per volta: serve al report settimanale per dire se e come cresce.
    """CREATE TABLE IF NOT EXISTS radar_snapshots (
        day TEXT PRIMARY KEY,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS radar_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER,
        project TEXT,
        kind TEXT,
        detail TEXT,
        at TEXT DEFAULT (datetime('now'))
    )""",
]

DEFAULT_SETTINGS = {
    "send_enabled": "0",       # spento finché Valerio non lo accende dall'admin
    "paused": "0",
    "daily_cap": "25",
    "window_start": "9",
    "window_end": "12",
    "followup_days": "10",
    "max_followups": "2",
    "no_repeat_days": "60",
    "drafts_per_run": "5",
    "llm_daily_calls": "40",   # tetto di chiamate al modello al giorno per i lavori automatici
}

SEED_SOURCES = [
    ("Centro del Corto · distributori", "https://www.centrodelcorto.it/it/distributori/", "directory", "non-fa-ridere", "sales", "IT"),
]


def migrate():
    db.batch([(s, []) for s in DDL])
    db.batch([("INSERT OR IGNORE INTO radar_settings(key, value) VALUES(?, ?)", [k, v]) for k, v in DEFAULT_SETTINGS.items()])
    db.batch([("INSERT OR IGNORE INTO radar_sources(name, url, kind, project, company_type, country) VALUES(?,?,?,?,?,?)", list(s)) for s in SEED_SOURCES])
    # Porta nella tabella per-progetto quello che oggi sta in contacts.project / priority / next_action.
    db.run("""INSERT OR IGNORE INTO contact_projects(contact_id, project, score, reason, shortlist, stage)
              SELECT id, project,
                     CASE WHEN CAST(priority AS INTEGER) >= 10 THEN CAST(priority AS INTEGER) END,
                     fit_reason,
                     CASE WHEN next_action LIKE 'Shortlist%' THEN 1 ELSE 0 END,
                     status
              FROM contacts WHERE project IN ('non-fa-ridere', 'walkie-talkie')""")


def settings():
    return {r["key"]: r["value"] for r in db.q("SELECT key, value FROM radar_settings")}
