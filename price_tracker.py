"""
Amazon Price Tracker
====================
Evaluates a product watchlist, records timestamped price checks, and previews
or sends alerts when prices fall below user-defined targets. The default path
is deterministic and offline.

Usage:
    python price_tracker.py                  # Deterministic offline demo
    python price_tracker.py --json-out run.json
    python price_tracker.py --live-scrape    # Explicit legacy adapter opt-in

Environment variables required for email alerts:
    SMTP_PASSWORD   Your Gmail app password (or SMTP provider password)

For live email only, provide SMTP_PASSWORD through the process environment.
The program intentionally does not load .env files.
"""

import argparse
import csv
import datetime as dt
import html
import json
import logging
import math
import os
import re
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("price_tracker")
logger.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_FILE = Path(__file__).parent / "config.json"
DEMO_FACTORS = (0.86, 1.08, 0.94, 1.11, 0.79)


def configure_logging(log_file: Path | None = None) -> None:
    """Configure console and rotating-file logging at runtime, not import time."""
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            log_file,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        logger.addHandler(rotating)


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------
# Amazon pages may expose several prices at once (list, deal, coupon, and
# current offer). Prefer elements explicitly marked as the price-to-pay and
# containers in the active product-price/buy-box area. A bare global
# ``a-offscreen`` first match is unsafe because it can be a crossed-out list
# price. Older single-price IDs remain supported after the modern selectors.
PRICE_TEXT_SELECTORS = (
    "#corePrice_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_desktop .priceToPay .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#buybox .priceToPay .a-offscreen",
    ".priceToPay .a-offscreen",
    ".apexPriceToPay .a-offscreen",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
)

PRICE_CONTAINER_SELECTORS = (
    "#corePrice_feature_div .priceToPay",
    "#corePriceDisplay_desktop_feature_div .priceToPay",
    "#corePrice_desktop .priceToPay",
    "#apex_desktop .priceToPay",
    "#buybox .priceToPay",
    ".priceToPay",
    ".apexPriceToPay",
)

LIST_PRICE_CLASSES = {"a-text-price", "basisPrice", "priceBlockStrikePriceString"}

_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)")


def _parse_money_text(value: str) -> float | None:
    match = _PRICE_RE.search(value)
    if not match:
        return None
    try:
        price = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return price if math.isfinite(price) and price > 0 else None


def _is_list_price(tag: Any) -> bool:
    """Identify crossed-out/list-price markup surrounding a candidate."""
    for ancestor in (tag, *tag.parents):
        classes = set(ancestor.get("class", [])) if hasattr(ancestor, "get") else set()
        if classes & LIST_PRICE_CLASSES:
            return True
        if ancestor.get("id") in {"priceblock_listprice", "listPrice"}:
            return True
    return False


def _parse_split_price(container: Any) -> float | None:
    """Combine Amazon's separate whole/fraction spans without truncation."""
    whole_tag = container.select_one(".a-price-whole")
    fraction_tag = container.select_one(".a-price-fraction")
    if not whole_tag or not fraction_tag:
        return None

    whole = re.sub(r"[^\d,]", "", whole_tag.get_text(strip=True))
    fraction = re.sub(r"\D", "", fraction_tag.get_text(strip=True))
    if not whole or len(fraction) not in {1, 2}:
        return None
    return _parse_money_text(f"{whole}.{fraction.ljust(2, '0')}")


