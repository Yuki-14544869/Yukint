#!/usr/bin/env python3
"""Daily token cost report — reads hourly CSV, zero log parsing.

Designed for Hermes cron (no_agent=True). Runs daily at 12:00.

Data flow:
  hourly CSV → aggregate → daily summary text → daily CSV row

NO raw log parsing — everything comes from the hourly CSV
that token_report_cron.py already wrote.
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "data"
HOURLY_CSV = DATA_DIR / "hourly_token_costs.csv"
DAILY_CSV = DATA_DIR / "daily_token_costs.csv"

DAILY_HEADERS = [
    "date", "total_cost", "messages", "api_calls",
    "tokens_in", "tokens_out", "tokens_cached", "cache_rate",
    "hours_active", "peak_hour", "peak_hour_cost",
    "avg_per_msg", "top_topics",
]


def load_csv(path):
    """Load CSV rows as list of dicts."""
    if not path.exists():
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path, rows, headers):
    """Save rows to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def aggregate_day(hourly_rows, today_str):
    """Aggregate hourly rows into a daily summary."""
    day_rows = [r for r in hourly_rows if r.get("date") == today_str]

    if not day_rows:
        return None

    total_cost = sum(float(r["cost"]) for r in day_rows)
    total_msgs = sum(int(r["messages"]) for r in day_rows)
    total_calls = sum(int(r["api_calls"]) for r in day_rows)
    total_in = sum(int(r["tokens_in"]) for r in day_rows)
    total_out = sum(int(r["tokens_out"]) for r in day_rows)
    total_cached = sum(int(r["tokens_cached"]) for r in day_rows)
    hours_active = len(day_rows)

    cache_rate = f"{total_cached / total_in * 100:.0f}" if total_in else "0"

    # Find peak hour
    peak_row = max(day_rows, key=lambda r: float(r["cost"]))
    peak_hour = peak_row["hour"]
    peak_cost = float(peak_row["cost"])

    avg_per_msg = f"{total_cost / total_msgs:.2f}" if total_msgs else "0.00"

    # Collect all topics
    all_topics = []
    for r in day_rows:
        topics = r.get("topics", "")
        if topics:
            all_topics.extend(topics.split(" | "))

    # Deduplicate and take top 8
    seen = set()
    unique_topics = []
    for t in all_topics:
        if t not in seen and len(t) > 2:
            seen.add(t)
            unique_topics.append(t)

    top_topics = " | ".join(unique_topics[:8])

    return {
        "date": today_str,
        "total_cost": f"{total_cost:.2f}",
        "messages": str(total_msgs),
        "api_calls": str(total_calls),
        "tokens_in": str(total_in),
        "tokens_out": str(total_out),
        "tokens_cached": str(total_cached),
        "cache_rate": cache_rate,
        "hours_active": str(hours_active),
        "peak_hour": peak_hour,
        "peak_hour_cost": f"{peak_cost:.2f}",
        "avg_per_msg": avg_per_msg,
        "top_topics": top_topics,
    }


def format_report(today_str, day_summary, hourly_rows, recent_days):
    """Generate Telegram-friendly daily report."""
    if not day_summary:
        return None

    total_cost = float(day_summary["total_cost"])
    total_msgs = int(day_summary["messages"])
    total_calls = int(day_summary["api_calls"])
    total_in = int(day_summary["tokens_in"])
    total_out = int(day_summary["tokens_out"])
    hours_active = int(day_summary["hours_active"])
    cache_rate = day_summary["cache_rate"]

    lines = []
    lines.append(f"📊 每日 Token 消耗报告 ({today_str})")
    lines.append("")
    lines.append(f"💰 总费用: ¥{total_cost:.2f}")
    lines.append(f"💬 消息: {total_msgs} | 调用: {total_calls} | 活跃: {hours_active}h")
    lines.append(f"📥 {total_in/1e6:.1f}M in | 📤 {total_out/1e3:.0f}K out | 💾 {cache_rate}% cached")
    lines.append("")

    # Hourly breakdown with topics
    lines.append("逐小时:")
    day_rows = sorted(
        [r for r in hourly_rows if r.get("date") == today_str],
        key=lambda r: r["hour"],
    )
    for r in day_rows:
        cost = float(r["cost"])
        msgs = int(r["messages"])
        hour = r["hour"]
        models = r.get("models", "")
        topics = r.get("topics", "")

        bar = "█" * min(int(cost / 2), 20)
        topic_preview = ""
        if topics:
            # Show first topic only in the line
            first_topic = topics.split(" | ")[0][:25]
            topic_preview = f" 📝{first_topic}"

        lines.append(
            f"  {hour} {msgs:>2}msg ¥{cost:>6.2f} {bar} {models}{topic_preview}"
        )

    if total_msgs:
        lines.append("")
        lines.append(f"📈 ¥{total_cost/total_msgs:.2f}/msg | ¥{total_cost/max(hours_active,1):.2f}/hr")

    # Topics summary
    all_topics = day_summary.get("top_topics", "")
    if all_topics:
        lines.append("")
        lines.append("📝 今日话题:")
        for t in all_topics.split(" | ")[:8]:
            lines.append(f"  · {t}")

    # Historical trend
    if recent_days:
        lines.append("")
        lines.append("📅 近期趋势:")
        for row in recent_days:
            cost = float(row["total_cost"])
            msgs = int(row["messages"])
            bar = "█" * min(int(cost / 5), 15)
            date = row["date"]
            topics_preview = row.get("top_topics", "")
            topic_hint = ""
            if topics_preview:
                first = topics_preview.split(" | ")[0][:15]
                topic_hint = f" ({first})"
            lines.append(f"  {date} ¥{cost:>7.2f} {msgs:>3}msg {bar}{topic_hint}")

    return "\n".join(lines)


def main():
    hourly_rows = load_csv(HOURLY_CSV)
    if not hourly_rows:
        sys.exit(0)

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Aggregate from hourly CSV
    day_summary = aggregate_day(hourly_rows, today_str)
    if not day_summary:
        sys.exit(0)

    # Save to daily CSV (upsert)
    daily_rows = load_csv(DAILY_CSV)
    updated = False
    for i, row in enumerate(daily_rows):
        if row["date"] == today_str:
            daily_rows[i] = day_summary
            updated = True
            break
    if not updated:
        daily_rows.append(day_summary)
    save_csv(DAILY_CSV, daily_rows, DAILY_HEADERS)

    # Generate report text
    recent = daily_rows[-7:] if len(daily_rows) >= 2 else []
    report = format_report(today_str, day_summary, hourly_rows, recent)

    if report:
        print(report)


if __name__ == "__main__":
    main()
