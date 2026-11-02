"""Censimento: normalizzazione, dedup, crediti, profilo, telefoni, do_not_contact, migrazioni. Nessuna rete."""
import json
import os
import sqlite3
import unittest
from unittest import mock

os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "x")
os.environ.setdefault("EMAIL_ADDRESS", "mittente@example.test")
os.environ.setdefault("EMAIL_PASSWORD", "x")

from radar import census, db, jobs, requests_queue, schema  # noqa: E402
from tests.real_schema import CONTACTS_DDL, PEOPLE_DDL  # noqa: E402
from tests.test_write_paths import Base, FakeTurso  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_name_strips_legal_forms_generic_words_accents_and_punctuation(self):
        cases = {
            "Prem1ere Film S.r.l.": "prem1ere",
            "Società Élan Productions Ltd": "societa elan",
            "[DIST] Premiere Film": "premiere",
            "Gargantua Film GmbH": "gargantua",
            "Zen Movie s.a.s.": "zen movie",
            "Indelebili": "indelebili",
            "Film Production": "film production",  # tolto tutto: si tiene il nome pulito
        }
        for raw, want in cases.items():
            self.assertEqual(census.normalize_name(raw), want, raw)

    def test_same_house_written_differently_gets_the_same_key(self):
        self.assertEqual(census.normalize_name("Sayonara Film S.r.l."), census.normalize_name("SAYONARA  film"))

    def test_domain_ignores_scheme_www_path_and_aggregators(self):
        self.assertEqual(census.site_domain("https://www.Ginkofilm.it/chi-siamo"), "ginkofilm.it")
        self.assertEqual(census.site_domain("ginkofilm.it"), "ginkofilm.it")
        self.assertIsNone(census.site_domain("https://filmitalia.org/it/production/166950/"))
        self.assertIsNone(census.site_domain("https://www.instagram.com/ginko/"))
        self.assertIsNone(census.site_domain(None))

    def test_vat(self):
        self.assertEqual(census.normalize_vat("IT 01234567890"), "01234567890")
        self.assertEqual(census.normalize_vat("01234567890"), "01234567890")
        self.assertIsNone(census.normalize_vat("123"))


class TestPhone(unittest.TestCase):
    def test_italian_formats(self):
        n = census.normalize_phone
        self.assertEqual(n("+39 06 1234 5678"), "+390612345678")
        self.assertEqual(n("06.1234.5678"), "+390612345678")
        self.assertEqual(n("0039 06 12345678"), "+390612345678")
        self.assertEqual(n("338 1234567"), "+393381234567")
        self.assertEqual(n("tel:+39-338-1234567"), "+393381234567")
        self.assertEqual(n("+39 (0)71 123456"), "+39071123456")
        self.assertEqual(n("06 1234 5678 int. 12"), "+390612345678")

    def test_other_european_formats_need_the_prefix(self):
        n = census.normalize_phone
        self.assertEqual(n("+33 1 42 68 53 00"), "+33142685300")
        self.assertEqual(n("+44 (0)20 7946 0958"), "+442079460958")
        self.assertEqual(n("0044 20 7946 0958"), "+442079460958")
        self.assertEqual(n("+49 30 123456"), "+4930123456")

    def test_rejects_garbage(self):
        for bad in ("", None, "abc", "12345", "+39 123", "1234567890", "+4930123456789012345"):
            self.assertIsNone(census.normalize_phone(bad), bad)


class TestMigration(unittest.TestCase):
    def test_migrate_adds_columns_once_and_never_drops(self):
        t = FakeTurso.__new__(FakeTurso)
        t.conn = sqlite3.connect(":memory:")
        t.conn.row_factory = sqlite3.Row
        for ddl in (CONTACTS_DDL, PEOPLE_DDL, *schema.DDL):
            t.conn.execute(ddl)
        t.conn.execute("INSERT INTO contacts(name, email) VALUES('Casa', 'a@b.it')")
        t.conn.commit()
        with mock.patch.object(db, "batch", t.batch):
            schema.add_missing_columns()
            schema.add_missing_columns()  # idempotente
        cols = {r[1] for r in t.conn.execute("PRAGMA table_info(contacts)")}
        self.assertTrue({"kind", "phone", "submission_url", "do_not_contact", "do_not_contact_reason", "last_verified_at", "vat_id", "profile_json"} <= cols)
        self.assertTrue({"phone", "last_verified_at"} <= {r[1] for r in t.conn.execute("PRAGMA table_info(people)")})
        row = t.conn.execute("SELECT name, email, do_not_contact FROM contacts").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("Casa", "a@b.it", 0))

    def test_census_setting_is_separate_and_defaults_to_200(self):
        self.assertEqual(schema.DEFAULT_SETTINGS["census_llm_daily_calls"], "200")
        self.assertEqual(schema.DEFAULT_SETTINGS["llm_daily_calls"], "40")


