#!/usr/bin/env python3
"""Hourly per-message token cost report — HTML version.

Generates a styled HTML file with per-message token breakdown table.
Used by Hermes cron job to deliver hourly reports.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

# --- Configuration ---
LOG_PATH = Path.home() / ".hermes" / "logs" / "agent.log"
OUTPUT_PATH = Path("/tmp/token_report.html")

INPUT_PRICE = 6.0
CACHE_PRICE = 1.5
OUTPUT_PRICE = 24.0
INPUT_PRICE_47 = 1.0
CACHE_PRICE_47 = 0.25
OUTPUT_PRICE_47 = 4.0

NOW = datetime.now()
TODAY_STR = NOW.strftime("%Y-%m-%d")
HOUR_AGO = NOW - timedelta(hours=1)

# Log rotation: Hermes rotates agent.log → agent.log.1 when file gets large.
# Must read both to get full day's data.
LOG_DIR = LOG_PATH.parent
ALL_LOG_PATHS = [LOG_PATH] + sorted(
    [p for p in LOG_DIR.glob("agent.log.[0-9]*")],
    key=lambda p: p.suffix,
)


def get_pricing(model):
    if "4.7" in model or "4.5" in model:
        return INPUT_PRICE_47, CACHE_PRICE_47, OUTPUT_PRICE_47
    return INPUT_PRICE, CACHE_PRICE, OUTPUT_PRICE


def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def truncate_msg(msg, max_len=35):
    msg = re.sub(r"^\[The user sent a voice message~.*?\"(.*?)\".*$", r"🎤\1", msg, flags=re.DOTALL)
    msg = msg.replace("\n", " ").strip()
    if len(msg) > max_len:
        return msg[:max_len - 1] + "…"
    return msg


def parse_logs():
    """Parse conversation turns from agent.log (and rotated agent.log.N).

    Reads ALL log files to handle log rotation. Deduplicates by timestamp
    since agent.log.1 tail and agent.log head may overlap.
    """
    all_lines = []
    for log_file in ALL_LOG_PATHS:
        if not log_file.exists():
            continue
        with open(log_file, "r") as f:
            all_lines.extend(f.readlines())

    today_lines = [l for l in all_lines if l.startswith(TODAY_STR)]
    # Note: do NOT sort today_lines. Lines within each log file are already
    # chronological, and we read rotated logs first (older) then current (newer).
    # Sorting would break stateful parsing (API call lines must follow their turn).

    turn_pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*conversation turn:.*msg=\'(.*?)\'\s*$')
    turn_pat2 = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*conversation turn:.*msg="(.*?)"\s*$')
    api_pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*API call #\d+: model=(\S+).*in=(\d+) out=(\d+).*cache=(\d+)/(\d+)')
    turn_end_pat = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Turn ended:.*response_len=(\d+)')

    turns = []
    current_turn = None

    for line in today_lines:
        m = turn_pat.match(line) or turn_pat2.match(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            msg = m.group(2)
            if msg.startswith("Review the conversation") or msg.startswith("[System note:"):
                continue
            current_turn = {"time": ts, "msg": msg, "api_calls": [], "response_len": 0}
            turns.append(current_turn)
            continue

        m = api_pat.match(line)
        if m and current_turn:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            current_turn["api_calls"].append({
                "time": ts, "model": m.group(2),
                "input": int(m.group(3)), "output": int(m.group(4)),
                "cached": int(m.group(5)),
            })
            continue

        m = turn_end_pat.match(line)
        if m and current_turn:
            current_turn["response_len"] = int(m.group(2))

    # Deduplicate: rotated log tail and current log head may overlap.
    # Use (timestamp, msg) as key to avoid collapsing distinct turns at the same second.
    seen = set()
    deduped = []
    for t in turns:
        key = (t["time"].strftime("%Y-%m-%d %H:%M:%S"), t["msg"][:50])
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    return deduped


def compute_turn(turn):
    model_stats = {}
    for c in turn["api_calls"]:
        m = c["model"]
        if m not in model_stats:
            model_stats[m] = {"input": 0, "output": 0, "cached": 0, "count": 0}
        model_stats[m]["input"] += c["input"]
        model_stats[m]["output"] += c["output"]
        model_stats[m]["cached"] += c["cached"]
        model_stats[m]["count"] += 1

    total_in = sum(s["input"] for s in model_stats.values())
    total_out = sum(s["output"] for s in model_stats.values())
    total_cached = sum(s["cached"] for s in model_stats.values())
    cache_rate = (total_cached / total_in * 100) if total_in > 0 else 0

    total_cost = 0
    for model, s in model_stats.items():
        ip, cp, op = get_pricing(model)
        eff = s["input"] - s["cached"]
        total_cost += (eff / 1e6 * ip) + (s["cached"] / 1e6 * cp) + (s["output"] / 1e6 * op)

    primary = max(model_stats.keys(), key=lambda m: model_stats[m]["input"]) if model_stats else "unknown"

    tags = []
    for m in sorted(model_stats.keys()):
        short = "GLM-5.1" if "5.1" in m else "GLM-4.7" if "4.7" in m else "GLM-5T" if "5-turbo" in m else m
        if len(model_stats) > 1:
            tags.append(f"{short}×{model_stats[m]['count']}")
        else:
            tags.append(short)

    is_cheap = "4.7" in primary or "4.5" in primary

    return {
        "calls": len(turn["api_calls"]),
        "input": total_in, "output": total_out,
        "cached": total_cached, "cache_rate": cache_rate,
        "cost": total_cost, "model": primary,
        "model_tag": "+".join(tags), "is_cheap": is_cheap,
    }


def build_table(turns, start_time=None, end_time=None, title=""):
    filtered = []
    for t in turns:
        if start_time and t["time"] < start_time:
            continue
        if end_time and t["time"] > end_time:
            continue
        if not t["api_calls"]:
            continue
        filtered.append(t)

    if not filtered:
        return "<p>无数据</p>", 0, 0, 0

    total_cost = 0
    total_calls = 0
    rows_html = ""

    for i, turn in enumerate(filtered, 1):
        s = compute_turn(turn)
        total_cost += s["cost"]
        total_calls += s["calls"]

        time_str = turn["time"].strftime("%H:%M")
        msg_preview = truncate_msg(turn["msg"], 50)
        # Escape HTML
        msg_preview = msg_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        model_color = "#4ECDC4" if s["is_cheap"] else "#FF6B6B"
        model_bg = "#1a3a38" if s["is_cheap"] else "#3a1a1a"
        cost_color = "#ff9966" if s["cost"] > 2.0 else "#ffffff"

        # Cost bar (relative to max ~7 yuan)
        bar_pct = min(s["cost"] / 7.0 * 100, 100)
        bar_color = "#FF6B6B" if s["cost"] > 3.0 else "#FFB347" if s["cost"] > 1.0 else "#4ECDC4"

        rows_html += f"""
        <tr>
            <td class="num">{i}</td>
            <td class="time">{time_str}</td>
            <td class="model" style="color:{model_color}; background:{model_bg}">{s['model_tag']}</td>
            <td class="msg">{msg_preview}</td>
            <td class="num">{s['calls']}</td>
            <td class="num">{fmt(s['input'])}</td>
            <td class="num">{s['cache_rate']:.0f}%</td>
            <td class="num">{fmt(s['output'])}</td>
            <td class="cost" style="color:{cost_color}">
                ¥{s['cost']:.2f}
                <div class="bar-bg"><div class="bar" style="width:{bar_pct}%; background:{bar_color}"></div></div>
            </td>
        </tr>"""

    return rows_html, len(filtered), total_calls, total_cost


def main():
    turns = parse_logs()

    hourly_rows, h_msgs, h_calls, h_cost = build_table(turns, start_time=HOUR_AGO, end_time=NOW, title="过去1小时")
    daily_rows, d_msgs, d_calls, d_cost = build_table(turns, start_time=None, end_time=NOW, title="今日")

    # Count 429s and compressions today (across all rotated logs)
    four29 = 0
    compressions = 0
    for log_file in ALL_LOG_PATHS:
        if not log_file.exists():
            continue
        with open(log_file) as f:
            for line in f:
                if not line.startswith(TODAY_STR):
                    continue
                if "1305" in line or "访问量过大" in line:
                    four29 += 1
                if "compression done" in line:
                    compressions += 1

    now_str = NOW.strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Token 用量播报 {now_str}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: #1a1a2e; color: #e0e0e0; font-family: -apple-system, "SF Pro", "Helvetica Neue", sans-serif; padding: 16px; font-size: 13px; }}
.header {{ text-align: center; margin-bottom: 16px; }}
.header h1 {{ color: #fff; font-size: 18px; margin-bottom: 4px; }}
.header .sub {{ color: #888; font-size: 12px; }}
.summary {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
.card {{ background: #16213e; border-radius: 10px; padding: 12px 16px; flex: 1; min-width: 120px; }}
.card .label {{ color: #888; font-size: 11px; margin-bottom: 4px; }}
.card .value {{ color: #fff; font-size: 18px; font-weight: 700; }}
.card .value.red {{ color: #FF6B6B; }}
.card .value.green {{ color: #4ECDC4; }}
.card .value.orange {{ color: #FFB347; }}
.section-title {{ color: #4ECDC4; font-size: 14px; font-weight: 700; margin: 16px 0 8px; padding-left: 8px; border-left: 3px solid #4ECDC4; }}
.section-sub {{ color: #888; font-size: 11px; margin-bottom: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
th {{ background: #16213e; color: #888; font-size: 11px; text-transform: uppercase; padding: 8px 6px; text-align: left; position: sticky; top: 0; }}
td {{ padding: 6px; border-bottom: 1px solid #1a2a4e; vertical-align: middle; }}
tr:hover {{ background: #1a2a4e; }}
.num {{ text-align: center; font-variant-numeric: tabular-nums; }}
.time {{ color: #aaa; text-align: center; white-space: nowrap; }}
.msg {{ color: #ddd; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.model {{ text-align: center; font-size: 11px; font-weight: 600; border-radius: 4px; padding: 2px 6px; white-space: nowrap; }}
.cost {{ text-align: right; font-weight: 600; white-space: nowrap; }}
.bar-bg {{ background: #2a2a4e; border-radius: 2px; height: 3px; margin-top: 2px; }}
.bar {{ height: 3px; border-radius: 2px; }}
.legend {{ display: flex; gap: 16px; margin-bottom: 12px; font-size: 11px; color: #888; }}
.legend span {{ display: flex; align-items: center; gap: 4px; }}
.dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
.dot-red {{ background: #FF6B6B; }}
.dot-green {{ background: #4ECDC4; }}
</style>
</head>
<body>

<div class="header">
    <h1>📊 Token 用量播报</h1>
    <div class="sub">{now_str}</div>
</div>

<div class="summary">
    <div class="card">
        <div class="label">今日费用</div>
        <div class="value red">¥{d_cost:.2f}</div>
    </div>
    <div class="card">
        <div class="label">今日调用</div>
        <div class="value">{d_calls}</div>
    </div>
    <div class="card">
        <div class="label">消息数</div>
        <div class="value">{d_msgs}</div>
    </div>
    <div class="card">
        <div class="label">429 限流</div>
        <div class="value orange">{four29}次</div>
    </div>
    <div class="card">
        <div class="label">压缩次数</div>
        <div class="value green">{compressions}</div>
    </div>
    <div class="card">
        <div class="label">过去1小时</div>
        <div class="value red">¥{h_cost:.2f}</div>
    </div>
</div>

<div class="legend">
    <span><span class="dot dot-red"></span> GLM-5.1（旗舰）</span>
    <span><span class="dot dot-green"></span> GLM-4.7（轻量）</span>
</div>

<div class="section-title">⏱ 过去1小时</div>
<div class="section-sub">{h_msgs} 条消息 · {h_calls} 次调用 · ¥{h_cost:.2f}</div>
<table>
    <tr><th>#</th><th>时间</th><th>模型</th><th>消息</th><th>调用</th><th>输入</th><th>缓存</th><th>输出</th><th>费用</th></tr>
    {hourly_rows}
</table>

<div class="section-title">📈 今日全部</div>
<div class="section-sub">{d_msgs} 条消息 · {d_calls} 次调用 · ¥{d_cost:.2f}</div>
<table>
    <tr><th>#</th><th>时间</th><th>模型</th><th>消息</th><th>调用</th><th>输入</th><th>缓存</th><th>输出</th><th>费用</th></tr>
    {daily_rows}
</table>

</body>
</html>"""

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)

    # Also print a short text summary for Telegram
    print(f"📊 Token 播报 ({NOW.strftime('%H:%M')})")
    print(f"今日 ¥{d_cost:.2f} | 过去1h ¥{h_cost:.2f} | 429: {four29}次")
    print(f"详细报表已生成 ↓")


if __name__ == "__main__":
    main()
