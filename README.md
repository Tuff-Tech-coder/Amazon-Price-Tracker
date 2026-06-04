# Amazon Price Tracker

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![requests](https://img.shields.io/badge/requests-2.x-2C5BB4)
![BeautifulSoup](https://img.shields.io/badge/BeautifulSoup-4.x-43B02A)
![License](https://img.shields.io/badge/License-MIT-green)

A scheduled price-monitoring tool that tracks a configurable list of Amazon products, logs every check to a timestamped CSV for trend analysis, and sends formatted HTML email alerts the moment a price drops below your target.

Built to be **safe to run unattended**: credentials live in environment variables, requests are throttled and retried, and a full demo mode lets you evaluate the tool without making a single live request.

---

## Why it's useful

Manually refreshing product pages to catch a price drop doesn't scale. This script turns that chore into a hands-off pipeline: set your products and target prices once, schedule it, and get an email only when it's time to buy. The CSV history doubles as a dataset for spotting pricing trends over time.

---

## Features

- **Live price scraping** from Amazon product pages via `requests` + `BeautifulSoup`, with a fallback chain of price selectors (handles standard listings, deals, and screen-reader prices).
- **HTML email alerts** over SMTP, styled as a clean table linking straight back to each product.
- **CSV history logging** — every check is appended with a timestamp, price, threshold, and whether an alert fired, ready for analysis in Excel or pandas.
- **Anti-blocking measures** — rotating User-Agent header pool, randomized 2–5s delays, and retry logic with backoff on HTTP/connection/timeout errors.
- **Three run modes** — run once, `--schedule` to loop on a configurable interval, or `--demo` to simulate prices with no network calls.
- **Zero hardcoded secrets** — the SMTP password is read from the `SMTP_PASSWORD` environment variable; everything else lives in `config.json`.

---

## Tech stack

`Python` · `requests` · `BeautifulSoup` · `lxml` · `smtplib` (SMTP/TLS) · `csv` · `argparse` · `logging`

---

## Project structure

```
amazon-price-tracker/
├── price_tracker.py     # Main script
├── config.json          # Product URLs, thresholds, email + interval settings
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template (SMTP password)
├── price_history.csv    # Generated: timestamped price log
└── tracker.log          # Generated: application log
```

---

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then add your Gmail App Password
```

Edit `config.json` to add your products and target prices:

```json
{
  "products": [
    { "name": "Sony WH-1000XM5", "url": "https://www.amazon.com/dp/B09XS7JWHH", "threshold": 299.99 }
  ],
  "email": {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "you@gmail.com",
    "recipients": ["alerts@gmail.com"]
  },
  "check_interval_hours": 24,
  "output_csv": "price_history.csv"
}
```

> **Gmail note:** use an [App Password](https://myaccount.google.com/apppasswords), not your account password.

---

## Usage

```bash
python price_tracker.py            # Run one check now
python price_tracker.py --demo     # Simulated prices, no live requests
python price_tracker.py --schedule # Loop on config.json interval
```

---

## How it works

1. Loads and validates `config.json`.
2. For each product, fetches the page (rotating headers + jittered delays) and parses the price through a prioritized selector list.
3. Appends results to the CSV history file.
4. If any price is below its threshold, builds a single HTML alert email and sends it over SMTP/TLS.
5. In `--schedule` mode, sleeps until the next interval and repeats.

---

## Possible extensions

Swap CSV for SQLite or Postgres, add price-trend charts, support non-Amazon retailers via a pluggable parser, or trigger Slack/Telegram alerts instead of email.
