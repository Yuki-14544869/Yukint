#!/usr/bin/env python3
"""Tests for token_cost_report.py and token_report_cron.py.

Test case IDs reference: monitoring/TEST_CASES.md
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts dir to path so we can import the modules
SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import token_cost_report as rpt
import token_report_cron as cron


# ============================================================
# PR-001~PR-004: get_pricing
# ============================================================

class TestGetPricing:
    """Tests for get_pricing(model) → (input_price, cache_price, output_price)."""

    def test_pr001_glm51_returns_flagship_pricing(self):
        """PR-001: glm-5.1 uses flagship pricing."""
        assert rpt.get_pricing("glm-5.1") == (6.0, 1.5, 24.0)

    def test_pr002_glm47_returns_budget_pricing(self):
        """PR-002: glm-4.7 uses budget pricing."""
        assert rpt.get_pricing("glm-4.7") == (1.0, 0.25, 4.0)

    def test_pr003_glm45_returns_budget_pricing(self):
        """PR-003: glm-4.5 also uses budget pricing."""
        assert rpt.get_pricing("glm-4.5") == (1.0, 0.25, 4.0)

    def test_pr004_unknown_model_defaults_to_flagship(self):
        """PR-004: unknown model defaults to flagship pricing."""
        assert rpt.get_pricing("glm-5-turbo") == (6.0, 1.5, 24.0)
        assert rpt.get_pricing("claude-sonnet") == (6.0, 1.5, 24.0)


# ============================================================
# FM-001~FM-004: fmt
# ============================================================

class TestFmt:
    """Tests for fmt(n) → human-readable number string."""

    def test_fm001_millions(self):
        """FM-001: 1.5M format."""
        assert rpt.fmt(1_500_000) == "1.5M"

    def test_fm002_thousands(self):
        """FM-002: 15K format."""
        assert rpt.fmt(15_000) == "15K"

    def test_fm003_small_number(self):
        """FM-003: plain number for <1000."""
        assert rpt.fmt(500) == "500"

    def test_fm004_zero(self):
        """FM-004: zero edge case."""
        assert rpt.fmt(0) == "0"

    def test_fm_exact_thousand(self):
        """Boundary: exactly 1000."""
        assert rpt.fmt(1000) == "1K"

    def test_fm_exact_million(self):
        """Boundary: exactly 1_000_000."""
        assert rpt.fmt(1_000_000) == "1.0M"


# ============================================================
# TR-001~TR-004: truncate_msg
# ============================================================

class TestTruncateMsg:
    """Tests for truncate_msg(msg, max_len)."""

    def test_tr001_short_message_not_truncated(self):
        """TR-001: short message passes through."""
        assert rpt.truncate_msg("hello") == "hello"

    def test_tr002_long_message_truncated(self):
        """TR-002: message > max_len gets truncated with ellipsis."""
        long_msg = "a" * 40
        result = rpt.truncate_msg(long_msg, max_len=35)
        assert len(result) == 35
        assert result.endswith("…")

    def test_tr003_voice_message_extracted(self):
        """TR-003: voice message pattern extracts the text."""
        voice = '[The user sent a voice message~ Transcription: "你好世界" end]'
        result = rpt.truncate_msg(voice)
        assert result.startswith("🎤")
        assert "你好世界" in result

    def test_tr004_newline_replaced(self):
        """TR-004: newlines become spaces."""
        assert rpt.truncate_msg("line1\nline2") == "line1 line2"

    def test_tr_exact_max_len(self):
        """Boundary: message exactly at max_len."""
        msg = "a" * 35
        assert rpt.truncate_msg(msg, max_len=35) == msg


# ============================================================
# PL-001~PL-005: parse_logs
# ============================================================

class TestParseLogs:
    """Tests for parse_logs() with temp log files."""

    def _make_log(self, content: str) -> Path:
        """Create a temp log file and return its path."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.write(content)
        tmp.close()
        return Path(tmp.name)

    def test_pl001_empty_log_returns_empty(self):
        """PL-001: empty log file → empty list."""
        log = self._make_log("")
        with patch.object(rpt, "LOG_PATH", log):
            result = rpt.parse_logs()
        os.unlink(log)
        assert result == []

    def test_pl002_nonexistent_log_returns_empty(self):
        """PL-002: nonexistent path → empty list."""
        with patch.object(rpt, "LOG_PATH", Path("/tmp/nonexistent_test_log_9999.log")):
            result = rpt.parse_logs()
        assert result == []

    def test_pl003_normal_turn_and_api_parsed(self):
        """PL-003: standard format lines → correct dict structure."""
        today = datetime.now().strftime("%Y-%m-%d")
        ts1 = f"{today} 10:00:00"
        ts2 = f"{today} 10:00:01"
        ts3 = f"{today} 10:00:05"
        content = (
            f'{ts1} INFO conversation turn: msg=\'用户消息\'\n'
            f'{ts2} INFO API call #1: model=glm-5.1 in=1000 out=500 cache=200/1000\n'
            f'{ts3} INFO Turn ended: response_len=1234\n'
        )
        log = self._make_log(content)
        with patch.object(rpt, "LOG_PATH", log):
            turns = rpt.parse_logs()
        os.unlink(log)

        assert len(turns) == 1
        t = turns[0]
        assert t["msg"] == "用户消息"
        assert len(t["api_calls"]) == 1
        assert t["api_calls"][0]["model"] == "glm-5.1"
        assert t["api_calls"][0]["input"] == 1000
        assert t["api_calls"][0]["output"] == 500
        assert t["api_calls"][0]["cached"] == 200
        assert t["response_len"] == 1234

    def test_pl004_system_messages_filtered(self):
        """PL-004: 'Review the conversation' turns are filtered out."""
        today = datetime.now().strftime("%Y-%m-%d")
        ts = f"{today} 10:00:00"
        content = (
            f"{ts} INFO conversation turn: msg='Review the conversation history'\n"
            f"{ts} INFO conversation turn: msg='normal message'\n"
        )
        log = self._make_log(content)
        with patch.object(rpt, "LOG_PATH", log):
            turns = rpt.parse_logs()
        os.unlink(log)

        assert len(turns) == 1
        assert turns[0]["msg"] == "normal message"

    def test_pl005_double_quoted_msg(self):
        """PL-005: double-quoted msg format also parsed."""
        today = datetime.now().strftime("%Y-%m-%d")
        ts = f"{today} 10:00:00"
        content = f'{ts} INFO conversation turn: msg="double quoted"\n'
        log = self._make_log(content)
        with patch.object(rpt, "LOG_PATH", log):
            turns = rpt.parse_logs()
        os.unlink(log)

        assert len(turns) == 1
        assert turns[0]["msg"] == "double quoted"

    def test_pl005_mixed_models(self):
        """PL-005: multiple model calls in one turn are tracked."""
        today = datetime.now().strftime("%Y-%m-%d")
        ts = f"{today} 10:00:00"
        content = (
            f"{ts} INFO conversation turn: msg='test'\n"
            f"{ts} INFO API call #1: model=glm-5.1 in=2000 out=300 cache=500/2000\n"
            f"{ts} INFO API call #2: model=glm-4.7 in=1000 out=100 cache=200/1000\n"
        )
        log = self._make_log(content)
        with patch.object(rpt, "LOG_PATH", log):
            turns = rpt.parse_logs()
        os.unlink(log)

        assert len(turns) == 1
        assert len(turns[0]["api_calls"]) == 2