def parse_price(soup: BeautifulSoup) -> float | None:
    """
    Extract a product price from a parsed Amazon page.

    Prefer the active price-to-pay over list-price markup, then combine the
    split whole/fraction representation when needed. Ambiguous global prices
    and incomplete whole-dollar spans are rejected rather than guessed.
    """
    for selector in PRICE_TEXT_SELECTORS:
        tag = soup.select_one(selector)
        if not tag:
            continue
        value = _parse_money_text(tag.get_text(strip=True))
        if value is not None:
            return value

    for selector in PRICE_CONTAINER_SELECTORS:
        container = soup.select_one(selector)
        if container:
            value = _parse_split_price(container)
            if value is not None:
                return value

    # A single non-list screen-reader price is safe as a compatibility
    # fallback. If several remain, choosing one would be an ungrounded guess.
    offscreen = [
        tag for tag in soup.select("span.a-offscreen") if not _is_list_price(tag)
    ]
    if len(offscreen) == 1:
        value = _parse_money_text(offscreen[0].get_text(strip=True))
        if value is not None:
            return value

    split_candidates = []
    for container in soup.select("span.a-price"):
        if _is_list_price(container):
            continue
        value = _parse_split_price(container)
        if value is not None:
            split_candidates.append(value)
    if len(split_candidates) == 1:
        return split_candidates[0]
    return None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_amazon_com_url(url: str) -> bool:
    """Return whether a URL is a credential-free HTTPS Amazon.com URL."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme.casefold() == "https"
        and parsed.username is None
        and parsed.password is None
        and (host == "amazon.com" or host.endswith(".amazon.com"))
    )


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    """Load and validate configuration with product-specific errors."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("Config root must be a JSON object")
    for key in ("products", "email", "output_csv"):
        if key not in config:
            raise ValueError(f"Missing required config key: '{key}'")

    products = config["products"]
    if not isinstance(products, list) or not products:
        raise ValueError("'products' must be a non-empty list")
    for index, product in enumerate(products, start=1):
        if not isinstance(product, dict):
            raise ValueError(f"Product {index} must be a JSON object")
        for key in ("name", "url", "threshold"):
            if key not in product:
                raise ValueError(f"Product {index} is missing '{key}'")
        if not _nonempty_string(product["name"]):
            raise ValueError(f"Product {index} name must be a non-empty string")
        if not _nonempty_string(product["url"]):
            raise ValueError(f"Product {index} URL must be a non-empty string")
        threshold = product["threshold"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or float(threshold) <= 0
        ):
            raise ValueError(f"Product {index} threshold must be a positive number")

    if not isinstance(config["email"], dict):
        raise ValueError("'email' must be a JSON object")
    if not _nonempty_string(config["output_csv"]):
        raise ValueError("'output_csv' must be a non-empty path string")
    interval = config.get("check_interval_hours", 24)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(float(interval))
        or float(interval) <= 0
    ):
        raise ValueError("'check_interval_hours' must be a positive number")
    return config


# ---------------------------------------------------------------------------
# Price scraper
# ---------------------------------------------------------------------------
def fetch_price(url: str, retries: int = 3) -> dict[str, Any]:
    """Use the explicitly enabled legacy HTML adapter for Amazon.com.

    Requests are restricted to credential-free HTTPS Amazon.com URLs,
    redirects are refused, and only transient failures are retried. New
    integrations should use Amazon's supported Creators API instead.
    """
    if not is_amazon_com_url(url):
        return {
            "name": "Unknown",
            "price": None,
            "url": url,
            "error": "Refused non-Amazon.com or non-HTTPS URL",
        }
    if retries < 1:
        raise ValueError("retries must be at least 1")

    headers = {
        "User-Agent": "PriceTrackerPortfolioDemo/1.0 (supported API recommended)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error = "All retries failed"
    session = requests.Session()
    try:
        for attempt in range(1, retries + 1):
            try:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=(5, 15),
                    allow_redirects=False,
                )
                if 300 <= response.status_code < 400:
                    last_error = "Redirect refused by the legacy safety guard"
                    break
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")
                name_tag = soup.find("span", {"id": "productTitle"})
                name = name_tag.get_text(strip=True) if name_tag else "Unknown Product"
                price = parse_price(soup)
                if price is None:
                    return {
                        "name": name,
                        "price": None,
                        "url": url,
                        "error": "Price not found in response",
                    }
                return {"name": name, "price": price, "url": url, "error": None}
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                last_error = f"HTTP {status or 'error'}"
                logger.warning(
                    "HTTP error on attempt %s/%s for %s: %s",
                    attempt,
                    retries,
                    url,
                    exc,
                )
                if status not in {429, 500, 502, 503, 504}:
                    break
            except requests.exceptions.ConnectionError as exc:
                last_error = f"Connection error: {exc}"
                logger.warning("Connection error on attempt %s/%s", attempt, retries)
            except requests.exceptions.Timeout:
                last_error = "Request timed out"
                logger.warning("Timeout on attempt %s/%s for %s", attempt, retries, url)
            except requests.exceptions.RequestException as exc:
                last_error = f"Request failed: {exc}"
                logger.warning("Request error on attempt %s/%s: %s", attempt, retries, exc)
                break

            if attempt < retries:
                backoff = min(2 ** (attempt - 1), 8)
                logger.info("Waiting %ss before retry", backoff)
                time.sleep(backoff)
    finally:
        session.close()

    return {"name": "Unknown", "price": None, "url": url, "error": last_error}


