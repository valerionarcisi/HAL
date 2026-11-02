"""Distributori di corti e graduatorie Marche. HTML e testi di prova locali, modello finto, nessuna rete."""
import os
import unittest

os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "x")
os.environ.setdefault("EMAIL_ADDRESS", "mittente@example.test")
os.environ.setdefault("EMAIL_PASSWORD", "x")

from radar import census, census_it, llm  # noqa: E402
from tests.test_write_paths import Base  # noqa: E402


def card(name, url, text):
    return (f'<div class="vc_grid-item vc_clearfix vc_col-sm-4"><div class="vc_grid-item-mini"><div class="vc_gitem-animated-block">'
            f'<a href="{url}#new_tab" title="{name}" class="vc_gitem-link vc-zone-link" ></a>'
            f'<h6 style="x" >{name}</h6><div class="vc_gitem-post-data-source-post_excerpt" ><p style="y" ><p>{text}</p> </p></div>'
            f'<a href="{url}#new_tab" title="{name}" class="vc_gitem-link vc-zone-link" ></a></div></div></div>')


HTML = ("<html>" + card("Sayonara Film", "https://www.sayonarafilm.com/", "(ex Elenfant Distribution) Bologna")
        + card("Gargantua", "https://www.gargantuafilm.it/", "Roma, Milano")
        + card("Allegorie Distribution", "https://www.instagram.com/allegorie/", "Bologna") + "</html>")


class TestDistributors(Base):
    def test_parse_reads_name_site_city_and_note_per_card(self):
        items = census_it.parse_centro_del_corto(HTML)
        self.assertEqual([i["name"] for i in items], ["Sayonara Film", "Gargantua", "Allegorie Distribution"])
        self.assertEqual((items[0]["city"], items[0]["note"]), ("Bologna", "ex Elenfant Distribution"))
        self.assertEqual(items[1]["city"], "Roma")
        self.assertEqual(items[1]["url"], "https://www.gargantuafilm.it/")

    def test_run_creates_short_distributors_with_source_and_is_idempotent(self):
        new, errors, _ = census_it.run_distributors(HTML)
        self.assertEqual((new, errors), (3, 0))
        r = self.row("SELECT kind, country, city, official_url FROM contacts WHERE name='Gargantua'")
        self.assertEqual((r["kind"], r["country"], r["city"]), ("short_distribution", "IT", "Roma"))
        self.assertIsNone(self.row("SELECT official_url FROM contacts WHERE name='Allegorie Distribution'")["official_url"])  # Instagram non è il sito
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_sources")["n"], 3)
        self.assertEqual(census_it.run_distributors(HTML)[0], 0)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contacts")["n"], 3)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_projects")["n"], 0)

    def test_changed_page_is_an_error_not_silence(self):
        self.assertEqual(census_it.run_distributors("<html>niente</html>")[:2], (0, 1))

    def test_existing_house_gets_kind_filled_but_nothing_overwritten(self):
        cid = self.contact("Gargantua", email="info@gargantuafilm.it", country="IT")
        census_it.run_distributors(HTML)
        r = self.row("SELECT kind, email FROM contacts WHERE id=?", cid)
        self.assertEqual((r["kind"], r["email"]), ("short_distribution", "info@gargantuafilm.it"))


TEXT = """ALLEGATO B - GRADUATORIA DEFINITIVA LUNGOMETRAGGI
     70652            NIGHTSWIM SRL        L'ESTATE PEGGIORE    GIANLUCA   Lungometraggio   1.437.500,00 €   300.000,00 €   300.000,00 €   78,9   AMMESSA A
                                           DELLA MIA VITA       SANTONI                                                                         FINANZIAMENTO
     69430   RESET SRL             AMICI PER CASO 2    MAX NADARI       Lungometraggio   24     NON FINANZIABILE PER MANCATO RAGGIUNGIMENTO
"""
ROWS = [
    {"id": "70652", "company": "NIGHTSWIM SRL", "title": "L'ESTATE PEGGIORE DELLA MIA VITA", "director": "GIANLUCA SANTONI", "kind": "lungometraggio", "amount": 300000.0, "funded": True},
    {"id": "69430", "company": "RESET SRL", "title": "AMICI PER CASO 2", "director": "MAX NADARI", "kind": "lungometraggio", "amount": None, "funded": False},
]


class TestMarche(Base):
    def test_verify_keeps_rows_the_text_confirms_and_drops_invented_ones(self):
        invented = [
            {**ROWS[0], "id": "99999"},                                          # ID che non c'è
            {**ROWS[0], "company": "TOTALMENTE INVENTATA SRL"},                  # società che non c'è
            {**ROWS[0], "amount": 123456.0},                                     # importo che non c'è
        ]
        ok, rejected = census_it.verify_rows(ROWS + invented, TEXT)
        self.assertEqual([r["id"] for r in ok], ["70652", "69430"])
        self.assertEqual(len(rejected), 3)

    def test_plan_only_funded_applications_become_credits(self):
        p = census_it.plan_marche(ROWS, 2025, "https://f.example/a.pdf")
        self.assertEqual(len(p["credits"]), 1)
        c = p["credits"][0]
        self.assertEqual((c["role"], c["format"], c["year"]), ("producer", "feature", 2025))
        self.assertEqual(c["funds"], "Marche Film Fund PR FESR 2025 (EUR 300000)")
        self.assertEqual(set(p["companies"]), {census.normalize_name("NIGHTSWIM SRL")})

    def test_run_with_fake_model_loads_and_counts_rejections(self):
        bad = {**ROWS[0], "id": "11111", "title": "NON ESISTE"}
        ask = lambda system, user: {"rows": ROWS + [bad]}  # noqa: E731
        new, errors, note = census_it.run_marche({2025: [("https://f.example/a.pdf", TEXT)]}, ask)
        self.assertEqual((new, errors), (1, 1))
        self.assertIn("1 scartate", note)
        self.assertEqual(self.row("SELECT count(*) AS n FROM credits")["n"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_projects")["n"], 0)

    def test_budget_exhausted_stops_without_crashing(self):
        def ask(system, user):
            raise llm.BudgetExceeded("tetto giornaliero raggiunto (200/200 chiamate al modello)")
        new, errors, note = census_it.run_marche({2025: [("https://f.example/a.pdf", TEXT)]}, ask)
        self.assertEqual((new, errors), (0, 1))
        self.assertIn("tetto", note)


class TestCensusBudget(Base):
    def test_census_budget_is_counted_apart_from_the_automatic_one(self):
        from unittest import mock
        self.t.conn.execute("UPDATE radar_settings SET value='2' WHERE key='census_llm_daily_calls'")
        self.t.conn.commit()
        today, n = llm._check_budget("census")
        llm._count_call(today, n, "census")
        llm._count_call(today, n + 1, "census")
        with self.assertRaises(llm.BudgetExceeded):
            llm._check_budget("census")
        self.assertEqual(llm._check_budget(True)[1], 0)  # il tetto dei lavori automatici è intatto


if __name__ == "__main__":
    unittest.main()
