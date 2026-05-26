#!/usr/bin/env python3
"""Monthly token cost report — reads daily CSV, trend analysis with topics.

Designed for Hermes cron (no_agent=True). Runs on the 1st of each month.

Data flow:
  daily CSV → aggregate → monthly summary with trends + topic analysis

NO raw log parsing — everything comes from CSV files.
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
    """Load CSV rows as list of dicts."""
    if not path.exists():
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def filter_month(rows, year, month):
    """Filter rows for a specific month."""
    prefix = f"{year}-{month:02d}"
    return [r for r in rows if r["date"].startswith(prefix)]


def classify_topics(month_rows):
    """Classify conversation topics into categories.

    Groups similar topics together for monthly overview.
    """
    categories = {
        "🔧 代码开发": ["代码", "脚本", "调试", "bug", "测试", "TDD", "commit", "pytest", "函数", "模块"],
        "🤖 AI/LLM": ["token", "模型", "GLM", "API", "429", "限流", "用量", "费用", "播报"],
        "📱 鸿蒙自动化": ["鸿蒙", "HDC", "签到", "手机", "华为", "hmdriver"],
        "🎮 游戏": ["崩3", "原神", "崩铁", "深渊", "乐土", "战场", "忘却", "虚构"],
        "⚙️ 系统配置": ["配置", "cron", "Hermes", "Telegram", "安装", "迁移"],
        "💬 日常对话": [],
    }

    all_topics = []
    for r in month_rows:
        topics = r.get("top_topics", "")
        if topics:
            all_topics.extend(topics.split(" | "))

    # Classify each topic
    cat_counts = Counter()
    unclassified = []

    for topic in all_topics:
        if len(topic) < 3:
            continue
        classified = False
        for cat, keywords in categories.items():
            if cat == "💬 日常对话":
                continue
            if any(kw in topic for kw in keywords):
                cat_counts[cat] += 1
                classified = True
                break
        if not classified:
            unclassified.append(topic)

    # Add unclassified as "日常对话"
    if unclassified:
        cat_counts["💬 日常对话"] = len(unclassified)

    return cat_counts


def format_monthly_report(year, month, month_rows, all_rows):
    """Generate the Telegram-friendly monthly report."""
    if not month_rows:
        return f"📊 {year}年{month}月 Token 消耗月报\n\n暂无数据"

    # Month totals
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
    lines.append(f"📈 日均: ¥{avg_daily:.2f} | ¥{total_cost/total_msgs:.2f}/msg" if total_msgs else "")

    # Topic analysis
    cat_counts = classify_topics(month_rows)
    if cat_counts:
        lines.append("")
        lines.append("📂 话题分布:")
        total_topics = sum(cat_counts.values())
        for cat, count in cat_counts.most_common():
            pct = count / total_topics * 100
            bar = "█" * min(int(pct / 5), 15)
            lines.append(f"  {cat} {count}次 {pct:.0f}% {bar}")

    # Daily breakdown
    lines.append("")
    lines.append("每日消耗:")
    for r in month_rows:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        peak = r.get("peak_hour", "")
        bar = "█" * min(int(cost / 3), 25)
        topic_hint = ""
        topics = r.get("top_topics", "")
        if topics:
            first = topics.split(" | ")[0][:20]
            topic_hint = f" ({first})"
        lines.append(f"  {r['date']} ¥{cost:>7.2f} {msgs:>3}msg {bar} {peak}{topic_hint}")

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

        # Collect week topics
        week_topics = []
        for r in week:
            t = r.get("top_topics", "")
            if t:
                week_topics.extend(t.split(" | ")[:2])
        topic_sample = week_topics[0][:20] if week_topics else ""

        lines.append(f"  {start}~{end} ¥{week_cost:>7.2f} {week_msgs:>3}msg {bar} {topic_sample}")

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

    # Top 3 most expensive days
    sorted_days = sorted(month_rows, key=lambda r: float(r["total_cost"]), reverse=True)
    lines.append("")
    lines.append("🔥 最贵3天:")
    for r in sorted_days[:3]:
        cost = float(r["total_cost"])
        msgs = int(r["messages"])
        peak = r.get("peak_hour", "??:00")
        topic = ""
        topics = r.get("top_topics", "")
        if topics:
            topic = f" — {topics.split(' | ')[0][:25]}"
        lines.append(f"  {r['date']} ¥{cost:.2f} ({msgs}msg, 峰值{peak}){topic}")

    # Insights
    lines.append("")
    lines.append("💡 洞察:")
    if avg_daily > 100:
        lines.append(f"  ⚠️ 日均 ¥{avg_daily:.0f} 偏高，建议关注高频使用时段")
    elif avg_daily < 50:
        lines.append(f"  ✅ 日均 ¥{avg_daily:.0f}，消费控制良好")

    top_cat = cat_counts.most_common(1)
    if top_cat:
        lines.append(f"  📌 主要精力花在: {top_cat[0][0]}（{top_cat[0][1]}次相关操作）")

    return "\n".join(lines)


def main():
    all_rows = load_csv(DAILY_CSV)
    if not all_rows:
        sys.exit(0)

    # Report for previous month (runs on the 1st)
    prev = datetime.now() - timedelta(days=1)
    year = prev.year
    month = prev.month

    month_rows = filter_month(all_rows, year, month)
    report = format_monthly_report(year, month, month_rows, all_rows)

    print(report)


if __name__ == "__main__":
    main()
