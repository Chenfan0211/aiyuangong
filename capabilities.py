#!/usr/bin/env python3
"""能力注册表（第 5 期）：把 `agent_tools.StoreTools` 的进程内 facade 升级为
带 id、作用域、授权与调用契约的能力注册表，作为后续各期的公共依赖。

边界（与总方案第 5 期一致）：
- 只做注册/描述/授权/调用契约；**不做沙箱、不做远程执行**（留给第 8 期）。
- **不改变既有工具行为**：`as_tool_specs()` 与 `llm_agent.TOOL_SPECS` 逐字一致（有用例锁定），
  发给模型的 tools 数组因此与改造前完全相同。
- 越权调用一律拒绝（`CapabilityDenied`）并写 `agent_tool_runs` 留痕。

实施偏差（记录在案）：计划原文希望 `ToolSpec` 由注册表生成；为了让"tools 数组与改造前逐字一致"
这条验收可证伪，本期方向定为 **ToolSpec 仍是声明真相源，注册表从它派生并提供等价视图**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from capability_scope import CapabilityScope, CapabilityScopeError, roles_with_permission
from governance import log_tool_call
from llm_agent import TOOL_SPECS, ToolSpec

CAPABILITY_PREFIX = "tool"


class CapabilityError(ValueError):
    """能力注册表自身的错误（重复注册、未知能力等）。"""


class CapabilityDenied(PermissionError):
    """越权或未知能力：调用被拒绝。"""


@dataclass(frozen=True)
class Capability:
    """一个可注册能力：id、所需权限、描述、参数 schema 与作用域。"""

    capability_id: str
    name: str
    permission: str
    description: str
    parameters: dict[str, Any]
    scope: CapabilityScope
    source: str = "agent_tools"

    @property
    def required_scope(self) -> CapabilityScope:
        return self.scope

    def as_dict(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "name": self.name,
                "permission": self.permission, "description": self.description,
                "parameters": self.parameters, "scope": self.scope.as_dict(),
                "source": self.source}


class CapabilityRegistry:
    """能力注册表：登记、查询、按身份过滤与受控调用。"""

    def __init__(self, *, tenant_id: str = "", store_id: str = "",
                 tool_log: Any = None, audit: Any = None):
        self.tenant_id = str(tenant_id or "")
        self.store_id = str(store_id or "")
        self.tool_log = tool_log
        self.audit = audit
        self._items: dict[str, Capability] = {}

    # ---------------- 注册与查询 ----------------

    def register(self, capability: Capability) -> Capability:
        if capability.capability_id in self._items:
            raise CapabilityError(f"能力已注册：{capability.capability_id}")
        self._items[capability.capability_id] = capability
        return capability

    def all(self) -> list[Capability]:
        return [self._items[key] for key in sorted(self._items)]

    def get(self, capability_id: str) -> Capability:
        key = str(capability_id or "").strip()
        if key not in self._items:
            raise CapabilityError(f"能力不存在：{capability_id}")
        return self._items[key]

    def available_for(self, user: dict[str, Any]) -> list[Capability]:
        """按身份过滤：既要持有权限，也要落在能力作用域内。

        既接受 `{"role": ...}`（测试/命令行），也接受 `{"permissions": ...}`
        （服务端 `SecurityContext` 已解析好的权限集），两者同源。
        """
        from auth import permissions_for
        user = user or {}
        role = str(user.get("role") or "")
        permissions = user.get("permissions")
        if permissions is None:
            permissions = permissions_for(role) if role else frozenset()
        permissions = frozenset(permissions)
        available = []
        for capability in self.all():
            if capability.permission not in permissions:
                continue
            required = capability.scope
            if required.roles and role and role not in required.roles:
                continue
            available.append(capability)
        return available

    def as_tool_specs(self) -> tuple[ToolSpec, ...]:
        """注册表等价视图：与 `llm_agent.TOOL_SPECS` 逐字一致（顺序与字段都不变）。"""
        by_name = {capability.name: capability for capability in self._items.values()}
        return tuple(ToolSpec(capability.name, capability.permission, capability.description,
                              capability.parameters)
                     for name in (spec.name for spec in TOOL_SPECS)
                     for capability in [by_name[name]])

    # ---------------- 调用 ----------------

    def invoke(self, capability_id: str, arguments: dict[str, Any], user: dict[str, Any],
               executor: Any) -> Any:
        """受控调用：未知 / 越权 / 作用域不足 → 拒绝并留痕；执行委托给 `executor`。

        `executor` 是可调用 ``(name, arguments, user)``——复用 `llm_agent.ToolCallingAgent._invoke`
        既有的参数适配，注册表**不复制第二份调用契约**（工具名与方法名、参数顺序并不统一）。
        """
        try:
            capability = self.get(capability_id)
        except CapabilityError as exc:
            self._log(capability_id, user, arguments, status="blocked", error=str(exc))
            raise CapabilityDenied(str(exc)) from exc
        from auth import has_permission
        role = str((user or {}).get("role") or "")
        if not has_permission(role, capability.permission):
            message = f"当前身份无权调用 {capability.name}（需要 {capability.permission}）"
            self._log(capability.name, user, arguments, status="blocked", error=message)
            raise CapabilityDenied(message)
        try:
            capability.scope.require(CapabilityScope.of(user.get("tenant_id"), user.get("store_id"),
                                                        [role]))
        except CapabilityScopeError as exc:
            self._log(capability.name, user, arguments, status="blocked", error=str(exc))
            raise CapabilityDenied(str(exc)) from exc
        result = executor(capability.name, arguments or {}, user)
        self._log(capability.name, user, arguments, status="success", output=result)
        return result

    def _log(self, name: str, user: Any, arguments: Any, *, status: str,
             error: Optional[str] = None, output: Any = None) -> None:
        log_tool_call(self.tool_log, self.store_id, name,
                      str((user or {}).get("user_id") or ""), input_payload=arguments,
                      output_payload=output, status=status, error=error)


def build_registry(*, tenant_id: str = "", store_id: str = "", tool_log: Any = None,
                   audit: Any = None, specs: Iterable[ToolSpec] = TOOL_SPECS) -> CapabilityRegistry:
    """把既有的 ToolSpec 声明注册成能力（注册表是它的等价视图，不是第二份定义）。"""
    from auth import ROLE_PERMISSIONS
    registry = CapabilityRegistry(tenant_id=tenant_id, store_id=store_id,
                                  tool_log=tool_log, audit=audit)
    for spec in specs:
        roles = roles_with_permission(spec.permission, ROLE_PERMISSIONS)
        registry.register(Capability(
            capability_id=f"{CAPABILITY_PREFIX}:{spec.name}", name=spec.name,
            permission=spec.permission, description=spec.description,
            parameters=spec.parameters, scope=CapabilityScope.of(tenant_id, store_id, roles)))
    return registry