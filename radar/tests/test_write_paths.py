"""Esegue tutte le scritture del motore su SQLite con lo schema vero (tabelle storiche + schema.py).

Perché esiste: un UPDATE su una colonna che non esiste non dà errore finché non viene eseguito, e
invio e riconoscimento delle risposte non erano mai stati eseguiti. Rete, SMTP, IMAP e modello sono finti.
Uso: python -m unittest discover -s tests
"""
import io
import json
import os
import sqlite3
import unittest
from unittest import mock

os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "x")
os.environ.setdefault("EMAIL_ADDRESS", "mittente@example.test")
os.environ.setdefault("EMAIL_PASSWORD", "x")

from radar import db, jobs, llm, report, requests_queue, schema, web  # noqa: E402
from tests.real_schema import CONTACTS_DDL, PEOPLE_DDL  # noqa: E402


class FakeTurso:
    """Sostituisce db.batch con SQLite in memoria: q, one e run passano da lì."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for ddl in (CONTACTS_DDL, PEOPLE_DDL, *schema.DDL):
            self.conn.execute(ddl)
        for table, cols in schema.NEW_COLUMNS.items():
            for name, decl in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.execute("INSERT INTO radar_settings(key, value) VALUES " + ",".join(f"('{k}','{v}')" for k, v in schema.DEFAULT_SETTINGS.items()))

    def batch(self, stmts):
        out = []
        for sql, args in stmts:
            cur = self.conn.execute(sql, list(args or []))
            out.append([dict(r) for r in cur.fetchall()])
        self.conn.commit()
        return out

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = FakeTurso()
        p = mock.patch.object(db, "batch", self.t.batch)
        p.start()
        self.addCleanup(p.stop)
        tg = mock.patch.object(jobs, "telegram")
        self.telegram = tg.start()
        self.addCleanup(tg.stop)
        tq = mock.patch.object(requests_queue, "telegram")
        self.queue_telegram = tq.start()
        self.addCleanup(tq.stop)

    def contact(self, name="Casa Prova", email="info@prova.example", project="walkie-talkie", stage="to-contact", **extra):
        cols = {"name": name, "email": email, "official_url": "https://prova.example", "verification_level": "verified",
                "status": "to-contact", "country": "IT", "hook_title": "Un loro film", **extra}
        cur = self.t.conn.execute(f"INSERT INTO contacts({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
        cid = cur.lastrowid
        self.t.conn.execute("INSERT INTO contact_projects(contact_id, project, stage, shortlist, score) VALUES(?,?,?,1,80)", (cid, project, stage))
        self.t.conn.commit()
        return cid

    def row(self, sql, *args):
        r = self.t.conn.execute(sql, args).fetchone()
        return dict(r) if r else None


class TestSend(Base):
    def _approved(self, kind="first"):
        cid = self.contact(stage="draft")
        self.t.conn.execute("INSERT INTO radar_outbox(contact_id, project, kind, to_email, subject, body, status, approved_at) "
                            "VALUES(?, 'walkie-talkie', ?, 'info@prova.example', 'Oggetto', 'Ciao, testo senza segnaposto.', 'approved', datetime('now'))", (cid, kind))
        self.t.conn.execute("UPDATE radar_settings SET value='1' WHERE key='send_enabled'")
        self.t.conn.execute("UPDATE radar_settings SET value='0' WHERE key='window_start'")
        self.t.conn.execute("UPDATE radar_settings SET value='24' WHERE key='window_end'")
        self.t.conn.commit()
        return cid

    def test_send_is_off_by_default(self):
        self._approved()
        self.t.conn.execute("UPDATE radar_settings SET value='0' WHERE key='send_enabled'")
        self.assertEqual(jobs.send()[2], "invii spenti")

    def test_first_mail_marks_everything_as_contacted(self):
        cid = self._approved()
        with mock.patch.object(jobs.smtplib, "SMTP_SSL") as smtp:
            sent, errors, _ = jobs.send()
        self.assertEqual((sent, errors), (1, 0))
        smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        self.assertEqual(self.row("SELECT status FROM radar_outbox")["status"], "sent")
        self.assertEqual(self.row("SELECT stage FROM contact_projects WHERE contact_id=?", cid)["stage"], "contacted")
        c = self.row("SELECT status, contacted_at FROM contacts WHERE id=?", cid)
        self.assertEqual(c["status"], "contacted")
        self.assertIsNotNone(c["contacted_at"])

    def test_followup_updates_status_and_stage(self):
        cid = self._approved(kind="followup")
        with mock.patch.object(jobs.smtplib, "SMTP_SSL"):
            self.assertEqual(jobs.send()[:2], (1, 0))
        self.assertEqual(self.row("SELECT stage FROM contact_projects WHERE contact_id=?", cid)["stage"], "followup")
        self.assertEqual(self.row("SELECT status FROM contacts WHERE id=?", cid)["status"], "follow-up-1")

    def test_placeholder_blocks_the_send(self):
        self._approved()
        self.t.conn.execute("UPDATE radar_outbox SET body='Screener: [link screener]'")
        with mock.patch.object(jobs.smtplib, "SMTP_SSL") as smtp:
            sent, errors, _ = jobs.send()
        self.assertEqual((sent, errors), (0, 1))
        smtp.assert_not_called()


class TestInbox(Base):
    def test_reply_is_recognised_and_recorded(self):
        cid = self.contact(stage="contacted")
        self.t.conn.execute("INSERT INTO radar_outbox(contact_id, project, to_email, subject, body, status, sent_at) "
                            "VALUES(?, 'walkie-talkie', 'info@prova.example', 'S', 'B', 'sent', datetime('now'))", (cid,))
        self.t.conn.commit()
        imap = mock.MagicMock()
        imap.search.return_value = ("OK", [b"1"])
        imap.fetch.return_value = ("OK", [(b"1", b"From: Casa <info@prova.example>\r\n\r\n")])
        with mock.patch.object(jobs.imaplib, "IMAP4_SSL", return_value=imap):
            hits, errors, _ = jobs.inbox()
        self.assertEqual((hits, errors), (1, 0))
        self.assertEqual(self.row("SELECT stage FROM contact_projects WHERE contact_id=?", cid)["stage"], "replied")
        c = self.row("SELECT status, reply FROM contacts WHERE id=?", cid)
        self.assertEqual(c["status"], "replied")
        self.assertIn("risposta", c["reply"])


class TestScoreAndDraft(Base):
    LLM_SCORE = {"tipo": "producer", "punteggi": {"attiva": 20, "esordienti": 25, "genere": 20, "coproduzione": 15, "scala": 10},
                 "prove": {"attiva": "x", "esordienti": "x", "genere": "x", "coproduzione": "x", "scala": "x"},
                 "motivo": "Adatta.", "aggancio": "Un loro film", "scartare": None}

    def _site(self):
        text = "Casa di produzione indipendente. " * 40
        page = f"<html><title>Casa Prova</title><body>{text} info@prova.example</body></html>"
        return {"url": "https://prova.example", "final": "https://prova.example", "error": None, "pages": {"https://prova.example": page}}

    def test_enrich_score_writes_and_caps_the_total(self):
        cid = self.contact(email=None, verification_level=None)
        with mock.patch.object(web, "crawl_site", return_value=self._site()), \
             mock.patch.object(web, "has_mx", return_value=True), \
             mock.patch.object(jobs.llm, "ask_json", return_value=self.LLM_SCORE):
            done, errors, _ = jobs.enrich_score(limit=5)
        self.assertEqual((done, errors), (1, 0))
        cp = self.row("SELECT score, scored_at FROM contact_projects WHERE contact_id=?", cid)
        self.assertEqual(cp["score"], jobs.MAX_TOTAL)
        self.assertIsNotNone(cp["scored_at"])
        self.assertEqual(self.row("SELECT email FROM contacts WHERE id=?", cid)["email"], "info@prova.example")

    def test_possible_off_target_is_flagged_but_never_discarded(self):
        cid = self.contact()
        reply = {**self.LLM_SCORE, "scartare": "è specializzata in animazione per bambini"}
        with mock.patch.object(web, "crawl_site", return_value=self._site()), \
             mock.patch.object(web, "has_mx", return_value=True), mock.patch.object(jobs.llm, "ask_json", return_value=reply):
            jobs.enrich_score(limit=5)
        cp = self.row("SELECT stage, reason FROM contact_projects WHERE contact_id=?", cid)
        self.assertEqual(cp["stage"], "to-contact")
        self.assertTrue(cp["reason"].startswith("Possibile fuori target"))

    def test_unreadable_site_keeps_the_old_score(self):
        cid = self.contact()
        thin = {"url": "x", "final": "x", "error": None, "pages": {"x": "<html>ok</html>"}}
        with mock.patch.object(web, "crawl_site", return_value=thin), mock.patch.object(jobs.llm, "ask_json") as ask:
            jobs.enrich_score(limit=5)
        ask.assert_not_called()
        self.assertEqual(self.row("SELECT score FROM contact_projects WHERE contact_id=?", cid)["score"], 80)

    def test_billing_error_stops_the_job_without_burning_attempts(self):
        cid = self.contact()
        err = jobs.urllib.error.HTTPError("u", 402, "Payment Required", {}, None)
        with mock.patch.object(web, "crawl_site", return_value=self._site()), mock.patch.object(jobs.llm, "ask_json", side_effect=err):
            done, errors, note = jobs.enrich_score(limit=5)
        self.assertIn("credito", note)
        self.assertEqual(self.row("SELECT coalesce(enrich_attempts,0) n FROM contacts WHERE id=?", cid)["n"], 0)

    def test_draft_job_creates_an_outbox_row(self):
        cid = self.contact()
        with mock.patch.object(jobs.llm, "ask_json", return_value={"oggetto": "O", "testo": "T"}):
            made, errors, _ = jobs.draft(per_run=3)
        self.assertEqual((made, errors), (1, 0))
        self.assertEqual(self.row("SELECT status, to_email FROM radar_outbox")["status"], "draft")
        self.assertEqual(self.row("SELECT stage FROM contact_projects WHERE contact_id=?", cid)["stage"], "draft")


class TestRequests(Base):
    def queue(self, kind, **kw):
        cols = {"kind": kind, **kw}
        self.t.conn.execute(f"INSERT INTO radar_requests({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
        self.t.conn.commit()

    def test_followup_request_builds_a_template_draft(self):
        cid = self.contact(stage="contacted")
        self.queue("followup", contact_id=cid, project="walkie-talkie")
        done, errors, _ = requests_queue.process_requests()
        self.assertEqual((done, errors), (1, 0))
        o = self.row("SELECT kind, status, subject FROM radar_outbox")
        self.assertEqual((o["kind"], o["status"]), ("followup", "draft"))
        self.assertEqual(self.row("SELECT status FROM radar_requests")["status"], "done")

    def test_rewrite_and_translate_update_the_draft(self):
        cid = self.contact(stage="draft")
        self.t.conn.execute("INSERT INTO radar_outbox(contact_id, project, to_email, subject, body, lang) VALUES(?, 'walkie-talkie', 'a@b.it', 'S', 'Testo lungo', 'it')", (cid,))
        self.t.conn.commit()
        self.queue("shorten", outbox_id=1)
        self.queue("translate", outbox_id=1)
        with mock.patch.object(requests_queue.llm, "ask_json", return_value={"oggetto": "Subject", "testo": "Short text"}):
            done, errors, _ = requests_queue.process_requests()
        self.assertEqual((done, errors), (2, 0))
        o = self.row("SELECT subject, body, lang FROM radar_outbox")
        self.assertEqual((o["subject"], o["lang"]), ("Subject", "en"))

    def test_draft_request_without_email_fails_with_a_clear_note(self):
        cid = self.contact(email=None)
        self.queue("draft", contact_id=cid, project="walkie-talkie")
        done, errors, _ = requests_queue.process_requests()
        self.assertEqual((done, errors), (0, 1))
        r = self.row("SELECT status, note FROM radar_requests")
        self.assertEqual(r["status"], "failed")
        self.assertIn("email", r["note"])

    def test_billing_error_marks_the_request_failed(self):
        cid = self.contact()
        self.queue("draft", contact_id=cid, project="walkie-talkie")
        err = requests_queue.llm.urllib.error.HTTPError("u", 402, "x", {}, None)
        with mock.patch.object(requests_queue.llm, "ask_json", side_effect=err), mock.patch.object(jobs, "telegram"):
            requests_queue.process_requests()
        self.assertIn("credito", self.row("SELECT note FROM radar_requests")["note"])

    def _house_site(self):
        page = ('<html><title>Casa Rossa Film | Home</title><a href="https://www.instagram.com/casarossa/">ig</a>'
                '<a href="https://www.linkedin.com/company/casa-rossa/">li</a><a href="https://www.linkedin.com/in/anna-bianchi-12ab34cd/">p</a>'
                '<p>info@casarossa.example anna@casarossa.example</p></html>')
        return {"url": "https://casarossa.example", "final": "https://casarossa.example", "error": None, "pages": {"https://casarossa.example": page}}

    def test_add_house_creates_the_contact_people_and_shortlist(self):
        self.queue("add_house", project="walkie-talkie", url="casarossa.example")
        with mock.patch.object(web, "crawl_site", return_value=self._house_site()), mock.patch.object(web, "has_mx", return_value=True):
            done, errors, _ = requests_queue.process_requests()
        self.assertEqual((done, errors), (1, 0))
        c = self.row("SELECT name, email, instagram, linkedin, source_system, verification_level FROM contacts")
        self.assertEqual(c["name"], "Casa Rossa Film")
        self.assertEqual(c["instagram"], "https://www.instagram.com/casarossa/")
        self.assertEqual(c["source_system"], "hal-telegram")
        self.assertEqual(self.row("SELECT shortlist FROM contact_projects")["shortlist"], 1)
        p = self.row("SELECT name, email, email_confidence FROM people")
        self.assertEqual((p["name"], p["email"]), ("Anna Bianchi", "anna@casarossa.example"))
        self.assertIn("Instagram", self.queue_telegram.call_args[0][0])

    def test_add_house_twice_updates_instead_of_duplicating(self):
        self.queue("add_house", project="walkie-talkie", url="casarossa.example")
        self.queue("add_house", project="non-fa-ridere", url="https://www.casarossa.example")
        with mock.patch.object(web, "crawl_site", return_value=self._house_site()), mock.patch.object(web, "has_mx", return_value=True):
            requests_queue.process_requests()
        self.assertEqual(self.row("SELECT count(*) n FROM contacts")["n"], 1)
        self.assertEqual(self.row("SELECT count(*) n FROM contact_projects")["n"], 2)
        self.assertEqual(self.row("SELECT count(*) n FROM people")["n"], 1)

    def test_add_house_unreachable_site_fails_cleanly(self):
        self.queue("add_house", url="https://nonesiste.example")
        dead = {"url": "x", "final": "x", "error": "timed out", "pages": {}}
        with mock.patch.object(web, "crawl_site", return_value=dead):
            done, errors, _ = requests_queue.process_requests()
        self.assertEqual((done, errors), (0, 1))
        self.assertEqual(self.row("SELECT count(*) n FROM contacts")["n"], 0)

    def test_invalid_url_is_rejected(self):
        self.queue("add_house", url="non un sito")
        self.assertEqual(requests_queue.process_requests()[1], 1)


class TestReport(Base):
    def test_report_runs_and_stores_a_snapshot(self):
        self.contact()
        with mock.patch.object(report, "telegram") as tg, mock.patch.object(report.llm, "balance", return_value=(1.5, 10.0)):
            report.weekly_report(send=1)
        self.assertIn("Contatti", tg.call_args[0][0])
        self.assertIn("sta per finire", tg.call_args[0][0])
        self.assertEqual(self.row("SELECT count(*) n FROM radar_snapshots")["n"], 1)


class FakeHTTP(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def llm_reply(text='{"oggetto": "O", "testo": "T"}'):
    return FakeHTTP(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


class TestBudget(Base):
    def setUp(self):
        super().setUp()
        os.environ["OPENROUTER_API_KEY"] = "test"

    def _calls_today(self):
        return int(self.row("SELECT value FROM radar_settings WHERE key='llm_calls_today'")["value"])

    def test_cap_stops_automatic_calls_but_not_manual_ones(self):
        self.t.conn.execute("UPDATE radar_settings SET value='2' WHERE key='llm_daily_calls'")
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=lambda *a, **k: llm_reply()):
            llm.ask_json("s", "u")
            llm.ask_json("s", "u")
            with self.assertRaises(llm.BudgetExceeded):
                llm.ask_json("s", "u")
            llm.ask_json("s", "u", budget=False)  # richiesta fatta a mano dall'admin
        self.assertEqual(self._calls_today(), 3)

    def test_counter_restarts_on_a_new_day(self):
        self.t.conn.execute("INSERT INTO radar_settings(key, value) VALUES('llm_day','2020-01-01'),('llm_calls_today','999')")
        self.t.conn.commit()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=lambda *a, **k: llm_reply()):
            llm.ask_json("s", "u")
        self.assertEqual(self._calls_today(), 1)

    def test_job_stops_quietly_at_the_cap_without_burning_attempts(self):
        cid = self.contact()
        site = {"url": "x", "final": "https://prova.example", "error": None,
                "pages": {"https://prova.example": "<html>" + "Casa di produzione indipendente. " * 40 + "</html>"}}
        with mock.patch.object(web, "crawl_site", return_value=site), \
             mock.patch.object(jobs.llm, "ask_json", side_effect=llm.BudgetExceeded("tetto giornaliero raggiunto (40/40 chiamate al modello)")):
            done, errors, note = jobs.enrich_score(limit=5)
        self.assertIn("fermato", note)
        self.assertEqual(self.row("SELECT coalesce(enrich_attempts,0) n FROM contacts WHERE id=?", cid)["n"], 0)
        self.telegram.assert_not_called()


class TestShortsEvidence(Base):
    def _score(self, reply):
        cid = self.contact(name="Distribuzione Prova", project="non-fa-ridere", email=None, verification_level=None)
        site = {"url": "x", "final": "https://prova.example", "error": None,
                "pages": {"https://prova.example": "<html>" + "Distribuzione cinematografica. " * 40 + "</html>"}}
        with mock.patch.object(web, "crawl_site", return_value=site), mock.patch.object(web, "has_mx", return_value=True), \
             mock.patch.object(jobs.llm, "ask_json", return_value=reply):
            jobs.enrich_score(limit=5)
        return self.row("SELECT score, reason, score_detail FROM contact_projects WHERE contact_id=?", cid)

    BASE = {"tipo": "distributor", "punteggi": {"attiva": 20, "corti": 20, "festival": 20, "genere": 15, "apertura": 10},
            "prove": {"attiva": "x", "corti": "x", "festival": "x", "genere": "x", "apertura": "x"}, "motivo": "Ok.", "aggancio": None, "scartare": None}

    def test_confirmed_shorts_keep_the_full_score(self):
        r = self._score({**self.BASE, "fa_corti": "si", "prova_corti": "Catalogo corti 2025"})
        self.assertEqual(json.loads(r["score_detail"])["corti"], 20)
        self.assertTrue(r["reason"].startswith("Fa corti: sì."))

    def test_unproven_or_unknown_shorts_are_capped_and_labelled(self):
        r = self._score({**self.BASE, "fa_corti": "non_so", "prova_corti": None})
        self.assertEqual(json.loads(r["score_detail"])["corti"], 6)
        self.assertTrue(r["reason"].startswith("Fa corti: non so."))

    def test_claiming_yes_without_a_quote_does_not_count(self):
        r = self._score({**self.BASE, "fa_corti": "si", "prova_corti": None})
        self.assertEqual(json.loads(r["score_detail"])["corti"], 6)


class TestCrawlPicksCatalogPages(unittest.TestCase):
    def test_catalog_index_pages_come_before_contact_pages(self):
        home = ('<a href="/chi-siamo">c</a><a href="/contatti">k</a><a href="/cortometraggi">s</a><a href="/film/titolo-lungo-di-un-film">f</a>'
                '<a href="/catalogo">cat</a><a href="/altro">x</a>')
        fetched = []

        def fake_fetch(u, timeout=15):
            fetched.append(u)
            return u, home if u == "https://prova.example" else "<html>x</html>", None

        with mock.patch.object(web, "fetch", side_effect=fake_fetch):
            site = web.crawl_site("https://prova.example", max_pages=4)
        self.assertEqual(fetched[0], "https://prova.example")
        self.assertEqual(len(site["pages"]), 4)
        self.assertIn("https://prova.example/cortometraggi", fetched[1:4])
        self.assertIn("https://prova.example/catalogo", fetched[1:4])
        self.assertNotIn("https://prova.example/altro", fetched)


if __name__ == "__main__":
    unittest.main()
