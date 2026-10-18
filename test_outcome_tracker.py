# -*- coding: utf-8 -*-
"""任务反馈与结果归因（outcome_tracker）的测试：采纳/驳回、幂等、D+7/D+28、缺数、跨店隔离。

约定的行为边界（实现必须满足）：
  1. 反馈：同一任务同一人只保留一条**最终决策**。重复提交完全相同的反馈是幂等的
     （created=False 且不覆盖 created_at）；改判覆盖该条记录（created=False、updated=True）；
     驳回/误报必须给出原因。
  2. 归因：基线窗口 = [closed_on - window_days, closed_on - 1]，当前窗口 =
     [closed_on + 1, closed_on + window_days]，两侧等长。窗口内任一天缺数 → status=no_data，
     baseline/current/delta 全部为 None，绝不补零；窗口尚未走完（closed_on + window_days > today）
     → status=pending，且不读取指标。
  3. 隔离：所有查询强制 tenant_id + store_id，同一门店号在不同租户之间互不可见。
"""
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from outcome_tracker import OutcomeTracker, OutcomeTrackerError

TENANT_A = "LPL-001"
TENANT_B = "LPL-002"
STORE = "WH-LION-001"
OTHER_STORE = "WH-LION-002"
TASK = "2026-09-10-package-MT-1"
OTHER_TASK = "2026-09-11-package-MT-2"

try:
    import pgserver
    import psycopg
    HAS_PG = True
except ImportError:                                  # pragma: no cover
    HAS_PG = False


def days(start: str, count: int) -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=offset)).isoformat() for offset in range(count)]


