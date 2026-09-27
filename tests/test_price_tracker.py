"""Tests for price parsing, config validation, and CSV logging."""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import price_tracker as tracker  # noqa: E402
from price_tracker import (  # noqa: E402
    build_alert_message,
    fetch_price_demo,
    is_amazon_com_url,
    load_config,
    log_to_csv,
    parse_price,
    run_check,
    write_json_summary,
)


def soup(html):
    return BeautifulSoup(html, "html.parser")


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tracker.requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class TestParsePrice:
    def test_prefers_offscreen_over_whole_dollars(self):
        """Regression: a-price-whole was read first, truncating $299.97 to 299.0."""
        html = ('<span class="a-price-whole">299</span>'
                '<span class="a-offscreen">$299.97</span>')
        assert parse_price(soup(html)) == 299.97

    def test_combines_split_whole_and_fraction_without_truncation(self):
        html = (
            '<span class="a-price">'
            '<span class="a-price-whole">299</span>'
            '<span class="a-price-fraction">97</span>'
            "</span>"
        )
        assert parse_price(soup(html)) == 299.97

    def test_chooses_current_offer_over_crossed_out_list_price(self):
        html = (
            '<span class="a-price a-text-price">'
            '<span class="a-offscreen">$399.99</span></span>'
            '<span class="a-price priceToPay">'
            '<span class="a-offscreen">$299.99</span></span>'
        )
        assert parse_price(soup(html)) == 299.99

    @pytest.mark.parametrize("html,expected", [
        ('<span class="a-offscreen">$1,299.50</span>', 1299.50),
        ('<span class="a-offscreen">$29.99</span>', 29.99),
        ('<span id="priceblock_ourprice">$45.00</span>', 45.00),
        ('<span id="priceblock_dealprice">$19.95</span>', 19.95),
    ])
    def test_selector_fallback_chain(self, html, expected):
        assert parse_price(soup(html)) == expected

    @pytest.mark.parametrize("html", [
        "<div>No price on this page</div>",
        '<span class="a-offscreen">Currently unavailable</span>',
        '<span class="a-price-whole">75</span>',
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
            "products": [{
                "name": "Widget",
                "url": "https://www.amazon.com/dp/EXAMPLE",
                "threshold": 10.0,
            }],
            "email": {},
            "output_csv": "out.csv",
        }))
        assert load_config(good)["output_csv"] == "out.csv"

    @pytest.mark.parametrize("threshold", [0, -1, True, "10", float("inf")])
    def test_rejects_invalid_threshold(self, tmp_path, threshold):
        bad = tmp_path / "c.json"
        bad.write_text(json.dumps({
            "products": [{"name": "X", "url": "https://amazon.com/dp/X",
                          "threshold": threshold}],
            "email": {}, "output_csv": "out.csv",
        }))
        with pytest.raises(ValueError, match="positive number"):
            load_config(bad)

    def test_rejects_empty_watchlist(self, tmp_path):
        bad = tmp_path / "c.json"
        bad.write_text(json.dumps({
            "products": [], "email": {}, "output_csv": "out.csv"}))
        with pytest.raises(ValueError, match="non-empty list"):
            load_config(bad)


class TestDemoMode:
    def test_price_is_deterministic_and_within_simulated_band(self):
        product = {"name": "X", "url": "http://e.com", "threshold": 100.0}
        first = fetch_price_demo(product, index=0, seed=42)
        second = fetch_price_demo(product, index=0, seed=42)
        assert first == second
        assert first["price"] == 94.0
        assert first["error"] is None

    def test_different_rows_use_different_scenarios(self):
        product = {"name": "X", "url": "http://e.com", "threshold": 100.0}
        assert fetch_price_demo(product, index=0)["price"] != fetch_price_demo(
            product, index=1
        )["price"]


class TestLogToCsv:
    @staticmethod
    def record(name="X"):
        return {
            "timestamp": "2026-01-01T00:00:00+00:00", "name": name,
            "price": 9.99, "threshold": 10.0, "alert_triggered": True,
            "url": "https://www.amazon.com/dp/X", "error": None,
        }

    def test_writes_header_once_and_appends(self, tmp_path):
        path = tmp_path / "history.csv"
        record = self.record()
        log_to_csv(str(path), [record])
        log_to_csv(str(path), [record])

        with path.open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows[0][0] == "timestamp"
        assert len(rows) == 3          # 1 header + 2 data rows

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "history.csv"
        log_to_csv(path, [self.record()])
        assert path.exists()

    def test_neutralizes_spreadsheet_formula_in_product_name(self, tmp_path):
        path = tmp_path / "history.csv"
        log_to_csv(path, [self.record("=HYPERLINK(\"https://evil.test\")")])
        with path.open(encoding="utf-8") as handle:
            row = list(csv.DictReader(handle))[0]
        assert row["name"].startswith("'=")

    def test_empty_existing_file_gets_header(self, tmp_path):
        path = tmp_path / "history.csv"
        path.touch()
        log_to_csv(path, [self.record()])
        with path.open(encoding="utf-8") as handle:
            assert list(csv.reader(handle))[0][0] == "timestamp"


