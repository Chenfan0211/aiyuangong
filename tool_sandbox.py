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

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

MAX_RESULT_BYTES = 200 * 1024
DEFAULT_LIMITS: dict[str, int] = {
    "max_read_bytes": 1024 * 1024,          # 单次读取 1 MiB
    "max_file_bytes": 10 * 1024 * 1024,     # 单文件 10 MiB
    "max_workspace_bytes": 100 * 1024 * 1024,   # 工作区 100 MiB
    "max_entries": 1000,                    # 条目数 1000
    "max_result_bytes": MAX_RESULT_BYTES,   # 结果 200 KiB
}
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
    "read_file": "读取工作区内的文件（受单次读取上限约束，返回大小/哈希/预览）",
    "list_files": "列出工作区条目（受条目数上限约束）",
}


class SandboxError(RuntimeError):
    """沙箱执行失败（超时、崩溃、输出非法、策略拒绝）。"""


def _workspace_path(workspace: Any, name: Any) -> Path:
    if not workspace:
        raise SandboxError("本次执行没有工作区")
    raw = str(name or "").strip()
    if not raw or os.path.isabs(raw) or ".." in raw.split("/") or ".." in raw.split("\\"):
        raise SandboxError(f"非法文件名：{name!r}")
    path = Path(workspace) / raw
    if path.parent != Path(workspace):
        raise SandboxError(f"非法文件名：{name!r}")
    if not path.is_file():
        raise SandboxError(f"工作区里没有该文件：{raw}")
    return path


def _run_entrypoint(entrypoint: str, payload: dict[str, Any],
                    limits: Optional[dict[str, Any]] = None,
                    workspace: Any = None) -> Any:
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
    if entrypoint == "list_files":
        if not workspace:
            return {"files": []}
        names = sorted(item.name for item in Path(workspace).iterdir())
        return {"files": names, "count": len(names)}
    if entrypoint == "read_file":
        merged = {**DEFAULT_LIMITS, **dict(limits or {})}
        path = _workspace_path(workspace, payload.get("name"))
        size = path.stat().st_size
        if size > int(merged["max_read_bytes"]):
            raise SandboxError(f"单次读取超过 {merged['max_read_bytes']} 字节上限：{size}")
        data = path.read_bytes()
        return {"name": path.name, "size": size, "sha256": hashlib.sha256(data).hexdigest(),
                "preview": data[:2000].decode("utf-8", "replace")}
    raise SandboxError(f"未授权的入口：{entrypoint}")


def _runner_main(workspace: Any = None) -> int:
    """子进程入口：从 stdin 读请求，向 stdout 写一行 JSON。"""
    # 显式用 UTF-8 字节流：Windows 上子进程 stdout 默认是本地代码页（GBK），
    # 会让中文结果在宿主侧按 UTF-8 解读失败。
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        request = json.loads(raw or "{}")
        result = _run_entrypoint(str(request.get("entrypoint") or ""), request.get("payload") or {},
                                 request.get("limits") or {}, workspace)
        payload = json.dumps({"ok": True, "result": result}, ensure_ascii=False)
    except Exception as exc:                     # noqa: BLE001 - 子进程里任何异常都要回给宿主
        payload = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False)
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 1
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def _prepare_workspace(workspace: Path, files: dict[str, Any], limits: dict[str, int]) -> None:
    """把待处理文件写进受限工作区；条目数/单文件/总量三项上限在这里真实生效。"""
    entries = list((files or {}).items())
    if len(entries) > int(limits["max_entries"]):
        raise SandboxError(f"工作区条目数超过 {limits['max_entries']} 上限：{len(entries)}")
    total = 0
    for name, content in entries:
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        if len(data) > int(limits["max_file_bytes"]):
            raise SandboxError(f"单文件超过 {limits['max_file_bytes']} 字节上限：{name}")
        total += len(data)
        if total > int(limits["max_workspace_bytes"]):
            raise SandboxError(f"工作区总量超过 {limits['max_workspace_bytes']} 字节上限")
        target = workspace / str(name)
        if target.parent != workspace:
            raise SandboxError(f"非法文件名：{name!r}")
        target.write_bytes(data)


def run_sandbox(entrypoint: str, payload: Optional[dict[str, Any]] = None, *,
                timeout: float = 30, network: str = "deny", network_required: bool = False,
                limits: Optional[dict[str, Any]] = None,
                files: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """在独立子进程里执行受控入口；超时/崩溃/输出过大/越限都抛 `SandboxError`。

    `files` 会写进受限工作区，读取类入口（`read_file` / `list_files`）只能访问该目录，
    且受"单次读取 1 MiB / 单文件 10 MiB / 工作区 100 MiB / 条目 1000"四项上限约束。
    """
    if entrypoint not in ENTRYPOINTS:
        raise SandboxError(f"入口不在白名单内：{entrypoint}")
    if network not in NETWORK_POLICIES:
        raise SandboxError(f"网络策略非法：{network!r}")
    if network == "deny" and network_required:
        raise SandboxError("模板声明需要网络访问，但当前网络策略为 deny")
    merged_limits = {**DEFAULT_LIMITS, **{key: int(value) for key, value in (limits or {}).items()}}
    seconds = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, float(timeout or 30)))
    request = json.dumps({"entrypoint": entrypoint, "payload": dict(payload or {}),
                          "limits": merged_limits}, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="tool-sandbox-") as tmp:
        workspace = Path(tmp)
        if files:
            _prepare_workspace(workspace, dict(files), merged_limits)
        command = [sys.executable, __file__, RUNNER_FLAG, str(workspace)]
        try:
            # 受控沙箱入口：唯一允许启动子进程的地方，理由见模块 docstring（已评审的独立白名单）
            completed = subprocess.run(  # noqa: dangerous - 受控沙箱子进程入口（已评审）
                command, input=request.encode("utf-8"), capture_output=True, timeout=seconds)
        except subprocess.TimeoutExpired as exc:   # noqa: dangerous - 同上，只做超时判定
            raise SandboxError(f"沙箱执行超时（>{seconds} 秒）") from exc
    if len(completed.stdout or b"") > int(merged_limits["max_result_bytes"]):
        raise SandboxError(f"沙箱结果超过 {int(merged_limits['max_result_bytes']) // 1024} KiB 上限")
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
            "timeout_seconds": seconds, "limits": merged_limits,
            "workspace_entries": len(files or {})}


if __name__ == "__main__" and RUNNER_FLAG in sys.argv:
    _workspace = sys.argv[sys.argv.index(RUNNER_FLAG) + 1] if len(sys.argv) > sys.argv.index(RUNNER_FLAG) + 1 else None
    raise SystemExit(_runner_main(_workspace))