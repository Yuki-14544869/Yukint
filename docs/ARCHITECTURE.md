# 架构设计 — Token 成本监控系统

## 1. 系统架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    Hermes Agent                          │
│                                                          │
│  agent.log ──┐                                           │
│  agent.log.1 │                                           │
│  agent.log.N │                                           │
│              ▼                                           │
│  ┌─────────────────────┐                                │
│  │ token_report_cron.py│  ← 每小时 cron (0 token)      │
│  │                     │                                │
│  │  1. 活性检测        │                                │
│  │  2. 解析日志        │                                │
│  │  3. 写 hourly CSV   │                                │
│  │  4. 归档对话        │                                │
│  │  5. 生成 HTML 报表  │                                │
│  │  6. 输出文本 → TG   │                                │
│  └─────────┬───────────┘                                │
│            │                                             │
│            ▼                                             │
│  ┌──────────────────┐  ┌──────────────────────┐         │
│  │ hourly CSV       │  │ logs/2026-05-26.log  │         │
│  └────────┬─────────┘  └──────────┬───────────┘         │
│           │                       │                      │
│           ▼                       ▼                      │
│  ┌─────────────────────────────────┐                    │
│  │ daily_token_report.py (v2)      │  ← 每天12:00      │
│  │                                 │    (~¥0.08)        │
│  │  1. 从 hourly CSV 汇总费用      │                    │
│  │  2. 从日志归档提取对话          │                    │
│  │  3. extract_user_messages_by_   │                    │
│  │     hour() → 按小时分组消息     │                    │
│  │  4. generate_all_summaries()    │                    │
│  │     ├ Call 1: GLM-5.1 全天总结 │                    │
│  │     └ Call 2: GLM-5.1 逐小时   │                    │
│  │       总结+爱莉希雅评语         │                    │
│  │  5. format_report() 含逐小时   │                    │
│  │     活动+🌸评语                 │                    │
│  │  6. 写 daily CSV               │                    │
│  │  7. 输出日报 → TG              │                    │
│  └──────────────┬──────────────────┘                    │
│                 │                                        │
│                 ▼                                        │
│  ┌──────────────────┐                                   │
│  │ daily CSV        │                                   │
│  └────────┬─────────┘                                   │
│           │                                              │
│           ▼                                              │
│  ┌─────────────────────────────────┐                    │
│  │ monthly_token_report.py         │  ← 每月1日        │
│  │                                 │    (0 token)       │
│  │  1. 从 daily CSV 汇总月度数据   │                    │
│  │  2. 话题分类（关键词匹配）      │                    │
│  │  3. 周趋势 + 同比               │                    │
│  │  4. 输出月报 → TG              │                    │
│  └─────────────────────────────────┘                    │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## 2. 核心模块

### 2.1 token_cost_report.py（解析库）

**职责：** 从 Hermes 日志解析对话 turn 和 API 调用，计算费用。

**关键函数：**

| 函数 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `parse_logs()` | 无（读 ALL_LOG_PATHS） | `list[Turn]` | 解析所有轮转日志，去重合并 |
| `compute_turn(turn)` | Turn dict | Stats dict | 计算单次 turn 的费用/模型/缓存率 |
| `build_table(turns, ...)` | turns + 时间范围 | HTML rows | 生成 HTML 报表行 |
| `get_pricing(model)` | 模型名 | (input, cache, output) 价格元组 | 按模型返回定价 |
| `fmt(n)` | 数字 | 字符串 | 人类可读格式（1.5M / 15K / 500） |
| `truncate_msg(msg)` | 消息文本 | 截断文本 | 语音消息提取 + 截断 |

**日志轮转处理：**
```python
ALL_LOG_PATHS = [LOG_PATH] + sorted(glob("agent.log.[0-9]*"))
# 先读 agent.log.1（旧数据），再读 agent.log（新数据）
# 解析后按 (timestamp, msg) 去重
```

**注意：** 不能对 today_lines 排序！排序会破坏状态机解析（API call 行必须在 turn 行之后）。

### 2.2 token_report_cron.py（每小时入口）

**职责：** 活性检测 + 费用播报 + 日志归档。

