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
SKIP = re.compile(
    r"sub.?total|service.?charge|gst|vat|tax|discount|total|invoice|cashier"
    r"|table|floor|dine|item\s+qty|thank|scan|powered|unsettled|kot|bot\s+\d",
    re.IGNORECASE,
)
# e.g. "Korean BBQ Dirty Fries  1 pcs T  84.00"  or  "Water 1.5ml  2 pcs  28.00"
ITEM_LINE = re.compile(
    r"^(.+?)\s+(\d+)\s*pcs?\b\s*(T\b)?\s*([\d,]+\.\d{2})\s*$", re.IGNORECASE
)
PRICE_END = re.compile(r"^(.+?)\s+([\d,]+\.\d{2})\s*$")


def parse_receipt_text(text: str) -> dict:
    items = []
    subtotal = sc = gst = total = 0.0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        lower = line.lower()
        if SKIP.search(lower):
            price_m = re.search(r"([\d,]+\.\d{2})\s*$", line)
            val = float(price_m.group(1).replace(",", "")) if price_m else 0.0
            if "sub" in lower and "total" in lower:
                subtotal = val
            elif "service" in lower:
                sc = val
            elif "gst" in lower or "vat" in lower:
                gst = val
            elif "total" in lower:
                total = val
            continue

        m = ITEM_LINE.match(line)
        if m:
            name = m.group(1).strip()
            qty = int(m.group(2))
            taxable = m.group(3) is not None
            price = float(m.group(4).replace(",", ""))
            items.append({"name": name, "price": price, "qty": qty, "taxable": taxable})
            continue

        m = PRICE_END.match(line)
        if m:
            name, price = m.group(1).strip(), float(m.group(2).replace(",", ""))
            if len(name) > 2 and not re.match(r"^[\d\s.]+$", name):
                items.append({"name": name, "price": price, "qty": 1, "taxable": False})

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

    n = len(people)
    base = [0.0] * n      # full subtotal per person
    taxable = [0.0] * n   # taxable subtotal per person (T-marked items)

    # Shared items split evenly
    if shared_idxs and n > 0:
        shared_total = sum(items[i]["price"] for i in shared_idxs)
        shared_taxable = sum(items[i]["price"] for i in shared_idxs if items[i].get("taxable"))
        for p in range(n):
            base[p] += shared_total / n
            taxable[p] += shared_taxable / n

    # Assigned items
    for p_idx, item_idxs in assignments.items():
        p = int(p_idx)
        for i in item_idxs:
            base[p] += items[i]["price"]
            if items[i].get("taxable"):
                taxable[p] += items[i]["price"]

    lines = ["🧾 *Bill Breakdown*\n"]
    grand = 0.0
    payer = session["payer_name"]

    for p in range(n):
        sc = base[p] * SC_RATE
        gst = (taxable[p] + sc) * GST_RATE
        total = base[p] + sc + gst
        grand += total
        suffix = "(you paid)" if people[p] == payer else "owes you"
        lines.append(f"👤 *{people[p]}* {suffix} — MVR {total:.2f}")

    lines.append(f"\n💰 *Grand Total: MVR {grand:.2f}*")
    lines.append("_10% SC on all items, 8% GST on T-marked items + SC_")
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
    """Keyboard showing all non-shared items, ticked if selected for this person."""
    items = session["items"]
    selected = set(session["assignments"].get(str(person_idx), []))
    shared = set(session["shared_items"])
    buttons = []
    for i, item in enumerate(items):
        if i in shared:
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

    # Auto-detect water items as shared
    shared_idxs = [i for i, item in enumerate(receipt["items"]) if is_water_item(item["name"])]

    sessions[chat_id] = {
        "items": receipt["items"],
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


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
