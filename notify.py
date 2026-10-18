#!/usr/bin/env python3
"""消息推送（二期批次 12）：日报、逾期提醒、待审批、数据质量告警。

设计要点：
  * **默认只出草稿**：`outbox` 通道把消息写成 JSON 文件，不碰网络；
    只有显式给出 webhook 并选择 feishu/wecom 通道时才会真正 POST（用标准库 urllib，零依赖）。
  * **发出去的消息可追溯**：`notifications` 表按「门店 + 类型 + 窗口 + 通道」记录发送结果，
    同一窗口不会重复推送（`--force` 可强制重发）。
  * **空消息不推**：没有逾期任务、没有待审批单、数据也完整时，消息标记为空并被跳过，
    避免"每天一条什么都没发生"的噪音。
  * 载荷形态与两家官方自定义机器人一致：
      - 飞书：`{"msg_type": "text", "content": {"text": "..."}}`
      - 企微：`{"msgtype": "markdown", "markdown": {"content": "..."}}`
  * 交互式审批卡片（点按钮直接批）需要公网回调与签名校验，属于下一步，本模块只推"待审批"通知。
"""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from db import Database
from snapshot_builder import parse_target_date

KINDS = ("daily", "overdue", "approvals", "quality")
CHANNELS = ("feishu", "wecom", "outbox")
DEFAULT_BASE_URL = "http://127.0.0.1:8766"