class TestDedup(Base):
    def setUp(self):
        super().setUp()
        self.old = self.contact("Gargantua Film S.r.l.", email="info@gargantua.example", country="IT")
        self.t.conn.execute("UPDATE contacts SET official_url='https://www.gargantua.example' WHERE id=?", (self.old,))
        self.t.conn.commit()
        self.index = census.CompanyIndex()

    def test_same_domain_merges_and_adds_the_source(self):
        cid, created = census.upsert_company(self.index, "Un altro nome", "https://fonte.example/a", "Fonte A", url="http://gargantua.example/chi-siamo")
        self.assertEqual((cid, created), (self.old, False))
        cid2, _ = census.upsert_company(self.index, "Gargantua", "https://fonte.example/b", "Fonte B")
        self.assertEqual(cid2, self.old)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contacts")["n"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_sources WHERE contact_id=?", self.old)["n"], 2)

    def test_same_source_twice_is_not_duplicated(self):
        for _ in range(2):
            census.upsert_company(self.index, "Gargantua", "https://fonte.example/a", "Fonte A")
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_sources")["n"], 1)

    def test_same_name_in_another_country_is_a_different_house(self):
        cid, created = census.upsert_company(self.index, "Gargantua Productions", "https://fonte.example/c", "Fonte C", country="FR")
        self.assertTrue(created)
        self.assertNotEqual(cid, self.old)

    def test_vat_matches_even_with_different_name_and_site(self):
        cid, _ = census.upsert_company(self.index, "Nome Nuovo", "https://fonte.example/d", "D", url="https://nuovo.example", vat="IT01234567890")
        again, created = census.upsert_company(self.index, "Altro Ancora", "https://fonte.example/e", "E", url="https://altro.example", vat="01234567890")
        self.assertEqual((again, created), (cid, False))

    def test_existing_values_are_never_overwritten_only_empty_ones_filled(self):
        census.upsert_company(self.index, "Gargantua", "https://f.example", "F", city="Roma", phone="06 1234 5678", kind="short_distribution", email="altra@x.example")
        r = self.row("SELECT email, city, phone, kind FROM contacts WHERE id=?", self.old)
        self.assertEqual((r["email"], r["city"], r["phone"], r["kind"]), ("info@gargantua.example", "Roma", "+390612345678", "short_distribution"))
        census.upsert_company(self.index, "Gargantua", "https://g.example", "G", city="Milano", phone="06 9999 9999", kind="production")
        r = self.row("SELECT city, phone, kind FROM contacts WHERE id=?", self.old)
        self.assertEqual((r["city"], r["phone"], r["kind"]), ("Roma", "+390612345678", "short_distribution"))

    def test_new_house_has_source_and_no_project_rows(self):
        cid, created = census.upsert_company(self.index, "Nuova Casa", "https://f.example/x", "F", url="https://nuovacasa.example", country="IT", kind="production")
        self.assertTrue(created)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_sources WHERE contact_id=?", cid)["n"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_projects WHERE contact_id=?", cid)["n"], 0)

    def test_requires_a_source_and_a_valid_kind(self):
        with self.assertRaises(ValueError):
            census.upsert_company(self.index, "X Casa", "", "F")
        with self.assertRaises(ValueError):
            census.upsert_company(self.index, "X Casa", "https://f.example", "F", kind="boh")

    def test_listing_page_domain_does_not_merge_two_houses(self):
        a, _ = census.upsert_company(self.index, "Alfa Cinema", "https://f.example", "F", url="https://filmitalia.org/it/production/1/")
        b, _ = census.upsert_company(self.index, "Beta Cinema", "https://f.example", "F", url="https://filmitalia.org/it/production/2/")
        self.assertNotEqual(a, b)


