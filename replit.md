# Amazon Affiliate Telegram Bot

بوت تيليجرام يستقبل روابط منتجات أمازون أو صور منتجات، ويرد بأقل سعر متاح مع رابط أفلييت.

A Telegram bot that accepts Amazon product links or product photos and replies with the lowest available price plus an affiliate link.

## Run & Operate

- **Telegram Bot** workflow — `cd bot && python3 bot.py` (console output)
- Bot starts in mock mode by default; set `MOCK_MODE=false` to use real APIs

## Stack

- Python 3.13
- python-telegram-bot 21.6
- Google Cloud Vision API (optional — for image recognition)
- Amazon Creators API (planned — currently mock)

## Where things live

```
bot/
├── bot.py           — Main bot entry point, Telegram handlers
├── config.py        — All config values (reads from env vars)
├── amazon_utils.py  — ASIN extraction, affiliate link builder, price lookup
├── vision_utils.py  — Google Vision API integration, Amazon keyword search
└── requirements.txt — Python dependencies
```

## Architecture decisions

- **Mock mode on by default**: `MOCK_MODE=true` returns fake prices/results so the bot can be tested before real API keys are ready.
- **Affiliate tag**: defaults to `rashedalhano-21` on `www.amazon.sa`. Override via `AFFILIATE_TAG` and `AMAZON_DOMAIN` env vars.
- **Image recognition**: uses Google Cloud Vision Web Detection + Label Detection for best product name accuracy. Falls back to mock if `GOOGLE_VISION_API_KEY` is not set.

## Product

- Send any Amazon product link → bot replies with lowest price + buy link (with affiliate tag)
- Send a product photo → bot identifies it via Vision API → searches Amazon → reports availability

## User preferences

_Populate as you build._

## Gotchas

- To switch from mock to real Amazon pricing: implement the `get_lowest_offer()` body in `bot/amazon_utils.py` using the Creators API (OffersV2 endpoint).
- To switch from mock to real image search: implement `search_amazon_by_keywords()` in `bot/vision_utils.py` using Creators API SearchItems.
- Always run `cd bot && python3 bot.py` from the workspace root (the workflow does this automatically).

## Required secrets

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram ✅ |
| `GOOGLE_VISION_API_KEY` | Optional — enables real image recognition |

## Optional env vars (non-secret)

| Variable | Default | Description |
|----------|---------|-------------|
| `AFFILIATE_TAG` | `rashedalhano-21` | Your Amazon affiliate tag |
| `AMAZON_DOMAIN` | `www.amazon.sa` | Amazon domain to target |
| `MOCK_MODE` | `true` | Set to `false` for real API calls |
