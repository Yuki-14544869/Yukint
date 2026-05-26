# 需求说明书 — Token 成本监控系统

## 1. 项目背景

用户使用 Hermes Agent + GLM Coding Plan 进行日常开发和自动化。Coding Plan 按实际 token 消耗计费，费用随使用量波动较大。用户需要一个自动化系统来：

- 了解每天/每月花了多少钱
- 知道钱花在了什么事情上（话题关联）
- 发现使用趋势和异常
- 在不活跃时不浪费额外 token

## 2. 用户角色

| 角色 | 描述 |
|------|------|
| 用户 | 查看费用报告、分析消费趋势 |
| 系统 | Hermes Agent cron 自动运行各报告脚本 |

## 3. 功能需求

### FR-01 每小时 Token 播报

- **触发：** 每整点自动运行（Hermes cron，no_agent 模式）
- **前置条件：** 检测过去 1 小时内是否有用户对话活动
- **行为：**
  - 有活动 → 解析日志，计算该小时费用，发送 Telegram 播报
  - 无活动 → 静默退出，不发消息
- **输出：** 简短文本（今日费用、过去 1h 费用、429 次数）
- **副作用：** 将费用数据写入 `hourly_token_costs.csv`，将原始对话归档到 `logs/YYYY-MM-DD.log`
- **成本约束：** 0 token（纯 Python 脚本）

### FR-02 每日 Token 报告

- **触发：** 每天 12:00（Hermes cron，no_agent 模式）
- **行为：**
  - 从 hourly CSV 汇总当日费用数据
  - 从日志归档读取当日对话记录
  - 调用 GLM-5.1 API 生成 2-3 句话的高质量日总结
  - 将汇总 + AI 总结写入 daily CSV
  - 发送 Telegram 日报
- **输出：** AI 总结 + 逐小时费用明细 + 近 7 天趋势
- **成本约束：** ~¥0.04/天（GLM-5.1 一次调用）

### FR-03 每月 Token 报告

- **触发：** 每月 1 日 10:00（Hermes cron，no_agent 模式）
- **行为：**
  - 从 daily CSV 读取上月所有数据
  - 聚合月度总费用、日均、消息数
  - 基于 AI 总结进行话题分类
  - 计算周趋势、同比变化
  - 发送 Telegram 月报
- **输出：** 月度汇总 + 每日明细（含 AI 总结）+ 话题分布 + 周趋势 + 上月同比 + 最贵 3 天
- **成本约束：** 0 token（纯 CSV 汇总）

### FR-04 日志归档

- **触发：** 每小时播报时附带执行
- **行为：**
  - 读取 Hermes agent.log + 轮转日志（agent.log.1 等）
  - 提取当日对话 turn、API call、turn ended 行
  - 追加到 `data/logs/YYYY-MM-DD.log`（增量写入，不重复）
- **保留策略：** 永久保留（用户需求：以后可复盘）

### FR-05 日志轮转兼容

- **问题：** Hermes 系统日志在文件过大时自动轮转（agent.log → agent.log.1），导致只读当前日志会丢失历史数据
- **行为：** 所有解析函数同时读取 agent.log + agent.log.N，按时间顺序处理并去重

### FR-06 费用计算

- **GLM-5.1 定价：** 输入 ¥6/MTok、缓存 ¥1.5/MTok、输出 ¥24/MTok
- **GLM-4.7 定价：** 输入 ¥1/MTok、缓存 ¥0.25/MTok、输出 ¥4/MTok
- **公式：** `费用 = (有效输入/1M × 输入价) + (缓存/1M × 缓存价) + (输出/1M × 输出价)`
- **缓存率：** `缓存率 = 缓存token / 总输入token × 100%`

## 4. 非功能需求

| 编号 | 需求 | 说明 |
|------|------|------|
| NFR-01 | 零 token 报告 | 除日报 GLM 总结外，所有报告脚本不消耗 LLM token |
| NFR-02 | 月报告成本 < ¥2 | 每天日报 ~¥0.04 × 30 = ~¥1.2/月 |
| NFR-03 | 日志归档持久化 | 归档日志永久保留，不受 Hermes 日志轮转影响 |
| NFR-04 | 数据完整性 | 日志轮转不丢失数据，去重不误删不同对话 |

## 5. 数据存储

| 文件 | 路径 | 说明 |
|------|------|------|
| 小时 CSV | `~/.hermes/data/hourly_token_costs.csv` | 每小时一行（费用、模型、时间） |
| 日 CSV | `~/.hermes/data/daily_token_costs.csv` | 每天一行（汇总 + AI 总结） |
| 日志归档 | `~/.hermes/data/logs/YYYY-MM-DD.log` | 每天一个文件（原始对话记录） |

## 6. Cron 配置

| 名称 | Cron ID | 时间 | 脚本 | 模式 |
|------|---------|------|------|------|
| 每小时播报 | 006bda9176a7 | `0 * * * *` | token_report_cron.py | no_agent |
| 每日报告 | 44f888b75880 | `0 12 * * *` | daily_token_report.py | no_agent |
| 每月报告 | 97f5c456bf9f | `0 10 1 * *` | monthly_token_report.py | no_agent |

## 7. 约束与假设

- Hermes Agent 日志格式固定（`YYYY-MM-DD HH:MM:SS INFO ...`）
- GLM API 使用 OpenAI 兼容协议（`/chat/completions`）
- GLM-5.1 为推理模型，`reasoning_content` 占用 output token 额度
- Coding Plan 优惠期内（2026 年 6 月底前）非高峰额度 ×2
