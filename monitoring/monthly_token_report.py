#!/usr/bin/env python3
"""Monthly token cost report — reads daily CSV with AI summaries.

Designed for Hermes cron (no_agent=True). Runs on the 1st of each month.

Pipeline:
  daily CSV (with summaries) → aggregate → monthly report with trends

NO API calls, NO log parsing — pure CSV aggregation.
"""

import csv
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "data"
DAILY_CSV = DATA_DIR / "daily_token_costs.csv"


def load_csv(path):
    if not path.exists():
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def filter_month(rows, year, month):
    prefix = f"{year}-{month:02d}"
    return [r for r in rows if r["date"].startswith(prefix)]


def classify_from_summaries(month_rows):
    """Classify topics from AI-generated daily summaries."""
    categories = {
        "🔧 代码开发": ["代码", "脚本", "调试", "bug", "测试", "TDD", "commit", "pytest", "函数", "修复", "重构"],
        "🤖 AI/LLM": ["token", "模型", "GLM", "API", "429", "限流", "用量", "费用", "播报", "缓存"],
        "📱 鸿蒙自动化": ["鸿蒙", "HDC", "签到", "手机", "华为", "hmdriver", "HarmonyOS"],
        "🎮 游戏": ["崩3", "原神", "崩铁", "深渊", "乐土", "战场", "忘却", "虚构"],
        "⚙️ 系统配置": ["配置", "cron", "Hermes", "Telegram", "安装", "迁移", "GitHub", "插件"],
        "💬 日常对话": [],
    }

    cat_counts = Counter()
    for r in month_rows:
        summary = r.get("summary", "")
        if not summary or summary.startswith("（"):
            cat_counts["💬 日常对话"] += 1
            continue
        classified = False
        for cat, keywords in categories.items():
            if cat == "💬 日常对话":
                continue
            if any(kw in summary for kw in keywords):
                cat_counts[cat] += 1
                classified = True
                break
        if not classified:
            cat_counts["💬 日常对话"] += 1

    return cat_counts


def format_monthly_report(year, month, month_rows, all_rows):
    if not month_rows:
        return f"📊 {year}年{month}月 Token 消耗月报\n\n暂无数据"

    total_cost = sum(float(r["total_cost"]) for r in month_rows)
    total_msgs = sum(int(r["messages"]) for r in month_rows)
    total_calls = sum(int(r["api_calls"]) for r in month_rows)
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
    lines.append(f"📈 日均: ¥{avg_daily:.2f}" + (f" | ¥{total_cost/total_msgs:.2f}/msg" if total_msgs else ""))

    # Topic classification from summaries
    cat_counts = classify_from_summaries(month_rows)
    if cat_counts:
        lines.append("")
        lines.append("📂 话题分布:")
        total_topics = sum(cat_counts.values())
        for cat, count in cat_counts.most_common():
            pct = count / total_topics * 100
            bar = "█" * min(int(pct / 5), 15)
            lines.append(f"  {cat} {count}天 {pct:.0f}% {bar}")

    # Daily breakdown WITH summaries
    lines.append("")
    lines.append("每日明细:")
    for r in month_rows:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        peak = r.get("peak_hour", "")
        bar = "█" * min(int(cost / 3), 25)
        summary = r.get("summary", "")
        summary_hint = f" — {summary[:40]}" if summary and not summary.startswith("（") else ""
        lines.append(f"  {r['date']} ¥{cost:>7.2f} {msgs:>3}msg {bar}{summary_hint}")

    # Weekly trend
    lines.append("")
    lines.append("周度趋势:")
    for i in range(0, len(month_rows), 7):
        week = month_rows[i:i + 7]
        week_cost = sum(float(r["total_cost"]) for r in week)
        week_msgs = sum(int(r["messages"]) for r in week)
        start = week[0]["date"][-5:]
        end = week[-1]["date"][-5:]
        bar = "█" * min(int(week_cost / 10), 20)

        # Pick best summary from the week
        week_summaries = [r.get("summary", "") for r in week
                         if r.get("summary", "") and not r["summary"].startswith("（")]
        best = week_summaries[0][:30] if week_summaries else ""

        lines.append(f"  {start}~{end} ¥{week_cost:>7.2f} {week_msgs:>3}msg {bar} {best}")

    # Comparison with previous month
    if len(all_rows) > days_active:
        idx = all_rows.index(month_rows[0])
        if idx > 0:
            prev_count = min(idx, days_active)
            prev_rows = all_rows[idx - prev_count:idx]
            prev_cost = sum(float(r["total_cost"]) for r in prev_rows)
            prev_msgs = sum(int(r["messages"]) for r in prev_rows)

            lines.append("")
            lines.append("📊 对比上期:")
            prev_avg = prev_cost / len(prev_rows) if prev_rows else 0
            cost_diff = ((avg_daily - prev_avg) / prev_avg * 100) if prev_avg else 0
            emoji = "📈" if cost_diff > 0 else "📉"
            lines.append(f"  日均费用: ¥{avg_daily:.2f} vs ¥{prev_avg:.2f} {emoji}{abs(cost_diff):.0f}%")
            lines.append(f"  日均消息: {total_msgs/days_active:.0f} vs {prev_msgs/len(prev_rows):.0f}")

    # Top 3 most expensive days with summaries
    sorted_days = sorted(month_rows, key=lambda r: float(r["total_cost"]), reverse=True)
    lines.append("")
    lines.append("🔥 最贵3天:")
    for r in sorted_days[:3]:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        peak = r.get("peak_hour", "??:00")
        summary = r.get("summary", "")
        s = f" — {summary[:50]}" if summary and not summary.startswith("（") else ""
        lines.append(f"  {r['date']} ¥{cost:.2f} ({msgs}msg, 峰值{peak}){s}")

    # Insights
    lines.append("")
    lines.append("💡 洞察:")
    if avg_daily > 100:
        lines.append(f"  ⚠️ 日均 ¥{avg_daily:.0f} 偏高，建议关注高频使用时段")
    elif avg_daily < 50:
        lines.append(f"  ✅ 日均 ¥{avg_daily:.0f}，消费控制良好")

    top_cat = cat_counts.most_common(1)
    if top_cat:
        lines.append(f"  📌 主要精力: {top_cat[0][0]}（{top_cat[0][1]}天）")

    return "\n".join(lines)


def main():
    all_rows = load_csv(DAILY_CSV)
    if not all_rows:
        sys.exit(0)

    prev = datetime.now() - timedelta(days=1)
    year = prev.year
    month = prev.month

    month_rows = filter_month(all_rows, year, month)
    report = format_monthly_report(year, month, month_rows, all_rows)
    print(report)


if __name__ == "__main__":
    main()
