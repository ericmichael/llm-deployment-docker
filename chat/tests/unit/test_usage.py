from decimal import Decimal

from django.test import SimpleTestCase

from chat import services_usage


def day(date, users, models, spend):
    return {
        "date": date, "spend": spend, "requests": len(models), "tokens": 10 * len(models),
        "users": {u: {"spend": s, "requests": 1, "tokens": 10} for u, s in users.items()},
        "models": {m: {"spend": s, "requests": 1, "tokens": 10} for m, s in models.items()},
    }


class DailyActivityAggregationTests(SimpleTestCase):
    def setUp(self):
        self.days = [
            day("2026-08-01", {"a@x.edu": 1.0, "b@x.edu": 2.0}, {"azure/gpt-5": 3.0}, 3.0),
            day("2026-08-02", {"a@x.edu": 0.5}, {"azure/gpt-4o": 0.5}, 0.5),
        ]

    def test_unfiltered_totals_and_breakdowns(self):
        agg = services_usage.aggregate_from_days(self.days)
        self.assertEqual(agg["total_spend"], Decimal("3.5"))
        self.assertEqual([u["email"] for u in agg["by_user"]], ["b@x.edu", "a@x.edu"])
        self.assertEqual(agg["by_user"][1]["total_spend"], Decimal("1.5"))
        self.assertEqual(agg["by_model"][0]["model"], "gpt-5")
        self.assertEqual(agg["total_requests"], 2)

    def test_course_filter_restricts_users(self):
        agg = services_usage.aggregate_from_days(self.days, filter_emails=["a@x.edu"])
        self.assertEqual(agg["total_spend"], Decimal("1.5"))
        self.assertEqual(len(agg["by_user"]), 1)
        self.assertEqual(agg["by_model"], [])

    def test_normalize_day_prefers_entities_then_key_alias(self):
        row = {
            "date": "2026-08-01",
            "metrics": {"spend": 2.0, "api_requests": 4, "total_tokens": 40},
            "breakdown": {
                "entities": {},
                "api_keys": {"hash1": {"metrics": {"spend": 2.0, "api_requests": 4, "total_tokens": 40}, "metadata": {"key_alias": "a@x.edu"}}},
                "models": {"azure/gpt-5": {"metrics": {"spend": 2.0, "api_requests": 4, "total_tokens": 40}}},
            },
        }
        norm = services_usage._normalize_day(row)
        self.assertEqual(norm["users"], {"a@x.edu": {"spend": 2.0, "requests": 4, "tokens": 40}})
        self.assertEqual(norm["spend"], 2.0)
        row["breakdown"]["entities"] = {"z@x.edu": {"metrics": {"spend": 2.0, "api_requests": 4, "total_tokens": 40}}}
        self.assertEqual(list(services_usage._normalize_day(row)["users"]), ["z@x.edu"])
