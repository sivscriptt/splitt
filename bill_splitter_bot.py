import os
import io
import time
import logging
import re
import threading
import requests
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DATALAB_API_KEY = os.environ["DATALAB_API_KEY"]
GST_RATE = 0.08
SC_RATE = 0.10

# --- In-memory session store ---
sessions = {}


# ─────────────────────────────────────────────
# DATALAB OCR
# ─────────────────────────────────────────────
def ocr_with_datalab(image_bytes: bytes) -> str:
    # Ensure clean JPEG
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    jpeg_bytes = buf.getvalue()

    headers = {"X-Api-Key": DATALAB_API_KEY}
    resp = requests.post(
        "https://api.datalab.to/api/v1/ocr",
        headers=headers,
        files={"file": ("receipt.jpg", jpeg_bytes, "image/jpeg")},
        data={"langs": "en"},
    )
    resp.raise_for_status()
    data = resp.json()

    check_url = data.get("request_check_url")
    if check_url:
        for _ in range(30):
            time.sleep(2)
            poll = requests.get(check_url, headers=headers)
            poll.raise_for_status()
            data = poll.json()
            if data.get("status") == "complete":
                break

    blocks = []
    for page in data.get("pages", []):
        for tl in page.get("text_lines", []):
            t = tl.get("text", "").strip()
            poly = tl.get("polygon") or []
            if not t or not poly:
                continue
            ys = [p[1] for p in poly]
            xs = [p[0] for p in poly]
            blocks.append({
                "text": t,
                "y": (min(ys) + max(ys)) / 2,
                "x": min(xs),
                "h": max(ys) - min(ys),
            })

    if not blocks:
        raise ValueError("Datalab returned no text")

    # Group blocks into rows by Y proximity, then sort each row by X
    blocks.sort(key=lambda b: b["y"])
    avg_h = sum(b["h"] for b in blocks) / len(blocks)
    row_threshold = max(avg_h * 0.6, 5)

    rows = []
    current = [blocks[0]]
    for b in blocks[1:]:
        if b["y"] - current[-1]["y"] <= row_threshold:
            current.append(b)
        else:
            current.sort(key=lambda x: x["x"])
            rows.append("  ".join(x["text"] for x in current))
            current = [b]
    current.sort(key=lambda x: x["x"])
    rows.append("  ".join(x["text"] for x in current))

    text = "\n".join(rows)
    logger.info(f"OCR result ({len(rows)} rows):\n{text}")
    return text


# ─────────────────────────────────────────────
# REGEX RECEIPT PARSER (no AI quota needed)
# ─────────────────────────────────────────────
# Pizano-style: "Name X pcs [T] price"
ITEM_LINE = re.compile(
    r"^(.+?)\s+(\d+)\s*pcs?\b\s*(T\b)?\s*([\d,]+\.\d{2})\s*$", re.IGNORECASE
)
NUM_PAT = re.compile(r"\d+(?:[.,]\d{1,3})?")
NUMBERS_ONLY = re.compile(r"^[\d,.\s/]+$")


def parse_num(s: str) -> float:
    s = s.strip().replace(" ", "")
    if "." in s and "," in s:
        return float(s.replace(",", ""))
    if "," in s and "." not in s:
        parts = s.split(",")
        if len(parts[-1]) == 2:
            return float(s.replace(",", "."))
        return float(s.replace(",", ""))
    return float(s)


def line_price(line: str) -> float:
    m = re.search(r"([\d,]+[.,]\d{1,3})\s*$", line)
    return parse_num(m.group(1)) if m else 0.0


def is_total_line(lower: str) -> bool:
    return any(
        kw in lower
        for kw in (
            "sub total", "subtotal", "service charge", "gst", "vat",
            "tax amount", "tax (", "net total", "net[", "net $", "net :",
            "discount", "disc (", "disc:", "tendered", "balance",
            "grand total", "amount due",
        )
    ) or lower.startswith("total")


def is_metadata_line(lower: str) -> bool:
    return any(kw in lower for kw in (
        "invoice", "cashier", "table", "floor", "dine", "tin ", "tin:",
        "till", "date ", "date:", "time ", "time:", "contact",
        "fill no", "order taken", "thank", "scan", "powered",
        "come again", "unsettled", "kot ", "bot ", "bill :", "bill:",
        "bill no", "signature", "reprint", "tendered", "balance amount",
    )) or re.match(r"^(item|qty|rate|amt|amount|dish|description)\b", lower)


