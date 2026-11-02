"""Parser DGCA e caricamento nel database, con un CSV di prova (nessuna rete)."""
import os
import unittest

os.environ.setdefault("TURSO_DATABASE_URL", "libsql://test.invalid")
os.environ.setdefault("TURSO_AUTH_TOKEN", "x")
os.environ.setdefault("EMAIL_ADDRESS", "mittente@example.test")
os.environ.setdefault("EMAIL_PASSWORD", "x")

from radar import census, census_dgca  # noqa: E402
from tests.test_write_paths import Base  # noqa: E402

URL = "https://cinema.cultura.gov.it/tempcsv/dbaiuti/DGCA-DB-aiuti-2025.csv"
HEADER = ("AIUTO;SETTORE;ANNO;TITOLO;REGISTA;DESTINAZIONE;DURATA (H:M);SOGGETTO RICHIEDENTE;"
          "IMPORTO DEL CONTRIBUTO ASSEGNATO;TIPOLOGIA;NAZIONALITA;PRODUTTORE;DISTRIBUTORE;")
ROWS = [
    # corto, contributo selettivo, due produttori
    "Contributi selettivi produzione;Contributi selettivi - Produzione - Cortometraggi;2025;IL CORTO;Anna Verdi;Cinema; 0:15;ROSSA FILM SRL;20.000,00;Finzione;Italia;ROSSA FILM SRL,BLU PRODUZIONI S.R.L.;;",
    # opera prima o seconda, campo SETTORE con testo doppiamente codificato
    "Contributi selettivi produzione;Contributi selettivi - Produzione - lungometraggi opere prime o seconde;2025;UN LUNGO;Mario Neri;Cinema; 1:34;ROSSA FILM SRL;200.000,00;Finzione;Francia, Italia;ROSSA FILM SRL,Gapbusters;ZETA DISTRIBUZIONE SRL;",
    # distribuzione: il richiedente è un distributore
    "Contributi automatici distribuzione;Contributi automatici per la distribuzione nazionale cinematografica;2025;UN LUNGO;Mario Neri;Cinema; 1:34;ZETA DISTRIBUZIONE SRL;48.257,59;Finzione;Italia;ROSSA FILM SRL;ZETA DISTRIBUZIONE SRL;",
    # distribuzione internazionale: sales
    "Contributi automatici distribuzione internazionale;Contributi automatici per la distribuzione internazionale di opere cinematografiche, televisive o web;2025;UN LUNGO;Mario Neri;Cinema; 1:34;ALFA SALES SRL;10.000,00;Finzione;Italia;ROSSA FILM SRL;ALFA SALES SRL;",
    # videogioco: non è cinema
    "Tax Credit Produzione;Tax credit produzione di videogiochi;2025;GIOCO;;Videogioco; ;GAME SRL;5.000,00;Videogioco;Italia;GAME SRL;;",
    # nome con virgola dentro: «BRAVO, S.R.L.»
    "Contributi automatici produzione;Contributi automatici per la produzione di opere cinematografiche;2025;ALTRO;Chi Lo So;Cinema; 1:20;CARTA SRL;1.000,00;Documentario;Italia;CARTA SRL,BRAVO, S.R.L.;;",
]


def sample(mojibake=True):
    # Come nel file vero: righe in cp1252 e un campo già in UTF-8 (qualità = c3 a0).
    head = ("Ultimo aggiornamento 02-11-2026;\r\n\r\nNB: dati autodichiarati dall'ufficio società perché è così;\r\n\r\n" + HEADER + "\r\n" + "\r\n".join(ROWS) + "\r\n").encode("cp1252")
    extra = ("Contributi selettivi produzione;Contributi selettivi - Produzione - Documentari di particolare qualità artistica;2025;DOCU;Lui;Cinema; 1:10;ROSSA FILM SRL;30.000,00;Documentario;Italia;ROSSA FILM SRL;;\r\n").encode("utf-8")
    return head + extra