NOTIFICATION_DDL = """
create table if not exists notifications (
  notification_id text primary key,
  tenant_id text not null default '',
  store_id text not null,
  kind text not null,
  window_key text not null,
  channel text not null,
  status text not null,
  detail_json text,
  created_at text not null,
  unique (tenant_id, store_id, kind, window_key, channel)
);
create index if not exists idx_notifications_store on notifications(store_id, kind, created_at);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Message:
    kind: str
    title: str
    text: str
    window_key: str
    empty: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def render_payload(channel: str, message: Message) -> dict[str, Any]:
    """把消息渲染成通道要求的载荷（两家官方自定义机器人的字段名不同）。"""
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": message.text}}
    if channel == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": message.text}}
    raise ValueError(f"未知通道：{channel}（可用：{'、'.join(CHANNELS)}）")


def _is_success(channel: str, body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if channel == "feishu":
        return body.get("code") == 0 or body.get("StatusCode") == 0
    if channel == "wecom":
        return body.get("errcode") == 0
    return True


# ---------------- 消息构建 ----------------

def _line(label: str, value: Any) -> str:
    return f"{label} {value}"


def build_daily_digest(reporter: Any, anchor: Any = None, base_url: str = DEFAULT_BASE_URL,
                       include_financial: bool = True) -> Message:
    """日报摘要：一天的关键指标 + 待办/待审批 + 数据质量，控制在手机一屏内。

    默认覆盖**昨天**：日报在早上推送，此时今天的平台数据通常还没导出；
    需要指定某一天时传 `anchor`（`YYYY-MM-DD` / `yesterday` / date）。
    """
    if anchor in (None, "today"):
        day = date.today() - timedelta(days=1)
    elif anchor == "yesterday":
        day = date.today() - timedelta(days=2)
    else:
        day = parse_target_date(anchor)
    dashboard = reporter.dashboard(days=1, include_financial=include_financial, today=day)
    window = dashboard["window"]
    kpis = dashboard["kpis"]
    changes = dashboard["change_percent"]
    tasks = dashboard["tasks"]
    pending = (dashboard.get("approvals") or {}).get("pending", 0)
    quality = dashboard["data_quality"]

    if not dashboard["has_data"]:
        text = (f"【{dashboard['store_name']}】经营日报 {window['end']}\n"
                f"⚠️ 昨天没有已校验的平台数据，无法统计。\n"
                f"请导出平台报表并放入 imports/inbox 后执行 daily_sync。")
        return Message("daily", "经营日报", text, window["end"], empty=True,
                       meta={"reason": "no_data"})

    def delta(metric: str) -> str:
        value = (changes or {}).get(metric)
        return "" if value is None else f"（环比 {value:+.1f}%）"

    lines = [f"【{dashboard['store_name']}】经营日报 {window['end']}",
             _line("曝光", f"{kpis['impressions']:,}") + delta("impressions")
             + " ｜ " + _line("访问", f"{kpis['detail_views']:,}") + delta("detail_views"),
             _line("支付", f"{kpis['paid_orders']:,} 单") + delta("paid_orders")
             + " ｜ " + _line("核销", f"{kpis['verified_orders']:,} 单") + delta("verified_orders")]
    if include_financial:
        lines.append(_line("GMV", f"¥{kpis['gmv']:,.2f}") + " ｜ "
                     + _line("核销率", f"{kpis['verify_rate']}%")
                     + " ｜ " + _line("退款率", f"{kpis['refund_rate']}%"))
    else:
        lines.append(_line("核销率", f"{kpis['verify_rate']}%"))
    lines.append(_line("待办", f"{tasks['total']} 项（逾期 {tasks['overdue_count']}）")
                 + " ｜ " + _line("待审批", f"{pending} 张"))
    quality_line = f"数据：缺日 {quality['missing_day_count']} 天、跳变 {quality['anomaly_count']} 处"
    lines.append(quality_line)
    if dashboard["issues"]:
        lines.append("需要关注：" + "；".join(dashboard["issues"][:3]))
    lines.append(f"看板 {base_url}/dashboard")
    return Message("daily", "经营日报", "\n".join(lines), window["end"],
                   meta={"paid_orders": kpis["paid_orders"], "pending_approvals": pending})


def build_overdue_digest(task_store: Any, store_id: str, base_url: str = DEFAULT_BASE_URL,
                         limit: int = 10) -> Message:
    overdue = task_store.overdue_tasks(store_id)
    day = date.today().isoformat()
    if not overdue:
        return Message("overdue", "逾期提醒", f"【{store_id}】今天没有逾期任务。", day, empty=True)
    lines = [f"【{store_id}】逾期任务 {len(overdue)} 项（请在 24 小时内处理并提交证据）"]
    for index, item in enumerate(overdue[:limit], 1):
        due = str(item.get("due_at") or "")[:16].replace("T", " ")
        lines.append(f"{index}. {item['title']}（负责人 {item['employee_id']}，截止 {due}）")
    if len(overdue) > limit:
        lines.append(f"…另有 {len(overdue) - limit} 项")
    lines.append(f"处理入口 {base_url}/agent.html")
    return Message("overdue", "逾期提醒", "\n".join(lines), day,
                   meta={"overdue_count": len(overdue)})


def build_approval_digest(approval_store: Any, store_id: str, base_url: str = DEFAULT_BASE_URL,
                          limit: int = 5) -> Message:
    pending = approval_store.list(store_id, status="pending", limit=limit)
    day = date.today().isoformat()
    if not pending:
        return Message("approvals", "待审批", f"【{store_id}】没有待审批单。", day, empty=True)
    lines = [f"【{store_id}】待审批 {len(pending)} 张（高风险动作需人工确认）"]
    for index, item in enumerate(pending, 1):
        payload = item.get("payload_snapshot") or {}
        title = payload.get("title") or item.get("action_type")
        lines.append(f"{index}. {title}（申请人 {item['requested_by']}，类型 {item['action_type']}）")
    lines.append(f"审批入口 {base_url}/governance")
    return Message("approvals", "待审批", "\n".join(lines), day,
                   meta={"pending_count": len(pending)})


def build_quality_alert(metric_store: Any, store_id: str, config: dict[str, Any],
                        base_url: str = DEFAULT_BASE_URL, days: int = 30) -> Message:
    rules = config.get("rules") or {}
    quality = metric_store.data_quality(store_id, days=days,
                                        fresh_days=int(rules.get("fresh_days", 2)),
                                        jump_alert_percent=float(rules.get("jump_alert_percent", 30)))
    day = date.today().isoformat()
    issues = quality["summary"]["issues"]
    if not issues:
        return Message("quality", "数据质量", f"【{store_id}】近 {days} 天数据完整，无异常。", day, empty=True)
    lines = [f"【{store_id}】数据质量提醒（近 {days} 天）"]
    lines.extend(f"- {issue}" for issue in issues[:6])
    lines.append(f"补数入口 {base_url}/dashboard")
    return Message("quality", "数据质量", "\n".join(lines), day,
                   meta={"missing_day_count": quality["summary"]["missing_day_count"],
                         "anomaly_count": quality["summary"]["anomaly_count"]})


# ---------------- 发送账本 ----------------

class NotificationLedger:
    """推送记录：同一门店/类型/窗口/通道只成功发送一次。"""

    def __init__(self, db_target: Any, tenant_id: str = ""):
        self.db = Database(db_target, tenant_id=tenant_id)
        if self.db.dialect == "sqlite":
            Path(self.db.target).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connection(self):
        return self.db.connection()

    def _init_schema(self) -> None:
        if self.db.dialect == "postgresql":
            with self._connection() as conn:
                self.db.apply_schema(conn)
            return
        with self._connection() as conn:
            conn.executescript(NOTIFICATION_DDL)
            self.db.ensure_tenant_column(conn, "notifications")

    def find(self, store_id: str, kind: str, window_key: str, channel: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                """select * from notifications
                   where store_id=? and kind=? and window_key=? and channel=?""",
                (store_id, kind, window_key, channel),
            ).fetchone()
        return self._row(row) if row else None

    def record(self, store_id: str, kind: str, window_key: str, channel: str, status: str,
               detail: Any = None) -> dict[str, Any]:
        moment = now_iso()
        with self._connection() as conn:
            conn.execute(
                """insert into notifications(notification_id,store_id,kind,window_key,channel,status,detail_json,created_at)
                   values(?,?,?,?,?,?,?,?)
                   on conflict(tenant_id,store_id,kind,window_key,channel) do update set
                     status=excluded.status, detail_json=excluded.detail_json, created_at=excluded.created_at""",
                (f"ntf-{secrets.token_hex(6)}", store_id, kind, window_key, channel, status,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None, moment),
            )
        found = self.find(store_id, kind, window_key, channel)
        assert found is not None
        return found

    def list(self, store_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "select * from notifications where store_id=? order by created_at desc limit ?",
                (store_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json") or "null")
        return item


# ---------------- 发送 ----------------

class Notifier:
    """把消息送到通道：outbox 写文件；feishu/wecom 真实 POST。"""

    def __init__(
        self,
        channel: str = "outbox",
        *,
        webhook_url: Optional[str] = None,
        outbox_dir: Optional[Path] = None,
        ledger: Optional[NotificationLedger] = None,
        store_id: str = "",
        timeout: int = 10,
        sender: Optional[Callable[[str, bytes, int], tuple[int, Any]]] = None,
    ):
        if channel not in CHANNELS:
            raise ValueError(f"未知通道：{channel}（可用：{'、'.join(CHANNELS)}）")
        if channel in ("feishu", "wecom") and not webhook_url:
            raise ValueError(f"{channel} 通道需要 webhook_url")
        self.channel = channel
        self.webhook_url = webhook_url
        self.outbox_dir = Path(outbox_dir) if outbox_dir else None
        self.ledger = ledger
        self.store_id = store_id
        self.timeout = timeout
        self._sender = sender or self._post

    # 真实发送（标准库实现，无第三方依赖）
    def _post(self, url: str, payload: bytes, timeout: int) -> tuple[int, Any]:
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            status = error.code
        try:
            body: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:500]}
        return status, body

    def send(self, message: Message, *, force: bool = False) -> dict[str, Any]:
        """发送一条消息；空消息直接跳过，已发过的窗口默认不重复推送。"""
        if message.empty and not force:
            return {"sent": False, "skipped": True, "reason": "empty", "kind": message.kind,
                    "window_key": message.window_key}

        if self.ledger and not force:
            prior = self.ledger.find(self.store_id, message.kind, message.window_key, self.channel)
            if prior and prior["status"] in ("sent", "drafted"):
                return {"sent": False, "skipped": True, "reason": "already_sent",
                        "kind": message.kind, "window_key": message.window_key,
                        "status": prior["status"], "at": prior["created_at"]}

        payload = render_payload(self.channel, message) if self.channel != "outbox" else {
            "channel": self.channel, "kind": message.kind, "title": message.title,
            "text": message.text, "window_key": message.window_key, "meta": message.meta,
            "generated_at": now_iso(),
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        if self.channel == "outbox":
            target = (self.outbox_dir or Path("outbox"))
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{message.window_key}-{message.kind}-{secrets.token_hex(3)}.json"
            path.write_bytes(raw)
            result = {"sent": False, "drafted": True, "path": str(path), "kind": message.kind,
                      "window_key": message.window_key}
            status = "drafted"
        else:
            http_status, body = self._sender(self.webhook_url or "", raw, self.timeout)
            ok = 200 <= http_status < 300 and _is_success(self.channel, body)
            result = {"sent": ok, "http_status": http_status, "response": body, "kind": message.kind,
                      "window_key": message.window_key}
            status = "sent" if ok else "failed"

        if self.ledger:
            self.ledger.record(self.store_id, message.kind, message.window_key, self.channel, status,
                               {"title": message.title, "meta": message.meta,
                                "result": {k: v for k, v in result.items() if k != "response"}})
        result["status"] = status
        return result
