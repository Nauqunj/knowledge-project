---
name: tech-summary
description: 当需要对采集的技术内容进行深度分析总结时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# Tech Summary 深度分析总结

## 使用场景

- 已有一批采集好的技术内容，需要逐条深度分析并总结时。
- 需要为技术情报流水线产出摘要、评分、标签与趋势判断时。
- 关键词触发：“生成技术摘要”“深度分析”“相关性评分”“打标签”。

## 执行步骤

1. **读取采集文件**
   - 读取 `knowledge/raw/` 下**最新**的采集文件（按文件名中的日期判断，必要时读取多个来源文件）。

2. **逐条深度分析**
   - `summary`：**不超过 50 字**，直接说清是什么。
   - `highlights`：技术亮点 **2-3 个**，用**事实**说话（具体能力、数据、设计取舍），不要空泛形容词。
   - `score`：**1-10 分**，并附 `score_reason` 说明理由（见下方评分标准）。
   - `tags`：给 3-5 个标签建议，英文小写、连字符分隔。

3. **趋势发现**
   - `common_themes`：本批内容反复出现的共同主题。
   - `emerging_concepts`：值得关注的新概念、新技术方向。

4. **输出分析结果 JSON**
   - 汇总为下述结构，UTF-8、2 空格缩进、不转义中文。

## 评分标准

| 分数 | 含义 |
|------|------|
| 9-10 | 改变格局（对 AI/LLM/Agent 领域有重大影响） |
| 7-8 | 直接有帮助（工程师能直接用上） |
| 5-6 | 值得了解 |
| 1-4 | 可略过 |

## 注意事项

- **分数分布约束**：一批 15 个项目中，**9-10 分不超过 2 个**，避免评分通胀。
- `summary` 严格 ≤ 50 字，技术术语保留英文原文，不用模板化开头。
- `highlights` 必须基于事实，不夸大、不臆测；信息不足时不编造。
- `score_reason` 与 `score` 必须一致，能自圆其说。
- WebFetch 仅用于补充必要上下文；抓不到就基于已有信息分析。
- 只做分析总结，不落地最终知识条目。

## 输出格式

```json
{
  "source": "github",
  "skill": "tech-summary",
  "analyzed_at": "2026-09-23T11:00:00Z",
  "count": 15,
  "items": [
    {
      "name": "openai/agents-sdk",
      "url": "https://github.com/openai/agents-sdk",
      "summary": "OpenAI 官方 Agent 开发 SDK，提供 Handoff 与 Guardrails 核心原语。",
      "highlights": [
        "内置 Handoff 任务交接机制，支持多 Agent 协作",
        "提供 Guardrails 安全护栏，可约束 Agent 行为"
      ],
      "score": 8,
      "score_reason": "官方实现、可直接用于生产，但偏工程封装、底层创新有限。",
      "tags": ["agent-framework", "multi-agent", "python", "openai"]
    }
  ],
  "trends": {
    "common_themes": ["多 Agent 协作", "Agent 安全护栏"],
    "emerging_concepts": ["类型化决策模型用于模型路由"]
  }
}
```
