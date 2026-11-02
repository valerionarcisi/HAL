"""Arricchimento: scheda Filmitalia, pagine team/contatti, persone, recapiti. HTML locali, nessuna rete."""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "x")
os.environ.setdefault("EMAIL_ADDRESS", "mittente@example.test")
os.environ.setdefault("EMAIL_PASSWORD", "x")

from radar import census, census_enrich as en, llm, web  # noqa: E402
from tests.test_write_paths import Base  # noqa: E402


def cf(email):
    key = 0x4F
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


FILMITALIA = f"""<html><body><table>
<tr><td><strong>responsabili:</strong></td><td itemprop="x">Saverio Pesapane , Valeria Autorino</td></tr>
<tr><td><strong>indirizzo:</strong></td><td>Via Nazionale Terza Traversa 3 IT-70128, Palese (Bari)</td></tr>
<tr><td><strong>tel:</strong></td><td>+39 080 2460358</td></tr>
<tr><td><strong>email:</strong></td><td><a class="__cf_email__" data-cfemail="{cf('info@rossafilm.example')}">[email protected]</a></td></tr>
<tr><td><strong>sito web:</strong></td><td>www.rossafilm.example/</td></tr></table>
<footer>Cinecittà S.p.A. <a data-cfemail="{cf('filmitalia@cinecitta.com')}">[email protected]</a></footer></body></html>"""

TEAM = """<html><head><script type="application/ld+json">{"@type":"Organization","telephone":"+39 06 1234 5678",
"employee":[{"@type":"Person","name":"Giulia Ferri","jobTitle":"Produttrice esecutiva","email":"giulia.ferri@rossafilm.example"}]}</script></head><body>
<h1>Chi siamo</h1>
<div class="card"><h3>Anna Bianchi</h3><p>Produttrice</p><a href="mailto:anna.bianchi@rossafilm.example">scrivi</a></div>
<div class="card"><h3>Marco De Santis</h3><p>Development &amp; Acquisitions</p></div>
<p>Luca Verdi — Festival Manager</p>
<p>Responsabile distribuzione: Paola Neri</p>
<p>Via Roma 1, Milano</p><p>Film Production Italia</p><p>Chi Siamo</p>
<p>Tel. 02 3456 7890</p><a href="https://forms.gle/abc">Invia il tuo progetto</a>
<p>info@rossafilm.example</p></body></html>"""


class TestParsing(unittest.TestCase):
    def test_cloudflare_email_is_decoded(self):
        self.assertEqual(en.decode_cfemail(cf("info@x.example")), "info@x.example")
        self.assertIsNone(en.decode_cfemail("zz"))

    def test_filmitalia_card(self):
        f = en.parse_filmitalia(FILMITALIA)
        self.assertEqual(f["managers"], ["Saverio Pesapane", "Valeria Autorino"])
        self.assertEqual(f["phone"], "+390802460358")
        self.assertEqual(f["email"], "info@rossafilm.example")  # non quella di Cinecittà
        self.assertEqual(f["website"], "http://www.rossafilm.example")

    def test_filmitalia_listing_url_is_not_a_website(self):
        f = en.parse_filmitalia("<td>sito web:</td><td>https://filmitalia.org/it/production/1/</td>")
        self.assertIsNone(f["website"])

    def test_name_filter(self):
        ok = ["Anna Bianchi", "Marco De Santis", "Jean-Luc Godard", "Maria D'Angelo", "ANNA ROSSI"]
        bad = ["Film Production", "Chi Siamo", "Via Roma", "Anna", "Roma Milano", "Contatti Team", "Info 2024", "Anna Bianchi Rossi Verdi Neri"]
        self.assertTrue(all(en.census_name_ok(n) for n in ok), [n for n in ok if not en.census_name_ok(n)])
        self.assertFalse(any(en.census_name_ok(n) for n in bad), [n for n in bad if en.census_name_ok(n)])

    def test_people_from_cards_pairs_and_labels(self):
        got = {p["name"]: p["role"] for p in en.extract_people(en.html_lines(TEAM), "Rossa Film")}
        self.assertEqual(got["Anna Bianchi"], "Produttrice")
        self.assertEqual(got["Marco De Santis"], "Development & Acquisitions")
        self.assertEqual(got["Luca Verdi"], "Festival Manager")
        self.assertIn("distribuzione", got["Paola Neri"].lower())
        self.assertNotIn("Via Roma", got)
        self.assertNotIn("Chi Siamo", got)

    def test_film_credits_and_site_chrome_are_not_people(self):
        lines = ["Jean-Luc Godard", "Director", "Sara Rajaei - Director of City of Poets", "Angelica Gallo, Regista \"La Notte Brucia\"",
                 "Mappa del Sito", "Your Cart", "THE OPEN REEL International Sales Company Lorem ipsum dolor sit amet"]
        self.assertEqual(en.extract_people(lines), [])

    def test_catalog_pages_are_ignored_but_team_pages_are_read(self):
        c = {"id": 1, "name": "Rossa Film", "site": "https://rossafilm.example/"}
        home = "<html><body><p>Anna Bianchi - Produttrice</p></body></html>"
        crawl = {"final": "https://rossafilm.example/", "error": None, "pages": {"https://rossafilm.example/": home, "https://rossafilm.example/team": "<p>Luca Verdi - Producer</p>"}}
        res = en.enrich_site(c, crawl, use_llm=False)
        self.assertEqual([p["name"] for p in res["people"]], ["Luca Verdi"])

    def test_a_name_without_a_role_is_not_a_person(self):
        self.assertEqual(en.extract_people(["Anna Bianchi", "Via Roma 1", "Milano"]), [])

    def test_jsonld_people_and_org(self):
        self.assertEqual([(p["name"], p["role"]) for p in en.jsonld_people(TEAM)], [("Giulia Ferri", "Produttrice esecutiva")])
        self.assertEqual(en.jsonld_org(TEAM)["phone"], "+39 06 1234 5678")

    def test_phones_normalized_from_tel_links_and_labels(self):
        h = '<a href="tel:+39%2006%201234%205678">x</a><p>Tel. 02 3456 7890</p><p>Fax 02 1111 2222</p>'
        self.assertEqual(en.page_phones(h), ["+390612345678", "+390234567890"])

    def test_submission_url(self):
        self.assertEqual(en.find_submission_url("https://rossafilm.example/", TEAM), "https://forms.gle/abc")
        self.assertIsNone(en.find_submission_url("https://rossafilm.example/", "<a href='/chi-siamo'>Chi siamo</a>"))


