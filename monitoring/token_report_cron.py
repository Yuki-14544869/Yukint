#!/usr/bin/env python3
"""Hourly token report cron — only outputs when user was active in the past hour.

Pipeline:
  1. Check recent activity (scan log for user turns)
  2. If active: run token_cost_report.py (generates HTML report)
  3. Parse hour's stats + extract conversation topics
  4. Write to hourly CSV (data for daily/monthly reports)
  5. Print summary (delivered to Telegram by Hermes)

All no_agent=True — zero LLM token cost.
"""

import csv
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG_PATH = Path.home() / ".hermes" / "logs" / "agent.log"
REPORT_SCRIPT = Path.home() / ".hermes" / "scripts" / "token_cost_report.py"
DATA_DIR = Path.home() / ".hermes" / "data"
HOURLY_CSV = DATA_DIR / "hourly_token_costs.csv"

HOURLY_HEADERS = [
    "timestamp", "date", "hour", "cost", "messages", "api_calls",
    "tokens_in", "tokens_out", "tokens_cached", "cache_rate",
    "models", "topics",
]


def has_recent_activity(hours: int = 1) -> bool:
    """Check if there were user conversation turns in the past N hours."""
    if not LOG_PATH.exists():
        return False

    cutoff = datetime.now() - timedelta(hours=hours)
    today_str = datetime.now().strftime("%Y-%m-%d")
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    turn_pat = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*conversation turn:.*'
    )

    with open(LOG_PATH, "r") as f:
        for line in f:
            if not line.startswith(today_str):
                continue
            m = turn_pat.match(line)
            if m and m.group(1) >= cutoff_str:
                msg_part = line.lower()
                if "review the conversation" in msg_part or "[system note:" in msg_part:
                    continue
                return True

    return False


def extract_hour_data():
    """Parse the past hour's stats and topics from logs.

    Returns dict with stats + extracted conversation topics, or None if no data.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import token_cost_report as rpt

    turns = rpt.parse_logs()
    now = datetime.now()
    hour_ago = now - timedelta(hours=1)

    # Filter to past hour, exclude system turns
    hour_turns = [
        t for t in turns
        if hour_ago <= t["time"] <= now and t["api_calls"]
    ]

    if not hour_turns:
        return None

    # Compute stats
    total_cost = 0
    total_calls = 0
    total_in = 0
    total_out = 0
    total_cached = 0
    models = {}

    for t in hour_turns:
        s = rpt.compute_turn(t)
        total_cost += s["cost"]
        total_calls += s["calls"]
        total_in += s["input"]
        total_out += s["output"]
        total_cached += s["cached"]
        for c in t["api_calls"]:
            short = ("5.1" if "5.1" in c["model"]
                     else "4.7" if "4.7" in c["model"]
                     else c["model"])
            models[short] = models.get(short, 0) + 1

    cache_rate = f"{total_cached / total_in * 100:.0f}" if total_in else "0"
    models_str = "+".join(f"{m}×{c}" for m, c in sorted(models.items()))

    # Extract topics — grab unique user messages, deduplicated
    topics = []
    seen_topics = set()
    for t in hour_turns:
        msg = t["msg"]
        # Clean up voice messages
        voice_m = re.match(r'\[The user sent a voice message.*?"(.+?)"', msg)
        if voice_m:
            msg = f"🎤{voice_m.group(1)}"
        # Truncate
        msg = msg[:30].replace("\n", " ").strip()
        # Deduplicate similar messages
        if msg not in seen_topics:
            seen_topics.add(msg)
            topics.append(msg)

    # Take first 5 unique topics, join with separator
    topics_str = " | ".join(topics[:5])

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:00"),
        "date": now.strftime("%Y-%m-%d"),
        "hour": now.strftime("%H:00"),
        "cost": f"{total_cost:.2f}",
        "messages": str(len(hour_turns)),
        "api_calls": str(total_calls),
        "tokens_in": str(total_in),
        "tokens_out": str(total_out),
        "tokens_cached": str(total_cached),
        "cache_rate": cache_rate,
        "models": models_str,
        "topics": topics_str,
    }


def append_hourly_csv(stats: dict):
    """Append or update an hourly data row in CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    if HOURLY_CSV.exists():
        with open(HOURLY_CSV, "r", newline="") as f:
            rows = list(csv.DictReader(f))

    # Update in place if same timestamp exists
    ts_key = stats["timestamp"]
    updated = False
    for i, row in enumerate(rows):
        if row.get("timestamp") == ts_key:
            rows[i] = stats
            updated = True
            break
    if not updated:
        rows.append(stats)

    with open(HOURLY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HOURLY_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not has_recent_activity(hours=1):
        # Silent exit — no activity, no message, zero cost
        sys.exit(0)

    # Extract data + topics from logs
    stats = extract_hour_data()
    if not stats:
        sys.exit(0)

    # Write to CSV for daily/monthly reports
    append_hourly_csv(stats)

    # Run the HTML report generator (for visual report)
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )

    # Print summary text (delivered to Telegram)
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout)
    else:
        # Fallback: print our own summary
        print(f"📊 Token 播报 ({stats['hour']})")
        print(f"¥{stats['cost']} | {stats['messages']}msg | {stats['api_calls']}api | {stats['models']}")
        if stats["topics"]:
            print(f"📝 {stats['topics']}")


if __name__ == "__main__":
    main()
