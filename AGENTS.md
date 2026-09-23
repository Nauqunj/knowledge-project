# AGENTS.md

自动化技术情报收集与分析系统：持续追踪 GitHub、Hacker News、arXiv 等来源，
把分散的技术资讯转成结构化、可检索的 JSON 知识条目。

> 当前已有 `.opencode/agent/`（collector / analyzer / organizer）、
> `.opencode/skill/`（github-trending / tech-summary）与 `knowledge/` 下的样例数据；
> `src/`、`tests/`、`pyproject.toml` 等脚手架尚未落地，相关命令仍为占位。

## 流水线架构（三阶段）

`采集 (collect) → 分析 (analyze) → 整理 (organize)`，用 **LangGraph** 编排为 Agent 协作流水线。

各阶段由 `.opencode/agent/` 下的子 Agent 定义，职责与边界如下：

| 阶段 | Agent | 读 | 写 | 说明 |
|------|-------|----|----|------|
| 采集 | `collector` | 外部 API | `knowledge/raw/` | 只采集，不分析 |
| 分析 | `analyzer` | `knowledge/raw/` | `knowledge/analyzed/` | **禁止写 `knowledge/raw/`** |
| 整理 | `organizer` | `knowledge/analyzed/`、`knowledge/raw/` | `knowledge/raw/`、`knowledge/articles/` | **禁止 WebFetch**，不得自行补数据 |

**写入权责（关键，易错）**：
- `analyzer` 只做“读取 + 思考 + 写暂存”：**不得修改 `knowledge/raw/`**，
  分析结果写入 `knowledge/analyzed/{date}.json`（含 `analyzed_at`）。
- 为什么用暂存文件：分析结果体量大，经对话转交会超出单条消息上限而截断，必须落盘。
- `organizer` 读取 `knowledge/analyzed/{date}.json`，校验 / 过滤 / 去重后产出
  `knowledge/articles/` 与索引；并把已分析条目**归档写回 `knowledge/raw/`**。
- 因此 `knowledge/raw/` 既存原始条目，也存**已分析**的条目（含 `analyzed_at`）。

## 目录与数据契约

```
knowledge/raw/        采集原始 + 分析后的条目（含 analyzed_at）、filtered-{date}.json 过滤日志
knowledge/analyzed/   analyzer 写入的暂存分析结果 {date}.json（含 analyzed_at）
knowledge/articles/   正式 JSON 知识条目 {date}-{slug}.json + index.json
.opencode/agent/      collector / analyzer / organizer 子 Agent 定义
src/                  流水线代码（计划，按 collect / analyze / organize 分模块）
tests/                测试（计划）
Dockerfile / docker-compose.yml（计划）
```

- 采集文件按来源 + 日期分文件（如 `github-trending-{date}.json`、`hackernews-top-{date}.json`）。
- 知识条目是对外契约，schema 稳定；改动需谨慎并显式说明。

## 分析字段契约（analyzer / tech-summary）

分析规范以 `tech-summary` 技能为准，字段如下：

- `summary`：**≤ 50 字**中文，术语保留英文。
- `highlights`：**2-3 个**技术亮点，用事实说话。
- `score`：**1-10 整数** + `score_reason`。9-10 改变格局、7-8 直接有帮助、5-6 值得了解、1-4 可略过。
- `tags`：3-5 个，英文小写连字符。
- `analyzed_at`：ISO 8601。
- 文件级 `trends`：`common_themes`、`emerging_concepts`。

**约束**：一批 15 个项目中 **9-10 分不超过 2 个**（防评分通胀）。
**过滤门槛**：organizer 丢弃 `score < 5` 及 `summary` 为空或超过 50 字的条目。

## 约定

- **幂等**：按来源 URL / 外部 ID 去重；当天文件已存在时**追加去重，不覆盖**。
- 严格**限速与重试**：尊重 rate limit / robots，arXiv、HN 的 API 要节流；失败重试带指数退避。
- 所有密钥走环境变量（`GITHUB_TOKEN`、`ARXIV_MAILTO`、LLM API key 等），**绝不写入仓库或日志**。
- 编码统一 UTF-8，不转义中文。
- Cron / Docker 下注意时区与“每日”任务的边界，日志要能定位单次运行。

## 已知环境与坑

- 本机 `uv` **未安装**；已验证可用：`python 3.13.1`、`node v22.23.2`（若继续用 `uv sync` 需先装 uv）。
- arXiv API 直连常被 CDN 以 **HTTP 406** 拒绝，需带联系方式 UA（`ARXIV_MAILTO`）并严格节流。
- GitHub API 必须带 `Accept: application/vnd.github.v3+json`；403/429 时读 `X-RateLimit-Reset` 等待。

## 命令（脚手架落地后确认）

> 占位——创建 `pyproject.toml` 后替换为真实命令。

- 安装依赖：`uv sync`（若改用 pip/poetry 请同步修正）
- 跑单阶段：`python -m <pkg> collect|analyze|organize`
- 跑完整流水线：`python -m <pkg> run`
- 测试：`pytest` / 单测：`pytest path::test_name`
- Lint / 格式化：`ruff check .`、`ruff format .`（若采用 ruff）
