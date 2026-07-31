# Amazon Price Tracker

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-15%20passing-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

A scheduled monitoring service that tracks a configurable watchlist of products, logs every check to a timestamped CSV for trend analysis, and sends a formatted HTML email the moment a price drops below target.

Built as a study in **unattended, long-running jobs**: credentials in environment variables, throttled and retried requests, structured logging, and an offline demo mode that exercises the entire pipeline — parse, threshold, log, alert — without a single network call.

```bash
pip install -r requirements.txt
python price_tracker.py --demo      # full pipeline, zero network calls
```

---

## ⚖️ Responsible use

Amazon's Conditions of Use prohibit automated data collection, and `robots.txt` disallows product-page crawling. **This project ships with `--demo` as its intended evaluation path**, which simulates prices locally.

If you want live price data, use a sanctioned source instead — the [Amazon Product Advertising API](https://webservices.amazon.com/paapi5/documentation/) (free with an Associates account) or a licensed pricing-data provider. The scraping path is retained as a reference implementation of resilient HTTP client design; the reporting, alerting and scheduling layers work identically against any price source, and swapping the source means replacing one function (`fetch_price`).

---

## Engineering notes

**Price parsing is the subtle part.** Amazon renders prices through several different elements depending on product type and layout. The selector chain is ordered most-precise-first: `a-offscreen` carries the complete screen-reader price (`$299.97`), while `a-price-whole` holds only the integer part (`299`).

Reading the wrong one first silently truncates every price to whole dollars. Two consequences, and the second is the dangerous one:

1. Every row in the history CSV is wrong by up to 99 cents, so any trend analysis built on it is quietly skewed.
2. The truncation can cross a threshold. A $300.40 item parses as `300.0` and fires an alert against a $300.99 threshold it never actually met.

`parse_price()` is isolated and unit-tested against each layout, with a named regression test asserting `a-offscreen` wins when both elements are present.

**Failure isolation.** Each product is checked independently; an HTTP error, timeout or unparseable page is logged against that row and the run continues. One bad product never aborts a scheduled job.

**Retry with backoff.** Three attempts per product with escalating delay, distinguishing HTTP errors, connection errors and timeouts so logs say what actually failed.

**Credentials never touch the repo.** `SMTP_PASSWORD` is read from the environment; if it is unset the job logs a warning and completes rather than crashing. `config.json` holds only non-secret settings.

**Append-only audit trail.** Every check appends a timestamped row — price, threshold, whether an alert fired, and any error — producing a dataset you can chart in pandas or Excel.

---

## Usage

```bash
python price_tracker.py --demo        # simulated prices, no requests
python price_tracker.py              # single live check
python price_tracker.py --schedule   # loop on config.json interval
```

## Configuration

`config.json`:

```json
{
  "products": [
    { "name": "Anker PowerCore 10000", "url": "https://…", "threshold": 25.00 }
  ],
  "email": {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "sender_email": "you@gmail.com",
    "recipients": ["you@gmail.com"]
  },
  "output_csv": "price_history.csv",
  "check_interval_hours": 24
}
```

Set the SMTP password out of band (Gmail requires an App Password):

```bash
export SMTP_PASSWORD="your-app-password"
```

## Development

```bash
pip install -r requirements.txt
pip install pytest ruff
pytest -q      # 15 tests
ruff check .
```

## Tech stack

`Python` · `requests` · `BeautifulSoup` · `lxml` · `smtplib` (SMTP/TLS) · `csv` · `argparse` · `logging`

## License

MIT — see [LICENSE](LICENSE).
