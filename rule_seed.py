#!/usr/bin/env python3
"""内置规则的初始定义（第 3 期第 2 步：导出种子）。

把 `prototype.py` 里那批硬编码规则的**阈值与动作模板**导出成数据，作为规则库的初始
`draft`。**本模块不改动任何运行时行为**：`prototype.py` 仍然跑自己的内置逻辑，
这里的定义只是"同一套规则的另一份表达"，供第 3 步做逐字段一致性验证。

规则清单（共 7 条；总方案里写的"4 类"是第 1 期之前的说法，第 1、2 期各加了规则）：

| rule_key          | 来源      | 阈值来自                                   |
|-------------------|-----------|--------------------------------------------|
| review_invite     | 第 1 期   | review_cycle.daily_invite_cap              |
| negative_review   | 第 1 期   | rules.negative_review_sla_hours            |
| package           | 第 1 期   | rules.package_min_views / _conversion_floor / _margin_floor |
| escalation        | 第 1 期   | missed_count >= 2（代码常量）              |
| low_slot          | 第 1 期   | rules.slot_min_days / low_slot_share_floor |
| inventory         | 第 1 期   | 分级在导入时算好，规则只读预警              |
| competitor        | 第 2/11 期| rules.competitor_area_shift_percent        |
"""

from __future__ import annotations

from typing import Any, Optional

BUILTIN_RULE_KEYS: tuple[str, ...] = (
    "review_invite", "negative_review", "package", "escalation",
    "low_slot", "inventory", "competitor",
)

# 升级包阈值：与 prototype.py 里"missed_count >= 2 就升级为 high"保持一致
ESCALATION_HIGH_AT = 2


def builtin_definitions(store_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """按当前门店配置生成 7 条内置规则定义（阈值全部取自配置，与运行时同源）。"""
    rules = config.get("rules") or {}
    cycle = config.get("review_cycle") or {}
    scope = {"store_id": store_id}
    return [
        {"rule_key": "review_invite", "scope": scope,
         "condition": {"kind": "review_invite",
                       "daily_invite_cap": cycle.get("daily_invite_cap")},
         "action_template": {
             "category": "reputation", "priority": "medium",
             "assignee_role": "review_operator", "due_at": "T21:00:00+08:00",
             "requires_approval": False,
             "evidence": ["平台订单/核销号（脱敏）", "邀请时间", "执行员工"]}},
        {"rule_key": "negative_review", "scope": scope,
         "condition": {"kind": "negative_review",
                       "sla_hours": int(rules.get("negative_review_sla_hours", 4))},
         "action_template": {
             "category": "reputation", "priority": "high", "priority_when_overdue": "urgent",
             "assignee_role": "store_manager", "due_at": "T18:00:00+08:00",
             "requires_approval": False,
             "requires_approval_when": "compensation_requested",
             "evidence": ["平台回复截图/记录", "问题分类", "补救结果", "是否需要店长复盘"]}},
        {"rule_key": "package", "scope": scope,
         "condition": {"kind": "package",
                       "min_views": int(rules.get("package_min_views", 50)),
                       "conversion_floor": float(rules.get("package_conversion_floor", 0.05)),
                       "margin_floor": float(rules.get("package_margin_floor", 0.25))},
         "action_template": {
             "category": "package", "priority": "high", "assignee_role": "online_operator",
             "due_at": "T17:00:00+08:00", "requires_approval": True,
             "change_one_variable_only": True,
             "evidence": ["调整前页面快照", "价格与毛利复核", "唯一变更变量", "审批记录"]}},
        {"rule_key": "escalation", "scope": scope,
         "condition": {"kind": "escalation", "high_when_missed_at": ESCALATION_HIGH_AT},
         "action_template": {
             "category": "management", "priority": "medium", "priority_when_repeat": "high",
             "assignee_role": "area_manager", "due_at": "T19:00:00+08:00",
             "requires_approval": True,
             "evidence": ["原任务要求", "截止时间", "员工提交记录", "负责人复核结论"]}},
        {"rule_key": "low_slot", "scope": scope,
         "condition": {"kind": "low_slot",
                       "slot_min_days": int(rules.get("slot_min_days", 3)),
                       "share_floor": float(rules.get("low_slot_share_floor", 0.15))},
         "action_template": {
             "category": "operations", "priority": "medium", "assignee_role": "online_operator",
             "due_at": "T17:00:00+08:00", "requires_approval": True,
             "change_one_variable_only": True,
             "evidence": ["时段经营数据截图", "低峰原因分析", "唯一变更变量", "审批记录"]}},
        {"rule_key": "inventory", "scope": scope,
         "condition": {"kind": "inventory", "levels": ["critical", "warning"]},
         "action_template": {
             "category": "operations", "priority": "high", "priority_when_critical": "urgent",
             "assignee_role": "store_manager", "due_at": "T12:00:00+08:00",
             "requires_approval": True, "aggregate": "one_task_per_batch",
             "evidence": ["盘点结果截图", "补货数量与预计到货日期", "采购审批记录"]}},
        {"rule_key": "competitor", "scope": scope,
         "condition": {"kind": "competitor",
                       "area_shift_percent": float(rules.get("competitor_area_shift_percent", 10))},
         "action_template": {
             "category": "competitor", "priority": "medium", "priority_when_outside": "high",
             "assignee_role": "online_operator", "due_at": "T17:00:00+08:00",
             "requires_approval": True, "aggregate": "one_task_per_area",
             "change_one_variable_only": True,
             "evidence": ["竞品采样截图", "采样时间与采集方式", "唯一变更变量", "审批记录"]}},
    ]


def seed_builtin_rules(store: Any, store_id: str, config: dict[str, Any],
                       approve: bool = False) -> dict[str, Any]:
    """把内置规则写入规则库，**幂等**：已存在任何版本的 rule_key 一律跳过。

    `approve=True` 时把新建版本直接审批为 `approved`（记为 `system:builtin-seed`），
    供“内置规则即生效”的生产接线使用；默认仍只建 `draft`。

    返回 `{"created": [...], "skipped": [...], "approved": [...], "total": 7}`。
    """
    created: list[str] = []
    skipped: list[str] = []
    approved: list[str] = []
    for item in builtin_definitions(store_id, config):
        key = item["rule_key"]
        existing = store.list_rules(key)
        if existing:
            skipped.append(key)
            # 已存在但没有生效版本时，approve 允许把最新草稿提为 approved（幂等）
            if approve and not any(row["status"] == "approved" for row in existing):
                store.decide(key, existing[0]["version"], "approved",
                             actor_id="system:builtin-seed")
                approved.append(key)
            continue
        record = store.save_draft(key, scope=item["scope"], condition=item["condition"],
                                  action_template=item["action_template"])
        created.append(key)
        if approve:
            store.decide(key, record["version"], "approved", actor_id="system:builtin-seed")
            approved.append(key)
    return {"created": created, "skipped": skipped, "approved": approved,
            "total": len(BUILTIN_RULE_KEYS)}


def resolve_runtime_rules(store: Any) -> tuple[Optional[dict[str, dict[str, Any]]], list[str]]:
    """解析当前可执行的规则集（仅 `approved` 版本）。

    只有 7 条内置规则**全部**存在已批准版本时才返回规则集；否则返回 `(None, 缺失键)`，
    由调用方回退到内置默认规则并告警，遵循“规则加载失败不静默降级为无规则”的硬约定。
    """
    active = {item["rule_key"]: item for item in store.approved_rules()}
    missing = [key for key in BUILTIN_RULE_KEYS if key not in active]
    return (active if not missing else None), missing
