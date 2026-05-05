# splitt

Telegram bot that splits restaurant bills from a photo. Snap the receipt, tap who took what, get the breakdown — taxes and service charge handled per person.

## What it does

1. You send a bill photo with `/split` in any Telegram chat
2. OCR pulls the line items off the receipt
3. The parser figures out item names, quantities, and prices (validated by `rate × qty ≈ amount`, so it works across receipt layouts — not hardcoded to one place)
4. You drop the names of everyone who ate, comma separated
5. Each person taps the items they had. Items disappear once claimed
6. Bot returns who owes what, with SC + GST applied per person — rates auto-detected from the receipt

Water is auto-marked as shared and split evenly. Items marked `T` on the bill (taxable) get GST applied; non-T items don't.

## Stack

- `python-telegram-bot` for the chat interface
- [Datalab OCR](https://datalab.to) for reading the receipt
- Custom regex parser — no LLM in the hot path, no rate limits, no flaky parsing

## Run it

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="..."   # from @BotFather
export DATALAB_API_KEY="..."  # from datalab.to
python bill_splitter_bot.py
```

## Deploy

Push to GitHub, connect to Render as a web service. `render.yaml` is already wired up — just paste the env vars in the dashboard. Free tier sleeps after 15 min of no traffic and wakes up on the next message (~30 sec cold start).
