"""Unit tests for pure functions in tg_checkin_playwright.

Pure functions don't need a browser — they take strings and return parsed
results. We test every branch: normal, edge, and error cases.

Run:  pytest tests/test_unit.py -v
"""

import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import from the script under test.
# The script lives one directory up from tests/.
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from tg_checkin_playwright import extract_coins, parse_gfc_info, classify_checkin_reply, classify_upgrade_reply


# ===========================================================================
# extract_coins — TC IDs: EC-001 ~ EC-008
# ===========================================================================

class TestExtractCoins:
    """Test extract_coins(text) which extracts coin/balance/traffic from
    bot replies using regex patterns."""

    # --- EC-001: Normal 白给币 format (Chinese colon) ---
    def test_normal_coins_chinese_colon(self):
        assert extract_coins("白给币：1871") == "白给币：1871"

    # --- EC-002: Normal 白给币 format (ASCII colon) ---
    def test_normal_coins_ascii_colon(self):
        assert extract_coins("白给币:500") == "白给币:500"

    # --- EC-003: Multi-line text containing 白给币 ---
    def test_multiline_with_coins(self):
        text = "签到成功\n白给币：200\n流量+1G"
        assert extract_coins(text) == "白给币：200"

    # --- EC-004: IKUUU balance format ---
    def test_ikuuu_balance(self):
        assert extract_coins("余额：123.45") == "余额：123.45"

    # --- EC-005: IKUUU traffic format (GB) ---
    def test_ikuuu_traffic_gb(self):
        assert extract_coins("流量：10.5 GB") == "流量：10.5 GB"

    # --- EC-006: IKUUU traffic format (TB) ---
    def test_ikuuu_traffic_tb(self):
        assert extract_coins("流量：1.2 TB") == "流量：1.2 TB"

    # --- EC-007: No match — irrelevant text ---
    def test_no_match(self):
        assert extract_coins("今天天气不错") == ""

    # --- EC-008: Empty string ---
    def test_empty_string(self):
        assert extract_coins("") == ""


# ===========================================================================
# parse_gfc_info — TC IDs: GI-001 ~ GI-009
# ===========================================================================

# Standard /info reply from GFC bot (🌸白給宗師).
STANDARD_INFO_REPLY = (
    "■    Info  -  简要信息    ■\n"
    "\n"
    "用户账号：y***\n"
    "用户等级：白给大师\n"
    "白给币：1871\n"
    "可用流量：11.55 GB | 73.24%\n"
    "等级到期：2026-05-28 19:34:43"
)


class TestParseGfcInfo:
    """Test parse_gfc_info(text) which parses the GFC bot's /info reply."""

    # --- GI-001: Complete normal reply — all fields parsed ---
    def test_complete_reply(self):
        result = parse_gfc_info(STANDARD_INFO_REPLY)
        assert result["level"] == "白给大师"
        assert result["coins"] == 1871
        assert result["traffic"] == "11.55 GB"
        assert result["traffic_pct"] == "73.24%"
        assert result["expire_time"] == datetime.datetime(2026, 5, 28, 19, 34, 43)

    # --- GI-002: Missing level line (user hasn't upgraded) ---
    def test_no_level(self):
        text = (
            "■    Info  -  简要信息    ■\n"
            "用户账号：y***\n"
            "白给币：1871\n"
            "可用流量：11.55 GB | 73.24%\n"
            "等级到期：2026-05-28 19:34:43"
        )
        result = parse_gfc_info(text)
        assert result["level"] == ""
        assert result["coins"] == 1871  # other fields still work

    # --- GI-003: Missing expire time ---
    def test_no_expire_time(self):
        text = (
            "■    Info  -  简要信息    ■\n"
            "用户账号：y***\n"
            "用户等级：白给大师\n"
            "白给币：1871\n"
            "可用流量：11.55 GB | 73.24%"
        )
        result = parse_gfc_info(text)
        assert result["expire_time"] is None
        assert result["coins"] == 1871  # other fields still work

    # --- GI-004: Zero coins ---
    def test_zero_coins(self):
        text = STANDARD_INFO_REPLY.replace("白给币：1871", "白给币：0")
        result = parse_gfc_info(text)
        assert result["coins"] == 0

    # --- GI-005: Large coin value ---
    def test_large_coins(self):
        text = STANDARD_INFO_REPLY.replace("白给币：1871", "白给币：99999")
        result = parse_gfc_info(text)
        assert result["coins"] == 99999

    # --- GI-006: Traffic in MB ---
    def test_traffic_mb(self):
        text = STANDARD_INFO_REPLY.replace("11.55 GB", "512 MB")
        result = parse_gfc_info(text)
        assert result["traffic"] == "512 MB"

    # --- GI-007: Empty reply ---
    def test_empty_reply(self):
        result = parse_gfc_info("")
        assert result["level"] == ""
        assert result["coins"] == 0
        assert result["traffic"] == ""
        assert result["traffic_pct"] == ""
        assert result["expire_time"] is None

    # --- GI-008: Unrelated text — no keywords ---
    def test_unrelated_text(self):
        result = parse_gfc_info("随机文本没有关键字")
        assert result["level"] == ""
        assert result["coins"] == 0
        assert result["expire_time"] is None

    # --- GI-009: Level name with spaces ---
    def test_level_with_spaces(self):
        text = STANDARD_INFO_REPLY.replace("白给大师", "白给大师 Pro")
        result = parse_gfc_info(text)
        assert result["level"] == "白给大师 Pro"