def fetch_price_demo(
    product: dict[str, Any], index: int = 0, seed: int = 42
) -> dict[str, Any]:
    """Simulate a deterministic price without making a network request."""
    factor = DEMO_FACTORS[(index + seed) % len(DEMO_FACTORS)]
    simulated_price = round(float(product["threshold"]) * factor, 2)
    return {
        "name": product["name"],
        "price": simulated_price,
        "url": product["url"],
        "error": None,
    }


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------
def _csv_safe(value: Any) -> Any:
    """Neutralize formulas in remote text before users open the CSV."""
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def log_to_csv(csv_path: str | Path, records: list[dict[str, Any]]) -> None:
    """Append records safely, creating parent folders and empty-file headers."""
    fieldnames = ["timestamp", "name", "price", "threshold", "alert_triggered", "url", "error"]
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0

    safe_records = []
    for record in records:
        safe_records.append({
            key: _csv_safe(record.get(key)) if key in {"name", "url", "error"}
            else record.get(key)
            for key in fieldnames
        })

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(safe_records)

    logger.info("Logged %s records to %s", len(records), path)


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------
def _safe_amazon_link(url: Any) -> str:
    value = str(url)
    return html.escape(value, quote=True) if is_amazon_com_url(value) else "#"


def build_alert_message(
    config: dict[str, Any], alerts: list[dict[str, Any]]
) -> MIMEMultipart:
    """Build a multipart alert without sending it, enabling direct tests."""
    email_cfg = config["email"]
    missing = [key for key in ("sender_email", "recipients") if key not in email_cfg]
    if missing:
        raise ValueError(f"Missing email config field(s): {', '.join(missing)}")
    sender = email_cfg["sender_email"]
    recipients = email_cfg["recipients"]
    if not _nonempty_string(sender) or "\n" in sender or "\r" in sender:
        raise ValueError("sender_email must be a safe, non-empty string")
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("recipients must be a non-empty list")
    if any(not _nonempty_string(item) or "\n" in item or "\r" in item for item in recipients):
        raise ValueError("recipients contains an unsafe or empty address")

    rows_html = []
    plain_rows = []
    for item in alerts:
        name = html.escape(str(item["name"]))
        safe_url = _safe_amazon_link(item["url"])
        price = float(item["price"])
        threshold = float(item["threshold"])
        rows_html.append(
            "<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'>{name}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#08783e'>"
            f"<strong>${price:.2f}</strong></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>${threshold:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'>"
            f"<a href='{safe_url}'>View product</a></td></tr>"
        )
        plain_rows.append(f"- {item['name']}: ${price:.2f} (target ${threshold:.2f})")

    body = (
        "<html><body><h2 style='color:#d86f00'>Price Alert</h2>"
        "<p>The following monitored products are below their target prices:</p>"
        "<table style='border-collapse:collapse;width:100%'>"
        "<tr style='background:#26364a;color:white'>"
        "<th style='padding:8px;text-align:left'>Product</th>"
        "<th style='padding:8px;text-align:left'>Current Price</th>"
        "<th style='padding:8px;text-align:left'>Target</th>"
        "<th style='padding:8px;text-align:left'>Link</th></tr>"
        + "".join(rows_html)
        + "</table><p style='color:#777;font-size:12px'>Sent by Price Tracker</p>"
        "</body></html>"
    )
    plain = "Price alert\n\n" + "\n".join(plain_rows)

    message = MIMEMultipart("alternative")
    message["Subject"] = f"Price Alert: {len(alerts)} product(s) below target"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(plain, "plain", "utf-8"))
    message.attach(MIMEText(body, "html", "utf-8"))
    return message


