#!/usr/bin/env python3
"""Tests for daily_token_report.py and monthly_token_report.py.

Tests the pure functions (CSV aggregation, topic classification, formatting)
without making real API calls.
"""

import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ============================================================
# Daily Report — aggregate_day
# ============================================================

class TestAggregateDay:
    """Tests for daily_token_report.aggregate_day()."""

    def _make_hourly_csv(self, rows, tmp_path):
        """Create a temp hourly CSV with given rows."""
        csv_path = tmp_path / "hourly_token_costs.csv"
        headers = ["timestamp", "date", "hour", "cost", "messages", "api_calls",
                   "tokens_in", "tokens_out", "tokens_cached", "cache_rate", "models"]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_aggregate_single_hour(self):
        """One hour of data → correct daily totals."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import aggregate_day

        rows = [{
            "timestamp": "2026-05-26 14:00",
            "date": "2026-05-26",
            "hour": "14:00",
            "cost": "12.50",
            "messages": "5",
            "api_calls": "50",
            "tokens_in": "5000000",
            "tokens_out": "10000",
            "tokens_cached": "4800000",
            "cache_rate": "96",
            "models": "5.1×50",
        }]

        result = aggregate_day(rows, "2026-05-26")
        assert result is not None
        assert result["total_cost"] == "12.50"
        assert result["messages"] == "5"
        assert result["hours_active"] == "1"
        assert result["peak_hour"] == "14:00"

    def test_aggregate_multiple_hours(self):
        """Multiple hours → summed correctly."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import aggregate_day

        rows = [
            {"timestamp": "2026-05-26 10:00", "date": "2026-05-26", "hour": "10:00",
             "cost": "5.00", "messages": "3", "api_calls": "30",
             "tokens_in": "3000000", "tokens_out": "5000",
             "tokens_cached": "2800000", "cache_rate": "93", "models": "5.1×30"},
            {"timestamp": "2026-05-26 14:00", "date": "2026-05-26", "hour": "14:00",
             "cost": "10.00", "messages": "5", "api_calls": "50",
             "tokens_in": "5000000", "tokens_out": "10000",
             "tokens_cached": "4800000", "cache_rate": "96", "models": "5.1×50"},
        ]

        result = aggregate_day(rows, "2026-05-26")
        assert result["total_cost"] == "15.00"
        assert result["messages"] == "8"
        assert result["hours_active"] == "2"
        assert result["peak_hour"] == "14:00"  # higher cost

    def test_aggregate_no_data_for_date(self):
        """Wrong date → None."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import aggregate_day

        rows = [{"timestamp": "2026-05-25 10:00", "date": "2026-05-25",
                 "hour": "10:00", "cost": "1.00", "messages": "1",
                 "api_calls": "1", "tokens_in": "100", "tokens_out": "10",
                 "tokens_cached": "50", "cache_rate": "50", "models": "5.1×1"}]

        result = aggregate_day(rows, "2026-05-26")
        assert result is None

    def test_aggregate_empty_rows(self):
        """Empty CSV → None."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import aggregate_day

        result = aggregate_day([], "2026-05-26")
        assert result is None


# ============================================================
# Monthly Report — classify_from_summaries
# ============================================================

class TestClassifyFromSummaries:
    """Tests for monthly_token_report.classify_from_summaries()."""

    def test_classify_coding(self):
        """Coding-related summary → correct category."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import classify_from_summaries

        rows = [{"summary": "今天修复了签到脚本的bug，添加了单元测试"}]
        cats = classify_from_summaries(rows)
        assert cats["🔧 代码开发"] == 1
        assert "🤖 AI/LLM" not in cats

    def test_classify_ai_topic(self):
        """AI/LLM topic → correct category."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import classify_from_summaries

        rows = [{"summary": "优化了token消耗报告系统，配置了GLM模型"}]
        cats = classify_from_summaries(rows)
        assert cats["🤖 AI/LLM"] == 1

    def test_classify_mixed_topics(self):
        """Multiple days, different topics."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import classify_from_summaries

        rows = [
            {"summary": "配置了HDC连接和hmdriver2库"},  # 鸿蒙
            {"summary": "优化了token缓存和429限流配置"},  # AI/LLM（token, 429）
            {"summary": "去超市买了菜"},                # 日常
        ]
        cats = classify_from_summaries(rows)
        assert cats.get("📱 鸿蒙自动化", 0) == 1
        assert cats.get("🤖 AI/LLM", 0) == 1
        assert cats.get("💬 日常对话", 0) == 1

    def test_classify_empty_summary(self):
        """Empty/missing summary → daily chat."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import classify_from_summaries

        rows = [{"summary": ""}, {"summary": "（无对话记录）"}]
        cats = classify_from_summaries(rows)
        assert cats.get("💬 日常对话", 0) == 2


# ============================================================
# CSV I/O helpers
# ============================================================

class TestCSVHelpers:
    """Tests for load_csv / save_csv helpers."""

    def test_load_nonexistent_returns_empty(self):
        """Nonexistent CSV → empty list."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import load_csv

        result = load_csv(Path("/tmp/nonexistent_csv_9999.csv"))
        assert result == []

    def test_save_and_load_roundtrip(self):
        """Write CSV → read back → same data."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from daily_token_report import load_csv, save_csv

        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "test.csv"
        headers = ["date", "cost"]
        rows = [{"date": "2026-05-26", "cost": "12.50"}]

        save_csv(csv_path, rows, headers)
        loaded = load_csv(csv_path)

        assert len(loaded) == 1
        assert loaded[0]["date"] == "2026-05-26"
        assert loaded[0]["cost"] == "12.50"

        # Cleanup
        os.unlink(csv_path)
        os.rmdir(tmp)


# ============================================================
# Monthly Report — filter_month
# ============================================================

class TestFilterMonth:
    """Tests for monthly_token_report.filter_month()."""

    def test_filter_correct_month(self):
        """Only rows matching the month prefix are returned."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import filter_month

        rows = [
            {"date": "2026-04-30", "cost": "5.00"},
            {"date": "2026-05-01", "cost": "10.00"},
            {"date": "2026-05-15", "cost": "15.00"},
            {"date": "2026-06-01", "cost": "20.00"},
        ]
        result = filter_month(rows, 2026, 5)
        assert len(result) == 2
        assert all(r["date"].startswith("2026-05") for r in result)

    def test_filter_empty(self):
        """No matching rows → empty list."""
        import sys
        sys.path.insert(0, str(Path.home() / ".hermes" / "scripts"))
        from monthly_token_report import filter_month

        rows = [{"date": "2026-04-30", "cost": "5.00"}]
        result = filter_month(rows, 2026, 5)
        assert result == []