def find_numbers(line: str):
    """Return list of (value, start, end, has_decimal) for each number in line."""
    out = []
    for m in NUM_PAT.finditer(line):
        s = m.group()
        out.append((parse_num(s), m.start(), m.end(), "." in s or "," in s))
    return out


def text_excluding(line: str, nums) -> str:
    """Text with numbers removed, picking the longest contiguous text chunk."""
    if not nums:
        return line.strip()
    chunks = []
    last = 0
    for _, s, e, _ in nums:
        chunks.append(line[last:s])
        last = e
    chunks.append(line[last:])
    chunks = [c.strip(" \t-:|") for c in chunks]
    chunks = [c for c in chunks if len(c) > 1]
    return max(chunks, key=len) if chunks else ""


def is_qty(v) -> bool:
    return v == round(v) and 1 <= v <= 99


def close_ratio(a: float, b: float, tol: float = 0.35) -> bool:
    """Whether two numbers are within tol fractional difference (handles discounts)."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= tol


def try_parse_item(line: str, next_line: str = None):
    """Try to extract one item. Returns (item_dict, lines_consumed) or (None, 0)."""
    # Pizano-style "X pcs [T] price"
    m = ITEM_LINE.match(line)
    if m:
        return {
            "name": m.group(1).strip(),
            "price": parse_num(m.group(4)),
            "qty": int(m.group(2)),
            "taxable": m.group(3) is not None,
        }, 1

    nums = find_numbers(line)
    if not nums:
        return None, 0

    name_here = text_excluding(line, nums)

    # 3 numbers: try (rate, qty, amount) and (qty, rate, amount) layouts.
    # Prefer the integer-formatted (non-decimal) number as qty.
    if len(nums) == 3:
        a, _, _, a_dec = nums[0]
        b, _, _, b_dec = nums[1]
        c, _, _, c_dec = nums[2]

        # rate × qty ≈ amount, qty in middle (e.g. "134.25  2  228.22")
        if is_qty(b) and not b_dec and close_ratio(a * b, c):
            name = name_here
            consumed = 1
            if not name and next_line and not is_total_line(next_line.lower()) \
                    and not is_metadata_line(next_line.lower()) \
                    and not NUMBERS_ONLY.match(next_line):
                name = next_line.strip()
                consumed = 2
            if name and len(name) > 1:
                return {"name": name, "price": c, "qty": int(b), "taxable": False}, consumed

        # qty × rate ≈ amount, qty first (e.g. "2  Burger  50.00  100.00")
        if is_qty(a) and not a_dec and close_ratio(a * b, c):
            name = name_here
            if name and len(name) > 1:
                return {"name": name, "price": c, "qty": int(a), "taxable": False}, 1

    # 2 numbers: "Name qty price" or "qty Name price"
    if len(nums) == 2 and name_here and len(name_here) > 1:
        a, _, _, _ = nums[0]
        b, _, _, _ = nums[1]
        if is_qty(a) and not nums[0][3]:  # first is integer-looking → qty
            return {"name": name_here, "price": b, "qty": int(a), "taxable": False}, 1

    # 2 numbers, both decimal and ~equal: OCR dropped qty=1 ("rate amount")
    if len(nums) == 2:
        a, _, _, a_dec = nums[0]
        b, _, _, b_dec = nums[1]
        if a_dec and b_dec and abs(a - b) < 0.01:
            name = name_here
            consumed = 1
            if not name and next_line and not is_total_line(next_line.lower()) \
                    and not is_metadata_line(next_line.lower()) \
                    and not NUMBERS_ONLY.match(next_line):
                name = next_line.strip()
                consumed = 2
            if name and len(name) > 1:
                return {"name": name, "price": b, "qty": 1, "taxable": False}, consumed

    # 1 number: "Name price"
    if len(nums) == 1 and nums[0][3] and name_here and len(name_here) > 1:
        return {"name": name_here, "price": nums[0][0], "qty": 1, "taxable": False}, 1

    return None, 0


def parse_receipt_text(text: str) -> dict:
    items = []
    subtotal = sc = gst = total = 0.0

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        lower = line.lower()

        # Capture totals
        if "sub" in lower and "total" in lower:
            subtotal = line_price(line) or subtotal
            i += 1; continue
        if "service" in lower and "charge" in lower:
            sc = line_price(line) or sc
            i += 1; continue
        if ("gst" in lower or "tax amount" in lower or "vat" in lower) and "net" not in lower:
            gst = line_price(line) or gst
            i += 1; continue
        if "net total" in lower or ("total" in lower and "sub" not in lower
                                    and "net[" not in lower and "net $" not in lower):
            total = line_price(line) or total
            i += 1; continue

        if is_total_line(lower) or is_metadata_line(lower):
            i += 1; continue

        item, consumed = try_parse_item(line, lines[i + 1] if i + 1 < len(lines) else None)
        if item:
            items.append(item)
            i += consumed
            continue

        i += 1

    if gst > 0 and not any(it.get("taxable") for it in items):
        for it in items:
            it["taxable"] = True

    return {"items": items, "subtotal": subtotal, "gst": gst, "sc": sc, "total": total, "currency": "MVR"}


def parse_receipt_with_gemini(image_bytes: bytes) -> dict:
    receipt_text = ocr_with_datalab(image_bytes)
    return parse_receipt_text(receipt_text)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def is_water_item(name: str) -> bool:
    return name.upper().startswith("WATER:") or "WATER" in name.upper()


def calculate_bill(session: dict) -> str:
    items = session["items"]
    people = session["people"]
    assignments = session["assignments"]
    shared_idxs = session["shared_items"]
    receipt = session.get("receipt", {})

    n = len(people)
    base = [0.0] * n
    taxable = [0.0] * n

    if shared_idxs and n > 0:
        shared_total = sum(items[i]["price"] for i in shared_idxs)
        shared_taxable = sum(items[i]["price"] for i in shared_idxs if items[i].get("taxable"))
        for p in range(n):
            base[p] += shared_total / n
            taxable[p] += shared_taxable / n

    for p_idx, item_idxs in assignments.items():
        p = int(p_idx)
        for i in item_idxs:
            base[p] += items[i]["price"]
            if items[i].get("taxable"):
                taxable[p] += items[i]["price"]

    # Detect rates from receipt totals
    items_sum = sum(it["price"] for it in items)
    taxable_sum = sum(it["price"] for it in items if it.get("taxable"))
    sc_total = receipt.get("sc", 0.0)
    gst_total = receipt.get("gst", 0.0)

    sc_rate = (sc_total / items_sum) if items_sum > 0 and sc_total > 0 else 0.0
    gst_base = taxable_sum + sc_total
    gst_rate = (gst_total / gst_base) if gst_base > 0 and gst_total > 0 else 0.0

    lines = ["🧾 *Bill Breakdown*\n"]
    grand = 0.0
    payer = session["payer_name"]

    for p in range(n):
        person_sc = base[p] * sc_rate
        person_gst = (taxable[p] + person_sc) * gst_rate
        total = base[p] + person_sc + person_gst
        grand += total
        suffix = "(you paid)" if people[p] == payer else "owes you"
        lines.append(f"👤 *{people[p]}* {suffix} — MVR {total:.2f}")

    lines.append(f"\n💰 *Grand Total: MVR {grand:.2f}*")
    note_parts = []
    if sc_rate > 0:
        note_parts.append(f"{sc_rate*100:.0f}% SC")
    if gst_rate > 0:
        note_parts.append(f"{gst_rate*100:.0f}% GST")
    if note_parts:
        lines.append(f"_(applied: {' + '.join(note_parts)})_")
    return "\n".join(lines)


def get_unassigned_items(session: dict) -> list:
    """Items not yet assigned and not shared."""
    assigned = set()
    for idxs in session["assignments"].values():
        assigned.update(idxs)
    shared = set(session["shared_items"])
    return [
        i for i in range(len(session["items"]))
        if i not in assigned and i not in shared
    ]


def item_keyboard(session: dict, person_idx: int) -> InlineKeyboardMarkup:
    items = session["items"]
    assignments = session["assignments"]
    shared = set(session["shared_items"])
    selected = set(assignments.get(str(person_idx), []))

    taken_by_others = set()
    for p_key, idxs in assignments.items():
        if str(p_key) != str(person_idx):
            taken_by_others.update(idxs)

    buttons = []
    for i, item in enumerate(items):
        if i in shared or i in taken_by_others:
            continue
        tick = "✅ " if i in selected else ""
        label = f"{tick}{item['name']} — MVR {item['price']:.2f}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"item:{person_idx}:{i}")])
    buttons.append([InlineKeyboardButton("✔️ Done with this person", callback_data=f"person_done:{person_idx}")])
    return InlineKeyboardMarkup(buttons)


def person_intro_text(session: dict, person_idx: int) -> str:
    name = session["people"][person_idx]
    shared_names = ", ".join(session["items"][i]["name"] for i in session["shared_items"])
    shared_note = f"\n_(Shared: {shared_names} — already split evenly)_" if shared_names else ""
    return f"👤 What did *{name}* take?{shared_note}\nTap items to select, then Done."


# ─────────────────────────────────────────────
# COMMAND: /split
# ─────────────────────────────────────────────
async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    message = update.message

    photo = None
    # Check if photo attached to the /split command
    if message.photo:
        photo = message.photo[-1]
    # Check if replying to a photo
    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1]

    if not photo:
        await message.reply_text("Please attach a photo of the bill with /split, or reply to a bill photo with /split.")
        return

    await message.reply_text("📸 Reading your bill, one sec...")

    try:
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        receipt = parse_receipt_with_gemini(bytes(image_bytes))
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
        await message.reply_text("❌ Couldn't read the bill. Try a clearer photo.")
        return

    if not receipt.get("items"):
        await message.reply_text("❌ No items found in the bill. Try a clearer photo.")
        return

    # Expand multi-quantity items into individual units
    expanded = []
    for it in receipt["items"]:
        qty = max(1, int(it.get("qty", 1)))
        unit_price = it["price"] / qty
        for k in range(qty):
            label_suffix = f" ({k+1}/{qty})" if qty > 1 else ""
            expanded.append({
                "name": it["name"] + label_suffix,
                "price": round(unit_price, 2),
                "qty": 1,
                "taxable": it.get("taxable", False),
            })
    receipt["items"] = expanded

    # Auto-detect water items as shared
    shared_idxs = [i for i, item in enumerate(receipt["items"]) if is_water_item(item["name"])]

    sessions[chat_id] = {
        "items": receipt["items"],
        "receipt": receipt,
        "shared_items": shared_idxs,
        "people": [],
        "assignments": {},
        "payer_name": message.from_user.first_name,
        "step": "collect_names",
        "current_person": 0,
    }

    item_lines = "\n".join(
        f"  {'🌊' if i in shared_idxs else ('🅣' if it.get('taxable') else '•')} {it['name']} — MVR {it['price']:.2f}"
        for i, it in enumerate(receipt["items"])
    )
    legend = []
    if shared_idxs:
        legend.append("🌊 = shared (water, split evenly)")
    if any(it.get("taxable") for it in receipt["items"]):
        legend.append("🅣 = taxable (8% GST applies)")
    legend_text = ("\n" + "\n".join(legend)) if legend else ""

    await message.reply_text(
        f"✅ Got it! Here's what I found:\n\n{item_lines}{legend_text}\n\n"
        f"Now send the names of everyone who ate, separated by commas.\n"
        f"e.g. `Alice, Bob, Charlie, Dave`",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
# MESSAGE HANDLER: text replies during session
# ─────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return

    text = update.message.text.strip()
    step = session.get("step")

    if step == "collect_names":
        names = [n.strip() for n in re.split(r"[,\n]+", text) if n.strip()]
        if len(names) < 2:
            await update.message.reply_text(
                "Send at least 2 names, separated by commas.\ne.g. `Alice, Bob, Charlie`",
                parse_mode="Markdown",
            )
            return
        session["people"] = names
        session["step"] = "assign_items"
        session["current_person"] = 0
        session["assignments"] = {str(i): [] for i in range(len(names))}
        await update.message.reply_text(
            person_intro_text(session, 0),
            reply_markup=item_keyboard(session, 0),
            parse_mode="Markdown",
        )


# ─────────────────────────────────────────────
# CALLBACK: inline button presses
# ─────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return

    data = query.data

    # Toggle item for person
    if data.startswith("item:"):
        _, p_idx, i_idx = data.split(":")
        p_idx, i_idx = int(p_idx), int(i_idx)
        assigned = session["assignments"][str(p_idx)]
        if i_idx in assigned:
            assigned.remove(i_idx)
        else:
            assigned.append(i_idx)
        await query.edit_message_reply_markup(reply_markup=item_keyboard(session, p_idx))

    # Done with this person
    elif data.startswith("person_done:"):
        p_idx = int(data.split(":")[1])
        next_p = p_idx + 1
        total = len(session["people"])

        if next_p < total:
            session["current_person"] = next_p
            await query.edit_message_text(
                person_intro_text(session, next_p),
                reply_markup=item_keyboard(session, next_p),
                parse_mode="Markdown"
            )
        else:
            # All done — calculate
            result = calculate_bill(session)
            await query.edit_message_text(result, parse_mode="Markdown")
            del sessions[chat_id]


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_health_server():
    port = int(os.environ.get("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass

    HTTPServer(("", port), Handler).serve_forever()


async def on_startup(app: Application) -> None:
    # Clear any stale webhook + drop backlog so polling starts clean.
    await app.bot.delete_webhook(drop_pending_updates=True)


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