class TestEmailRules(unittest.TestCase):
    def test_pattern_needs_a_published_personal_address_with_the_same_shape(self):
        pat = en.email_pattern([("Anna Bianchi", "anna.bianchi@rossafilm.example")])
        self.assertEqual(en.apply_pattern(pat, "Marco Santis"), "marco.santis@rossafilm.example")
        self.assertIsNone(en.email_pattern([("Anna Bianchi", "info@rossafilm.example")]))

    def test_free_mail_never_makes_a_pattern(self):
        self.assertIsNone(en.email_pattern([("Anna Bianchi", "anna.bianchi@gmail.com")]))

    def test_a_generic_company_address_next_to_a_name_is_not_the_persons(self):
        lines = ["Federico Fasulo", "Artistic Director", "films@rossafilm.example"]
        self.assertIsNone(en.person_email_near("Federico Fasulo", lines, {"films@rossafilm.example"}))

    def test_email_next_to_the_name_is_published(self):
        lines = ["Anna Bianchi", "Produttrice", "anna@rossafilm.example"]
        self.assertEqual(en.person_email_near("Anna Bianchi", lines, {"anna@rossafilm.example"}), "anna@rossafilm.example")
        self.assertIsNone(en.person_email_near("Marco De Santis", lines, {"anna@rossafilm.example"}))


