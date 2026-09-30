"""Оценка и словарь: проверяются на синтетических находках и на кодах из движка."""
import re
import unittest
from pathlib import Path

from yaseo_app import glossary, score


def issue(sev, code, url="https://site.test/", evidence="ev", fix="fix"):
    return {"severity": sev, "code": code, "url": url, "evidence": evidence, "fix": fix}


def result(tech_issues, geo_issues, pages=10, geo_pages=3):
    return {
        "audit": {"pages_crawled": pages, "issues": tech_issues, "error": None},
        "geo": {"issues": geo_issues, "pages": [{"url": "x"}] * geo_pages,
                "bots": [{"token": "GPTBot", "role": "training"},
                         {"token": "OAI-SearchBot", "role": "search"}]},
    }


class ScoreTest(unittest.TestCase):
    def test_clean_site_is_green(self):
        a = score.assess(result([], []))
        self.assertEqual([l.score for l in a.lights], [100, None, 100])
        self.assertEqual(a.total, 100)
        self.assertEqual(a.lights[1].color, "grey")

    def test_penalty_scales_with_severity(self):
        one_minor = score.tech_light(result([issue("minor", "images-no-alt")], [])["audit"])
        one_crit = score.tech_light(result([issue("critical", "page-unavailable")], [])["audit"])
        self.assertEqual(one_minor.score, 99)
        self.assertEqual(one_crit.score, 90)

    def test_informational_rows_do_not_count(self):
        r = result([issue("minor", "noindex-not-checked")], [])
        self.assertEqual(score.tech_light(r["audit"]).score, 100)

    def test_bad_site_is_red_and_floor_is_zero(self):
        issues = [issue("critical", "page-unavailable", f"https://site.test/{i}") for i in range(30)]
        light = score.tech_light(result(issues, [], pages=5)["audit"])
        self.assertEqual(light.score, 0)
        self.assertEqual(light.color, "red")

    def test_total_ignores_unmeasured(self):
        a = score.assess(result([issue("major", "missing-h1")], []))
        self.assertEqual(a.lights[0].score, 97)
        self.assertEqual(a.total, round((0.4 * 97 + 0.3 * 100) / 0.7))

    def test_unreachable_site_is_grey(self):
        r = {"audit": {"pages_crawled": 0, "issues": [], "error": "нет ответа"},
             "geo": {"issues": [issue("critical", "page-unavailable")], "pages": [], "bots": []}}
        a = score.assess(r)
        self.assertIsNone(a.total)
        self.assertEqual([l.color for l in a.lights], ["grey"] * 3)

    def test_steps_grouped_and_ordered(self):
        r = result(
            [issue("minor", "images-no-alt", "https://site.test/a"),
             issue("minor", "images-no-alt", "https://site.test/b"),
             issue("major", "missing-description", "https://site.test/a"),
             issue("critical", "broken-internal-link", "https://site.test/c")],
            [issue("critical", "robots-blocks-oai-searchbot"),
             issue("minor", "robots-blocks-gptbot")])
        steps = score.build_steps(r)
        self.assertEqual([s.code for s in steps][:2],
                         ["broken-internal-link", "robots-blocks-oai-searchbot"])
        alt = next(s for s in steps if s.code == "images-no-alt")
        self.assertEqual(alt.pages, 2)
        self.assertEqual(alt.who, glossary.SELF)
        gpt = next(s for s in steps if s.code == "robots-blocks-gptbot")
        self.assertEqual(gpt.title, glossary.AI["robots-blocks-training"].title)

    def test_steps_limit(self):
        issues = [issue("minor", code) for code in list(glossary.TECH)[:12]
                  if code not in glossary.INFORMATIONAL]
        self.assertEqual(len(score.build_steps(result(issues, []), limit=7)), 7)


class GlossaryCoversEngineTest(unittest.TestCase):
    """Положительный контроль: коды берутся из исходников установленного движка."""

    @staticmethod
    def engine_codes(module_path: str) -> set[str]:
        import yaseo
        src = (Path(yaseo.__file__).parent / module_path).read_text(encoding="utf-8")
        return set(re.findall(r'Issue\(\s*"(?:critical|major|minor)",\s*"([a-z0-9а-я-]+)"', src)) | \
            set(re.findall(r'Issue\(\s*"[a-z]+" if [^,]+ else "[a-z]+",\s*"([a-z0-9-]+)"', src)) | \
            set(re.findall(r'Issue\(\s*\n?\s*"(?:critical|major|minor)",\s*"([a-z0-9-]+)"', src))

    def test_engine_has_codes(self):
        self.assertGreaterEqual(len(self.engine_codes("audit.py")), 20)
        self.assertGreaterEqual(len(self.engine_codes("geo/readiness.py")), 15)

    def test_every_tech_code_described(self):
        missing = self.engine_codes("audit.py") - set(glossary.TECH)
        self.assertEqual(missing, set())

    def test_every_ai_code_described(self):
        codes = self.engine_codes("geo/readiness.py")
        missing = {c for c in codes if glossary.ai_entry(c, "search") is None}
        self.assertEqual(missing, set())

    def test_entries_are_plain_and_complete(self):
        for name, table in (("TECH", glossary.TECH), ("AI", glossary.AI)):
            for code, e in table.items():
                for f in ("title", "problem", "action", "who", "time", "effect"):
                    self.assertTrue(getattr(e, f), f"{name}:{code}:{f}")
                self.assertIn(e.who, (glossary.SELF, glossary.DEV, glossary.COPY), code)


if __name__ == "__main__":
    unittest.main()
