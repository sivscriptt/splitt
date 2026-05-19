# Maintainer notes

Developer cheat sheet for picking this back up later. Read the [README](README.md) first for the user-facing overview.

## File map

- `bill_splitter_bot.py` — everything (~600 lines, single file on purpose).
- `render.yaml` — Render service spec. Env vars: `TELEGRAM_TOKEN`, `DATALAB_API_KEY`.
- `.python-version` — pins Python to `3.12.7` for Render. Don't drop this — see "Render gotchas".
- `requirements.txt` — `python-telegram-bot`, `Pillow`, `requests`.

## The pipeline (where to look when something breaks)

```
Telegram photo
   └─ split_command()                      bill_splitter_bot.py:398
       └─ ocr_with_datalab()               bill_splitter_bot.py:32
            ├─ POST image to api.datalab.to/api/v1/ocr
            ├─ Poll request_check_url until "complete"
            └─ Group text blocks into rows by Y proximity, sort by X
                (row_threshold = max(avg_h * 0.6, 5))
       └─ parse_receipt_text()             bill_splitter_bot.py:241
            ├─ For each line, classify: totals / metadata / item / skip
            ├─ try_parse_item()            bill_splitter_bot.py:184
            │    handles "Name N pcs price", 3-num (rate qty amt),
            │    2-num qty+price, 2-num implicit qty=1, 1-num name+price
            └─ Returns {items, subtotal, gst, sc, total, currency}
       └─ Expand multi-qty items into individual units (split_command, line ~430)
       └─ Auto-tag water as shared (is_water_item)
   └─ User sends names → handle_message()
   └─ User taps items per person → handle_callback()
   └─ calculate_bill()                     bill_splitter_bot.py:296
       └─ Reconcile GST against captured Net Total (the important bit)
       └─ Apply per-person SC and GST
```

## Parser invariants (don't violate these)

- **`is_total_line` and `is_metadata_line` run before item parsing.** New "must-skip" line types (e.g., a new bank's footer) belong in their keyword tuples at `bill_splitter_bot.py:128` and `:140`.
- **Items are validated via `rate × qty ≈ amount`** (`close_ratio`, default tol 0.35). This is what makes the parser generic across receipt layouts. Tightening tolerance risks dropping discounted items; loosening risks false positives.
- **Item prices are expanded to unit prices** in `split_command` (one inline button per unit). `calculate_bill` then sums those units. If you change one, change both.
- **Tax rates are derived, not assumed.** `sc_rate` and `gst_rate` come from receipt totals at runtime — never hardcoded. The "8% GST" string in the legend is just cosmetic copy in `split_command`.

## Recent fixes (May 2026)

| Commit | Symptom | Root cause | Fix |
|---|---|---|---|
| `f90bc5f` | Render build fails | `requests` not declared; `GEMINI_API_KEY` env var still in `render.yaml` after OCR backend switch to Datalab | Updated `requirements.txt` and `render.yaml` to match what the code actually uses |
| `2f41689` | `RuntimeError: There is no current event loop` on Render start | Render's default Python jumped to 3.14; `python-telegram-bot==21.6` still calls `asyncio.get_event_loop()` the pre-3.14 way | `.python-version` pins to `3.12.7` |
| `6130817` | Bot silent — no logs after `Bot running...` when /split sent | Stale webhook from prior local run was siphoning updates; new instance couldn't reclaim polling | `post_init` calls `delete_webhook(drop_pending_updates=True)`; `run_polling(drop_pending_updates=True)` |
| `efe6183` | 26 phantom `Bill No:JCY/FB/` items at MVR 284 each | `is_metadata_line` only caught `bill:` and `bill :`, not `Bill No:`. The "26" and "7384" in the bill number got parsed as qty + total → unit price 284 | Added `bill no`, `signature`, `reprint`, `tendered`, `balance amount` keywords |
| `b2df1d7` | Single-qty items (Caramel, Sprite) silently dropped | OCR sometimes renders `46.29 1 46.29` as `46.29 46.29` — the qty=1 column is lost | New 2-number branch in `try_parse_item`: if both numbers are decimal and `abs(a - b) < 0.01`, treat as implicit qty=1 and pull name from `next_line` |
| `c532484` | Grand total off by ~MVR 20; "5% GST applied" when receipt clearly says 8% | OCR misread `54.13` tax as `34.130` (single digit slip on a low-contrast line) | In `calculate_bill`, when `subtotal + sc + gst` doesn't reconcile to the captured Net Total (within max(1.0, 2%)), attribute the gap back to GST |

## Render gotchas

- **Python version**: Render auto-bumps to whatever it thinks is "default". `python-telegram-bot==21.6` does **not** work on 3.14+. Keep `.python-version`.
- **Free tier sleeps after 15 min idle**. The `HTTPServer` thread in `run_health_server()` exists only to satisfy Render's port-binding requirement for `type: web` — it doesn't keep the service warm against polling pauses; Render decides based on inbound HTTP traffic.
- **`sync: false` env vars** in `render.yaml` are placeholders; the actual values live in the dashboard Environment tab. Forgetting to set them = `KeyError` at startup (line 20–21).
- **Logs are not accessible via CLI** unless you install `render` and authenticate. Otherwise, dashboard → Logs tab is the only way.

## Debugging cheat sheet

| Symptom | First thing to check |
|---|---|
| Bot doesn't respond | Render Logs after `Bot running...` — if silent, suspect webhook conflict or a second polling instance (e.g., laptop still running). |
| Items missing from output | Grep logs for `OCR result (N rows)` — that block shows exactly what the parser saw. Compare against expected receipt text. |
| Wrong total | Check the reconciliation: is `Net Total` captured correctly? `parse_receipt_text` logs the receipt dict via the OCR block. |
| Random metadata showing up as item | Add the offending substring (lowercased) to `is_metadata_line` or `is_total_line` keywords. |
| Discounted items dropped | `close_ratio` tolerance — currently 0.35. Receipts with > 35% line-item discounts won't validate `rate × qty ≈ amount`. |

## Adding a new receipt format

If a new place's bills consistently fail:

1. Send the bill to the bot, grab the `OCR result (...)` block from Render logs.
2. Walk through `try_parse_item` mentally with each line.
3. Most fixes are one of: new keyword in `is_metadata_line`, a new number-count branch in `try_parse_item`, or a tolerance tweak in `close_ratio`.
4. Resist the urge to add receipt-specific regex — keep the parser generic.

## Security

- Bot token leaks: rotate via `@BotFather` → `/revoke`, get fresh token, update `TELEGRAM_TOKEN` in Render Environment tab. Leaks have happened by pasting logs containing the token.
- Datalab API key: rotate via the Datalab dashboard.