# ============================================================
# CT-001~CT-004: compute_turn
# ============================================================

class TestComputeTurn:
    """Tests for compute_turn(turn) → stats dict."""

    def test_ct001_single_model_cost(self):
        """CT-001: single glm-5.1 call → correct cost."""
        turn = {
            "api_calls": [{
                "model": "glm-5.1", "input": 100_000,
                "output": 10_000, "cached": 50_000,
            }],
        }
        s = rpt.compute_turn(turn)
        # Cost = (100K-50K)/1M*6 + 50K/1M*1.5 + 10K/1M*24
        #      = 0.3 + 0.075 + 0.24 = 0.615
        assert abs(s["cost"] - 0.615) < 0.001
        assert s["calls"] == 1
        assert s["input"] == 100_000
        assert s["output"] == 10_000
        assert s["cached"] == 50_000
        assert s["cache_rate"] == 50.0
        assert "5.1" in s["model"]

    def test_ct002_mixed_model_cost(self):
        """CT-002: glm-5.1 + glm-4.7 → correct total cost."""
        turn = {
            "api_calls": [
                {"model": "glm-5.1", "input": 200_000, "output": 20_000, "cached": 100_000},
                {"model": "glm-4.7", "input": 50_000, "output": 5_000, "cached": 10_000},
            ],
        }
        s = rpt.compute_turn(turn)
        # 5.1: (200K-100K)/1M*6 + 100K/1M*1.5 + 20K/1M*24 = 0.6+0.15+0.48 = 1.23
        # 4.7: (50K-10K)/1M*1 + 10K/1M*0.25 + 5K/1M*4 = 0.04+0.0025+0.02 = 0.0625
        # Total: 1.2925
        assert abs(s["cost"] - 1.2925) < 0.001
        assert s["calls"] == 2
        assert "5.1" in s["model_tag"]
        assert "4.7" in s["model_tag"]

    def test_ct003_cache_rate(self):
        """CT-003: cache rate computed correctly."""
        turn = {
            "api_calls": [{
                "model": "glm-5.1", "input": 200,
                "output": 50, "cached": 100,
            }],
        }
        s = rpt.compute_turn(turn)
        assert s["cache_rate"] == 50.0

    def test_ct004_empty_calls_zero_cost(self):
        """CT-004: no API calls → zero cost."""
        turn = {"api_calls": []}
        s = rpt.compute_turn(turn)
        assert s["cost"] == 0
        assert s["calls"] == 0
        assert s["input"] == 0


