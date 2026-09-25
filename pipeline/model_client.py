"""
模型客户端 — 流水线版本（V2 遗留）

V3 中推荐使用 workflows/model_client.py，此文件保持向后兼容。
"""

import sys
from pathlib import Path

# 以脚本方式运行时（python pipeline/pipeline.py）需要把仓库根目录加入导入路径
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 直接复用 workflows 版本
from workflows.model_client import (  # noqa: F401, E402
    PROVIDERS,
    CostRecord,
    CostTracker,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderConfig,
    Usage,
    accumulate_usage,
    chat,
    chat_json,
    chat_with_retry,
    cost_tracker,
    create_provider,
    estimate_cost,
    estimate_tokens,
    get_client,
    get_cost_guard,
    get_provider,
    quick_chat,
)