class FakeMetrics:
    """按 (day, metric_key) 给值的假指标源；没有登记的日子返回 None（等同于缺数）。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, store_id: str, day: str, metric_key: str):
        self.calls.append((store_id, day, metric_key))
        return self.values.get((day, metric_key))

    def fill(self, start: str, count: int, value: float, metric: str = "gmv") -> "FakeMetrics":
        for day in days(start, count):
            self.values[(day, metric)] = value
        return self


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tracker = OutcomeTracker(self.root / "store_ops.db", tenant_id=TENANT_A)

    def tearDown(self):
        self.temp.cleanup()

    def test_accept_and_reject_feedback_are_recorded(self):
        accepted = self.tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        self.assertEqual((accepted["decision"], accepted["actor_id"]), ("accepted", "店长王"))
        self.assertTrue(accepted["created"])
        self.tracker.record_feedback(STORE, OTHER_TASK, "店长王", "rejected", reason="误报：测试账号下单")

        rows = self.tracker.list_feedback(STORE)
        self.assertEqual([row["task_id"] for row in rows], [TASK, OTHER_TASK])
        self.assertEqual(rows[1]["reason"], "误报：测试账号下单")
        self.assertEqual(self.tracker.list_feedback(STORE, task_id=TASK)[0]["decision"], "accepted")

    def test_reject_and_false_positive_require_reason(self):
        for decision in ("rejected", "false_positive"):
            with self.assertRaises(OutcomeTrackerError):
                self.tracker.record_feedback(STORE, TASK, "店长王", decision, reason="   ")

    def test_unknown_decision_and_missing_scope_are_rejected(self):
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.record_feedback(STORE, TASK, "店长王", "maybe")
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.record_feedback("", TASK, "店长王", "accepted")
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.record_feedback(STORE, TASK, "", "accepted")

    def test_repeated_identical_feedback_is_idempotent(self):
        first = self.tracker.record_feedback(STORE, TASK, "店长王", "rejected", reason="不是真实差评")
        again = self.tracker.record_feedback(STORE, TASK, "店长王", "rejected", reason="不是真实差评")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertFalse(again["updated"])
        self.assertEqual(again["created_at"], first["created_at"])
        self.assertEqual(len(self.tracker.list_feedback(STORE, task_id=TASK)), 1)

    def test_changed_decision_overwrites_single_row(self):
        self.tracker.record_feedback(STORE, TASK, "店长王", "rejected", reason="先按误报驳回")
        revised = self.tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        self.assertFalse(revised["created"])
        self.assertTrue(revised["updated"])
        self.assertEqual(revised["decision"], "accepted")
        self.assertEqual(revised["reason"], "")
        rows = self.tracker.list_feedback(STORE, task_id=TASK)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "accepted")

    def test_different_actors_keep_their_own_decision(self):
        self.tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        self.tracker.record_feedback(STORE, TASK, "区域负责人", "false_positive", reason="派单阈值需调整")
        rows = self.tracker.list_feedback(STORE, task_id=TASK)
        self.assertEqual(sorted(row["actor_id"] for row in rows), ["区域负责人", "店长王"])


class ComputeOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tracker = OutcomeTracker(self.root / "store_ops.db", tenant_id=TENANT_A)

    def tearDown(self):
        self.temp.cleanup()

    def test_d7_outcome_uses_equal_length_windows_around_closed_on(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        row = self.tracker.compute_outcome(
            STORE, TASK, metric_key="gmv", window_days=7, closed_on="2026-09-10",
            today="2026-10-01", metric_reader=metrics, guardrail={"refunds_delta_max": 3})
        self.assertEqual(row["status"], "computed")
        self.assertEqual(row["baseline_value"], 10.0)
        self.assertEqual(row["current_value"], 12.5)
        self.assertEqual(row["delta"], 2.5)
        self.assertEqual(row["window_days"], 7)
        self.assertEqual(row["guardrail_json"], '{"refunds_delta_max": 3}')
        self.assertEqual(sorted(call[1] for call in metrics.calls),
                         sorted(days("2026-09-03", 7) + days("2026-09-11", 7)))
        self.assertEqual({call[0] for call in metrics.calls}, {STORE})
        self.assertEqual({call[2] for call in metrics.calls}, {"gmv"})

    def test_d7_and_d28_are_stored_as_separate_windows(self):
        metrics = (FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 8.0)
                   .fill("2026-07-04", 28, 5.0).fill("2026-08-02", 28, 6.0))
        d7 = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                         closed_on="2026-09-10", today="2026-10-18", metric_reader=metrics)
        d28 = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=28,
                                          closed_on="2026-08-01", today="2026-10-18", metric_reader=metrics)
        self.assertEqual((d7["delta"], d28["delta"]), (-2.0, 1.0))
        rows = self.tracker.list_outcomes(STORE, task_id=TASK)
        self.assertEqual([(row["window_days"], row["status"]) for row in rows],
                         [(7, "computed"), (28, "computed")])

    def test_missing_day_in_current_window_returns_no_data_without_zero_fill(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        del metrics.values[("2026-09-13", "gmv")]
        row = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                          closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(row["status"], "no_data")
        self.assertIsNone(row["baseline_value"])
        self.assertIsNone(row["current_value"])
        self.assertIsNone(row["delta"])

    def test_missing_day_in_baseline_window_returns_no_data(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        del metrics.values[("2026-09-05", "gmv")]
        row = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                          closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(row["status"], "no_data")
        self.assertIsNone(row["delta"])

    def test_whole_missing_metric_returns_no_data_for_another_metric_key(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0, metric="refunds").fill("2026-09-11", 7, 0.0, metric="refunds")
        row = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                          closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(row["status"], "no_data")
        self.assertIsNone(row["current_value"])

    def test_real_zero_is_data_not_missing(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 0.0, metric="refunds").fill("2026-09-11", 7, 0.0, metric="refunds")
        row = self.tracker.compute_outcome(STORE, TASK, metric_key="refunds", window_days=7,
                                          closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(row["status"], "computed")
        self.assertEqual((row["baseline_value"], row["current_value"], row["delta"]), (0.0, 0.0, 0.0))

    def test_window_not_elapsed_is_pending_and_reader_is_not_called(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        row = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                          closed_on="2026-09-10", today="2026-09-16", metric_reader=metrics)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["current_value"])
        self.assertIsNone(row["delta"])
        self.assertEqual(metrics.calls, [])

    def test_recompute_is_idempotent_per_window_and_metric(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        first = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                            closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        second = self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                             closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(first["computed_at"], second["computed_at"])
        self.assertEqual(len(self.tracker.list_outcomes(STORE, task_id=TASK)), 1)

    def test_missing_metric_source_and_bad_arguments_are_rejected(self):
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                        closed_on="2026-09-10", today="2026-10-01")
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=0,
                                        closed_on="2026-09-10", today="2026-10-01",
                                        metric_reader=FakeMetrics())
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.compute_outcome(STORE, TASK, metric_key="", window_days=7,
                                        closed_on="2026-09-10", today="2026-10-01",
                                        metric_reader=FakeMetrics())
        with self.assertRaises(OutcomeTrackerError):
            self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                        closed_on="not-a-date", today="2026-10-01",
                                        metric_reader=FakeMetrics())

    def test_list_outcomes_filters_by_scope_task_and_status(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                     closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.tracker.compute_outcome(OTHER_STORE, TASK, metric_key="gmv", window_days=7,
                                     closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.tracker.compute_outcome(STORE, OTHER_TASK, metric_key="refunds", window_days=28,
                                     closed_on="2026-09-10", today="2026-10-20", metric_reader=FakeMetrics())
        self.assertEqual(len(self.tracker.list_outcomes(STORE)), 2)
        self.assertEqual([row["status"] for row in self.tracker.list_outcomes(STORE, status="no_data")],
                         ["no_data"])
        self.assertEqual(len(self.tracker.list_outcomes(OTHER_STORE)), 1)
        self.assertEqual(self.tracker.list_outcomes(STORE, task_id=TASK)[0]["metric_key"], "gmv")
        self.assertEqual(self.tracker.list_outcomes(STORE, window_days=28)[0]["metric_key"], "refunds")
        self.assertEqual(self.tracker.list_outcomes(STORE, metric_key="review_count"), [])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "store_ops.db"
        self.tracker = OutcomeTracker(self.db_path, tenant_id=TENANT_A)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_summary_has_no_rates(self):
        summary = self.tracker.outcome_summary(STORE)
        self.assertEqual((summary["store_id"], summary["tenant_id"]), (STORE, TENANT_A))
        self.assertEqual(summary["feedback"]["total"], 0)
        self.assertIsNone(summary["feedback"]["acceptance_rate"])
        self.assertIsNone(summary["feedback"]["false_positive_rate"])
        self.assertEqual(summary["outcomes"]["total"], 0)

    def test_summary_rates_and_outcome_gaps(self):
        self.tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        self.tracker.record_feedback(STORE, OTHER_TASK, "店长王", "false_positive", reason="规则阈值过松")
        self.tracker.record_feedback(STORE, OTHER_TASK, "区域负责人", "rejected", reason="证据不足")
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        self.tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                     closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.tracker.compute_outcome(STORE, OTHER_TASK, metric_key="gmv", window_days=7,
                                     closed_on="2026-09-10", today="2026-10-01", metric_reader=FakeMetrics())
        summary = self.tracker.outcome_summary(STORE)
        self.assertEqual(summary["feedback"]["total"], 3)
        self.assertEqual(summary["feedback"]["tasks"], 2)
        self.assertEqual(summary["feedback"]["accepted"], 1)
        self.assertEqual(summary["feedback"]["rejected"], 1)
        self.assertEqual(summary["feedback"]["false_positive"], 1)
        self.assertEqual(summary["feedback"]["acceptance_rate"], 0.3333)
        self.assertEqual(summary["feedback"]["false_positive_rate"], 0.3333)
        self.assertEqual(summary["outcomes"], {
            "total": 2, "computed": 1, "no_data": 1, "pending": 0,
            "windows": [{"window_days": 7, "total": 2, "computed": 1, "no_data": 1, "pending": 0}],
        })

    def test_summary_is_scoped_to_store(self):
        self.tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        self.tracker.record_feedback(OTHER_STORE, TASK, "店长王", "accepted")
        self.assertEqual(self.tracker.outcome_summary(STORE)["feedback"]["total"], 1)
        self.assertEqual(self.tracker.outcome_summary(OTHER_STORE)["feedback"]["total"], 1)


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "store_ops.db"
        self.tenant_a = OutcomeTracker(self.db_path, tenant_id=TENANT_A)
        self.tenant_b = OutcomeTracker(self.db_path, tenant_id=TENANT_B)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_store_id_in_two_tenants_is_isolated(self):
        self.tenant_a.record_feedback(STORE, TASK, "店长王", "accepted")
        self.tenant_b.record_feedback(STORE, TASK, "店长王", "false_positive", reason="另一个租户的数据")
        self.assertEqual([row["decision"] for row in self.tenant_a.list_feedback(STORE)], ["accepted"])
        self.assertEqual([row["decision"] for row in self.tenant_b.list_feedback(STORE)], ["false_positive"])
        self.assertEqual(self.tenant_a.outcome_summary(STORE)["feedback"]["total"], 1)
        self.assertEqual(self.tenant_b.outcome_summary(STORE)["feedback"]["total"], 1)

    def test_outcomes_are_isolated_by_tenant_and_store(self):
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        self.tenant_a.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                      closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.tenant_b.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                      closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.tenant_b.compute_outcome(OTHER_STORE, TASK, metric_key="gmv", window_days=7,
                                      closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual(len(self.tenant_a.list_outcomes(STORE)), 1)
        self.assertEqual(len(self.tenant_b.list_outcomes(STORE)), 1)
        self.assertEqual(len(self.tenant_b.list_outcomes(OTHER_STORE)), 1)
        self.assertEqual(self.tenant_a.list_outcomes(OTHER_STORE), [])
        self.assertEqual(self.tenant_a.outcome_summary(STORE)["outcomes"]["total"], 1)

    def test_tenant_attribution_is_written_to_rows(self):
        self.tenant_a.record_feedback(STORE, TASK, "店长王", "accepted")
        conn = sqlite3.connect(self.db_path)                 # Windows 上必须显式关闭，否则会锁住临时目录
        try:
            rows = conn.execute(
                "select tenant_id, store_id, task_id from task_feedback order by tenant_id").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(TENANT_A, STORE, TASK)])


@unittest.skipUnless(HAS_PG, "需要 pgserver 才能验证 PostgreSQL 上的结果归因")
class PgOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="aiyg_outcome_"))
        cls.server = pgserver.get_server(cls.tmp / "pgdata")
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.cleanup()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop schema public cascade; create schema public")

    def test_feedback_and_outcome_roundtrip_on_postgres(self):
        tracker = OutcomeTracker(self.dsn, tenant_id=TENANT_A)
        tracker.record_feedback(STORE, TASK, "店长王", "accepted")
        metrics = FakeMetrics().fill("2026-09-03", 7, 10.0).fill("2026-09-11", 7, 12.5)
        row = tracker.compute_outcome(STORE, TASK, metric_key="gmv", window_days=7,
                                      closed_on="2026-09-10", today="2026-10-01", metric_reader=metrics)
        self.assertEqual((row["status"], row["delta"]), ("computed", 2.5))
        self.assertEqual(tracker.outcome_summary(STORE)["outcomes"]["computed"], 1)
        self.assertEqual(OutcomeTracker(self.dsn, tenant_id=TENANT_B).list_feedback(STORE), [])
        self.assertEqual(OutcomeTracker(self.dsn, tenant_id=TENANT_A).list_feedback(OTHER_STORE), [])

    def test_outcome_tables_enable_row_level_security_on_postgres(self):
        OutcomeTracker(self.dsn, tenant_id=TENANT_A).record_feedback(STORE, TASK, "店长王", "accepted")
        role, probe_password = "outcome_probe", "outcome-probe-pass"
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("drop role if exists " + role)
            cur.execute(f"create role {role} login password '{probe_password}'")
            cur.execute(f"grant usage on schema public to {role}")
            cur.execute(f"grant select on task_feedback, task_outcomes to {role}")
            cur.execute("select relname, relrowsecurity from pg_class "
                        "where relname in ('task_feedback','task_outcomes')")
            self.assertEqual(dict(cur.fetchall()),
                             {"task_feedback": True, "task_outcomes": True})
        head, tail = self.dsn.split("@", 1)
        with psycopg.connect(f"postgresql://{role}:{probe_password}@{tail}") as conn, conn.cursor() as cur:
            cur.execute("select count(*) from task_feedback")            # 未设置租户 → 看不到任何行
            self.assertEqual(int(cur.fetchone()[0]), 0)
            cur.execute("select set_config('app.tenant_id', %s, false)", (TENANT_A,))
            cur.execute("select decision from task_feedback")
            self.assertEqual([row[0] for row in cur.fetchall()], ["accepted"])


if __name__ == "__main__":
    unittest.main()
