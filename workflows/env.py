#!/usr/bin/env python3
"""极简 ``.env`` 加载器（仅标准库）。

把仓库根目录 ``.env`` 中的 ``KEY=VALUE`` 读入 ``os.environ``。默认**不覆盖**
已存在的环境变量，因此在 CI 中仍然以真实的 Secrets 为准；本地缺失的变量则由
``.env`` 补齐。

``.env`` 已列入 ``.gitignore``，不会被提交。
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = _REPO_ROOT / ".env"


def load_dotenv(
    path: str | Path | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """从 ``.env`` 文件加载变量到 ``os.environ``。

    Args:
        path: ``.env`` 路径；默认使用仓库根目录的 ``.env``。
        override: 为 ``True`` 时覆盖已存在的环境变量；默认不覆盖。

    Returns:
        实际从文件中解析出的键值对；文件不存在时为空 dict。
    """
    env_path = Path(path) if path else DEFAULT_ENV_FILE
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()

        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value

    return loaded


def load_env(*, override: bool = False) -> dict[str, str]:
    """加载仓库根目录 ``.env``（:func:`load_dotenv` 的便捷封装）。"""
    return load_dotenv(DEFAULT_ENV_FILE, override=override)


__all__ = ["DEFAULT_ENV_FILE", "load_dotenv", "load_env"]
