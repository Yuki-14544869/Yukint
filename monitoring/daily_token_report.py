#!/usr/bin/env python3
"""Daily token cost report — generates text summary and appends to local CSV.

Designed for Hermes cron (no_agent=True). Runs once daily at 12:00.

- Reads agent.log + agent.log.N (handles log rotation)
- Outputs today's hourly breakdown text (delivered to user via Telegram)
- Appends a summary row to ~/.hermes/data/daily_token_costs.csv
- If the day's row already exists, updates it in-place

CSV columns:
    date, total_cost, messages, api_calls, tokens_in, tokens_out,
    tokens_cached, cache_rate, rate_limit_429, compressions,
    avg_per_msg, peak_hour, peak_hour_cost
"""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import token_cost_report as rpt

DATA_DIR = Path.home() / ".hermes" / "data"
CSV_PATH = DATA_DIR / "daily_token_costs.csv"

CSV_HEADERS = [
    "date", "total_cost", "messages", "api_calls",
    "tokens_in", "tokens_out", "tokens_cached", "cache_rate",
    "rate_limit_429", "compressions",
    "avg_per_msg", "peak_hour", "peak_hour_cost",
]


def compute_hourly(turns):
    """Group turns by hour, compute per-hour stats."""
    hours = {}
    for t in turns:
        h_key = t["time"].strftime("%H:00")
        if h_key not in hours:
            hours[h_key] = {
                "msgs": 0, "calls": 0, "cost": 0.0,
                "ti": 0, "to": 0, "tc": 0, "models": {},
            }
        hours[h_key]["msgs"] += 1
        for c in t["api_calls"]:
            hours[h_key]["calls"] += 1
            hours[h_key]["cost"] += rpt.compute_turn(t)["cost"] / max(len(t["api_calls"]), 1) * len([c])
            # Actually, let's just compute from the raw data directly
            pass

    # Recompute properly using compute_turn on full turns
    hours = {}
    for t in turns:
        h_key = t["time"].strftime("%H:00")
        if h_key not in hours:
            hours[h_key] = {
                "msgs": 0, "calls": 0, "cost": 0.0,
                "ti": 0, "to": 0, "tc": 0, "models": {},
            }
        hours[h_key]["msgs"] += 1
        stats = rpt.compute_turn(t)
        hours[h_key]["calls"] += stats["calls"]
        hours[h_key]["cost"] += stats["cost"]
        hours[h_key]["ti"] += stats["input"]
        hours[h_key]["to"] += stats["output"]
        hours[h_key]["tc"] += stats["cached"]
        # Count models
        for c in t["api_calls"]:
            short = ("5.1" if "5.1" in c["model"]
                     else "4.7" if "4.7" in c["model"]
                     else c["model"])
            hours[h_key]["models"][short] = (
                hours[h_key]["models"].get(short, 0) + 1
            )

    return hours


def count_events():
    """Count 429s and compressions across all rotated logs."""
    four29 = 0
    compressions = 0
    for log_file in rpt.ALL_LOG_PATHS:
        if not log_file.exists():
            continue
        with open(log_file) as f:
            for line in f:
                if not line.startswith(rpt.TODAY_STR):
                    continue
                if "1305" in line or "访问量过大" in line:
                    four29 += 1
                if "compression done" in line:
                    compressions += 1
    return four29, compressions


def load_csv_rows():
    """Load existing CSV rows as list of dicts."""
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, "r", newline="") as f:
        return list(csv.DictReader(f))


