#!/usr/bin/env python3
"""Token report cron wrapper — only outputs when user was active in the past hour.

Designed for Hermes cron with no_agent=True:
  - Has user activity → prints summary text (delivered to user)
  - No activity → silent exit (nothing sent, zero token cost)

Activity detection: scans agent.log for "conversation turn" lines in the past hour.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOG_PATH = Path.home() / ".hermes" / "logs" / "agent.log"
REPORT_SCRIPT = Path.home() / ".hermes" / "scripts" / "token_cost_report.py"


def has_recent_activity(hours: int = 1) -> bool:
    """Check if there were user conversation turns in the past N hours."""
    if not LOG_PATH.exists():
        return False

    cutoff = datetime.now() - timedelta(hours=hours)
    # Today's date prefix for quick filtering
    today_str = datetime.now().strftime("%Y-%m-%d")
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    turn_pat = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*conversation turn:.*'
    )

    with open(LOG_PATH, "r") as f:
        for line in f:
            # Quick filter: only today's lines
            if not line.startswith(today_str):
                continue
            m = turn_pat.match(line)
            if m and m.group(1) >= cutoff_str:
                # Skip system/internal turns
                msg_part = line.lower()
                if "review the conversation" in msg_part or "[system note:" in msg_part:
                    continue
                return True

    return False


def main():
    if not has_recent_activity(hours=1):
        # Silent exit — no output means no message sent to user
        sys.exit(0)

    # Activity found — run the full report
    import subprocess
    result = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout)
    else:
        # Report script failed — silent exit to avoid noise
        if result.stderr:
            print(f"⚠️ 报表生成异常: {result.stderr[:200]}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
