#!/usr/bin/env python3
"""Hourly token report cron — only outputs when user was active in the past hour.

Also appends hourly stats to CSV for daily/monthly trend analysis.

Designed for Hermes cron with no_agent=True:
  - Has user activity → prints summary text (delivered to user) + writes CSV
  - No activity → silent exit (nothing sent, zero token cost)

Activity detection: scans agent.log for "conversation turn" lines in the past hour.
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
    "timestamp", "cost", "messages", "api_calls",
    "tokens_in", "tokens_out", "tokens_cached", "cache_rate",
    "models",
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


def append_hourly_csv(stats: dict):
    """Append an hourly data row to CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    if HOURLY_CSV.exists():
        with open(HOURLY_CSV, "r", newline="") as f:
            rows = list(csv.DictReader(f))

    # Check if this hour already logged — update in place
    ts_key = stats["timestamp"]
    updated = False
    for i, row in enumerate(rows):
        if row["timestamp"] == ts_key:
            rows[i] = stats
            updated = True
            break
    if not updated:
        rows.append(stats)

    with open(HOURLY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HOURLY_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def parse_hour_from_logs():
    """Parse current hour's stats from the log for CSV recording."""
    import subprocess

    # Run the report script to generate HTML + get summary
    result = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return None

    # Now parse the detailed data for CSV
    sys.path.insert(0, str(Path(__file__).parent))
    import token_cost_report as rpt

    turns = rpt.parse_logs()
    now = datetime.now()
    hour_ago = now - timedelta(hours=1)

    # Filter to past hour
    hour_turns = [t for t in turns if hour_ago <= t["time"] <= now and t["api_calls"]]

    if not hour_turns:
        return None

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

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:00"),
        "cost": f"{total_cost:.2f}",
        "messages": str(len(hour_turns)),
        "api_calls": str(total_calls),
        "tokens_in": str(total_in),
        "tokens_out": str(total_out),
        "tokens_cached": str(total_cached),
        "cache_rate": cache_rate,
        "models": models_str,
    }


def main():
    if not has_recent_activity(hours=1):
        # Silent exit — no output means no message sent to user
        sys.exit(0)

    # Activity found — run the full report (generates HTML + prints summary)
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0 and result.stdout.strip():
        # Record to CSV
        csv_stats = parse_hour_from_logs()
        if csv_stats:
            append_hourly_csv(csv_stats)

        # Print the report text (delivered to Telegram)
        print(result.stdout)
    else:
        if result.stderr:
            print(f"⚠️ 报表生成异常: {result.stderr[:200]}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
