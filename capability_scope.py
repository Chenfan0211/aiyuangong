#!/usr/bin/env python3
"""能力作用域（第 5 期）：把"谁、在哪个租户/门店、以什么角色"能调用什么能力显式化。

两条硬约定：
1. **required ⊆ allowed**：任何"需要的作用域"必须是"已授权作用域"的子集，否则抛
   `CapabilityScopeError`，不做隐式放宽。
2. 作用域是只读值对象（frozen dataclass）：校验失败只报错，不改写调用方的作用域。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


class CapabilityScopeError(ValueError):
    """作用域不足或声明非法。"""


@dataclass(frozen=True)
class CapabilityScope:
    """一次调用或一个能力的作用域声明。空字符串表示"未限定"。"""

    tenant_id: str = ""
    store_id: str = ""
    roles: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def of(cls, tenant_id: Any = "", store_id: Any = "",
           roles: Iterable[Any] = ()) -> "CapabilityScope":
        return cls(str(tenant_id or ""), str(store_id or ""),
                   frozenset(str(role) for role in roles if str(role or "")))

    def as_dict(self) -> dict[str, Any]:
        return {"tenant_id": self.tenant_id, "store_id": self.store_id,
                "roles": sorted(self.roles)}

    def covers(self, required: "CapabilityScope") -> bool:
        """已授权作用域是否覆盖所需作用域（required ⊆ allowed）。"""
        if required.tenant_id and required.tenant_id != self.tenant_id:
            return False
        if required.store_id and required.store_id != self.store_id:
            return False
        return set(required.roles) <= set(self.roles)

    def require(self, required: "CapabilityScope") -> None:
        if not self.covers(required):
            raise CapabilityScopeError(
                f"作用域不足：需要 {required.as_dict()}，已授权 {self.as_dict()}")


def roles_with_permission(permission: str,
                          role_permissions: Mapping[str, Iterable[str]]) -> frozenset[str]:
    """从权限矩阵反查持有该权限的角色（能力作用域的默认来源）。"""
    return frozenset(role for role, permissions in role_permissions.items()
                     if permission in frozenset(permissions))