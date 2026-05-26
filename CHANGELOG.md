# Changelog

All notable changes to the Token Cost Monitoring System.

## [0.2.0] - 2026-05-27

### Added
- **逐小时活动总结**：每个有用户消息的小时都生成一句话活动概括（`extract_user_messages_by_hour()`）
- **爱莉希雅（🌸）评语**：以崩坏3rd角色口吻为全天和每个活跃小时写俏皮评语
- **`extract_user_messages_by_hour()`**：新函数，按 `HH:00` 分组提取用户消息，替代 v1 的平铺列表
- **`generate_all_summaries()`**：新函数，编排两次 GLM-5.1 调用（全天总结 + 逐小时总结/评语）

### Changed
- **迁移到 GLM-5.1**：日报从 1 次 GLM 调用升级为 2 次 GLM-5.1 调用
  - Call 1: 全天总结（`max_tokens=2000`，temperature=0.3）
  - Call 2: 逐小时总结 + 爱莉希雅评语（`max_tokens=4000`，temperature=0.5）
- **`format_report()`**：新增 `hourly_summaries`、`daily_comment`、`hourly_comments` 参数
  - 逐小时行后追加 `↳ 活动概括` 和 `🌸 评语`
  - 全天评语显示在 AI 总结下方
- **解析器兼容 5.1 markdown 输出**：去除 `**bold**` 标记、`-`/`•` 列表前缀
- **`max_tokens=4000`**：5.1 推理链需要 ~1000-2000 tokens，确保有足够空间输出
- **去除 cost 过滤**：展示所有有用户消息的小时（不再只展示有费用的小时）
- **超时从 60s → 120s**：5.1 推理 + 长 prompt 需要更多响应时间

### Fixed
- 系统消息过滤新增 `⏳ Still working` 过滤规则
- 消息截断长度从 80 → 100 字符

## [0.1.0] - 2026-05-26

### Added
- 每小时 token 费用监控（`token_report_cron.py`）
- 每日 token 日报 + GLM AI 总结（`daily_token_report.py` v1，1 次调用）
- 每月 token 月报 + 话题分类（`monthly_token_report.py`）
- 日志归档系统（`data/logs/YYYY-MM-DD.log`）
- HTML 报表生成（`token_cost_report.py`）
- 活性检测：无用户活动时静默退出
- Telegram 输出格式
