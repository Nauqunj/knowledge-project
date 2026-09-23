---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools: Read, Grep, Glob, WebFetch
---

# GitHub Trending 采集

## 使用场景

- 需要追踪 GitHub 上 AI / LLM / Agent 领域的热门开源项目时。
- 为技术情报流水线补充最新仓库线索时。
- 关键词触发：“采集 GitHub Trending”“拉取 GitHub 仓库”“收集 AI 开源项目”。

## 执行步骤

1. **搜索热门仓库（GitHub API）**
   - 端点：`https://api.github.com/search/repositories`
   - 关键词：`AI OR LLM OR agent OR "large language model" OR RAG OR MCP`
   - 排序 `sort=stars&order=desc`，时间窗口过去 7 天（`created:>YYYY-MM-DD`），`per_page=20`
   - 请求头带 `Accept: application/vnd.github.v3+json`；有 `GITHUB_TOKEN` 则用于认证。

2. **提取信息**
   - 从返回 JSON 逐条提取：`full_name`、`name`、`description`、`html_url`、`stargazers_count`、`language`、`topics`、`created_at`、`pushed_at`。

3. **过滤**
   - **纳入**：属于 AI / LLM / Agent 及紧密相关方向（RAG、MCP、多模态、模型工具链等）。
   - **排除**：与 AI 无关的纯工具库、模板、awesome 列表、面试题、营销页、无技术实质的仓库。
   - 明显无关的直接丢弃，边界模糊的保留待摘要阶段再判定。

4. **生成摘要与亮点**
   - 为每条写 **≤ 50 字**中文摘要（直接说清是什么），并提炼 **2-3 个**技术亮点（用事实说话），术语保留英文，不用模板化开头。
   - 具体规范见 `tech-summary` 技能。

5. **评分与打标签**
   - 给出 `score`（1-10 整数）与 `score_reason`，以及 `tags`（3-5 个，英文小写连字符）。
   - 一批 15 个项目中 **9-10 分不超过 2 个**。

6. **去重（幂等）**
   - 若当天文件已存在，先读取，再按 `name`（`full_name`）追加去重，**不要覆盖**。

7. **写入 JSON 文件**
   - 输出到 `knowledge/raw/github-trending-{YYYY-MM-DD}.json`，UTF-8、2 空格缩进、不转义中文。

## 注意事项

- **限流**：HTTP 403 / 429 时读取 `X-RateLimit-Reset` 头并等待；失败重试带指数退避。未认证 60 次/小时，认证后 5000 次/小时。
- **密钥**：只用环境变量 `GITHUB_TOKEN`，绝不写入文件或日志。
- **编码**：UTF-8，不转义中文。
- **文件名**：必须含当天日期。
- 只做采集与摘要，不落地最终知识条目（由 organizer 负责）。

## 输出格式

```json
{
  "source": "github",
  "skill": "github-trending",
  "collected_at": "2026-09-23T10:30:00Z",
  "count": 20,
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
      "stars": 15200,
      "language": "Python",
      "topics": ["ai", "agents", "openai", "llm"],
      "tags": ["agent-framework", "multi-agent", "python", "openai"]
    }
  ]
}
```

质量检查：
- [ ] 每条含非空 `name`、`url`、`summary`
- [ ] `summary` ≤ 50 字，`highlights` 2-3 个
- [ ] `score` 为 1-10 整数，附 `score_reason`
- [ ] `url` 以 `https://` 开头
- [ ] `stars` 为数字类型
- [ ] 无重复 `name`
- [ ] `collected_at` 为 ISO 8601
- [ ] JSON 合法、文件名含当天日期