class TestParse(unittest.TestCase):
    def setUp(self):
        self.rows = census_dgca.parse_csv(sample())

    def test_skips_notes_and_reads_every_row(self):
        self.assertEqual(len(self.rows), 7)
        self.assertEqual(self.rows[0]["TITOLO"], "IL CORTO")

    def test_repairs_double_encoded_text(self):
        self.assertIn("qualità artistica", self.rows[-1]["SETTORE"])

    def test_missing_header_is_an_error_not_silent(self):
        with self.assertRaises(ValueError):
            census_dgca.parse_csv(b"Pagina cambiata;\r\n<html>")

    def test_amount_minutes_and_countries(self):
        self.assertEqual(census_dgca.parse_amount("189.553,08"), 189553)
        self.assertIsNone(census_dgca.parse_amount("n.d."))
        self.assertEqual(census_dgca.parse_minutes(" 1:34"), 94)
        self.assertIsNone(census_dgca.parse_minutes(" "))
        self.assertEqual(census_dgca.countries_of("Francia, Italia, Rep. Ceca"), "FR, IT, CZ")

    def test_split_names_keeps_legal_form_with_its_company(self):
        self.assertEqual(census_dgca.split_names("CARTA SRL,BRAVO, S.R.L.,ZETA"), ["CARTA SRL", "BRAVO, S.R.L.", "ZETA"])

    def test_format_and_role(self):
        fmt = [census_dgca.credit_format(r) for r in self.rows]
        self.assertEqual(fmt, ["short", "feature", "feature", "feature", None, "documentary", "documentary"])
        self.assertEqual(census_dgca.aid_role(self.rows[2]), ("distributor", "theatrical_distribution"))
        self.assertEqual(census_dgca.aid_role(self.rows[3]), ("sales", "sales_agent"))
        self.assertEqual(census_dgca.aid_role(self.rows[0]), ("producer", "production"))

    def test_fund_label_has_no_commas_so_it_survives_splitting(self):
        label = census_dgca.fund_label(self.rows[1])
        self.assertEqual(label, "MiC Contributi selettivi produzione opere prime o seconde 2025 (EUR 200000)")
        self.assertNotIn(",", label)


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.p = census_dgca.plan(census_dgca.parse_csv(sample()), URL)

    def test_roles_funds_and_no_videogames(self):
        by = {}
        for c in self.p["credits"]:
            by.setdefault((c["company"], c["role"]), []).append(c)
        self.assertNotIn("game", {c["company"] for c in self.p["credits"]})
        zeta = by[(census.normalize_name("ZETA DISTRIBUZIONE SRL"), "distributor")]
        self.assertTrue(any("EUR 48257" in (c["funds"] or "") for c in zeta))
        alfa = by[(census.normalize_name("ALFA SALES SRL"), "sales")]
        self.assertEqual(len(alfa), 1)

    def test_applicant_is_italian_other_producers_have_no_country(self):
        comps = self.p["companies"]
        self.assertEqual(comps[census.normalize_name("ROSSA FILM SRL")]["country"], "IT")
        self.assertIsNone(comps[census.normalize_name("Gapbusters")]["country"])

    def test_only_the_applicant_gets_the_funds(self):
        blu = [c for c in self.p["credits"] if c["company"] == census.normalize_name("BLU PRODUZIONI S.R.L.")]
        self.assertEqual([c["funds"] for c in blu], [None])

    def test_applicant_with_a_too_short_name_does_not_crash_the_plan(self):
        rows = census_dgca.parse_csv(sample())
        rows[0]["SOGGETTO RICHIEDENTE"] = "X"
        p = census_dgca.plan(rows, URL)
        self.assertTrue(all(c["company"] for c in p["credits"]))

    def test_every_company_has_a_source(self):
        self.assertTrue(all(c["sources"] == {URL} for c in self.p["companies"].values()))


