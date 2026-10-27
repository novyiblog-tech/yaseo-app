import json
import unittest
from pathlib import Path

from yaseo_app import report, score, serp_kinds

HERE = Path(__file__).resolve().parent.parent / "src" / "yaseo_app" / "examples"


class KindTest(unittest.TestCase):
    def test_known_and_subdomains(self):
        self.assertEqual(serp_kinds.kind("www.avito.ru"), "агрегатор")
        self.assertEqual(serp_kinds.kind("krasnodar.cian.ru"), "агрегатор")
        self.assertEqual(serp_kinds.kind("2gis.ru"), "справочник")
        self.assertEqual(serp_kinds.kind("obuvmaster.example"), "обычный")

    def test_summary_counts_places(self):
        s = serp_kinds.summarize([{"top3": ["avito.ru", "a.ru", "cian.ru"]}, {"top3": ["avito.ru", "b.ru", "c.ru"]},
                                  {"error": "x"}])
        self.assertEqual((s["total"], s["aggregators"]), (6, 3))
        self.assertEqual(s["names"][0], "avito.ru")

    def test_no_data_is_none(self):
        self.assertIsNone(serp_kinds.summarize([]))


class ReportTest(unittest.TestCase):
    def render(self, top3):
        result = json.loads((HERE / "test-site.json").read_text(encoding="utf-8"))
        paid = {"demand": {"status": "ok", "items": []},
                "positions": {"status": "ok", "items": [{"query": "q", "position": 5, "top3": top3}]},
                "answers": {"status": "ok", "items": []}}
        return report.render(result, score.assess(result, paid=paid), paid=paid)

    def test_note_shown_when_aggregators_in_top(self):
        html = self.render(["avito.ru", "a.ru", "cian.ru"])
        self.assertIn("агрегаторы и справочники", html)
        self.assertIn('class="agg"', html)

    def test_no_note_without_aggregators(self):
        self.assertNotIn("агрегаторы и справочники", self.render(["a.ru", "b.ru", "c.ru"]))


if __name__ == "__main__":
    unittest.main()
