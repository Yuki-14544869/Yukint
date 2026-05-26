#!/usr/bin/env python3
"""Monthly token cost report — reads daily CSV for trend analysis.

Designed for Hermes cron (no_agent=True). Runs on the 1st of each month.

Reads from: daily_token_costs.csv
Generates: Monthly summary with trends, cost trajectory, and insights.
"""

import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "data"
DAILY_CSV = DATA_DIR / "daily_token_costs.csv"


def load_daily_rows():
    """Load all daily CSV rows."""
    if not DAILY_CSV.exists():
        return []
    with open(DAILY_CSV, "r", newline="") as f:
        return list(csv.DictReader(f))


def filter_month(rows, year, month):
    """Filter rows for a specific month."""
    prefix = f"{year}-{month:02d}"
    return [r for r in rows if r["date"].startswith(prefix)]


def format_monthly_report(year, month, month_rows, all_rows):
    """Generate the Telegram-friendly monthly report."""
    if not month_rows:
        return f"📊 {year}年{month}月 Token 消耗月报\n\n暂无数据"

    # Month totals
    total_cost = sum(float(r["total_cost"]) for r in month_rows)
    total_msgs = sum(int(r["messages"]) for r in month_rows)
    total_calls = sum(int(r["api_calls"]) for r in month_rows)
    total_429 = sum(int(r["rate_limit_429"]) for r in month_rows)
    total_compressions = sum(int(r["compressions"]) for r in month_rows)
    total_in = sum(int(r["tokens_in"]) for r in month_rows)
    total_out = sum(int(r["tokens_out"]) for r in month_rows)
    days_active = len(month_rows)
    avg_daily = total_cost / days_active if days_active else 0

    lines = []
    lines.append(f"📊 {year}年{month}月 Token 消耗月报")
    lines.append("")
    lines.append(f"💰 月度总费用: ¥{total_cost:.2f}")
    lines.append(f"📅 活跃天数: {days_active} 天")
    lines.append(f"💬 总消息: {total_msgs} | 调用: {total_calls}")
    lines.append(f"📥 {total_in/1e6:.1f}M in | 📤 {total_out/1e6:.1f}M out")
    lines.append(f"⚠️ 429: {total_429} 次 | 📦 压缩: {total_compressions} 次")
    lines.append(f"📈 日均: ¥{avg_daily:.2f} | ¥{total_cost/total_msgs:.2f}/msg" if total_msgs else "")

    # Daily breakdown with bar chart
    lines.append("")
    lines.append("每日消耗:")
    for r in month_rows:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        bar = "█" * min(int(cost / 3), 25)
        peak = r.get("peak_hour", "")
        lines.append(f"  {r['date']} ¥{cost:>7.2f} {msgs:>3}msg {bar} {peak}")

    # Weekly trend
    lines.append("")
    lines.append("周度趋势:")
    for i in range(0, len(month_rows), 7):
        week = month_rows[i:i+7]
        week_cost = sum(float(r["total_cost"]) for r in week)
        week_msgs = sum(int(r["messages"]) for r in week)
        start = week[0]["date"][-5:]
        end = week[-1]["date"][-5:]
        bar = "█" * min(int(week_cost / 10), 20)
        lines.append(f"  {start}~{end} ¥{week_cost:>7.2f} {week_msgs:>3}msg {bar}")

    # Comparison with previous month
    if len(all_rows) > days_active:
        # Find previous month rows
        prev_month_rows = [r for r in all_rows if r not in month_rows]
        # Just take the last N rows before this month
        idx = all_rows.index(month_rows[0])
        if idx > 0:
            # Simple: compare with whatever days we had before
            prev_count = min(idx, days_active)  # same number of days for fair comparison
            prev_rows = all_rows[idx - prev_count:idx]
            prev_cost = sum(float(r["total_cost"]) for r in prev_rows)
            prev_msgs = sum(int(r["messages"]) for r in prev_rows)

            lines.append("")
            lines.append("📊 对比上期:")
            cost_diff = ((total_cost / days_active) - (prev_cost / len(prev_rows))) / (prev_cost / len(prev_rows)) * 100 if prev_rows else 0
            emoji = "📈" if cost_diff > 0 else "📉"
            lines.append(f"  日均费用: ¥{avg_daily:.2f} vs ¥{prev_cost/len(prev_rows):.2f} {emoji}{abs(cost_diff):.0f}%")
            lines.append(f"  日均消息: {total_msgs/days_active:.0f} vs {prev_msgs/len(prev_rows):.0f}")

    # Top 3 most expensive days
    sorted_days = sorted(month_rows, key=lambda r: float(r["total_cost"]), reverse=True)
    lines.append("")
    lines.append("🔥 最贵3天:")
    for r in sorted_days[:3]:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        peak = r.get("peak_hour", "??:00")
        lines.append(f"  {r['date']} ¥{cost:.2f} ({msgs}msg, 峰值{peak})")

    # Insights
    lines.append("")
    lines.append("💡 洞察:")
    if total_cost > 3000:
        lines.append("  ⚠️ 月费用超过 ¥3000，建议关注高频使用时段")
    if total_429 > 100:
        lines.append(f"  ⚠️ 429 限流 {total_429} 次，高峰期可考虑切换到 4.7")
    cache_avg = sum(float(r.get("cache_rate", "0")) for r in month_rows) / days_active
    if cache_avg > 90:
        lines.append(f"  ✅ 缓存率 {cache_avg:.0f}% 非常高，上下文压缩配置效果好")
    if avg_daily < 50:
        lines.append(f"  ✅ 日均 ¥{avg_daily:.2f}，消费控制良好")

    return "\n".join(lines)


def main():
    all_rows = load_daily_rows()
    if not all_rows:
        sys.exit(0)

    # Report for previous month (since this runs on the 1st)
    now = datetime.now()
    prev = now - timedelta(days=1)  # Last day of previous month
    year = prev.year
    month = prev.month

    month_rows = filter_month(all_rows, year, month)
    report = format_monthly_report(year, month, month_rows, all_rows)

    print(report)


if __name__ == "__main__":
    main()
