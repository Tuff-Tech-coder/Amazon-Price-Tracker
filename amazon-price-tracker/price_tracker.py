"""
Amazon Price Tracker
====================
Monitors Amazon product prices and sends email alerts when prices drop
below user-defined thresholds. Results are logged to a CSV file with
timestamps for trend analysis.

Usage:
    python price_tracker.py                  # Run once immediately
    python price_tracker.py --demo           # Run with simulated prices (no live requests)
    python price_tracker.py --schedule       # Run on the configured interval

Environment variables required for email alerts:
    SMTP_PASSWORD   Your Gmail app password (or SMTP provider password)

Setup:
    1. Copy .env.example to .env and fill in your SMTP password
    2. Edit config.json to add your product URLs and thresholds
    3. Run: python price_tracker.py
"""

import os
import re
import csv
import json
import time
import random
import logging
import smtplib
import argparse
import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging setup — logs to both console and a rotating file
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("tracker.log"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_FILE = Path(__file__).parent / "config.json"
HEADERS_POOL = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Safari/605.1.15"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
        "Referer": "https://www.amazon.com/",
    },
]


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------
# Ordered most-precise first. `a-offscreen` carries the full screen-reader
# price ("$299.97"); `a-price-whole` holds only the integer part ("299"), so
# reading it first silently truncated every price to whole dollars and could
# fire a false alert on a $299.97 item with a $299.99 threshold.
PRICE_SELECTORS = [
    ("span", {"class": "a-offscreen"}),        # Screen-reader price: full value
    ("span", {"id": "priceblock_ourprice"}),   # Older layout
    ("span", {"id": "priceblock_dealprice"}),  # Deal / sale price
    ("span", {"class": "a-price-whole"}),      # Last resort: whole dollars only
]

_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d{1,2})?)")


def parse_price(soup: BeautifulSoup) -> float | None:
    """
    Extract a product price from a parsed Amazon page.

    Tries selectors in order of precision and returns the first value that
    parses to a plausible price. Returns None if nothing usable is found.
    """
    for tag_name, attrs in PRICE_SELECTORS:
        tag = soup.find(tag_name, attrs)
        if not tag:
            continue
        match = _PRICE_RE.search(tag.get_text(strip=True))
        if not match:
            continue
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if value > 0:
            return value
    return None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