# ============================================================
# BT-001~BT-002: build_table
# ============================================================

class TestBuildTable:
    """Tests for build_table(turns, start_time, end_time)."""

    def _make_turn(self, hours_ago: float, msg: str = "test"):
        """Create a turn dict at a specific time offset."""
        ts = datetime.now() - timedelta(hours=hours_ago)
        return {
            "time": ts,
            "msg": msg,
            "api_calls": [{
                "model": "glm-5.1", "input": 1000,
                "output": 100, "cached": 200,
            }],
            "response_len": 50,
        }

    def test_bt001_no_data_returns_empty_html(self):
        """BT-001: empty turns → '无数据'."""
        html, n_msgs, n_calls, cost = rpt.build_table([])
        assert "无数据" in html
        assert n_msgs == 0
        assert cost == 0

    def test_bt002_time_filter_excludes_old_turns(self):
        """BT-002: turns outside time range are excluded."""
        old_turn = self._make_turn(5, "old")
        recent_turn = self._make_turn(0.5, "recent")
        now = datetime.now()
        hour_ago = now - timedelta(hours=1)

        html, n_msgs, _, _ = rpt.build_table(
            [old_turn, recent_turn],
            start_time=hour_ago,
            end_time=now,
        )
        assert n_msgs == 1
        assert "recent" in html

    def test_turns_without_api_calls_filtered(self):
        """Turns with no api_calls are skipped."""
        turn = {
            "time": datetime.now(), "msg": "empty",
            "api_calls": [], "response_len": 0,
        }
        html, n_msgs, _, _ = rpt.build_table([turn])
        assert n_msgs == 0


# ============================================================
# HA-001~HA-005: has_recent_activity (token_report_cron)
# ============================================================

class TestHasRecentActivity:
    """Tests for has_recent_activity(hours) in token_report_cron."""

    def _make_log(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.write(content)
        tmp.close()
        return Path(tmp.name)

    def test_ha001_recent_activity_returns_true(self):
        """HA-001: turn within 1 hour → True."""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        today = now.strftime("%Y-%m-%d")
        content = f"{ts} INFO conversation turn: msg='hello'\n"
        log = self._make_log(content)
        with patch.object(cron, "LOG_PATH", log):
            assert cron.has_recent_activity(hours=1) is True
        os.unlink(log)

    def test_ha002_no_recent_activity_returns_false(self):
        """HA-002: no turns in past hour → False."""
        old = datetime.now() - timedelta(hours=3)
        ts = old.strftime("%Y-%m-%d %H:%M:%S")
        content = f"{ts} INFO conversation turn: msg='old'\n"
        log = self._make_log(content)
        with patch.object(cron, "LOG_PATH", log):
            assert cron.has_recent_activity(hours=1) is False
        os.unlink(log)

    def test_ha003_nonexistent_log_returns_false(self):
        """HA-003: no log file → False."""
        with patch.object(cron, "LOG_PATH", Path("/tmp/no_such_log_9999.log")):
            assert cron.has_recent_activity(hours=1) is False

    def test_ha004_empty_log_returns_false(self):
        """HA-004: empty log → False."""
        log = self._make_log("")
        with patch.object(cron, "LOG_PATH", log):
            assert cron.has_recent_activity(hours=1) is False
        os.unlink(log)

    def test_ha005_system_turns_not_counted(self):
        """HA-005: 'Review the conversation' turns don't count."""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        content = f"{ts} INFO conversation turn: msg='Review the conversation history'\n"
        log = self._make_log(content)
        with patch.object(cron, "LOG_PATH", log):
            assert cron.has_recent_activity(hours=1) is False
        os.unlink(log)

    def test_system_note_turns_not_counted(self):
        """System note turns don't count as activity."""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        content = f"{ts} INFO conversation turn: msg='[System note: context compaction]'\n"
        log = self._make_log(content)
        with patch.object(cron, "LOG_PATH", log):
            assert cron.has_recent_activity(hours=1) is False
        os.unlink(log)
