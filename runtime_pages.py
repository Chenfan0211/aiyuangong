# -*- coding: utf-8 -*-
"""仅用于本地演示的页面注入工具。"""
from __future__ import annotations

import os
import re
from html import escape
from pathlib import Path
from typing import Mapping, Optional

DEMO_USER_ENV = "STORE_AGENT_DEMO_PREFILL_USER"
DEMO_PASSWORD_ENV = "STORE_AGENT_DEMO_PREFILL_PASSWORD"


def _inject_value(content: str, input_id: str, value: str) -> str:
    pattern = re.compile(rf'<input id="{re.escape(input_id)}"([^>]*?)(\s*/>)')
    escaped = escape(value, quote=True)

    def replace(match: re.Match[str]) -> str:
        attrs = match.group(1)
        if re.search(r"\bvalue\s*=", attrs):
            return match.group(0)
        return f'<input id="{input_id}"{attrs} value="{escaped}"{match.group(2)}'

    return pattern.sub(replace, content, count=1)


def render_demo_agent_page(path: Path | str, env: Optional[Mapping[str, str]] = None) -> str:
    """只有在显式提供演示账号和口令时才给登录框填默认值。"""
    content = Path(path).read_text(encoding="utf-8")
    values = dict(os.environ if env is None else env)
    username = str(values.get(DEMO_USER_ENV) or "").strip()
    password = str(values.get(DEMO_PASSWORD_ENV) or "")
    if not username or not password:
        return content
    return _inject_value(
        _inject_value(content, "username", username),
        "password",
        password,
    )