class TestCredits(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.contact("Casa Rossa", country="IT")

    def test_same_credit_is_merged_not_duplicated(self):
        census.add_credit(self.cid, "Il Film", "producer", "https://s.example/1", year=2025, format="short", funds="MiC opere prime 2025")
        created = census.add_credit(self.cid, "il film", "producer", "https://s.example/2", year=2025, funds="Marche 2025", festivals="Venezia")
        self.assertFalse(created)
        r = self.row("SELECT funds, festivals, format FROM credits")
        self.assertEqual(self.row("SELECT count(*) AS n FROM credits")["n"], 1)
        self.assertEqual((r["funds"], r["festivals"], r["format"]), ("MiC opere prime 2025, Marche 2025", "Venezia", "short"))

    def test_credit_without_year_is_completed_when_the_year_appears(self):
        census.add_credit(self.cid, "Il Film", "producer", "https://s.example/1")
        census.add_credit(self.cid, "Il Film", "producer", "https://s.example/2", year=2024)
        self.assertEqual([r[0] for r in self.t.conn.execute("SELECT year FROM credits")], [2024])

    def test_different_role_is_a_different_credit(self):
        census.add_credit(self.cid, "Il Film", "producer", "https://s.example/1", year=2025)
        census.add_credit(self.cid, "Il Film", "distributor", "https://s.example/1", year=2025)
        self.assertEqual(self.row("SELECT count(*) AS n FROM credits")["n"], 2)

    def test_rejects_unknown_role_format_and_missing_source(self):
        for kw in ({"role": "regista"}, {"format": "video"}, {"source_url": ""}):
            args = {"role": "producer", "source_url": "https://s.example", **kw}
            with self.assertRaises(ValueError):
                census.add_credit(self.cid, "T", **args)


class TestProfile(Base):
    def test_profile_comes_from_credits(self):
        cid = self.contact("Casa Rossa", country="IT")
        census.add_credit(cid, "A", "producer", "https://s.example", year=2025, format="short", genres="commedia, drammatico", countries="IT, FR", funds="MiC opere prime 2025")
        census.add_credit(cid, "B", "coproducer", "https://s.example", year=2024, format="feature", genres="drammatico", countries="IT, DE", funds="Eurimages 2026", festivals="Locarno")
        census.add_credit(cid, "C", "producer", "https://s.example", year=2020, format="short", funds="opere seconde")
        census.add_credit(cid, "D", "distributor", "https://s.example", year=2023, format="short")
        for i in range(4):
            census.add_credit(cid, f"Titolo {i}", "producer", "https://s.example", year=2022)
        p = census.refresh_profile(cid, this_year=2026)
        self.assertEqual(p["credits"], 8)
        self.assertEqual(p["formats"], {"short": 3, "feature": 1})
        self.assertEqual(p["genres"], {"drammatico": 2, "commedia": 1})
        self.assertEqual((p["first_year"], p["last_year"], p["active_last_3y"]), (2020, 2025, True))
        self.assertEqual((p["first_works"], p["second_works"]), (1, 1))
        self.assertEqual(p["coproduction_countries"], {"FR": 1, "DE": 1})  # il paese della casa non conta
        self.assertEqual(p["funds"], ["Eurimages 2026", "MiC opere prime 2025", "opere seconde"])
        self.assertEqual(p["funds_eur_total"], 0)
        self.assertEqual(len(p["latest_titles"]), 5)
        self.assertEqual(p["latest_titles"][0]["title"], "A")
        self.assertEqual(json.loads(self.row("SELECT profile_json FROM contacts WHERE id=?", cid)["profile_json"])["credits"], 8)

    def test_profile_groups_funds_by_label_and_sums_the_amounts(self):
        cid = self.contact("Casa Verde", country="IT")
        census.add_credit(cid, "A", "producer", "https://s.example", year=2025, funds="MiC Tax Credit 2025 (EUR 1000)")
        census.add_credit(cid, "B", "producer", "https://s.example", year=2025, funds="MiC Tax Credit 2025 (EUR 2500), Marche 2025")
        p = census.refresh_profile(cid, this_year=2026)
        self.assertEqual(p["funds"], ["Marche 2025", "MiC Tax Credit 2025"])
        self.assertEqual(p["funds_eur_total"], 3500)

    def test_profile_without_credits_is_empty_not_an_error(self):
        cid = self.contact("Casa Vuota")
        p = census.refresh_profile(cid, this_year=2026)
        self.assertEqual((p["credits"], p["first_year"], p["active_last_3y"], p["latest_titles"]), (0, None, False, []))


class TestPeople(Base):
    def test_person_needs_name_role_and_source(self):
        cid = self.contact("Casa Rossa")
        with self.assertRaises(ValueError):
            census.add_person(cid, "Anna Bianchi", "produttrice", "")
        with self.assertRaises(ValueError):
            census.add_person(cid, "Anna Bianchi", "produttrice", "https://p.example", email="a@x.example", email_confidence="guessed-valid")

    def test_person_is_saved_once_with_phone_in_e164(self):
        cid = self.contact("Casa Rossa")
        pid = census.add_person(cid, "Anna Bianchi", "produttrice", "https://p.example/team", email="anna@casarossa.example", email_confidence="published", phone="06 1234 5678")
        self.assertIsNotNone(pid)
        self.assertIsNone(census.add_person(cid, "Anna Bianchi", "produttrice", "https://p.example/team"))
        r = self.row("SELECT phone, email_confidence, source_url FROM people WHERE id=?", pid)
        self.assertEqual((r["phone"], r["email_confidence"], r["source_url"]), ("+390612345678", "published", "https://p.example/team"))


class TestDoNotContact(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.contact("Premiere Film", project="walkie-talkie")
        self.other = self.contact("Altra Casa", email="info@altra.example")
        for c in (self.cid, self.other):
            for status in ("draft", "approved", "sent", "cancelled"):
                self.t.conn.execute("INSERT INTO radar_outbox(contact_id, project, to_email, subject, body, status) VALUES(?, 'walkie-talkie', 'x@y.it', 's', 'b', ?)", (c, status))
        self.t.conn.execute("INSERT INTO radar_requests(kind, contact_id, project, status) VALUES('draft', ?, 'walkie-talkie', 'pending')", (self.cid,))
        self.t.conn.commit()

    def test_marks_the_house_cancels_open_drafts_only(self):
        n = census.mark_do_not_contact([self.cid], "non più una priorità")
        self.assertEqual(n, 2)
        self.assertEqual(self.row("SELECT do_not_contact AS d, do_not_contact_reason AS r FROM contacts WHERE id=?", self.cid), {"d": 1, "r": "non più una priorità"})
        st = {r[0]: r[1] for r in self.t.conn.execute("SELECT status, count(*) FROM radar_outbox WHERE contact_id=? GROUP BY 1", (self.cid,))}
        self.assertEqual(st, {"cancelled": 3, "sent": 1})
        other = {r[0]: r[1] for r in self.t.conn.execute("SELECT status, count(*) FROM radar_outbox WHERE contact_id=? GROUP BY 1", (self.other,))}
        self.assertEqual(other, {"draft": 1, "approved": 1, "sent": 1, "cancelled": 1})
        self.assertEqual(self.row("SELECT status FROM radar_requests")["status"], "cancelled")
        self.assertEqual(self.row("SELECT do_not_contact AS d FROM contacts WHERE id=?", self.other)["d"], 0)

    def test_find_by_domain_or_name_catches_every_row_of_the_house(self):
        a = self.contact("[DIST] Premiere Film", email=None)
        b = self.contact("Altro Nome", email=None)
        self.t.conn.execute("UPDATE contacts SET official_url='https://premierefilm.it/it/' WHERE id=?", (b,))
        self.t.conn.commit()
        self.assertEqual(sorted(census.find_by_domain_or_name("premierefilm.it", "Premiere Film")), sorted([self.cid, a, b]))

    def test_send_skips_an_approved_draft_of_a_do_not_contact_house(self):
        for key, value in (("send_enabled", "1"), ("window_start", "0"), ("window_end", "24")):
            self.t.conn.execute("UPDATE radar_settings SET value=? WHERE key=?", (value, key))
        self.t.conn.execute("DELETE FROM radar_outbox WHERE contact_id=?", (self.other,))
        self.t.conn.execute("UPDATE radar_outbox SET status='cancelled' WHERE contact_id=? AND status='draft'", (self.cid,))
        self.t.conn.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (self.cid,))
        self.t.conn.commit()
        with mock.patch("smtplib.SMTP_SSL") as smtp:
            sent, errors, _ = jobs.send()
        self.assertEqual((sent, errors), (0, 0))
        smtp.assert_not_called()

    def test_manual_requests_refuse_a_do_not_contact_house(self):
        self.t.conn.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (self.cid,))
        self.t.conn.commit()
        with self.assertRaises(requests_queue.RequestFailed):
            requests_queue._contact(self.cid, "walkie-talkie")


if __name__ == "__main__":
    unittest.main()
