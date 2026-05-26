#!/usr/bin/env python3
"""Telegram bot daily check-in via Playwright headless Chromium.

Automates Telegram Web K to send check-in/upgrade commands to VPN bots.
Session is persisted so login is only needed once.

Usage:
    # First time: login (shows browser for QR scan)
    python3 tg_checkin_playwright.py --login

    # Daily check-in (headless, no window)
    python3 tg_checkin_playwright.py

    # Debug mode (visible browser, slow motion)
    python3 tg_checkin_playwright.py --debug
"""

import argparse
import csv
import datetime
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DIR = HERMES_HOME / "playwright_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_CSV = Path(__file__).resolve().parent / "checkin_log.csv"

TG_WEB_URL = "https://web.telegram.org/k/"

# Telegram Web K CSS selectors (verified 2025-05)
SEL = {
    "search_input": ".input-search-input",
    "chat_item": ".chatlist-chat",
    "chat_title": ".peer-title",
    "clear_search": ".input-search-clear",
    "msg_input": ".input-message-input",
    "send_btn": ".btn-send",
    "message": ".message",
    "bubble": ".bubble",
}

# Bot interaction flow:
#   IKUUU → "iKuuuu VPN" 群（在签到文件夹里）发 /checkin
#   GFC   → "Getfree.Cloud | 在?签个到" 群（搜索打开）发 /checkin
#   GFC   → 同群发 /upgrade（升级白给大师，消耗6白给币获得3天+1G流量）
#
# 注意：GFC 的 /checkin 仅支持在群组中发送，私聊 bot（🌸白給宗師）不行。
# 群组全名是 "Getfree.Cloud | 在?签个到 | 公益机场 | 永久免费 |"，
# 搜索时用 "Getfree.Cloud | 在?" 即可匹配。

# Timeouts (seconds)
NAV_TIMEOUT = 30
SETTLE_DELAY = 5
SEARCH_DELAY = 2
REPLY_DELAY = 8  # Group chat bots can be slow; 5s was too short for GFC.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """Timestamped stdout log."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def append_csv(record: dict) -> None:
    """Append a result row to the check-in CSV log.

    Fields: timestamp, bot, cmd, status, reply, coins, gfc_level, gfc_traffic,
            gfc_expire. The gfc_* fields are only populated for GFC_info rows.
    """
    exists = LOG_CSV.exists()
    fields = [
        "timestamp", "bot", "cmd", "status", "reply", "coins",
        "gfc_level", "gfc_traffic", "gfc_expire",
    ]
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(record)


def extract_coins(text: str) -> str:
    """Extract coin/balance/travel info from bot reply."""
    flat = text.replace("\n", " ")
    for pat in [
        r"白给币[：:]\s*\d+",
        r"余额[：:]\s*[\d.]+",
        r"流量[：:]\s*[\d.]+\s*[GMT]B",
    ]:
        m = re.search(pat, flat)
        if m:
            return m.group(0)
    return ""


# Keywords in bot replies that confirm a successful check-in.
# "已相见" = already checked in (GFC group bot's unique phrasing).
_CHECKIN_SUCCESS_KEYWORDS = ("签到成功", "签过到", "已经签过", "已相见")


def classify_checkin_reply(reply: str) -> str:
    """Classify a bot's /checkin reply as 'OK' or 'FAIL'.

    Returns 'OK' only if the reply contains a confirmation keyword.
    Any other response (including empty) is treated as FAIL.
    This ensures we never silently assume success without verification.

    Args:
        reply: The bot's reply text after sending /checkin.

    Returns:
        'OK' if check-in confirmed, 'FAIL' otherwise.
    """
    for kw in _CHECKIN_SUCCESS_KEYWORDS:
        if kw in reply:
            return "OK"
    return "FAIL"