class TestEnrichSite(Base):
    def crawl(self, **extra):
        return {"final": "https://rossafilm.example/", "error": None, "pages": {"https://rossafilm.example/chi-siamo": TEAM}, **extra}

    def contact_row(self):
        cid = self.contact("Rossa Film", email=None)
        self.t.conn.execute("UPDATE contacts SET official_url='https://rossafilm.example/' WHERE id=?", (cid,))
        self.t.conn.commit()
        return {"id": cid, "name": "Rossa Film", "site": "https://rossafilm.example/"}

    def test_people_have_source_role_and_email_confidence(self):
        c = self.contact_row()
        with mock.patch.object(web, "has_mx", return_value=True):
            res = en.enrich_site(c, self.crawl(), use_llm=False)
        by = {p["name"]: p for p in res["people"]}
        self.assertEqual((by["Anna Bianchi"]["email"], by["Anna Bianchi"]["confidence"]), ("anna.bianchi@rossafilm.example", "published"))
        self.assertEqual((by["Giulia Ferri"]["email"], by["Giulia Ferri"]["confidence"]), ("giulia.ferri@rossafilm.example", "published"))
        # Marco non ha email pubblicata: la ricava dallo schema nome.cognome e resta «pattern»
        self.assertEqual(by["Marco De Santis"]["confidence"], "pattern")
        self.assertTrue(by["Marco De Santis"]["email"].endswith("@rossafilm.example"))
        self.assertTrue(all(p["source_url"] and p["role"] for p in res["people"]))
        self.assertEqual((res["email"], res["submission_url"]), ("info@rossafilm.example", "https://forms.gle/abc"))

    def test_pattern_email_is_dropped_when_the_domain_has_no_mx(self):
        c = self.contact_row()
        with mock.patch.object(web, "has_mx", return_value=False):
            res = en.enrich_site(c, self.crawl(), use_llm=False)
        marco = next(p for p in res["people"] if p["name"] == "Marco De Santis")
        self.assertIsNone(marco["email"])

    def test_apply_fills_only_empty_fields_adds_people_once_and_stamps_verification(self):
        c = self.contact_row()
        self.t.conn.execute("UPDATE contacts SET phone='+390111111111' WHERE id=?", (c["id"],))
        self.t.conn.commit()
        with mock.patch.object(web, "has_mx", return_value=True):
            res = en.enrich_site(c, self.crawl(), use_llm=False)
        n = en.apply_result(c, res)
        self.assertGreaterEqual(n, 4)
        r = self.row("SELECT email, phone, submission_url, last_verified_at, verification_level FROM contacts WHERE id=?", c["id"])
        self.assertEqual((r["email"], r["phone"], r["submission_url"]), ("info@rossafilm.example", "+390111111111", "https://forms.gle/abc"))
        self.assertIsNotNone(r["last_verified_at"])
        before = self.row("SELECT count(*) AS n FROM people")["n"]
        en.apply_result(c, res)
        self.assertEqual(self.row("SELECT count(*) AS n FROM people")["n"], before)

    def test_unreachable_site_is_logged_not_fatal(self):
        c = self.contact_row()
        res = en.enrich_site(c, {"final": "x", "error": "HTTP Error 404", "pages": {}})
        en.apply_result(c, res)
        self.assertEqual(self.row("SELECT kind FROM radar_events WHERE contact_id=?", c["id"])["kind"], "site_unreachable")
        self.assertIsNotNone(self.row("SELECT last_verified_at AS v FROM contacts WHERE id=?", c["id"])["v"])

    def test_model_fallback_keeps_only_names_the_page_confirms(self):
        c = self.contact_row()
        page = "<html><body><h1>Il nostro team</h1><div>Sara Conti è la nostra head of development</div><div>Altro testo</div></body></html>"
        crawl = {"final": "https://rossafilm.example/", "error": None, "pages": {"https://rossafilm.example/team": page}}
        ask = lambda s, u: {"people": [{"name": "Sara Conti", "role": "head of development"}, {"name": "Mario Inventato", "role": "producer"}]}  # noqa: E731
        with mock.patch.object(web, "has_mx", return_value=True):
            res = en.enrich_site(c, crawl, ask=ask)
        self.assertEqual([p["name"] for p in res["people"]], ["Sara Conti"])
        self.assertEqual(res["llm_calls"], 1)

    def test_model_budget_exhausted_is_reported_not_raised(self):
        c = self.contact_row()
        page = "<html><body><h1>Team</h1><div>Sara Conti head of development</div></body></html>"
        crawl = {"final": "https://rossafilm.example/", "error": None, "pages": {"https://rossafilm.example/team": page}}
        def ask(s, u):
            raise llm.BudgetExceeded("tetto")
        res = en.enrich_site(c, crawl, ask=ask)
        self.assertTrue(res.get("budget_exhausted"))


class TestFilmitaliaApply(Base):
    def test_site_becomes_a_source_and_fills_empty_url_only(self):
        cid = self.contact("Rossa Film", email=None)
        self.t.conn.execute("UPDATE contacts SET official_url='https://filmitalia.org/it/production/1/' WHERE id=?", (cid,))
        self.t.conn.commit()
        f = en.parse_filmitalia(FILMITALIA)
        n = en.apply_filmitalia({"id": cid}, "https://filmitalia.org/it/production/1/", f)
        self.assertEqual(n, 2)
        r = self.row("SELECT email, phone, official_url FROM contacts WHERE id=?", cid)
        self.assertEqual((r["email"], r["phone"]), ("info@rossafilm.example", "+390802460358"))
        self.assertEqual(r["official_url"], "https://filmitalia.org/it/production/1/")  # non riscritto
        self.assertEqual(self.row("SELECT url FROM contact_sources WHERE source_name='Sito ufficiale'")["url"], "http://www.rossafilm.example")
        p = self.row("SELECT role, source_url, email_confidence FROM people WHERE name='Saverio Pesapane'")
        self.assertEqual((p["role"], p["email_confidence"]), ("responsabile", None))


class TestCandidates(Base):
    def test_order_and_exclusions(self):
        a = self.contact("Distributore Corti", email=None)
        b = self.contact("Casa Con Corti", email=None)
        c = self.contact("Casa Qualunque", email=None)
        d = self.contact("Casa Esclusa", email=None)
        for cid, kind, prof in ((a, "short_distribution", {"credits": 1}), (b, "production", {"credits": 3, "formats": {"short": 2}}),
                                (c, "production", {"credits": 1}), (d, "production", {"credits": 9})):
            self.t.conn.execute("UPDATE contacts SET kind=?, profile_json=?, official_url='https://x.example' WHERE id=?", (kind, json.dumps(prof), cid))
        self.t.conn.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (d,))
        self.t.conn.commit()
        self.assertEqual([r["id"] for r in en.candidates(10)], [a, b, c])


if __name__ == "__main__":
    unittest.main()