class TestEmailHtmlEscaping:
    """Product names are scraped from a remote page -- untrusted input."""

    @staticmethod
    def _render(name, url="https://www.amazon.com/dp/X"):
        config = {"email": {
            "sender_email": "sender@example.com",
            "recipients": ["recipient@example.com"],
        }}
        alert = {"name": name, "url": url, "price": 9.0, "threshold": 10.0}
        message = build_alert_message(config, [alert])
        return message.get_payload()[1].get_payload(decode=True).decode("utf-8")

    def test_script_tag_in_product_name_is_escaped(self):
        out = self._render("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_quote_breakout_in_product_name_is_escaped(self):
        out = self._render("\" onmouseover=\"alert(1)")
        assert 'onmouseover="alert(1)' not in out

    def test_javascript_scheme_url_is_rejected(self):
        out = self._render("Widget", url="javascript:alert(1)")
        assert "javascript:" not in out
        assert "href='#'" in out

    def test_https_url_is_preserved(self):
        out = self._render("Widget", url="https://www.amazon.com/dp/B09XS7JWHH")
        assert "https://www.amazon.com/dp/B09XS7JWHH" in out

    def test_non_amazon_https_url_is_rejected(self):
        out = self._render("Widget", url="https://example.com/product")
        assert "example.com" not in out
        assert "href=\"#\"" in out or "href='#'" in out

    def test_message_has_plain_and_html_parts(self):
        config = {"email": {
            "sender_email": "sender@example.com",
            "recipients": ["recipient@example.com"],
        }}
        message = build_alert_message(config, [{
            "name": "Widget", "url": "https://amazon.com/dp/X",
            "price": 9.0, "threshold": 10.0,
        }])
        assert [part.get_content_type() for part in message.get_payload()] == [
            "text/plain", "text/html"
        ]


class TestUrlSafety:
    @pytest.mark.parametrize("url", [
        "https://amazon.com/dp/X",
        "https://www.amazon.com/dp/X",
        "https://smile.amazon.com/dp/X",
    ])
    def test_accepts_amazon_https_urls(self, url):
        assert is_amazon_com_url(url)

    @pytest.mark.parametrize("url", [
        "http://amazon.com/dp/X",
        "https://amazon.com.evil.test/dp/X",
        "https://user:pass@amazon.com/dp/X",
        "file:///etc/passwd",
        "not a url",
    ])
    def test_rejects_unsafe_urls(self, url):
        assert not is_amazon_com_url(url)

    def test_fetch_rejects_unsafe_url_before_network(self, monkeypatch):
        monkeypatch.setattr(
            tracker.requests,
            "Session",
            lambda: pytest.fail("network client should not be constructed"),
        )
        result = tracker.fetch_price("http://127.0.0.1/admin")
        assert result["price"] is None
        assert "Refused" in result["error"]


class TestLegacyFetchAdapter:
    url = "https://www.amazon.com/dp/EXAMPLE"
    product_html = (
        '<span id="productTitle">Example Product</span>'
        '<div id="corePrice_feature_div">'
        '<span class="a-price priceToPay">'
        '<span class="a-offscreen">$29.97</span>'
        "</span></div>"
    )

    @staticmethod
    def install(monkeypatch, session):
        monkeypatch.setattr(tracker.requests, "Session", lambda: session)

    def test_success_parses_current_price_and_closes_session(self, monkeypatch):
        session = FakeSession(FakeResponse(text=self.product_html))
        self.install(monkeypatch, session)

        result = tracker.fetch_price(self.url)

        assert result == {
            "name": "Example Product",
            "price": 29.97,
            "url": self.url,
            "error": None,
        }
        assert session.calls[0][1]["allow_redirects"] is False
        assert session.calls[0][1]["timeout"] == (5, 15)
        assert session.closed is True

    def test_redirect_is_refused_without_retry(self, monkeypatch):
        session = FakeSession(FakeResponse(status_code=302))
        self.install(monkeypatch, session)

        result = tracker.fetch_price(self.url)

        assert "Redirect refused" in result["error"]
        assert len(session.calls) == 1
        assert session.closed is True

    def test_transient_429_retries_then_succeeds(self, monkeypatch):
        session = FakeSession(
            FakeResponse(status_code=429),
            FakeResponse(text=self.product_html),
        )
        sleeps = []
        self.install(monkeypatch, session)
        monkeypatch.setattr(tracker.time, "sleep", sleeps.append)

        result = tracker.fetch_price(self.url)

        assert result["price"] == 29.97
        assert len(session.calls) == 2
        assert sleeps == [1]
        assert session.closed is True

    def test_non_retryable_404_stops_immediately(self, monkeypatch):
        session = FakeSession(FakeResponse(status_code=404))
        sleeps = []
        self.install(monkeypatch, session)
        monkeypatch.setattr(tracker.time, "sleep", sleeps.append)

        result = tracker.fetch_price(self.url)

        assert result["error"] == "HTTP 404"
        assert len(session.calls) == 1
        assert sleeps == []
        assert session.closed is True

    def test_timeout_exhaustion_is_bounded_and_closes_session(self, monkeypatch):
        timeout = tracker.requests.exceptions.Timeout()
        session = FakeSession(timeout, tracker.requests.exceptions.Timeout())
        sleeps = []
        self.install(monkeypatch, session)
        monkeypatch.setattr(tracker.time, "sleep", sleeps.append)

        result = tracker.fetch_price(self.url, retries=2)

        assert result["error"] == "Request timed out"
        assert len(session.calls) == 2
        assert sleeps == [1]
        assert session.closed is True


class TestRunPipeline:
    @staticmethod
    def config(path):
        return {
            "products": [
                {"name": "Below", "url": "https://amazon.com/dp/A", "threshold": 100.0},
                {"name": "Above", "url": "https://amazon.com/dp/B", "threshold": 100.0},
            ],
            "email": {},
            "output_csv": str(path),
            "check_interval_hours": 24,
        }

    def test_demo_returns_summary_and_suppresses_smtp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            tracker,
            "send_alert_email",
            lambda *_args, **_kwargs: pytest.fail("demo mode must not send email"),
        )
        summary = run_check(
            self.config(tmp_path / "history.csv"),
            demo=True,
            seed=42,
            checked_at=dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC),
        )
        assert summary["mode"] == "demo"
        assert summary["products_checked"] == 2
        assert summary["alerts_triggered"] == 1
        assert summary["email_sent"] is False
        assert summary["checked_at"] == "2026-01-02T03:04:00+00:00"

    def test_json_summary_creates_parent(self, tmp_path):
        destination = tmp_path / "nested" / "summary.json"
        write_json_summary(destination, {"products_checked": 2})
        assert json.loads(destination.read_text(encoding="utf-8"))["products_checked"] == 2

    def test_main_defaults_to_offline_demo(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config = self.config(tmp_path / "history.csv")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        summary_path = tmp_path / "summary.json"
        monkeypatch.setattr(
            tracker,
            "fetch_price",
            lambda _url: pytest.fail("default CLI must not perform live HTTP"),
        )
        assert tracker.main([
            "--config", str(config_path),
            "--json-out", str(summary_path),
        ]) == 0
        assert json.loads(summary_path.read_text(encoding="utf-8"))["mode"] == "demo"

    def test_main_live_flag_dispatches_to_legacy_adapter(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.json"
        config = self.config(tmp_path / "history.csv")
        config["products"] = [config["products"][0]]
        config_path.write_text(json.dumps(config), encoding="utf-8")
        seen = []

        def fake_fetch(url):
            seen.append(url)
            return {"name": "Live", "price": 101.0, "url": url, "error": None}

        monkeypatch.setattr(tracker, "fetch_price", fake_fetch)
        assert tracker.main([
            "--config", str(config_path),
            "--live-scrape",
        ]) == 0
        assert seen == ["https://amazon.com/dp/A"]


class TestSmtpDelivery:
    def test_uses_timeout_and_verified_tls(self, monkeypatch):
        events = {}

        class FakeSmtp:
            def __init__(self, host, port, timeout):
                events["connection"] = (host, port, timeout)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def ehlo(self):
                events["ehlo"] = events.get("ehlo", 0) + 1

            def starttls(self, *, context):
                events["tls_context"] = context

            def login(self, sender, password):
                events["login"] = (sender, password)

            def sendmail(self, sender, recipients, message):
                events["sent"] = (sender, recipients, message)

        monkeypatch.setenv("SMTP_PASSWORD", "app-password")
        monkeypatch.setattr(tracker.smtplib, "SMTP", FakeSmtp)
        config = {"email": {
            "smtp_server": "smtp.example.com", "smtp_port": 587,
            "sender_email": "sender@example.com",
            "recipients": ["recipient@example.com"],
        }}
        alert = [{
            "name": "Widget", "url": "https://amazon.com/dp/X",
            "price": 9.0, "threshold": 10.0,
        }]
        assert tracker.send_alert_email(config, alert) is True
        assert events["connection"] == ("smtp.example.com", 587, 30)
        assert events["ehlo"] == 2
        assert events["tls_context"].verify_mode != 0
