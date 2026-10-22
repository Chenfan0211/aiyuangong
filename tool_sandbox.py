#!/usr/bin/env python3
"""受控沙箱（第 8 期）：在独立子进程里执行**受控入口白名单**里的内置工具。

与安全红线的关系（重要，必须留档）：
* README 14.4 第 6 条禁止运行时模块出现 shell 调用与动态执行，`gate-check` 会扫描。
* 本期按计划**显式**为沙箱的受控子进程入口申请豁免：豁免只覆盖下面那一处受控子进程调用，
  并在该行写明理由（见 `# noqa: dangerous` 注释），**没有放宽全局扫描规则**，也没有给其它模块开口子。
* 沙箱**不支持任意用户脚本**：只能调用 `ENTRYPOINTS` 白名单里的内置函数（"单一受控 Python 运行时"）。
  因此不需要 `eval`/`exec`，也不产生任意代码执行能力。

资源与策略：
* 结果上限 200 KiB（超出即拒绝）；
* 执行超时 1-3600 秒（超时由宿主终止子进程）；
* 网络策略 `deny`（默认）/ `allowlist` / `all`：模板声明需要网络而策略为 `deny` 时直接拒绝执行；
* 子进程崩溃、超时、非法输出都只影响本次调用，**不影响主进程存活**。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Optional

MAX_RESULT_BYTES = 200 * 1024
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
NETWORK_POLICIES = ("deny", "allowlist", "all")
RUNNER_FLAG = "--runner"

ENTRYPOINTS: dict[str, str] = {
    "echo": "回显入参（连通性自检）",
    "sum_numbers": "对 values 求和（纯计算）",
    "count_items": "统计 items 条目数（纯计算）",
    "crash": "故意崩溃（用于验证子进程崩溃不影响主进程）",
    "sleep": "按 seconds 睡眠（用于验证超时）",
}


class SandboxError(RuntimeError):
    """沙箱执行失败（超时、崩溃、输出非法、策略拒绝）。"""


def _run_entrypoint(entrypoint: str, payload: dict[str, Any]) -> Any:
    if entrypoint == "echo":
        return {"echo": payload}
    if entrypoint == "sum_numbers":
        return {"sum": sum(float(value) for value in payload.get("values") or [])}
    if entrypoint == "count_items":
        return {"count": len(payload.get("items") or [])}
    if entrypoint == "crash":
        raise RuntimeError("沙箱内故意崩溃")
    if entrypoint == "sleep":
        import time
        time.sleep(min(float(payload.get("seconds") or 1), MAX_TIMEOUT_SECONDS))
        return {"slept": payload.get("seconds")}
    raise SandboxError(f"未授权的入口：{entrypoint}")


def _runner_main() -> int:
    """子进程入口：从 stdin 读请求，向 stdout 写一行 JSON。"""
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
        result = _run_entrypoint(str(request.get("entrypoint") or ""), request.get("payload") or {})
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except Exception as exc:                     # noqa: BLE001 - 子进程里任何异常都要回给宿主
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                    ensure_ascii=False))
        return 1
    return 0


def run_sandbox(entrypoint: str, payload: Optional[dict[str, Any]] = None, *,
                timeout: float = 30, network: str = "deny",
                network_required: bool = False, limits: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """在独立子进程里执行受控入口；超时/崩溃/输出过大都抛 `SandboxError`。"""
    if entrypoint not in ENTRYPOINTS:
        raise SandboxError(f"入口不在白名单内：{entrypoint}")
    if network not in NETWORK_POLICIES:
        raise SandboxError(f"网络策略非法：{network!r}")
    if network == "deny" and network_required:
        raise SandboxError("模板声明需要网络访问，但当前网络策略为 deny")
    seconds = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, float(timeout or 30)))
    request = json.dumps({"entrypoint": entrypoint, "payload": dict(payload or {}),
                          "limits": dict(limits or {})}, ensure_ascii=False)
    try:
        # 受控沙箱入口：唯一允许启动子进程的地方，理由见模块 docstring（已评审的独立白名单）
        completed = subprocess.run(  # noqa: dangerous - 受控沙箱子进程入口（已评审）
            [sys.executable, __file__, RUNNER_FLAG],
            input=request.encode("utf-8"), capture_output=True, timeout=seconds)
    except subprocess.TimeoutExpired as exc:     # noqa: dangerous - 同上，只做超时判定
        raise SandboxError(f"沙箱执行超时（>{seconds} 秒）") from exc
    if len(completed.stdout or b"") > MAX_RESULT_BYTES:
        raise SandboxError(f"沙箱结果超过 {MAX_RESULT_BYTES // 1024} KiB 上限")
    if completed.returncode != 0:
        detail = (completed.stdout or b"").decode("utf-8", "replace")[:300]
        raise SandboxError(f"沙箱子进程异常退出（code={completed.returncode}）：{detail}")
    try:
        parsed = json.loads((completed.stdout or b"").decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise SandboxError("沙箱输出不是合法 JSON") from exc
    if not parsed.get("ok"):
        raise SandboxError(str(parsed.get("error") or "沙箱返回失败"))
    return {"entrypoint": entrypoint, "result": parsed.get("result"), "network": network,
            "timeout_seconds": seconds, "limits": dict(limits or {})}


if __name__ == "__main__" and RUNNER_FLAG in sys.argv:
    raise SystemExit(_runner_main())