def parse_gfc_info(text: str) -> dict:
    """Parse GFC /info reply into a structured dict.

    Expected format:
        ■    Info  -  简要信息    ■
        用户账号：y***
        用户等级：白给大师
        白给币：1871
        可用流量：11.55 GB | 73.24%
        等级到期：2026-05-28 19:34:43

    Returns:
        dict with keys: level, coins, traffic, traffic_pct, expire_time (datetime or None)
    """
    info = {
        "level": "",
        "coins": 0,
        "traffic": "",
        "traffic_pct": "",
        "expire_time": None,
    }
    flat = text.replace("\n", " ")

    m = re.search(r"用户等级[：:]\s*(.+?)(?=\s+白给币|$)", flat)
    if m:
        info["level"] = m.group(1).strip()

    m = re.search(r"白给币[：:]\s*(\d+)", flat)
    if m:
        info["coins"] = int(m.group(1))

    m = re.search(r"可用流量[：:]\s*([\d.]+\s*[GMT]B)\s*\|\s*([\d.]+%)", flat)
    if m:
        info["traffic"] = m.group(1).strip()
        info["traffic_pct"] = m.group(2).strip()

    m = re.search(r"等级到期[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", flat)
    if m:
        try:
            info["expire_time"] = datetime.datetime.strptime(
                m.group(1), "%Y-%m-%d %H:%M:%S"
            )
        except ValueError as e:
            log(f"⚠️ Failed to parse expiry date '{m.group(1)}': {e}")

    return info


# Keywords that confirm a successful /upgrade (spends 6 coins for 3 days).
# The GFC bot uses varied phrasing: "等级提升为【白给大师】", "消耗6白给币",
# "持续 3 天", "解锁了" etc. We match specific combo-phrases to avoid
# false positives from /info replies that happen to mention "白给大师".
_UPGRADE_SUCCESS_KEYWORDS = (
    "升级成功", "续期成功", "已为您续期", "续费成功", "成功续期",
    "等级提升为", "持续 3 天",
)


def classify_upgrade_reply(reply: str) -> str:
    """Classify a bot's /upgrade reply as 'OK' or 'FAIL'.

    Upgrade costs 6 白给币 — we MUST verify the bot confirmed success,
    never assume it worked just because the command was sent.

    Args:
        reply: The bot's reply text after sending /upgrade.

    Returns:
        'OK' if upgrade confirmed, 'FAIL' otherwise.
    """
    for kw in _UPGRADE_SUCCESS_KEYWORDS:
        if kw in reply:
            return "OK"
    return "FAIL"


# How many hours before upgrade expiry to renew. Upgrade costs 6 coins for
# 3 days of 白给大师 status (+1 GB traffic). We don't want to waste coins
# by upgrading too early, but also don't want a gap in status.
UPGRADE_HOURS_BEFORE_EXPIRY = 24


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def make_context(playwright, headless: bool = True):
    """Create a persistent Chromium context (reuses login session).

    Uses --headless=new (Chrome's "new headless" mode) because Telegram Web K
    does not load group chat content in the old headless mode. The new headless
    mode runs a fuller browser pipeline, making SPAs like Telegram Web work.
    """
    args = ["--headless=new"] if headless else []
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(STATE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 800},
        args=args,
    )


def wait_for_telegram(page, timeout: int = NAV_TIMEOUT) -> bool:
    """Wait until Telegram Web K sidebar is loaded."""
    log("Waiting for Telegram to load...")
    try:
        page.wait_for_selector(SEL["search_input"], timeout=timeout * 1000)
        log("Telegram loaded ✅")
        return True
    except PwTimeout:
        log("Telegram did not load in time.")
        return False


def switch_folder(page, folder_name: str) -> bool:
    """Switch to a specific folder tab in the sidebar.

    Telegram Web K organizes chats into folders (All Chats, 签到, 机场们, etc.)
    shown as tabs at the bottom of the sidebar. This function clicks the
    matching tab to display that folder's chats.

    Args:
        page: Playwright page object.
        folder_name: Substring to match in the folder tab text (e.g. '签到').

    Returns:
        True if the folder tab was found and clicked, False otherwise.
    """
    tabs = page.query_selector_all(".menu-horizontal-div-item")
    for tab in tabs:
        try:
            text = tab.inner_text().strip()
            if folder_name in text:
                tab.click()
                log(f"Switched to folder: {text}")
                time.sleep(2)
                return True
        except Exception as e:
            log(f"⚠️ Error reading folder tab: {e}")
            continue
    log(f"Folder '{folder_name}' not found")
    return False


