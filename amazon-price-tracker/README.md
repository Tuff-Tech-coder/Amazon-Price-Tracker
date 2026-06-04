# Amazon Price Tracker

A Python automation script that monitors Amazon product prices daily, logs price history to CSV, and sends email alerts when prices drop below your target thresholds.

## Features

- Scrapes live prices from Amazon product pages using `requests` + `BeautifulSoup`
- Rotates User-Agent headers to reduce detection
- Logs every price check to a timestamped CSV file for trend analysis
- Sends formatted HTML email alerts via SMTP when prices drop below thresholds
- Configurable product list, thresholds, and check interval via `config.json`
- Demo mode (`--demo`) runs without live requests — great for testing
- Scheduler mode (`--schedule`) runs continuously on a defined interval
- All credentials stored in environment variables (never hardcoded)

## Project Structure

```
amazon-price-tracker/
├── price_tracker.py       # Main script
├── config.json            # Product URLs, thresholds, email settings
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── price_history.csv      # Generated: price log (created on first run)
└── tracker.log            # Generated: application log
```

## Setup

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure products

Edit `config.json` to add the Amazon URLs you want to track and your price thresholds:

```json
{
  "products": [
    {
      "name": "Sony WH-1000XM5 Headphones",
      "url": "https://www.amazon.com/dp/B09XS7JWHH",
      "threshold": 299.99
    }
  ]
}
```

### 3. Set up email alerts (optional)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and add your Gmail App Password:

```
SMTP_PASSWORD=your_gmail_app_password_here
```

> **Note:** Use a Gmail [App Password](https://myaccount.google.com/apppasswords), not your regular Gmail password. Enable 2-Step Verification first.

Then load it before running:

```bash
export $(cat .env | xargs)
```

Update the `email` section in `config.json` with your sender address and recipients.

## Usage

```bash
# Check prices once (live)
python price_tracker.py

# Demo mode — simulated prices, no HTTP requests
python price_tracker.py --demo

# Run on the configured schedule (every 24 hours by default)
python price_tracker.py --schedule

# Use a custom config file
python price_tracker.py --config /path/to/my_config.json
```

## Sample Output

**Console / log output:**
```
2024-05-10 09:00:01 [INFO] Starting price check for 5 products...
2024-05-10 09:00:03 [INFO] Checking: Sony WH-1000XM5 Headphones
2024-05-10 09:00:06 [INFO]   Price: $278.99 (threshold: $299.99)
2024-05-10 09:00:06 [INFO]   *** ALERT: Price dropped below threshold! ***
2024-05-10 09:00:11 [INFO] Sending email alert for 1 product(s)...
2024-05-10 09:00:12 [INFO] Alert email sent to: alert_recipient@gmail.com
```

**CSV log (`price_history.csv`):**
```
timestamp,name,price,threshold,alert_triggered,url,error
2024-05-10T09:00:06,Sony WH-1000XM5 Headphones,278.99,299.99,True,https://...,
2024-05-10T09:00:09,Kindle Paperwhite,119.99,99.99,False,https://...,
```

## Notes

- Amazon actively limits automated scraping. For production use, consider adding proxy rotation or using the [Amazon Product Advertising API](https://webservices.amazon.com/paapi5/documentation/).
- The script adds 2–5 second random delays between requests to be polite and reduce blocking.
- Run this on a server or cloud instance (e.g., AWS EC2, DigitalOcean) with `--schedule` for fully automated monitoring.
