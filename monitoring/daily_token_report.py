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


def extract_user_messages_by_hour(today_str):
    """Extract user messages grouped by hour from archived daily log.

    Returns:
        dict: {\"HH:00\": [\"[HH:MM:SS] message\", ...], ...}
    """
    archive_path = LOGS_ARCHIVE_DIR / f"{today_str}.log"
    if not archive_path.exists():
        return {}

    import re
    turn_pat = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*conversation turn:.*msg=['\"](.+?)['\"]"
    )

    hourly = {}
    with open(archive_path) as f:
        for line in f:
            m = turn_pat.match(line)
            if not m:
                continue
            msg = m.group(2)
            # Filter system messages
            if msg.startswith(("Review the conversation", "[System note:",
                               "[IMPORTANT:", "[CONTEXT COMPACT",
                               "⏳ Still working")):
                continue
            # Clean voice messages
            voice_m = re.match(r'\[The user sent a voice message.*?"(.+?)"', msg)
            if voice_m:
                msg = f"🎤{voice_m.group(1)}"
            hour_key = line[11:13] + ":00"
            ts = line[11:19]  # HH:MM:SS
            hourly.setdefault(hour_key, []).append(f"[{ts}] {msg[:100]}")

    return hourly


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


def generate_all_summaries(today_str, hourly_msgs, hourly_rows, cost_data):
    """Generate daily summary (GLM-5.1) + hourly summaries + Elysia commentary (GLM-4.7).

    Uses 5.1 for the daily summary (better reasoning) and 4.7 for structured
    hourly output (follows format instructions more reliably).

    Returns:
        tuple: (daily_summary_str, {"HH:00": "summary", ...}, daily_comment, {"HH:00": "comment", ...})
    """
    api_key = get_api_key()
    if not api_key:
        return "（API key 未找到）", {}, "", {}

    # Build hour-by-hour context with user messages
    cost_map = {}
    for r in hourly_rows:
        if r.get("date") == today_str:
            cost_map[r["hour"]] = float(r.get("cost", 0))

    hour_blocks = []
    for hour_key in sorted(hourly_msgs.keys()):
        msgs = hourly_msgs[hour_key]
        if not msgs:
            continue
        msg_list = "\n".join(msgs[:4])  # Cap at 4 msgs per hour to avoid timeout
        hour_blocks.append(f"[{hour_key}] cost={cost_map.get(hour_key, 0):.2f}yuan\n{msg_list}")

    if not hour_blocks:
        return "（无活动记录）", {}, "", {}

    all_hours_text = "\n\n".join(hour_blocks)

    # --- Call 1: GLM-5.1 for daily summary (better reasoning quality) ---
    daily_summary = ""
    summary_prompt = (
        f"以下是 {today_str} 用户与AI助手对话的逐小时记录。"
        f"总费用: ¥{cost_data['total_cost']} | 消息: {cost_data['messages']}条"
        f" | 活跃: {cost_data['hours_active']}h\n\n"
        f"用2-3句精炼的话概括今天干了什么。直接说重点："
        f"完成了什么、发现了什么问题、做了什么决策。不要客套废话。\n\n"
        f"{all_hours_text}"
    )
    summary_payload = {
        "model": MODEL,  # glm-5.1
        "messages": [{"role": "user", "content": summary_prompt}],
        "max_tokens": 2000,
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            f"{API_BASE_URL}/chat/completions",
            data=json.dumps(summary_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            daily_summary = result["choices"][0]["message"].get("content", "").strip()
            if not daily_summary:
                daily_summary = "（5.1 总结为空）"
    except Exception as e:
        daily_summary = f"（5.1 总结失败: {str(e)[:50]}）"

    # --- Call 2: GLM-5.1 for hourly summaries + Elysia commentary ---
    prompt = (
        f"以下是 {today_str} 用户与AI助手对话的逐小时记录和费用。\n\n"
        f"请完成两个任务：\n\n"
        f"【任务1】为下面列出的每个小时都用一句话概括在干嘛。"
        f"不要遗漏任何小时。"
        f"要求生动具体，比如'深度调查429 fallback bug'比'调试代码'好。"
        f"格式严格为：HH:MM 概括内容（每行一个，不要用markdown）\n\n"
        f"【任务2】以爱莉希雅（崩坏3rd粉色妖精小姐·人之律者）的口吻，"
        f"为全天和每个有活动的小时各写一句评语。"
        f"要求：俏皮、温暖、偶尔吐槽但带着鼓励和宠溺。"
        f"可以调侃用户熬夜、心疼花钱、夸奖解决问题、吐槽bug。"
        f"评语要生动有画面感，比如调侃具体场景、引用对话细节、"
        f"用小剧场方式吐槽（'凌晨三点还在跟API限额吵架'比'辛苦了'好一万倍）。\n"
        f"格式为：\n"
        f"DAY: 全天评语\n"
        f"HH:MM* 逐小时评语\n\n"
        f"---\n"
        f"总费用: ¥{cost_data['total_cost']} | 消息: {cost_data['messages']}条"
        f" | 活跃: {cost_data['hours_active']}h\n\n"
        f"{all_hours_text}"
    )

    payload = {
        "model": MODEL,  # glm-5.1
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,  # 5.1 reasoning uses ~1000-2000, need room for output
        "temperature": 0.5,  # Higher for more personality
    }

    req = urllib.request.Request(
        f"{API_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"].get("content", "").strip()
            if not content:
                return daily_summary, {}, "", {}

            import re
            # Normalize 5.1's markdown quirks: strip **bold**, -, •, etc.
            content_clean = re.sub(r'\*\*', '', content)  # Remove **bold**
            content_clean = re.sub(r'^[•\-]\s*', '', content_clean, flags=re.MULTILINE)

            hourly_summaries = {}
            hourly_comments = {}
            daily_comment = ""
            for line in content_clean.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # DAY: comment
                day_match = re.match(r'^DAY:\s*(.+)', line, re.IGNORECASE)
                if day_match:
                    daily_comment = day_match.group(1).strip()
                    continue
                # HH:MM* comment (Elysia commentary)
                comment_match = re.match(r'^(\d{2}:\d{2})\*?\s*(.+)', line)
                if comment_match and '*' in line:
                    hourly_comments[comment_match.group(1)] = comment_match.group(2).strip()[:100]
                    continue
                # HH:MM summary (factual) — skip lines with * or markdown headers
                if len(line) >= 5 and line[2] == ":" and "*" not in line and not line.startswith("#"):
                    rest = line[5:].strip()
                    if rest and rest[0] == ":":
                        rest = rest[1:].strip()
                    elif rest and rest[0] == " ":
                        rest = rest.strip()
                    # Skip if it looks like a section header (任务1, 概括, etc.)
                    if rest and not re.match(r'^(概括|评语|任务|Task)', rest):
                        hour_key = line[:5]
                        hourly_summaries[hour_key] = rest[:80]

            return daily_summary, hourly_summaries, daily_comment, hourly_comments
    except Exception as e:
        return daily_summary, {}, "", {}


def format_report(today_str, day_summary, hourly_rows, recent_days,
                  hourly_summaries=None, daily_comment="",
                  hourly_comments=None):
    """Generate Telegram-friendly daily report."""
    total_cost = float(day_summary["total_cost"])
    total_msgs = int(day_summary["messages"])
    hours_active = int(day_summary["hours_active"])
    cache_rate = day_summary["cache_rate"]
    hourly_summaries = hourly_summaries or {}
    hourly_comments = hourly_comments or {}

    lines = []
    lines.append(f"📊 每日 Token 消耗报告 ({today_str})")
    lines.append("")

    # AI Summary (the highlight!)
    summary = day_summary.get("summary", "")
    if summary:
        lines.append(f"📝 {summary}")
    if daily_comment:
        lines.append(f"🌸 {daily_comment}")
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
        hour_line = (
            f"  {r['hour']} {msgs:>2}msg ¥{cost:>6.2f} {bar} {models}"
        )
        # Append activity summary if available
        activity = hourly_summaries.get(r["hour"], "")
        comment = hourly_comments.get(r["hour"], "")
        if activity:
            hour_line += f"\n         ↳ {activity}"
        if comment:
            hour_line += f"\n         🌸 {comment}"
        lines.append(hour_line)

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
    hourly_msgs = extract_user_messages_by_hour(today_str)

    # Single GLM-4.7 call for daily summary + hourly summaries + Elysia commentary
    daily_summary, hourly_summaries, daily_comment, hourly_comments = (
        generate_all_summaries(today_str, hourly_msgs, hourly_rows, day_summary)
    )
    day_summary["summary"] = daily_summary

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
    report = format_report(
        today_str, day_summary, hourly_rows, recent,
        hourly_summaries=hourly_summaries,
        daily_comment=daily_comment,
        hourly_comments=hourly_comments,
    )
    if report:
        print(report)


if __name__ == "__main__":
    main()
