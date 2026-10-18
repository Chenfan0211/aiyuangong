import unittest

from prototype import generate_daily_plan


def base_snapshot():
    return {
        "store": {"tenant_id": "T1", "store_id": "S1", "name": "武汉狮子座店"},
        "date": "2026-09-03",
        "store_manager": "店长",
        "reviews": {
            "eligible_review_invites_today": 3,
            "qualified_reviews_month": 8,
            "qualified_review_target_month": 20,
            "days_remaining_in_cycle": 4,
            "open_negative_reviews": [],
        },
        "packages": [],
        "overdue_staff_tasks": [],
    }


class DailyPlanTests(unittest.TestCase):
    def test_review_target_is_capped_by_real_eligible_customers(self):
        plan = generate_daily_plan(base_snapshot())
        task = next(item for item in plan["tasks"] if item["task_id"].endswith("review-invite"))
        self.assertEqual(task["target"]["qualified_review_invites"], 3)
        self.assertFalse(task["requires_approval"])

    def test_no_review_task_when_cycle_target_is_met(self):
        snapshot = base_snapshot()
        snapshot["reviews"]["qualified_reviews_month"] = 20
        plan = generate_daily_plan(snapshot)
        self.assertFalse(any(item["category"] == "reputation" for item in plan["tasks"]))

    def test_package_change_and_staff_warning_are_approval_gated(self):
        snapshot = base_snapshot()
        snapshot["packages"] = [{
            "package_id": "P1",
            "name": "低转化套餐",
            "detail_views_7d": 100,
            "paid_orders_7d": 2,
            "gross_margin_rate": 0.3,
        }]
        snapshot["overdue_staff_tasks"] = [{
            "task_id": "OT1", "employee_id": "E1", "missed_count": 2
        }]
        plan = generate_daily_plan(snapshot)
        gated = [item for item in plan["tasks"] if item["category"] in {"package", "management"}]
        self.assertEqual(len(gated), 2)
        self.assertTrue(all(item["requires_approval"] for item in gated))
        self.assertTrue(all(item["status"] == "proposed" for item in gated))

    def test_negative_review_compensation_needs_approval_and_no_phone_field(self):
        snapshot = base_snapshot()
        snapshot["reviews"]["open_negative_reviews"] = [{
            "review_id": "R1", "age_hours": 8, "compensation_requested": True
        }]
        plan = generate_daily_plan(snapshot)
        task = next(item for item in plan["tasks"] if item["task_id"].endswith("negative-R1"))
        self.assertEqual(task["priority"], "urgent")
        self.assertTrue(task["requires_approval"])
        self.assertNotIn("phone", str(task).lower())


if __name__ == "__main__":
    unittest.main()
