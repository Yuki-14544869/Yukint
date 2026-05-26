#!/usr/bin/env python3
"""Hourly token report cron — activity check + cost report + log archive.

Pipeline:
  1. Check recent activity
  2. If active: generate report + archive raw messages to daily log file
  3. Write stats to hourly CSV
  4. Print summary (delivered to Telegram by Hermes)

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
LOGS_ARCHIVE_DIR = DATA_DIR / "logs"

HOURLY_HEADERS = [
    "timestamp", "date", "hour", "cost", "messages", "api_calls",
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


def archive_daily_messages():
    """Archive today's user messages to a dated log file.

    Appends NEW messages only (tracks last archived timestamp).
    The archive preserves raw conversation turns for future review/summarization.
    """
    LOGS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    archive_path = LOGS_ARCHIVE_DIR / f"{today_str}.log"

    # Read what we've already archived
    last_archived_ts = ""
    if archive_path.exists():
        with open(archive_path, "r") as f:
            lines = f.readlines()
            if lines:
                # Last line's timestamp
                last_archived_ts = lines[-1][:19] if len(lines[-1]) > 19 else ""

    # Read current logs and find new messages
    sys.path.insert(0, str(Path(__file__).parent))
    import token_cost_report as rpt

    new_lines = []
    for log_file in rpt.ALL_LOG_PATHS:
        if not log_file.exists():
            continue
        with open(log_file, "r") as f:
            for line in f:
                if not line.startswith(today_str):
                    continue
                ts = line[:19]
                if ts > last_archived_ts:
                    # Only archive conversation turns and API calls
                    if ("conversation turn:" in line or
                        "API call #" in line or
                        "Turn ended:" in line):
                        new_lines.append(line)

    if new_lines:
        new_lines.sort()
        with open(archive_path, "a") as f:
            f.writelines(new_lines)

    return archive_path


def extract_hour_data():
    """Parse the past hour's stats from logs."""
    sys.path.insert(0, str(Path(__file__).parent))
    import token_cost_report as rpt

    turns = rpt.parse_logs()
    now = datetime.now()
    hour_ago = now - timedelta(hours=1)

    hour_turns = [
        t for t in turns
        if hour_ago <= t["time"] <= now and t["api_calls"]
    ]

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
    }


def append_hourly_csv(stats: dict):
    """Append or update an hourly data row in CSV."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    if HOURLY_CSV.exists():
        with open(HOURLY_CSV, "r", newline="") as f:
            rows = list(csv.DictReader(f))

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
        sys.exit(0)

    # Archive raw messages for daily summarization
    archive_daily_messages()

    # Extract hour stats
    stats = extract_hour_data()
    if not stats:
        sys.exit(0)

    # Write to CSV
    append_hourly_csv(stats)

    # Generate HTML report
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT)],
        capture_output=True, text=True, timeout=30,
    )

    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout)


if __name__ == "__main__":
    main()
