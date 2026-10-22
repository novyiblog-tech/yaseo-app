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


class PaidReportTest(unittest.TestCase):
    def test_full_audit_shows_paid_sections_instead_of_placeholders(self):
        result = json.loads(FIXTURE.read_text(encoding="utf-8"))
        paid = {"demand": {"status": "ok", "items": [{"query": "окна", "freq": 12906}]},
                "positions": {"status": "ok", "items": [{"query": "окна", "position": 7,
                                                         "top3": ["a.ru", "b.ru"]}]},
                "answers": {"status": "ok", "items": [{"query": "окна", "cited": False,
                                                       "rivals": ["x.ru"]}]}}
        a = score.assess(result, paid=paid)
        html = report.render(result, a, kind="Полный аудит", paid=paid)
        self.assertIn("Полный аудит", html)
        self.assertNotIn("В бесплатной проверке не измеряется", html)
        for needle in ("12\u00a0906", "a.ru, b.ru", "Называет вас", "x.ru"):
            self.assertIn(needle, html)

    def test_free_report_keeps_placeholders(self):
        html = report.render(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.assertIn("В бесплатной проверке не измеряется", html)
        self.assertIn("Бесплатная проверка", html)


class AiBriefTest(unittest.TestCase):
    def setUp(self):
        self.result = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_open_site_without_answers(self):
        brief = report._ai_brief(self.result, None)
        self.assertEqual(brief["readable"][0], "green")
        self.assertIsNone(brief["mention"])
        self.assertTrue(brief["fixes"], "что исправить берётся из находок по нейросетям")
        self.assertLessEqual(len(brief["fixes"]), 4)

    def test_blocked_search_bot_is_red(self):
        for b in self.result["geo"]["bots"]:
            if b["role"] == "search":
                b["allowed"] = False
        brief = report._ai_brief(self.result, None)
        self.assertEqual(brief["readable"][0], "red")
        self.assertIn("закрыт для нейросетей", brief["readable"][1])

    def test_mention_summary_names_rivals(self):
        paid = {"answers": {"items": [{"query": "a", "cited": False, "rivals": ["x.ru", "y.ru"]},
                                      {"query": "b", "cited": False, "rivals": ["x.ru"]}]}}
        text = report._ai_brief(self.result, paid)["mention"][1]
        self.assertIn("ни разу не назвала", text)
        self.assertIn("x.ru, y.ru", text)
        paid["answers"]["items"][0]["cited"] = True
        self.assertEqual(report._ai_brief(self.result, paid)["mention"][1],
                         "Нейросеть Яндекса назвала ваш сайт в 1 из 2 ответов.")

    def test_pdf_expands_developer_block(self):
        html = report.render(self.result, expand=True)
        self.assertIn('<details class="fold" open style="margin-top:24px">', html)
        self.assertNotIn('<details class="fold" open', report.render(self.result).split("Для разработчика")[0][-120:])
