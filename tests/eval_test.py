#!/usr/bin/env python3
"""AI 知识库分析质量的评估测试（pytest）。

评估对象：把一段输入交给 LLM 分析，产出 ``summary`` / ``keywords`` /
``relevance``，再用范围断言（``>=``、``<=``、``in``）判断是否满足场景预期。

用例分三类：
- 正面：技术文章 -> 应有摘要、关键词，相关度高。
- 负面：无关内容 -> 相关度低（被过滤/标记低相关）。
- 边界：极短输入（"AI"）-> 不得崩溃，返回结构合法。

LLM 相关测试标记为 ``slow``，未配置密钥时自动跳过：
    pytest tests/eval_test.py -m "not slow"   # 只跑本地结构校验
    pytest tests/eval_test.py                 # 跑全部（含 LLM）
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path

import pytest
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

try:
    from pytest import PytestUnknownMarkWarning
except ImportError:  # 兼容旧版 pytest
    PytestUnknownMarkWarning = Warning  # type: ignore[assignment, misc]

warnings.filterwarnings("ignore", category=PytestUnknownMarkWarning)

from workflows.model_client import chat  # noqa: E402

_PROVIDER_KEY_ENV = {
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _load_llm_key() -> str:
    """读取 LLM 密钥，并在仅有 LLM_API_KEY 时补到当前 provider 的变量上。"""
    provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    key_env = _PROVIDER_KEY_ENV.get(provider, "DEEPSEEK_API_KEY")
    generic = os.getenv("LLM_API_KEY", "").strip()
    specific = os.getenv(key_env, "").strip()
    if generic and not specific:
        os.environ[key_env] = generic
    return specific or generic


LLM_KEY = _load_llm_key()

SYSTEM_PROMPT = "你是技术情报分析助手。只输出一个 JSON 对象，不要输出任何解释或 Markdown。"

ANALYZE_PROMPT = """分析下面的内容，只输出 JSON：
{{"summary": "不超过50字的中文摘要", "keywords": ["3-5个小写连字符关键词"], "relevance": 0到10的整数, "filtered": true或false}}

判定标准：与技术 / 开源 / 科研相关则 relevance >= 5；完全无关则 relevance <= 4 且 filtered 为 true。
若内容过短或信息不足，给出能给出的结果，relevance 取低值，不要报错。

内容：
\"\"\"
{text}
\"\"\"
"""

JUDGE_SYSTEM = "你是严格的技术内容评审。只输出一个 1-10 的整数，不要任何其他文字。"

JUDGE_PROMPT = """给下面的分析结果打分（1-10），评判摘要是否准确概括原文、关键词是否贴合。
只输出整数，不要解释。

原文：
{content}