def open_chat_by_folder(page, title: str, folder: str = "签到") -> bool:
    """Open a chat by switching to its folder and clicking it.

    This is more reliable than searching, because searching for a chat
    name like 'iKuuuu VPN' may match a bot private chat instead of the
    group, causing navigation to the wrong conversation.

    Args:
        page: Playwright page object.
        title: Exact title text of the chat to open.
        folder: Folder name to switch to first (e.g. '签到').

    Returns:
        True if the chat was found and opened, False otherwise.
    """
    if not switch_folder(page, folder):
        return False

    chats = page.query_selector_all(SEL["chat_item"])
    for chat in chats:
        title_el = chat.query_selector(SEL["chat_title"])
        if title_el and title in title_el.inner_text().strip():
            chat.click()
            log(f"Opened chat: {title_el.inner_text().strip()[:40]}")
            time.sleep(2)
            # Verify the chat actually opened (message input must exist).
            if not page.query_selector(SEL["msg_input"]):
                log(f"Chat '{title}' clicked but no input field — not really open")
                return False
            return True

    log(f"Chat '{title}' not found in folder '{folder}'")
    return False


def open_chat_by_search(page, search_text: str, title_kw: str) -> bool:
    """Open a chat by typing in the search bar and clicking the result.

    Use this for chats that are NOT in any folder (e.g. Getfree.Cloud group
    which only appears in search results, not in folder tabs).

    Args:
        page: Playwright page object.
        search_text: Text to type in the search bar.
        title_kw: Substring that must appear in the chat title.

    Returns:
        True if the chat was found and opened, False otherwise.
    """
    log(f"Searching for chat: {search_text}")

    # Use the sidebar search input directly (always visible on the page).
    # Telegram Web K uses input.input-search-input for the sidebar search,
    # NOT a popup search box. Meta+F does not reliably open it.
    inp = page.query_selector("input.input-search-input")
    if not inp:
        # Fallback: try the selector from SEL dict
        inp = page.query_selector(SEL["search_input"])
    if not inp:
        log("Search input not found")
        return False
    inp.click()
    inp.fill("")
    inp.fill(search_text)
    time.sleep(SEARCH_DELAY + 1)  # Extra wait for results to load

    chats = page.query_selector_all(SEL["chat_item"])
    for chat in chats:
        try:
            title_el = chat.query_selector(SEL["chat_title"])
            if title_el and title_kw.lower() in title_el.inner_text().lower():
                # MUST use Playwright native click with force=True.
                # JS el.click() does NOT trigger Telegram Web K's SPA routing,
                # so the chat would never actually open.
                chat.click(force=True, timeout=5000)
                log(f"Opened chat via search: {title_el.inner_text().strip()[:40]}")
                time.sleep(3)
                # Verify the chat actually opened (input field exists)
                if not page.query_selector(SEL["msg_input"]):
                    log("Chat did not open after click (no input field)")
                    return False
                # IMPORTANT: Do NOT press Escape to clear search — in Telegram
                # Web K, Escape closes the entire chat view, destroying all
                # loaded messages. Clear the search input via the × button or
                # by navigating back to the chat list instead.
                clear_btn = page.query_selector(SEL["clear_search"])
                if clear_btn:
                    try:
                        clear_btn.click()
                    except Exception:
                        pass  # Non-critical; search will auto-clear on nav.
                time.sleep(1)
                return True
        except Exception as e:
            log(f"⚠️ Error clicking search result: {e}")
            continue

    log(f"Chat matching '{title_kw}' not found in search results")
    clear_btn = page.query_selector(SEL["clear_search"])
    if clear_btn:
        try:
            clear_btn.click()
        except Exception:
            pass
    return False


