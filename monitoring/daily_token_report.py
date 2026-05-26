#!/usr/bin/env python3
"""Daily token cost report with GLM-5.1 summarization.

Designed for Hermes cron (no_agent=True). Runs daily at 12:00.

Pipeline:
  1. Read hourly CSV → aggregate costs
  2. Read archived daily log → extract user messages
  3. Call GLM-5.1 API once → generate quality daily summary
  4. Save summary to daily CSV
  5. Print full report with summary (delivered to Telegram)

Cost: ~¥0.05/day for the one 5.1 API call.
"""

import csv
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

DATA_DIR = Path.home() / ".hermes" / "data"
HOURLY_CSV = DATA_DIR / "hourly_token_costs.csv"
DAILY_CSV = DATA_DIR / "daily_token_costs.csv"
LOGS_ARCHIVE_DIR = DATA_DIR / "logs"

# GLM API config (from Hermes auth.json)
AUTH_PATH = Path.home() / ".hermes" / "auth.json"
API_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
MODEL = "glm-5.1"

DAILY_HEADERS = [
    "date", "total_cost", "messages", "api_calls",
    "tokens_in", "tokens_out", "tokens_cached", "cache_rate",
    "hours_active", "peak_hour", "peak_hour_cost",
    "avg_per_msg", "summary",
]


def load_csv(path):
    if not path.exists():
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(path, rows, headers):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def get_api_key():
    """Read GLM API key from Hermes auth.json."""
    with open(AUTH_PATH) as f:
        auth = json.load(f)
    for provider, creds in auth.get("credential_pool", {}).items():
        for c in creds:
            token = c.get("access_token", "")
            if token:
                return token
    return None


def aggregate_day(hourly_rows, today_str):
    """Aggregate hourly rows into daily cost stats."""
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
    peak_row = max(day_rows, key=lambda r: float(r["cost"]))
    avg_per_msg = f"{total_cost / total_msgs:.2f}" if total_msgs else "0.00"

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
        "peak_hour": peak_row["hour"],
        "peak_hour_cost": f"{float(peak_row['cost']):.2f}",
        "avg_per_msg": avg_per_msg,
        "summary": "",  # filled by GLM
    }


def extract_user_messages(today_str):
    """Extract user messages from archived daily log."""
    archive_path = LOGS_ARCHIVE_DIR / f"{today_str}.log"
    if not archive_path.exists():
        return []

    import re
    turn_pat = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}.*conversation turn:.*msg=['\"](.+?)['\"]"
    )

    messages = []
    with open(archive_path) as f:
        for line in f:
            m = turn_pat.match(line)
            if m:
                msg = m.group(1)
                # Filter system messages
                if msg.startswith(("Review the conversation", "[System note:",
                                   "[IMPORTANT:", "[CONTEXT COMPACT")):
                    continue
                # Clean voice messages
                voice_m = re.match(r'\[The user sent a voice message.*?"(.+?)"', msg)
                if voice_m:
                    msg = f"🎤{voice_m.group(1)}"
                ts = line[11:19]  # HH:MM:SS
                messages.append(f"[{ts}] {msg[:80]}")

    return messages


def summarize_with_glm(today_str, user_messages, cost_data):
    """Call GLM-5.1 once to generate a quality daily summary."""
    api_key = get_api_key()
    if not api_key:
        return "（API key 未找到，跳过 AI 总结）"

    # Build prompt
    msg_list = "\n".join(user_messages[-30:])  # Last 30 messages
    cost_summary = (
        f"日期: {today_str}\n"
        f"总费用: ¥{cost_data['total_cost']}\n"
        f"消息数: {cost_data['messages']}\n"
        f"活跃时段: {cost_data['hours_active']}小时\n"
        f"峰值时段: {cost_data['peak_hour']}"
    )

    prompt = (
        f"以下是 {today_str} 的对话记录和费用统计。请用 2-3 句话总结今天主要做了什么，"
        f"重点标注重要成果和发现的问题。格式示例：\n"
        f"「今天主要完成了XXX，期间发现了YYY问题，已通过ZZZ解决。」\n\n"
        f"【费用统计】\n{cost_summary}\n\n"
        f"【对话记录】\n{msg_list}"
    )

    # Call API
    # Call API — GLM-5.1 is a reasoning model, need enough tokens for both
    # reasoning (~1000) + actual output (~200). Use max_tokens=1500.
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.3,
    }

    req = urllib.request.Request(
        f"{API_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"（AI 总结失败: {str(e)[:50]}）"


def format_report(today_str, day_summary, hourly_rows, recent_days):
    """Generate Telegram-friendly daily report."""
    total_cost = float(day_summary["total_cost"])
    total_msgs = int(day_summary["messages"])
    hours_active = int(day_summary["hours_active"])
    cache_rate = day_summary["cache_rate"]

    lines = []
    lines.append(f"📊 每日 Token 消耗报告 ({today_str})")
    lines.append("")

    # AI Summary (the highlight!)
    summary = day_summary.get("summary", "")
    if summary:
        lines.append(f"📝 AI 总结: {summary}")
        lines.append("")

    lines.append(f"💰 总费用: ¥{total_cost:.2f}")
    lines.append(f"💬 消息: {total_msgs} | 活跃: {hours_active}h")
    lines.append(f"💾 缓存率: {cache_rate}%")
    lines.append("")

    # Hourly breakdown
    lines.append("逐小时:")
    day_rows = sorted(
        [r for r in hourly_rows if r.get("date") == today_str],
        key=lambda r: r["hour"],
    )
    for r in day_rows:
        cost = float(r["cost"])
        msgs = int(r["messages"])
        models = r.get("models", "")
        bar = "█" * min(int(cost / 2), 20)
        lines.append(
            f"  {r['hour']} {msgs:>2}msg ¥{cost:>6.2f} {bar} {models}"
        )

    if total_msgs:
        lines.append("")
        lines.append(f"📈 ¥{total_cost/total_msgs:.2f}/msg | ¥{total_cost/max(hours_active,1):.2f}/hr")

    # Historical trend
    if recent_days:
        lines.append("")
        lines.append("📅 近期趋势:")
        for row in recent_days:
            cost = float(row["total_cost"])
            msgs = int(row["messages"])
            bar = "█" * min(int(cost / 5), 15)
            summary_hint = ""
            s = row.get("summary", "")
            if s and len(s) > 5:
                summary_hint = f" — {s[:30]}"
            lines.append(f"  {row['date']} ¥{cost:>7.2f} {msgs:>3}msg {bar}{summary_hint}")

    return "\n".join(lines)


def main():
    hourly_rows = load_csv(HOURLY_CSV)
    if not hourly_rows:
        sys.exit(0)

    today_str = datetime.now().strftime("%Y-%m-%d")

    # Aggregate costs from hourly CSV
    day_summary = aggregate_day(hourly_rows, today_str)
    if not day_summary:
        sys.exit(0)

    # Get user messages from archive
    user_messages = extract_user_messages(today_str)

    # Call GLM-5.1 for quality summary (~¥0.05)
    if user_messages:
        summary = summarize_with_glm(today_str, user_messages, day_summary)
        day_summary["summary"] = summary
    else:
        day_summary["summary"] = "（无对话记录）"

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

    # Generate report
    recent = daily_rows[-7:] if len(daily_rows) >= 2 else []
    report = format_report(today_str, day_summary, hourly_rows, recent)
    if report:
        print(report)


if __name__ == "__main__":
    main()
