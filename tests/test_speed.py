import unittest

from yaseo_app import speed


def reply(perf=0.42, lcp=5800, cls=0.31, tbt=900):
    return {"lighthouseResult": {"categories": {"performance": {"score": perf}}, "audits": {
        "largest-contentful-paint": {"numericValue": lcp},
        "cumulative-layout-shift": {"numericValue": cls},
        "total-blocking-time": {"numericValue": tbt}}}}


class SpeedTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(speed.parse(reply()), {"status": "ok", "strategy": "mobile", "score": 42,
                                                "lcp_s": 5.8, "cls": 0.31, "tbt_ms": 900})

    def test_no_score_is_unavailable(self):
        self.assertEqual(speed.parse({})["status"], "unavailable")

    def test_slow_site_gets_two_issues(self):
        codes = {i["code"]: i["severity"] for i in speed.issues(speed.parse(reply()), "https://a.ru/")}
        self.assertEqual(codes, {"slow-mobile": "major", "layout-shift": "major"})

    def test_middling_is_minor_and_fast_is_silent(self):
        mid = speed.issues(speed.parse(reply(perf=0.7, cls=0.05)), "https://a.ru/")
        self.assertEqual([(i["code"], i["severity"]) for i in mid], [("slow-mobile", "minor")])
        self.assertEqual(speed.issues(speed.parse(reply(perf=0.95, cls=0.0)), "https://a.ru/"), [])

    def test_failure_never_raises(self):
        def boom(url, key):
            raise TimeoutError("медленно")
        res = speed.check("https://a.ru/", fetcher=boom)
        self.assertEqual(res["status"], "unavailable")
        self.assertEqual(speed.issues(res, "https://a.ru/"), [])


if __name__ == "__main__":
    unittest.main()