# ===========================================================================
# classify_checkin_reply — TC IDs: GC-001 ~ GC-005
# ===========================================================================

class TestClassifyCheckinReply:
    """Test classify_checkin_reply(reply) which determines if a bot's
    /checkin reply indicates success or failure.

    Core principle: NO confirmation keyword = FAIL. Never assume success
    without explicit bot confirmation.
    """

    # --- GC-001: Bot confirms check-in success ---
    def test_checkin_success(self):
        reply = "【Lv.2白给大师】y***签到成功，累计签到715天，获得12.75 GB流量"
        assert classify_checkin_reply(reply) == "OK"

    # --- GC-002: Already checked in today (GFC "已相见" variant) ---
    def test_already_checked_in_gfc(self):
        reply = "今日已相见,转身便想念?"
        assert classify_checkin_reply(reply) == "OK"

    # --- GC-003: Already checked in today (standard) ---
    def test_already_checked_in(self):
        reply = "您今天已经签过到了！"
        assert classify_checkin_reply(reply) == "OK"

    # --- GC-004: Bot replied but no confirmation keyword ---
    def test_no_confirmation_keyword(self):
        reply = "请使用正确的命令"
        assert classify_checkin_reply(reply) == "FAIL"

    # --- GC-005: Empty reply (bot silent) ---
    def test_empty_reply(self):
        assert classify_checkin_reply("") == "FAIL"

    # --- GC-006: Unrelated text ---
    def test_unrelated_text(self):
        assert classify_checkin_reply("今天天气不错") == "FAIL"


# ===========================================================================
# classify_upgrade_reply — TC IDs: GU-001 ~ GU-006
# ===========================================================================

class TestClassifyUpgradeReply:
    """Test classify_upgrade_reply(reply) which verifies /upgrade succeeded.

    Upgrade costs 6 白给币 — must confirm bot acknowledged success.
    Keywords were tightened to avoid false positives from /info replies
    that happen to mention "白给大师" or generic "成功".
    """

    # --- GU-001: Successful upgrade with "升级成功" ---
    def test_upgrade_success(self):
        reply = "升级成功！白给大师+3天"
        assert classify_upgrade_reply(reply) == "OK"

    # --- GU-002: Successful with "续期成功" ---
    def test_upgrade_renewed(self):
        reply = "续期成功！已为您续期白给大师，消耗6白给币"
        assert classify_upgrade_reply(reply) == "OK"

    # --- GU-003: Successful with "已为您续期" ---
    def test_upgrade_already_renewed(self):
        reply = "已为您续期白给大师3天"
        assert classify_upgrade_reply(reply) == "OK"

    # --- GU-004: Successful with "续费成功" ---
    def test_upgrade_renewal_success(self):
        reply = "续费成功，白给大师到期时间已更新"
        assert classify_upgrade_reply(reply) == "OK"

    # --- GU-004b: Real GFC bot reply with "等级提升为" ---
    def test_real_gfc_upgrade_reply(self):
        reply = ("y***消耗 6 白给币,解锁了【TW.HiNet】节点.\n"
                 "等级提升为【白给大师】,持续 3 天!\n"
                 "赠送1G流量\n白给币剩余1869")
        assert classify_upgrade_reply(reply) == "OK"

    # --- GU-005: False positive guard — /info reply mentioning 白给大师 ---
    def test_info_reply_not_mistaken_as_upgrade(self):
        """\"用户等级：白给大师\" from /info must NOT match upgrade success."""
        reply = "用户等级：白给大师\n白给币：1871"
        assert classify_upgrade_reply(reply) == "FAIL"

    # --- GU-006: Bot replied but no success keyword ---
    def test_upgrade_no_success_keyword(self):
        reply = "白给币不足，升级失败"
        assert classify_upgrade_reply(reply) == "FAIL"

    # --- GU-007: Empty reply ---
    def test_upgrade_empty_reply(self):
        assert classify_upgrade_reply("") == "FAIL"

    # --- GU-008: Generic "成功" without upgrade-specific prefix ---
    def test_generic_success_not_matched(self):
        """Bare "成功" is too vague — must have upgrade-specific keyword."""
        reply = "操作成功"
        assert classify_upgrade_reply(reply) == "FAIL"
