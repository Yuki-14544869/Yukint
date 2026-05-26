# Yukint 🛠️

> [Yukint](https://github.com/Yuki-14544869) 的个人自动化工具集，由 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 协作开发和维护。

本项目收集了日常使用的自动化脚本，涵盖 Telegram 签到、LLM Token 成本监控、鸿蒙手机自动化等场景。

## 项目结构

```
Yukint/
├── checkin/                    # Telegram 群组自动签到
│   ├── tg_checkin_playwright.py    主脚本（Playwright 驱动）
│   └── tests_test_unit.py          45 个单元测试
├── monitoring/                 # LLM Token 成本监控
│   ├── token_cost_report.py        核心解析库（日志 → 费用数据）
│   ├── token_report_cron.py        每小时播报 + 日志归档（cron 调用）
│   ├── daily_token_report.py       日报 + GLM-5.1 AI 总结（每天 12:00）
│   ├── monthly_token_report.py     月报 + 趋势分析（每月 1 日）
│   ├── TEST_CASES.md               测试用例设计表
│   └── sample_daily_token_costs.csv 样例数据
├── harmonyos/                  # 鸿蒙自动签到（开发中）
├── tests/                      # 公共测试
│   └── test_token_report.py        34 个测试（解析/计算/活性检测）
├── docs/
│   ├── REQUIREMENTS.md             需求说明书
│   └── ARCHITECTURE.md             架构设计文档
├── README.md                   # 本文件
└── LICENSE
```

## 功能模块

### 📊 Token 成本监控（monitoring/）

**解决的问题：** 追踪 GLM（智谱 AI）Coding Plan 的 token 消耗，让用户了解每天花了多少钱、花在了什么地方。

**三层报告体系：**

| 报告 | 频率 | 成本 | 内容 |
|------|------|------|------|
| 逐小时播报 | 每整点 | ¥0（纯 Python） | 费用概览，无活跃则静默 |
| 每日报告 | 每天 12:00 | ~¥0.04（GLM-5.1 一次调用） | 逐小时明细 + AI 总结 |
| 每月报告 | 每月 1 日 | ¥0（纯 CSV 汇总） | 日报汇总 + 话题分析 + 同比 |

**数据管线：**

```
Hermes agent.log（系统日志）
        │
        ▼ 每小时 cron 解析（0 token）
┌───────────────────┐
│ hourly CSV        │ ← 费用、模型、时间
│ logs/2026-05-26.log │ ← 原始对话归档
└───────────────────┘
        │
        ▼ 每天 12:00 聚合（~¥0.04）
┌───────────────────┐
│ daily CSV         │ ← 日汇总 + GLM-5.1 AI 总结
└───────────────────┘
        │
        ▼ 每月 1 日聚合（0 token）
┌───────────────────┐
│ 月报文本          │ ← 话题分类 + 周趋势 + 同比
└───────────────────┘
```

**定价参考（GLM Coding Plan）：**
- GLM-5.1：输入 ¥6/M、缓存 ¥1.5/M、输出 ¥24/M
- GLM-4.7：输入 ¥1/M、缓存 ¥0.25/M、输出 ¥4/M

### ✅ Telegram 签到（checkin/）

自动在 Telegram 群组中完成每日签到任务（IKUUU、GFC 等），基于 Playwright 浏览器自动化。

### 📱 鸿蒙自动化（harmonyos/）

基于 HDC + hmdriver2 的 HarmonyOS NEXT 手机自动化框架（开发中）。

## 环境要求

- Python 3.9+
- Hermes Agent（运行环境）
- GLM Coding Plan（智谱 AI API）
- Playwright（签到脚本）

## 使用方式

这些脚本设计为 Hermes Agent 的 cron job 运行：

```bash
# 手动测试日报
python3 monitoring/daily_token_report.py

# 运行测试
pytest tests/ -v
```

## 许可证

MIT