def send_and_read(page, cmd: str) -> tuple:
    """Send a command and read the bot's reply.

    Strategy: snapshot the last 3 bubble texts before sending, then wait for
    NEW bubbles to appear (i.e. text not in the snapshot). This avoids picking
    up unrelated group messages that were already there.

    Returns:
        (sent: bool, reply_text: str)
        sent=False means the command could not be sent (input not found).
        reply_text may be empty if no bot reply was detected.
    """
    log(f"Sending: {cmd}")

    # Snapshot existing bubble texts (last 10) so we can detect truly new ones
    old_texts = set()
    for b in page.query_selector_all(SEL["bubble"])[-10:]:
        try:
            t = b.inner_text().strip()
            if t:
                old_texts.add(t)
        except Exception:
            pass  # Snapshot failure is non-critical; skip silently.
    old_count = len(page.query_selector_all(SEL["bubble"]))

    # Find the first visible .input-message-input (contenteditable div).
    # Telegram Web K has two: real + fake overlay; we want the first one.
    inp = page.query_selector(SEL["msg_input"])
    if not inp:
        log("Message input not found ❌")
        return False, ""

    # Force-focus via JS (more reliable than .click() in headless mode)
    page.evaluate("document.querySelector('.input-message-input').focus()")
    time.sleep(0.3)

    # Use execCommand to insert text — this fires the correct input events
    # that Telegram Web K's React handlers recognize. keyboard.type() alone
    # does not reliably trigger the internal state update in headless mode.
    escaped_cmd = cmd.replace("\\", "\\\\").replace("'", "\\'")
    page.evaluate(
        f"document.execCommand('selectAll', false, null);"
        f"document.execCommand('delete', false, null);"
        f"document.execCommand('insertText', false, '{escaped_cmd}')"
    )
    time.sleep(0.5)
    page.keyboard.press("Enter")
    time.sleep(1)

    log("Command sent, waiting for reply...")

    # Wait for new message(s) to appear (bubble count must increase)
    for _ in range(REPLY_DELAY):
        time.sleep(1)
        new_count = len(page.query_selector_all(SEL["bubble"]))
        if new_count > old_count:
            break

    # Extra settle time for bot reply to render
    time.sleep(2)

    # Collect new bubbles (texts not in old snapshot)
    bubbles = page.query_selector_all(SEL["bubble"])
    new_replies = []
    for b in bubbles[-8:]:
        try:
            t = b.inner_text().strip()
            if t and t not in old_texts:
                new_replies.append(t)
        except Exception as e:
            log(f"⚠️ Error reading bubble: {e}")
            continue

    # Pick the longest new reply — bot responses tend to be longer than the
    # echo of our own "/checkin" message.
    reply = ""
    if new_replies:
        reply = max(new_replies, key=len)
    else:
        # Fallback: grab the last non-empty bubble. This is UNRELIABLE — it
        # could be a group member's message, an ad, or our own command echo.
        # Log a warning so the caller knows the reply is low-confidence.
        # Search last 10 bubbles (not just 5) — group chats have more noise.
        for b in reversed(bubbles[-10:]):
            try:
                t = b.inner_text().strip()
                if t:
                    reply = t
                    log(f"⚠️ Fallback bubble (unreliable): {t[:60]}")
                    break
            except Exception as e:
                log(f"⚠️ Error reading fallback bubble: {e}")
                continue

    log(f"Reply: {reply[:120]}{'...' if len(reply) > 120 else ''}")
    return True, reply


