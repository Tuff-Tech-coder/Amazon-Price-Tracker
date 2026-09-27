# Amazon Price Tracker

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-52%20passing-brightgreen)
![License](https://img.shields.io/badge/License-MIT-green)

A demo-first monitoring service that evaluates a configurable product watchlist, logs every check to a timestamped CSV, exports a structured run summary, and can send formatted email alerts when an explicitly enabled live source reports a price below target.

Built as a study in **safe, unattended jobs**: deterministic offline evaluation, validated configuration, rotating logs, CSV-injection protection, verified SMTP/TLS, bounded retries, and failure isolation. The default run exercises thresholding, persistence, and alert previewing without HTTP or SMTP traffic.

```bash
pip install -r requirements.txt
python price_tracker.py --demo      # full pipeline, zero network calls
python price_tracker.py --json-out run_summary.json
```

---

## Responsible use

**Offline demo mode is the default and intended evaluation path.** It produces deterministic prices, writes the audit trail, and previews threshold alerts without fetching Amazon pages or sending email—even if SMTP credentials happen to exist in the environment.

For live Amazon catalog data, use Amazon's supported [Creators API](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction) or a licensed data provider. PA-API 5 is deprecated. A deliberately constrained Amazon.com HTML adapter remains behind the explicit `--live-scrape` flag only as an HTTP reliability study; it is not the recommended production source. It refuses non-HTTPS/non-Amazon hosts and redirects, does not disguise itself as a consumer browser, and should only be used where the operator has authorization.

---

## Engineering notes

**Price parsing is the subtle part.** Amazon can render the current offer, a crossed-out list price, and split dollar/fraction spans on the same page. The parser prioritizes the active `priceToPay` region, excludes list-price markup, and combines `a-price-whole` with `a-price-fraction` when no complete screen-reader value is available.

Reading the wrong one first silently truncates every price to whole dollars. Two consequences, and the second is the dangerous one:

1. Every row in the history CSV is wrong by up to 99 cents, so any trend analysis built on it is quietly skewed.
2. The truncation can cross a threshold. A $300.40 item parses as `300.0` and fires an alert against a $300.99 threshold it never actually met.

`parse_price()` is isolated and unit-tested against each layout, including regressions for split cents and a crossed-out list price appearing before the active offer.

**Safe source boundary.** The default source is a deterministic local scenario. The optional legacy HTTP adapter accepts only credential-free HTTPS Amazon.com URLs, refuses redirects, and retries only transient failures. This prevents a watchlist entry from becoming an arbitrary network request.

**Retry with backoff.** The legacy adapter uses three attempts with bounded exponential delay, while distinguishing HTTP errors, connection errors, and timeouts so logs say what actually failed.

**Credentials never touch the repo.** `SMTP_PASSWORD` is read from the process environment; `.env` files are not loaded automatically. If the password is absent, the job logs a warning and completes. Demo mode always suppresses SMTP. Live delivery uses a verified TLS context and a connection timeout.

**Append-only audit trail.** Every check appends a UTC-timestamped row—price, target, alert status, URL, and error—and neutralizes spreadsheet-formula prefixes in remote text before it reaches CSV.

**Reproducible evaluation.** `--seed` controls the deterministic demo scenario. `--json-out` emits the same run as structured data for dashboards, CI artifacts, or portfolio demonstrations.

---

## Usage

```bash
python price_tracker.py                         # deterministic offline demo
python price_tracker.py --demo                  # same, made explicit
python price_tracker.py --seed 7                # alternate reproducible scenario
python price_tracker.py --json-out summary.json # CSV + structured summary
python price_tracker.py --schedule              # repeat the offline demo
python price_tracker.py --live-scrape            # explicit legacy adapter opt-in
```

The configured `output_csv` is resolved relative to the selected config file, not the caller's current directory. Use `--output-csv` to override it.

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

Set the SMTP password in the process environment only when live email delivery is desired. The program intentionally does not parse `.env` files:

```bash
# PowerShell
$env:SMTP_PASSWORD="your-app-password"

# bash/zsh
export SMTP_PASSWORD="your-app-password"
```

## Development

```bash
pip install -r requirements.txt
pip install pytest ruff
pytest -q      # 52 tests
ruff check .
```

## Tech stack

`Python` · `requests` · `BeautifulSoup` · `lxml` · `smtplib` (verified SMTP/TLS) · `csv` · `argparse` · rotating logs

## License

MIT — see [LICENSE](LICENSE).