def load_config(path: Path = CONFIG_FILE) -> dict:
    """Load and validate the JSON configuration file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        config = json.load(f)
    required_keys = ["products", "email", "output_csv"]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required config key: '{key}'")
    return config


# ---------------------------------------------------------------------------
# Price scraper
# ---------------------------------------------------------------------------
def fetch_price(url: str, retries: int = 3) -> dict:
    """
    Scrape a product's name and price from Amazon.

    Returns a dict with keys: name, price (float or None), url, error.
    Rotates User-Agent headers and adds jitter between retries to reduce
    the chance of being identified as a bot.
    """
    for attempt in range(1, retries + 1):
        try:
            headers = random.choice(HEADERS_POOL)
            # Random delay 2-5 seconds — polite crawling
            time.sleep(random.uniform(2, 5))

            session = requests.Session()
            response = session.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "lxml")

            # --- Product name ---
            name_tag = soup.find("span", {"id": "productTitle"})
            name = name_tag.get_text(strip=True) if name_tag else "Unknown Product"

            price = parse_price(soup)

            return {"name": name, "price": price, "url": url, "error": None}

        except requests.exceptions.HTTPError as e:
            logger.warning(f"HTTP error on attempt {attempt}/{retries} for {url}: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error on attempt {attempt}/{retries}: {e}")
        except requests.exceptions.Timeout:
            logger.warning(f"Timeout on attempt {attempt}/{retries} for {url}")
        except Exception as e:
            logger.warning(f"Unexpected error on attempt {attempt}/{retries}: {e}")

        if attempt < retries:
            backoff = 5 * attempt
            logger.info(f"Waiting {backoff}s before retry...")
            time.sleep(backoff)

    return {"name": "Unknown", "price": None, "url": url, "error": "All retries failed"}


def fetch_price_demo(product: dict) -> dict:
    """
    Simulate a price fetch for demo/testing without making live requests.
    Generates a realistic price slightly above or below the threshold.
    """
    base = product["threshold"]
    # Randomly simulate either a drop below threshold or a price just above
    simulated_price = round(random.uniform(base * 0.80, base * 1.15), 2)
    return {
        "name": product["name"],
        "price": simulated_price,
        "url": product["url"],
        "error": None,
    }


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------
def log_to_csv(csv_path: str, records: list[dict]) -> None:
    """
    Append price check results to a CSV file.
    Creates the file with a header row if it does not exist.
    """
    fieldnames = ["timestamp", "name", "price", "threshold", "alert_triggered", "url", "error"]
    path = Path(csv_path)
    write_header = not path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(records)

    logger.info(f"Logged {len(records)} records to {csv_path}")


# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------
def send_alert_email(config: dict, alerts: list[dict]) -> None:
    """
    Send an HTML email listing all products whose prices dropped below threshold.
    Reads the SMTP password from the SMTP_PASSWORD environment variable.
    """
    smtp_password = os.environ.get("SMTP_PASSWORD")
    if not smtp_password:
        logger.warning(
            "SMTP_PASSWORD environment variable not set — skipping email alert. "
            "Set it to enable email notifications."
        )
        return

    email_cfg = config["email"]
    subject = f"Price Alert: {len(alerts)} product(s) dropped below threshold!"

    # Build HTML body
    rows_html = ""
    for item in alerts:
        rows_html += (
            f"<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'>{item['name']}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:green'>"
            f"<strong>${item['price']:.2f}</strong></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>${item['threshold']:.2f}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'>"
            f"<a href='{item['url']}'>View on Amazon</a></td>"
            f"</tr>"
        )

    body = f"""
    <html><body>
    <h2 style="color:#e47911;">Amazon Price Alert</h2>
    <p>The following products have dropped below your target prices:</p>
    <table style="border-collapse:collapse;width:100%">
      <tr style="background:#e47911;color:white">
        <th style="padding:8px;text-align:left">Product</th>
        <th style="padding:8px;text-align:left">Current Price</th>
        <th style="padding:8px;text-align:left">Your Threshold</th>
        <th style="padding:8px;text-align:left">Link</th>
      </tr>
      {rows_html}
    </table>
    <p style="color:#888;font-size:12px">Sent by Amazon Price Tracker</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender_email"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(email_cfg["sender_email"], smtp_password)
            server.sendmail(
                email_cfg["sender_email"],
                email_cfg["recipients"],
                msg.as_string(),
            )
        logger.info(f"Alert email sent to: {', '.join(email_cfg['recipients'])}")
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. For Gmail, use an App Password "
            "(myaccount.google.com → Security → App passwords)."
        )
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------
def run_check(config: dict, demo: bool = False) -> None:
    """
    Check all configured products once and log results.
    Triggers email alerts for any products below their threshold.
    """
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    records = []
    alerts = []

    logger.info(f"Starting price check for {len(config['products'])} products...")

    for product in config["products"]:
        logger.info(f"Checking: {product['name']} — {product['url']}")

        result = fetch_price_demo(product) if demo else fetch_price(product["url"])
        threshold = product["threshold"]
        price = result["price"]
        alert_triggered = False

        if result["error"]:
            logger.error(f"  Error: {result['error']}")
        elif price is None:
            logger.warning(f"  Could not parse price from page")
        else:
            logger.info(f"  Price: ${price:.2f} (threshold: ${threshold:.2f})")
            if price < threshold:
                alert_triggered = True
                logger.info(f"  *** ALERT: Price dropped below threshold! ***")
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

    if alerts:
        logger.info(f"Sending email alert for {len(alerts)} product(s)...")
        send_alert_email(config, alerts)
    else:
        logger.info("No prices dropped below thresholds — no alert sent.")

    logger.info("Price check complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Amazon Price Tracker")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with simulated prices (no live HTTP requests)",
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
    args = parser.parse_args()

    config = load_config(Path(args.config))

    if args.schedule:
        interval_hours = config.get("check_interval_hours", 24)
        logger.info(f"Scheduler mode: checking every {interval_hours} hour(s).")
        while True:
            run_check(config, demo=args.demo)
            next_check = datetime.datetime.now() + datetime.timedelta(hours=interval_hours)
            logger.info(f"Next check at {next_check.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(interval_hours * 3600)
    else:
        run_check(config, demo=args.demo)


if __name__ == "__main__":
    main()