摘要：{summary}
关键词：{keywords}
"""

EVAL_CASES: list[dict] = [
    {
        "name": "positive-technical-article",
        "kind": "positive",
        "input": (
            "LangGraph 是一个用于构建有状态多 Agent 应用的编排框架。它把工作流"
            "建模成图，节点是 Agent 或工具调用，边决定状态如何在节点之间传递，"
            "并原生支持检查点、暂停与人工介入。相比链式调用，LangGraph 更适合"
            "需要循环、分支和持久化的复杂推理流水线。"
        ),
        "expected": {
            "relevance_min": 5,
            "summary_min_len": 15,
            "keywords_min": 2,
            "any_of": ["LangGraph", "agent", "智能体", "编排", "图", "工作流"],
        },
    },
    {
        "name": "negative-irrelevant-content",
        "kind": "negative",
        "input": (
            "今天中午做了番茄炒蛋，先把鸡蛋打散加一点盐，热锅凉油下锅翻炒，"
            "然后加入切好的番茄，最后加糖和葱花出锅，配米饭特别香。"
        ),
        "expected": {
            "relevance_max": 4,
        },
    },
    {
        "name": "boundary-minimal-input",
        "kind": "boundary",
        "input": "AI",
        "expected": {
            "relevance_min": 0,
            "relevance_max": 10,
        },
    },
]

_REQUIRED_CASE_KEYS = {"name", "input", "expected"}
_KNOWN_EXPECTED_KEYS = {
    "relevance_min",
    "relevance_max",
    "summary_min_len",
    "keywords_min",
    "any_of",
}


def _extract_json(raw: str) -> dict:
    """从可能带 Markdown 围栏的文本里抽出第一个 JSON 对象。"""
    cleaned = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"LLM 未返回 JSON：{raw[:200]!r}")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"LLM 返回的不是 JSON 对象：{raw[:200]!r}")
    return data


def _normalize(data: dict) -> dict:
    """把 LLM 的自由输出规整成统一、可断言的结构。"""
    summary = str(data.get("summary") or "").strip()
    raw_keywords = data.get("keywords") or []
    if isinstance(raw_keywords, str):
        raw_keywords = re.split(r"[,，;；、]", raw_keywords)
    keywords = [str(item).strip() for item in raw_keywords if str(item).strip()]
    try:
        relevance = int(round(float(data.get("relevance", 0))))
    except (TypeError, ValueError):
        relevance = 0
    relevance = max(0, min(10, relevance))
    return {
        "summary": summary,
        "keywords": keywords,
        "relevance": relevance,
        "filtered": bool(data.get("filtered", False)),
    }


def analyze_with_llm(text: str) -> dict:
    """调用 ``chat`` 分析输入文本并返回规整结果。"""
    reply, _usage = chat(
        ANALYZE_PROMPT.format(text=text),
        system=SYSTEM_PROMPT,
        temperature=0,
    )
    return _normalize(_extract_json(reply))


def _check_expectations(result: dict, expected: dict) -> None:
    """按范围条件校验分析结果，不做精确匹配。"""
    relevance = result["relevance"]
    bound = expected.get("relevance_min")
    if bound is not None:
        assert relevance >= bound, f"relevance={relevance} 应 >= {bound}"
    bound = expected.get("relevance_max")
    if bound is not None:
        assert relevance <= bound, f"relevance={relevance} 应 <= {bound}"

    min_len = expected.get("summary_min_len", 0)
    if min_len:
        assert len(result["summary"]) >= min_len, (
            f"summary 长度 {len(result['summary'])} 应 >= {min_len}："
            f"{result['summary']!r}"
        )

    keywords_min = expected.get("keywords_min", 0)
    if keywords_min:
        assert len(result["keywords"]) >= keywords_min, (
            f"keywords 数量 {len(result['keywords'])} 应 >= {keywords_min}"
        )

    any_of = expected.get("any_of") or []
    if any_of:
        blob = (result["summary"] + " " + " ".join(result["keywords"])).lower()
        assert any(term.lower() in blob for term in any_of), (
            f"摘要或关键词未命中任一词 {any_of}：{blob!r}"
        )


def _require_llm() -> None:
    if not LLM_KEY:
        pytest.skip("未配置 LLM_API_KEY（或 provider 对应的 *_API_KEY），跳过 LLM 用例")


def test_eval_cases_structure() -> None:
    """本地校验：EVAL_CASES 结构完整、场景齐全、范围合法（不调用 LLM）。"""
    assert len(EVAL_CASES) >= 3, "至少需要 3 个评估用例"

    seen_kinds = set()
    for case in EVAL_CASES:
        assert _REQUIRED_CASE_KEYS <= case.keys(), f"用例缺少字段：{case.get('name')}"
        assert isinstance(case["name"], str) and case["name"]
        assert isinstance(case["input"], str) and case["input"]
        expected = case["expected"]
        assert isinstance(expected, dict) and expected
        assert set(expected) <= _KNOWN_EXPECTED_KEYS, f"未知期望字段：{set(expected)}"

        low = expected.get("relevance_min")
        high = expected.get("relevance_max")
        if low is not None:
            assert 0 <= low <= 10, f"relevance_min 越界：{low}"
        if high is not None:
            assert 0 <= high <= 10, f"relevance_max 越界：{high}"
        if low is not None and high is not None:
            assert low <= high, f"relevance 区间反了：{low} > {high}"
        assert expected.get("summary_min_len", 0) >= 0
        assert expected.get("keywords_min", 0) >= 0

        kind = case.get("kind")
        assert kind in {"positive", "negative", "boundary"}, f"未知场景：{kind}"
        seen_kinds.add(kind)

    assert {"positive", "negative", "boundary"} <= seen_kinds, "必须覆盖三类场景"


@pytest.mark.slow
@pytest.mark.parametrize("case", EVAL_CASES, ids=[c["name"] for c in EVAL_CASES])
def test_eval_case(case: dict) -> None:
    """逐个用例调用 LLM 分析并按范围断言（需要密钥，标记 slow）。"""
    _require_llm()
    result = analyze_with_llm(case["input"])
    _check_expectations(result, case["expected"])


@pytest.mark.slow
def test_llm_as_judge() -> None:
    """LLM-as-Judge：让 LLM 给正面用例的分析结果打分，断言 >= 5。"""
    _require_llm()
    case = next(item for item in EVAL_CASES if item["kind"] == "positive")
    result = analyze_with_llm(case["input"])

    reply, _usage = chat(
        JUDGE_PROMPT.format(
            content=case["input"],
            summary=result["summary"],
            keywords=", ".join(result["keywords"]),
        ),
        system=JUDGE_SYSTEM,
        temperature=0,
    )
    match = re.search(r"\b(10|[1-9])\b", reply)
    assert match is not None, f"未能从评审输出解析分数：{reply[:200]!r}"
    score = int(match.group(1))
    assert 1 <= score <= 10, f"分数越界：{score}"
    assert score >= 5, f"LLM 评审分数 {score} 应 >= 5"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