**执行流程：**
1. `has_recent_activity()` — 扫描 agent.log，检查过去 1h 有无用户 turn
2. `archive_daily_messages()` — 增量归档对话到 `data/logs/YYYY-MM-DD.log`
3. `extract_hour_data()` — 调用 token_cost_report 解析当前小时数据
4. `append_hourly_csv()` — 写入 hourly CSV
5. 调用 `token_cost_report.py` 生成 HTML 报表 + 打印文本摘要

**活性检测原理：**
- 扫描日志中 `conversation turn:` 行
- 过滤系统消息（`Review the conversation`、`[System note:`）
- 有匹配 → True → 继续执行
- 无匹配 → False → `sys.exit(0)` 静默退出

### 2.3 daily_token_report.py（日报 v2 — 两次 GLM-5.1 调用）

**职责：** 从 CSV 聚合当日数据，调用 GLM-5.1 生成全天总结、逐小时活动总结和爱莉希雅评语。

**v2 架构变化（相比 v1）：**

v1 使用 1 次 GLM 调用生成简单总结。v2 改为 2 次 GLM-5.1 调用，分别处理不同任务：

- **Call 1：全天总结** — 更好的推理质量，生成 2-3 句精炼概述
- **Call 2：逐小时总结 + 爱莉希雅评语** — 结构化输出，同时生成事实性活动概括和角色化评语

**新增/变更函数：**

| 函数 | 状态 | 说明 |
|------|------|------|
| `extract_user_messages_by_hour()` | **新增** | 从归档日志按小时分组提取用户消息，返回 `{"HH:00": ["[HH:MM:SS] msg", ...]}` |
| `generate_all_summaries()` | **新增** | 编排两次 GLM-5.1 调用，返回 `(daily_summary, hourly_summaries, daily_comment, hourly_comments)` |
| `format_report()` | **变更** | 新增 `hourly_summaries`, `daily_comment`, `hourly_comments` 参数，逐小时行后追加 `↳ 活动概括` 和 `🌸 爱莉希雅评语` |
| `summarize_with_glm()` | 保留（v1兼容） | 单次调用接口，仍可用于简单场景 |

**两次 GLM-5.1 调用详情：**

```
generate_all_summaries(today_str, hourly_msgs, hourly_rows, cost_data)
    │
    ├─ Call 1: 全天总结
    │   ├─ model: glm-5.1
    │   ├─ max_tokens: 2000
    │   ├─ temperature: 0.3
    │   ├─ prompt: 逐小时对话记录 → 2-3句精炼总结
    │   └─ 输出: daily_summary (str)
    │
    └─ Call 2: 逐小时 + 评语
        ├─ model: glm-5.1
        ├─ max_tokens: 4000 (reasoning ~1000-2000 + output ~1500)
        ├─ temperature: 0.5 (更高 = 更有个性)
        ├─ prompt: 逐小时记录 → 任务1: 活动概括 + 任务2: 爱莉希雅评语
        └─ 输出: hourly_summaries (dict) + daily_comment (str) + hourly_comments (dict)
```

**GLM-5.1 调用要点：**
- 端点：`https://open.bigmodel.cn/api/coding/paas/v4/chat/completions`
- GLM-5.1 是**推理模型**：`reasoning_content` 占 output token 额度
- `max_tokens` 必须设 4000+（reasoning ~1000-2000 + output ~1500）
- API key 存储在 `~/.hermes/auth.json` → `credential_pool.zai[0].access_token`
- 超时设 120 秒（推理模型 + 长 prompt 响应较慢）

**5.1 Markdown 兼容性：**

GLM-5.1 输出常含 markdown 格式（`**bold**`、`-` 列表等），解析器需做兼容处理：
```python
# 去 **bold** 标记
content_clean = re.sub(r'\*\*', '', content)
# 去 • / - 列表前缀
content_clean = re.sub(r'^[•\-]\s*', '', content_clean, flags=re.MULTILINE)
```

