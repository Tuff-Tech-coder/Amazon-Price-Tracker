"""Tests for price parsing, config validation, and CSV logging."""
import csv
import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from price_tracker import (  # noqa: E402
    fetch_price_demo,
    load_config,
    log_to_csv,
    parse_price,
)


def soup(html):
    return BeautifulSoup(html, "html.parser")


class TestParsePrice:
    def test_prefers_offscreen_over_whole_dollars(self):
        """Regression: a-price-whole was read first, truncating $299.97 to 299.0."""
        html = ('<span class="a-price-whole">299</span>'
                '<span class="a-offscreen">$299.97</span>')
        assert parse_price(soup(html)) == 299.97

    @pytest.mark.parametrize("html,expected", [
        ('<span class="a-offscreen">$1,299.50</span>', 1299.50),
        ('<span class="a-offscreen">$29.99</span>', 29.99),
        ('<span id="priceblock_ourprice">$45.00</span>', 45.00),
        ('<span id="priceblock_dealprice">$19.95</span>', 19.95),
        ('<span class="a-price-whole">75</span>', 75.0),
    ])
    def test_selector_fallback_chain(self, html, expected):
        assert parse_price(soup(html)) == expected

    @pytest.mark.parametrize("html", [
        "<div>No price on this page</div>",
        '<span class="a-offscreen">Currently unavailable</span>',
        "",
    ])
    def test_returns_none_when_absent(self, html):
        assert parse_price(soup(html)) is None

    def test_ignores_zero_price(self):
        assert parse_price(soup('<span class="a-offscreen">$0.00</span>')) is None


class TestLoadConfig:
    def test_rejects_missing_required_keys(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text(json.dumps({"products": []}))
        with pytest.raises(ValueError, match="Missing required config key"):
            load_config(bad)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.json")

    def test_accepts_valid_config(self, tmp_path):
        good = tmp_path / "c.json"
        good.write_text(json.dumps({
            "products": [], "email": {}, "output_csv": "out.csv"}))
        assert load_config(good)["output_csv"] == "out.csv"


class TestDemoMode:
    def test_price_is_within_simulated_band(self):
        product = {"name": "X", "url": "http://e.com", "threshold": 100.0}
        for _ in range(50):
            r = fetch_price_demo(product)
            assert 80.0 <= r["price"] <= 115.0
            assert r["error"] is None


class TestLogToCsv:
    def test_writes_header_once_and_appends(self, tmp_path):
        path = tmp_path / "history.csv"
        record = {
            "timestamp": "2026-01-01T00:00:00", "name": "X", "price": 9.99,
            "threshold": 10.0, "alert_triggered": True, "url": "http://e.com",
            "error": None,
        }
        log_to_csv(str(path), [record])
        log_to_csv(str(path), [record])

        rows = list(csv.reader(path.open()))
        assert rows[0][0] == "timestamp"
        assert len(rows) == 3          # 1 header + 2 data rows