def run_checkin(headless: bool = True) -> int:
    """Main check-in routine. Returns 0=ok, 1=partial fail, 2=no login.

    Strategy:
      1. Switch to 签到 folder → open iKuuuu VPN → send /checkin (read reply)
      2. Switch to 签到 folder → open 🌸白給宗師 (GFC bot DM) → send /info,
         parse status, decide if /upgrade is needed
      3. Search → open Getfree.Cloud 签到群 → send /checkin (fire-and-forget)
    """
    results = []

    with sync_playwright() as p:
        ctx = make_context(p, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.goto(TG_WEB_URL, wait_until="domcontentloaded",
                      timeout=NAV_TIMEOUT * 1000)
            time.sleep(SETTLE_DELAY)

            if not wait_for_telegram(page):
                return 2

            # ============================================================
            # Step 1: IKUUU check-in (签到 folder)
            # ============================================================
            log("--- IKUUU: /checkin ---")
            ok = open_chat_by_folder(page, "iKuuuu VPN", "签到")
            if not ok:
                results.append({"name": "IKUUU", "cmd": "/checkin",
                                "status": "FAIL",
                                "reply": "Chat not found", "coins": ""})
            else:
                sent, reply = send_and_read(page, "/checkin")
                if not sent:
                    results.append({"name": "IKUUU", "cmd": "/checkin",
                                    "status": "FAIL", "reply": "Send failed",
                                    "coins": ""})
                else:
                    status = classify_checkin_reply(reply)
                    coins = extract_coins(reply) if status == "OK" else ""
                    results.append({"name": "IKUUU", "cmd": "/checkin",
                                    "status": status,
                                    "reply": reply[:200] or "No bot reply",
                                    "coins": coins})
                time.sleep(2)

            # ============================================================
            # Step 2: GFC bot DM — /info + optional /upgrade (签到 folder)
            # ============================================================
            log("--- GFC: /info (bot DM) ---")
            ok = open_chat_by_folder(page, "白給宗師", "签到")
            if not ok:
                results.append({"name": "GFC_info", "cmd": "/info",
                                "status": "FAIL",
                                "reply": "Bot DM not found in folder",
                                "coins": ""})
            else:
                sent, reply = send_and_read(page, "/info")
                if sent and reply:
                    gfc = parse_gfc_info(reply)
                    log(f"GFC status: level={gfc['level']}, "
                        f"coins={gfc['coins']}, "
                        f"traffic={gfc['traffic']}, "
                        f"expire={gfc['expire_time']}")
                    results.append({
                        "name": "GFC_info", "cmd": "/info",
                        "status": "OK", "reply": reply[:200],
                        "coins": str(gfc["coins"]),
                        "gfc_parsed": gfc})

                    # Decide if upgrade is needed.
                    if gfc["expire_time"]:
                        now = datetime.datetime.now()
                        time_left = gfc["expire_time"] - now
                        hours_left = time_left.total_seconds() / 3600
                        log(f"Upgrade expires in {hours_left:.1f} hours "
                            f"(threshold: {UPGRADE_HOURS_BEFORE_EXPIRY}h)")

                        if hours_left <= UPGRADE_HOURS_BEFORE_EXPIRY:
                            log("⏰ Expiring soon! Sending /upgrade...")
                            sent, reply = send_and_read(page, "/upgrade")
                            if not sent:
                                status = "FAIL"
                            else:
                                status = classify_upgrade_reply(reply)
                            results.append({
                                "name": "GFC_upgrade", "cmd": "/upgrade",
                                "status": status,
                                "reply": reply[:200] or "No reply",
                                "coins": (extract_coins(reply)
                                          if status == "OK" else ""),
                            })
                        else:
                            log(f"✅ Status valid for {hours_left:.1f}h, "
                                f"skip upgrade")
                            results.append({
                                "name": "GFC_upgrade", "cmd": "/upgrade",
                                "status": "SKIP",
                                "reply": (f"expires in {hours_left:.1f}h, "
                                          f"no need yet"),
                                "coins": "",
                            })
                    else:
                        log("No expiry parsed, sending /upgrade as fallback")
                        sent, reply = send_and_read(page, "/upgrade")
                        if not sent:
                            status = "FAIL"
                        else:
                            status = classify_upgrade_reply(reply)
                        results.append({
                            "name": "GFC_upgrade", "cmd": "/upgrade",
                            "status": status,
                            "reply": reply[:200] or "No reply",
                            "coins": (extract_coins(reply)
                                      if status == "OK" else ""),
                        })
                else:
                    results.append({"name": "GFC_info", "cmd": "/info",
                                    "status": "FAIL",
                                    "reply": "No reply from bot",
                                    "coins": ""})

            # ============================================================
            # Step 3: GFC group check-in (search, send, verify response)
            # ============================================================
            log("--- GFC: /checkin (group) ---")
            ok = open_chat_by_search(page, "Getfree.Cloud | 在?", "签个到")
            if not ok:
                results.append({"name": "GFC_checkin", "cmd": "/checkin",
                                "status": "FAIL",
                                "reply": "Chat not found or failed to open",
                                "coins": ""})
            else:
                sent, reply = send_and_read(page, "/checkin")
                if not sent:
                    results.append({"name": "GFC_checkin", "cmd": "/checkin",
                                    "status": "FAIL",
                                    "reply": "Failed to send", "coins": ""})
                else:
                    status = classify_checkin_reply(reply)
                    coins = extract_coins(reply) if status == "OK" else ""
                    results.append({"name": "GFC_checkin", "cmd": "/checkin",
                                    "status": status,
                                    "reply": reply[:200] or "No bot reply",
                                    "coins": coins})

        finally:
            ctx.close()

    # Log to CSV
    now = datetime.datetime.now().isoformat()
    fails = 0
    for r in results:
        if r["status"] == "FAIL":
            fails += 1

        row = {
            "timestamp": now, "bot": r["name"], "cmd": r["cmd"],
            "status": r["status"], "reply": r["reply"][:100],
            "coins": r.get("coins", ""),
        }

        # For GFC_info rows, attach detailed parsed fields for trend tracking.
        if r["name"] == "GFC_info" and r.get("gfc_parsed"):
            g = r["gfc_parsed"]
            row["gfc_level"] = g.get("level", "")
            row["gfc_traffic"] = g.get("traffic", "")
            row["gfc_expire"] = (
                g["expire_time"].isoformat() if g.get("expire_time") else ""
            )

        append_csv(row)

    # Summary
    log("=" * 40)
    log("CHECK-IN RESULTS")
    for r in results:
        if r["status"] == "SKIP":
            icon = "⏭️"
        elif r["status"] == "OK":
            icon = "✅"
        else:
            icon = "❌"
        extra = f" → {r['coins']}" if r.get("coins") else ""
        log(f"  {icon} {r['name']}: {r['status']}{extra}")
    log("=" * 40)

    return 0 if fails == 0 else 1


def do_login() -> int:
    """Interactive login: open browser for QR scan, wait until logged in.

    Returns 0 on success, 1 on timeout. Callers should sys.exit() on non-zero.
    """
    with sync_playwright() as p:
        ctx = make_context(p, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.goto(TG_WEB_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)

            print()
            print("=" * 50)
            print("  📱 请用手机 Telegram 扫码登录")
            print("  Settings → Devices → Link Desktop Device")
            print("=" * 50)
            print()

            for i in range(100):  # 5 min max
                time.sleep(3)
                search = page.query_selector(SEL["search_input"])
                if search:
                    print("✅ 登录成功！Session 已保存。")
                    time.sleep(2)
                    return 0
                if i % 5 == 4:
                    print(f"  等待扫码... ({(i+1)*3}s)")

            print("⏰ 超时。请重新运行 --login。")
            return 1
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Telegram bot check-in (Playwright)")
    ap.add_argument("--login", action="store_true", help="Interactive QR login")
    ap.add_argument("--debug", action="store_true", help="Visible browser, for debugging")
    ap.add_argument("--headless", action="store_true", help="No browser window (default when not --login/--debug)")
    args = ap.parse_args()

    if args.login:
        sys.exit(do_login())
    elif args.debug:
        sys.exit(run_checkin(headless=False))
    else:
        sys.exit(run_checkin(headless=True))
