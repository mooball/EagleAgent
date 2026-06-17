# Currency Conversion

EagleAgent supports currency conversion for RFQ price comparisons, transaction display, and supplier quoting. Rates are sourced from the European Central Bank (ECB) with 24-hour caching.

## How It Works

1. **ECB daily rates** — Free, no API key required. Rates are published daily against EUR.
2. **Cross-rates** — All conversions go through EUR as the common base (e.g., USD→AUD is USD→EUR then EUR→AUD).
3. **24-hour cache** — Rates are fetched once per day. A thread-safe lock prevents concurrent fetches.

## Key Module

`includes/currency.py`

| Function | Purpose |
|---|---|
| `get_rate(from_currency, to_currency)` | Get the current exchange rate between two currencies |
| `convert(amount, from_currency, to_currency)` | Convert an amount between currencies |

## Usage

```python
from includes.currency import convert, get_rate

# Convert 80 USD to AUD
aud_amount = convert(80.0, "USD", "AUD")  # → ~124.0

# Get the current rate
rate = get_rate("USD", "AUD")  # → ~1.55
```

## Supported Currencies

All currencies published by the ECB are supported. Common ones include AUD, USD, EUR, GBP, NZD, CAD, JPY, SGD, and many more. The `suppliers` table stores each supplier's currency, and the dashboard displays converted amounts automatically.

## Caching

- **TTL**: 24 hours (`_CACHE_TTL = 86400`)
- **Storage**: In-memory dict with timestamp
- **Thread safety**: `threading.Lock` prevents concurrent ECB fetches
- **Fallback**: If ECB fetch fails, the last cached rates are used until expiry
