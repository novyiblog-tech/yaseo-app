"""Отчёт собирается из результата аудита; стили не экранируются, ключевые разделы на месте."""
import json
import unittest
from pathlib import Path

from yaseo_app import report, score

FIXTURE = Path(__file__).parent / "fixtures" / "free-audit-result.json"


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.html = report.render(cls.result)

    def test_css_not_escaped(self):
        style = self.html.split("<style>")[1].split("</style>")[0]
        self.assertIn('"Manrope"', style)
        self.assertNotIn("&#34;", style)
        self.assertNotIn("&gt;", style)

    def test_sections_present(self):
        for text in ("Что сделать первым", "Спрос", "Позиции и конкуренты", "Нейросети",
                     "Техника по страницам", "Как считали", "не измеряется"):
            self.assertIn(text, self.html, text)

    def test_score_and_steps_rendered(self):
        a = score.assess(self.result)
        # цифра окрашена по светофору: красная оценка не должна быть чёрной
        self.assertIn(f'<div class="num {score.color_of(a.total)}">{a.total}</div>', self.html)
        for s in a.steps:
            self.assertIn(s.title, self.html)

    def test_evidence_quoted_verbatim(self):
        first = self.result["audit"]["issues"][0]
        self.assertIn(first["evidence"].replace('"', "&#34;"), self.html)

    def test_plural(self):
        self.assertEqual([report.plural(n, "шаг", "шага", "шагов") for n in (1, 3, 5, 11, 21, 22)],
                         ["шаг", "шага", "шагов", "шагов", "шаг", "шага"])

    def test_back_link_only_in_cabinet_view(self):
        self.assertNotIn('class="back"', self.html)
        html = report.render(self.result, back_url="/jobs/7")
        self.assertIn('href="/jobs/7"', html)

    def test_pages_word(self):
        self.assertEqual(report._pages_word(1), "страница")
        self.assertEqual(report._pages_word(3), "страницы")
        self.assertEqual(report._pages_word(11), "страниц")
        self.assertEqual(report._pages_word(25), "страниц")


if __name__ == "__main__":
    unittest.main()