class TestLoad(Base):
    def run_load(self):
        return census_dgca.load(census_dgca.plan(census_dgca.parse_csv(sample()), URL))

    def test_load_creates_houses_credits_sources_and_profiles_but_no_projects(self):
        st = self.run_load()
        self.assertGreater(st["companies_new"], 5)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_projects")["n"], 0)
        rossa = self.row("SELECT * FROM contacts WHERE name='ROSSA FILM SRL'")
        self.assertEqual((rossa["country"], rossa["kind"], rossa["source_system"]), ("IT", "production", "census:dgca"))
        self.assertEqual(self.row("SELECT count(*) AS n FROM contact_sources WHERE contact_id=?", rossa["id"])["n"], 1)
        import json
        prof = json.loads(rossa["profile_json"])
        self.assertEqual(prof["credits"], 3)  # il corto, un lungo (una volta sola anche se compare in due aiuti), il documentario
        self.assertEqual(prof["first_or_second_works"], 1)
        self.assertEqual(prof["formats"]["short"], 1)

    def test_running_twice_changes_nothing(self):
        self.run_load()
        counts = [self.row(f"SELECT count(*) AS n FROM {t}")["n"] for t in ("contacts", "credits", "contact_sources")]
        st = self.run_load()
        self.assertEqual((st["companies_new"], st["credits_new"], st["credits_merged"]), (0, 0, 0))
        self.assertEqual(counts, [self.row(f"SELECT count(*) AS n FROM {t}")["n"] for t in ("contacts", "credits", "contact_sources")])

    def test_existing_house_is_matched_by_name_and_not_rewritten(self):
        old = self.contact("Rossa Film S.r.l.", email="info@rossa.example", country="IT")
        self.t.conn.execute("UPDATE contacts SET kind='sales_agent' WHERE id=?", (old,))
        self.t.conn.commit()
        st = self.run_load()
        self.assertGreaterEqual(st["companies_matched"], 1)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contacts WHERE name LIKE 'ROSSA%' OR name LIKE 'Rossa%'")["n"], 1)
        r = self.row("SELECT email, kind, name FROM contacts WHERE id=?", old)
        self.assertEqual((r["email"], r["kind"], r["name"]), ("info@rossa.example", "sales_agent", "Rossa Film S.r.l."))
        self.assertGreater(self.row("SELECT count(*) AS n FROM credits WHERE contact_id=?", old)["n"], 0)

    def test_dry_run_writes_nothing(self):
        census_dgca.load(census_dgca.plan(census_dgca.parse_csv(sample()), URL), dry_run=True)
        self.assertEqual(self.row("SELECT count(*) AS n FROM contacts")["n"], 0)


class TestRun(Base):
    def test_job_reads_local_files_registers_the_source_and_reports(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            for y in (2024, 2025):
                with open(os.path.join(d, f"DGCA-DB-aiuti-{y}.csv"), "wb") as f:
                    f.write(sample())
            found, errors, note = census_dgca.run("2024,2025", d)
        self.assertEqual(errors, 0)
        self.assertGreater(found, 5)
        self.assertIn("2024: 7 righe", note)
        src = self.row("SELECT kind, project FROM radar_sources WHERE name=?", census_dgca.SOURCE_NAME)
        self.assertEqual((src["kind"], src["project"]), ("census", "census"))
        self.assertEqual(self.row("SELECT count(DISTINCT url) AS n FROM contact_sources")["n"], 2)

    def test_unreachable_year_is_counted_as_error_not_a_crash(self):
        from unittest import mock
        with mock.patch("radar.web.fetch_bytes", return_value=(b"", "HTTP Error 404")):
            self.assertEqual(census_dgca.run("2024", "")[:2], (0, 1))

    def test_person_rows_are_not_matched_through_their_company_field(self):
        pid = self.contact("Stefano Centini", email=None)
        self.t.conn.execute("UPDATE contacts SET company='ROSSA FILM SRL' WHERE id=?", (pid,))
        self.t.conn.commit()
        self.assertIsNone(census.CompanyIndex().find("ROSSA FILM SRL"))


if __name__ == "__main__":
    unittest.main()