def send_alert_email(config: dict[str, Any], alerts: list[dict[str, Any]]) -> bool:
    """Send an alert through verified TLS and report delivery success."""
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not smtp_password:
        logger.warning("SMTP_PASSWORD is not set; email alert skipped")
        return False

    try:
        email_cfg = config["email"]
        for key in ("smtp_server", "smtp_port"):
            if key not in email_cfg:
                raise ValueError(f"Missing email config field: '{key}'")
        smtp_port = int(email_cfg["smtp_port"])
        message = build_alert_message(config, alerts)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Invalid email configuration: %s", exc)
        return False

    try:
        with smtplib.SMTP(
            email_cfg["smtp_server"],
            smtp_port,
            timeout=30,
        ) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(email_cfg["sender_email"], smtp_password)
            server.sendmail(
                email_cfg["sender_email"],
                email_cfg["recipients"],
                message.as_string(),
            )
        logger.info("Alert email sent to: %s", ", ".join(email_cfg["recipients"]))
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed; verify the provider app password")
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        logger.error("Failed to send email: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------
def run_check(
    config: dict[str, Any],
    *,
    demo: bool = True,
    seed: int = 42,
    send_email: bool = True,
    checked_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Check the watchlist once, persist records, and return a run summary."""
    now = checked_at or dt.datetime.now(dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    timestamp = now.astimezone(dt.UTC).isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []

    logger.info(
        "Starting %s price check for %s products",
        "demo" if demo else "live",
        len(config["products"]),
    )

    for index, product in enumerate(config["products"]):
        logger.info("Checking: %s", product["name"])
        result = (
            fetch_price_demo(product, index=index, seed=seed)
            if demo
            else fetch_price(product["url"])
        )
        threshold = float(product["threshold"])
        price = result["price"]
        alert_triggered = False

        if result["error"]:
            logger.error("  Error: %s", result["error"])
        elif price is not None:
            logger.info("  Price: $%.2f (target: $%.2f)", price, threshold)
            if price < threshold:
                alert_triggered = True
                logger.info("  ALERT: Price is below target")
                alerts.append({**result, "threshold": threshold})

        records.append({
            "timestamp": timestamp,
            "name": result["name"],
            "price": price,
            "threshold": threshold,
            "alert_triggered": alert_triggered,
            "url": product["url"],
            "error": result.get("error"),
        })

    log_to_csv(config["output_csv"], records)

    email_sent = False
    if alerts and demo:
        logger.info(
            "Demo mode: %s alert(s) previewed; SMTP is always suppressed",
            len(alerts),
        )
    elif alerts and send_email:
        email_sent = send_alert_email(config, alerts)
    else:
        logger.info("No email alert required")

    summary = {
        "checked_at": timestamp,
        "mode": "demo" if demo else "legacy-live-scrape",
        "products_checked": len(records),
        "alerts_triggered": len(alerts),
        "email_sent": email_sent,
        "records": records,
    }
    logger.info(
        "Price check complete: %s checked, %s below target",
        len(records),
        len(alerts),
    )
    return summary


def write_json_summary(destination: str | Path, summary: dict[str, Any]) -> None:
    """Write a structured run artifact, creating parent folders as needed."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
    logger.info("Wrote run summary to %s", path)


def _resolve_output_path(value: str, config_path: Path) -> str:
    path = Path(value)
    return str(path if path.is_absolute() else config_path.parent / path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Demo-first product price tracker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Explicit offline demo (the default)")
    mode.add_argument(
        "--live-scrape",
        action="store_true",
        help="Enable the legacy Amazon.com HTML adapter; Creators API is recommended",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run continuously on the interval defined in config.json",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_FILE),
        help="Path to config JSON file (default: config.json)",
    )
    parser.add_argument("--output-csv", help="Override the configured CSV destination")
    parser.add_argument("--json-out", help="Write the latest structured run summary")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic demo scenario seed")
    args = parser.parse_args(argv)

    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    configured_output = args.output_csv or config["output_csv"]
    config["output_csv"] = _resolve_output_path(configured_output, config_path)
    configure_logging(config_path.parent / "tracker.log")

    demo = not args.live_scrape
    if args.live_scrape:
        logger.warning(
            "Legacy live scraping explicitly enabled. "
            "Amazon's supported integration is the Creators API."
        )

    def execute_once() -> dict[str, Any]:
        summary = run_check(config, demo=demo, seed=args.seed)
        if args.json_out:
            write_json_summary(args.json_out, summary)
        return summary

    if args.schedule:
        interval_hours = float(config.get("check_interval_hours", 24))
        logger.info("Scheduler mode: checking every %s hour(s)", interval_hours)
        try:
            while True:
                execute_once()
                next_check = dt.datetime.now().astimezone() + dt.timedelta(hours=interval_hours)
                logger.info("Next check at %s", next_check.isoformat(timespec="seconds"))
                time.sleep(interval_hours * 3600)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user")
    else:
        execute_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