def save_csv_rows(rows):
    """Save rows back to CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def upsert_daily_row(today_str, totals, four29, compressions, hours):
    """Insert or update today's row in CSV."""
    rows = load_csv_rows()

    total_cost = totals["cost"]
    total_msgs = totals["msgs"]
    total_calls = totals["calls"]
    total_in = totals["ti"]
    total_out = totals["to"]
    total_cache = totals["tc"]
    cache_rate = f"{total_cache / total_in * 100:.0f}" if total_in else "0"

    # Find peak hour
    peak_hour = ""
    peak_cost = 0.0
    for h, d in hours.items():
        if d["cost"] > peak_cost:
            peak_cost = d["cost"]
            peak_hour = h

    avg_per_msg = f"{total_cost / total_msgs:.2f}" if total_msgs else "0.00"

    new_row = {
        "date": today_str,
        "total_cost": f"{total_cost:.2f}",
        "messages": str(total_msgs),
        "api_calls": str(total_calls),
        "tokens_in": str(total_in),
        "tokens_out": str(total_out),
        "tokens_cached": str(total_cache),
        "cache_rate": cache_rate,
        "rate_limit_429": str(four29),
        "compressions": str(compressions),
        "avg_per_msg": avg_per_msg,
        "peak_hour": peak_hour,
        "peak_hour_cost": f"{peak_cost:.2f}",
    }

    # Update existing or append
    updated = False
    for i, row in enumerate(rows):
        if row["date"] == today_str:
            rows[i] = new_row
            updated = True
            break
    if not updated:
        rows.append(new_row)

    save_csv_rows(rows)


def format_text_report(today_str, hours, totals, four29, compressions):
    """Generate the Telegram-friendly text report."""
    total_cost = totals["cost"]
    total_msgs = totals["msgs"]
    total_calls = totals["calls"]
    total_in = totals["ti"]
    total_out = totals["to"]
    total_cache = totals["tc"]
    cache_rate = total_cache / total_in * 100 if total_in else 0

    lines = []
    lines.append(f"📊 每日 Token 消耗报告 ({today_str})")
    lines.append("")
    lines.append(f"💰 总费用: ¥{total_cost:.2f}")
    lines.append(f"💬 消息: {total_msgs} | 调用: {total_calls} | 429: {four29}")
    lines.append(f"📥 {total_in/1e6:.1f}M in | 📤 {total_out/1e3:.0f}K out | 💾 {cache_rate:.0f}% cached")
    lines.append("")
    lines.append("逐小时:")

    for h in sorted(hours.keys()):
        d = hours[h]
        cr = d["tc"] / d["ti"] * 100 if d["ti"] else 0
        ms = "+".join(f"{m}×{c}" for m, c in sorted(d["models"].items()))
        bar = "█" * min(int(d["cost"] / 2), 20)
        lines.append(
            f"  {h} {d['msgs']:>2}msg {d['calls']:>3}api "
            f"¥{d['cost']:>6.2f} {bar} {ms}"
        )

    if total_msgs:
        lines.append("")
        lines.append(f"📈 ¥{total_cost/total_msgs:.2f}/msg | ¥{total_cost/max(len(hours),1):.2f}/hr")

    # Historical comparison if CSV has data
    rows = load_csv_rows()
    if len(rows) >= 2:
        lines.append("")
        lines.append("📅 近期趋势:")
        for row in rows[-7:]:
            cost = float(row["total_cost"])
            msgs = int(row["messages"])
            bar = "█" * min(int(cost / 5), 15)
            lines.append(f"  {row['date']} ¥{cost:>7.2f} {msgs:>3}msg {bar}")

    return "\n".join(lines)


def main():
    turns = rpt.parse_logs()

    if not turns:
        # No activity today — silent exit (no message sent)
        sys.exit(0)

    hours = compute_hourly(turns)
    four29, compressions = count_events()

    # Compute totals from hourly data
    totals = {
        "msgs": sum(h["msgs"] for h in hours.values()),
        "calls": sum(h["calls"] for h in hours.values()),
        "cost": sum(h["cost"] for h in hours.values()),
        "ti": sum(h["ti"] for h in hours.values()),
        "to": sum(h["to"] for h in hours.values()),
        "tc": sum(h["tc"] for h in hours.values()),
    }

    today_str = rpt.TODAY_STR

    # Save to CSV
    upsert_daily_row(today_str, totals, four29, compressions, hours)

    # Print text report (delivered to Telegram by cron)
    print(format_text_report(today_str, hours, totals, four29, compressions))


if __name__ == "__main__":
    main()
