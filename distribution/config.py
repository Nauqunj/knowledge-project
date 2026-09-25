#!/usr/bin/env python3
"""分发渠道密钥配置加载。

优先从 ``distribution/config.json`` 读取渠道密钥，环境变量作为兜底。
可用 ``DISTRIBUTION_CONFIG_PATH`` 指定其它配置文件路径。

配置样例见 ``distribution/config.example.json``。真实的
``config.json`` 含密钥，已加入 ``.gitignore``，切勿提交。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
CONFIG_PATH_ENV = "DISTRIBUTION_CONFIG_PATH"


def config_path(path: str | Path | None = None) -> Path:
    """解析配置文件路径。

    Args:
        path: 显式路径；为空时读 ``DISTRIBUTION_CONFIG_PATH``，再回退到
            ``distribution/config.json``。

    Returns:
        配置文件路径。
    """
    if path is not None:
        return Path(path)
    env_path = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(env_path) if env_path else DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取整个配置文件。

    Args:
        path: 配置文件路径；默认见 :func:`config_path`。

    Returns:
        解析后的 dict；文件缺失、不可读或损坏时返回空 dict。
    """
    target = config_path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def channel_config(
    channel: str, path: str | Path | None = None
) -> dict[str, Any]:
    """读取某个渠道的配置段。

    Args:
        channel: 渠道名，如 ``telegram`` / ``feishu``。
        path: 配置文件路径；默认见 :func:`config_path`。

    Returns:
        该渠道的配置 dict；缺失时返回空 dict。
    """
    section = load_config(path).get(channel)
    return section if isinstance(section, dict) else {}


__all__ = [
    "CONFIG_PATH_ENV",
    "DEFAULT_CONFIG_PATH",
    "channel_config",
    "config_path",
    "load_config",
]