解析规则兼容多种格式变体：
- `HH:MM 概括` / `HH:MM: 概括`（有无冒号）
- `DAY: 评语`（全天评语标记）
- `HH:MM* 评语`（`*` 后缀标记爱莉希雅评语）
- 自动跳过 section headers（任务1、概括、评语等非数据行）

**费用估算（v2）：**
- Call 1: Prompt ~500 + Reasoning ~800 + Output ~200 ≈ 1500 tokens
- Call 2: Prompt ~800 + Reasoning ~1500 + Output ~1200 ≈ 3500 tokens
- 总计 ≈ ¥0.08/天

**extract_user_messages_by_hour() 与 v1 的区别：**

| 特性 | v1 `extract_user_messages()` | v2 `extract_user_messages_by_hour()` |
|------|------|------|
| 返回类型 | `list[str]` | `dict[str, list[str]]` |
| 分组方式 | 平铺列表 | 按 `HH:00` 分组 |
| 系统过滤 | 基础4种 | +`⏳ Still working` |
| 消息截断 | 80 字符 | 100 字符 |

### 2.4 monthly_token_report.py（月报）

**职责：** 纯 CSV 聚合，话题分类，趋势分析。

**话题分类：** 基于 daily CSV 中的 `summary` 字段做关键词匹配。分类词典：
```python
"🔧 代码开发": ["代码", "脚本", "调试", "bug", ...]
"🤖 AI/LLM":   ["token", "模型", "GLM", ...]
"📱 鸿蒙自动化": ["鸿蒙", "HDC", "签到", ...]
...
```

## 3. 数据模型

### hourly_token_costs.csv

| 字段 | 类型 | 说明 |
|------|------|------|
| timestamp | string | `YYYY-MM-DD HH:00` |
| date | string | `YYYY-MM-DD` |
| hour | string | `HH:00` |
| cost | float | 该小时费用（元） |
| messages | int | 用户消息数 |
| api_calls | int | API 调用次数 |
| tokens_in | int | 输入 token |
| tokens_out | int | 输出 token |
| tokens_cached | int | 缓存 token |
| cache_rate | string | 缓存率百分比 |
| models | string | 模型使用统计（如 `5.1×75+4.7×3`） |

### daily_token_costs.csv

| 字段 | 类型 | 说明 |
|------|------|------|
| date | string | 日期 |
| total_cost | float | 当日总费用 |
| messages | int | 当日消息数 |
| ... | ... | （同 hourly 聚合） |
| summary | string | **GLM-5.1 AI 生成的当日总结** |

### logs/YYYY-MM-DD.log

Hermes 原始日志的子集，只保留：
- `conversation turn:` 行（含消息内容）
- `API call #N:` 行（含 token 统计）
- `Turn ended:` 行（含响应长度）

## 4. 关键设计决策

| 决策 | 理由 |
|------|------|
| 只在每小时解析日志 | 避免重复解析，日报/月报纯从 CSV 读取 |
| 每天 2 次 GLM-5.1 调用 | v1 只调1次生成简单总结；v2 拆分为全天总结+逐小时+评语，兼顾推理质量和角色化输出 |
| 日志归档独立存储 | Hermes 日志会轮转丢失，归档是永久记录 |
| no_agent 模式 | 所有 cron 脚本零 token 消耗（除日报 GLM 调用） |
| 去重用 (timestamp, msg) | 同一秒可能有不同对话，不能只按时间戳去重 |
| max_tokens=4000 | 5.1 推理链消耗 ~1000-2000 tokens，必须留足够空间给实际输出 |
| 解析器去 markdown | 5.1 输出含 `**bold**`、`-` 列表等，需正则清理后再解析结构 |
| 去除 cost 过滤 | v2 展示所有有用户消息的小时（v1 只展示有费用的小时） |

## 5. 已知限制

- 话题分类依赖硬编码关键词，新话题需要手动更新
- GLM-5.1 推理模型偶尔会因 `max_tokens` 不够只输出 reasoning 不输出 content
- 日志归档不包含 AI 回复内容（只有用户消息和 API 统计）
- 5.1 markdown 输出格式不稳定，解析器需持续兼容新变体
- 逐小时评语依赖 5.1 严格遵循 `HH:MM*` 格式，偶尔解析失败
