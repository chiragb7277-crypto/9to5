"""
telegram_bot.py
================
Telegram bot interface for 9to5 Reel Bot ("9 to 5 Office").
Generates 1080x1920 PNG graphics and 7-second silent MP4 reels directly on demand via Telegram.

Usage:
    python telegram_bot.py
"""

import os
import re
import sys
import json
import asyncio
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from bot import (
    BASE_DIR,
    ASSETS_DIR,
    OUTPUT_DIR,
    init_environment,
    validate_item,
    generate_slug,
    append_log,
)
from render import render_reel, create_mp4

# Load environment variables
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = (
    {int(x.strip()) for x in ALLOWED_USER_IDS_RAW.split(",") if x.strip().isdigit()}
    if ALLOWED_USER_IDS_RAW
    else None
)


def is_authorized(user_id: int) -> bool:
    """Checks if the user is authorized to use the bot."""
    if ALLOWED_USER_IDS is None:
        return True
    return user_id in ALLOWED_USER_IDS


import random

WORK_CAPTIONS = [
    "Corporate life 💼\n\n#work #officelife #corporatelife #workhumor #job",
    "Every single day ☕\n\n#work #corporate #worklife #office #job",
    "Too relatable 😭\n\n#officelife #workhumor #corporate #work #job",
    "Office reality check 🚶‍♂️\n\n#work #corporate #worklife #office #job",
    "Corporate survival mode 🥲\n\n#officelife #workhumor #corporate #work #job",
    "Monday to Friday mood ☕\n\n#work #office #worklife #corporate #job",
    "Relatable work moments 💼\n\n#worklife #corporate #office #workhumor #job",
]


def build_smart_caption() -> str:
    """
    Returns a short 2-3 word caption followed by spacing and work hashtags (no 9to5).
    Does NOT repeat the joke copy.
    """
    return random.choice(WORK_CAPTIONS)


def pick_smart_highlight(punchline: str) -> str:
    """
    Selects a key punchy phrase from punchline to highlight in yellow (#F7EB9A).
    Ensures the highlight is ALWAYS an exact case-insensitive substring of punchline.
    """
    p = punchline.strip()

    # 1. Quoted words inside punchline
    quotes_match = re.findall(r'["\']([^"\']{2,30})["\']', p)
    if quotes_match:
        for q in reversed(quotes_match):
            if q in p:
                return q

    # 2. Key numbers or metrics (e.g. "15K", "50K", "100%", "9 to 5")
    metric_match = re.search(r'(\b\d+[\w%+-]*\s+[\w]+(?:\s+[\w]+)?)', p, re.IGNORECASE)
    if metric_match and metric_match.group(1) in p:
        return metric_match.group(1)

    # 3. Check after comma or dash (punchlines usually peak at the end)
    clause_split = re.split(r'[,;\-—]', p)
    last_clause = clause_split[-1].strip() if len(clause_split) > 1 else p
    words = last_clause.split()

    if len(words) >= 4:
        chosen = " ".join(words[-3:]) if len(words) >= 5 else " ".join(words[-2:])
        clean = chosen.rstrip('.!?,:;')
        if clean and clean in p:
            return clean

    # 4. Fallback to last 2-3 words of punchline
    p_words = p.split()
    if len(p_words) <= 3:
        clean = p.rstrip('.!?,:;')
        return clean if clean else p
    else:
        chosen = " ".join(p_words[-2:]).rstrip('.!?,:;')
        if chosen and chosen in p:
            return chosen
        single = p_words[-1].rstrip('.!?,:;')
        return single if single in p else p_words[-1]


def parse_text_to_items(text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Parses incoming message text into a list of joke items.
    Supports:
    1. Raw plain text (Zero formatting needed: user just sends text!)
       - Exactly 2 lines: line 1 = setup, line 2 = punchline (separated by empty space, exact text preserved)
       - 1 line: punchline only (or split on colon if present)
    2. Key-value formatted text (Setup:, Punchline:, Highlight:, Caption:)
    3. Raw JSON object or array of objects
    """
    text = text.strip()
    if not text:
        return [], "Message is empty."

    # 1. Attempt JSON parsing
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return [data], None
            elif isinstance(data, list):
                return data, None
        except Exception:
            pass

    # 2. Check for explicit Key-Value tags (Setup:, Punchline:, Highlight:)
    has_explicit_keys = bool(re.search(r'^(punchline|highlight)\s*:\s*', text, re.IGNORECASE | re.MULTILINE))
    if has_explicit_keys:
        blocks = [b.strip() for b in re.split(r'\n\s*---\s*\n', text) if b.strip()]
        items: List[Dict[str, Any]] = []

        for block in blocks:
            current_item: Dict[str, str] = {}
            current_key = None
            for line in block.splitlines():
                line_str = line.strip()
                if not line_str:
                    continue

                match = re.match(r'^(setup|punchline|highlight|caption)\s*:\s*(.*)$', line_str, re.IGNORECASE)
                if match:
                    current_key = match.group(1).lower()
                    current_item[current_key] = match.group(2).strip()
                elif current_key:
                    current_item[current_key] += " " + line_str

            if current_item:
                if "highlight" not in current_item and "punchline" in current_item:
                    current_item["highlight"] = pick_smart_highlight(current_item["punchline"])
                if "caption" not in current_item:
                    current_item["caption"] = build_smart_caption()
                items.append(current_item)

        if items:
            return items, None

    # 3. Pure Plain Text: Zero formatting!
    # Splits by multiple empty lines or '---' if user sent multiple separate jokes
    raw_blocks = [b.strip() for b in re.split(r'\n\s*(?:---|\n\n+)\s*\n', text) if b.strip()]
    plain_items: List[Dict[str, Any]] = []

    for block in raw_blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        setup = ""
        punchline = ""

        # EXACT 2 LINES: Pick two lines only, add a space between them, as given
        if len(lines) == 2:
            setup = lines[0]
            punchline = lines[1]
        elif len(lines) == 1:
            # Single line: check if there's a colon split e.g. "Setup text: Punchline text"
            single = lines[0]
            if ":" in single:
                parts = single.split(":", 1)
                if len(parts[0].strip()) <= 50 and parts[1].strip():
                    setup = parts[0].strip() + ":"
                    punchline = parts[1].strip()
                else:
                    setup = ""
                    punchline = single
            else:
                setup = ""
                punchline = single
        else:
            # 3+ lines: Line 1 is setup, remaining lines joined as punchline
            setup = lines[0]
            punchline = " ".join(lines[1:])

        # Clean wrapping quotes if the whole punchline is in quotes
        punchline = punchline.strip()
        if (punchline.startswith('"') and punchline.endswith('"')) or (punchline.startswith("'") and punchline.endswith("'")):
            punchline = punchline[1:-1].strip()

        highlight = pick_smart_highlight(punchline)

        # 2-3 word caption with spacing and work hashtags (no 9to5, no copy repeated)
        caption = build_smart_caption()

        plain_items.append({
            "setup": setup,
            "punchline": punchline,
            "highlight": highlight,
            "caption": caption
        })

    if plain_items:
        return plain_items, None

    return [], "Could not process text."


async def send_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends start / help message."""
    help_text = (
        "👋 *Welcome to 9to5 Reel Bot!* 🏢👔\n\n"
        "⚡ *Just send me any plain text joke* — no formatting or JSON needed at all! Just type and send.\n\n"
        "*Example:*\n"
        "`Corporate Friday survival tip: If you can't solve it by noon, it's officially a Monday issue`\n\n"
        "I will automatically:\n"
        "• Style the page header (@9to5_office)\n"
        "• Highlight key punchline words in yellow\n"
        "• Render a 1080×1920 PNG graphic\n"
        "• Generate a 7-second silent MP4 reel\n"
        "• Send ready-to-copy caption with hashtags!\n\n"
        "Commands:\n"
        "/test — Generate a sample demo reel\n"
        "/help — Show this help message"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)


async def handle_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates and returns a sample reel."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    sample = {
        "setup": "Corporate Friday survival tip:",
        "punchline": "If you can't solve it by noon, it's officially a Monday issue",
        "highlight": "Monday issue",
        "caption": "Tag your Friday escape partner 🏃‍♂️ #9to5Office"
    }

    await update.message.reply_text("⏳ Generating sample test reel...")
    await render_and_send_items(update, context, [sample])


async def render_and_send_items(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    items: List[Dict[str, Any]],
):
    """Processes, validates, renders, and sends items back to the user."""
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    output_date_dir = OUTPUT_DIR / today_str
    output_date_dir.mkdir(parents=True, exist_ok=True)

    for i, item in enumerate(items, start=1):
        is_valid, reason = validate_item(item, i)
        if not is_valid:
            await update.message.reply_text(f"⚠️ *Item #{i} Skipped:*\n{reason}", parse_mode=constants.ParseMode.MARKDOWN)
            continue

        slug = generate_slug(item)
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        file_prefix = f"tg-{timestamp}-{slug}"

        png_path = output_date_dir / f"{file_prefix}.png"
        mp4_path = output_date_dir / f"{file_prefix}.mp4"
        txt_path = output_date_dir / f"{file_prefix}.txt"

        caption_text = str(item.get("caption", "")).strip()

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=constants.ChatAction.UPLOAD_PHOTO,
        )

        try:
            # Render PNG and MP4 in background thread to avoid blocking asyncio loop
            await asyncio.to_thread(render_reel, item, str(png_path), str(ASSETS_DIR))
            await asyncio.to_thread(create_mp4, str(png_path), str(mp4_path), 12)

            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(caption_text + ("\n" if caption_text else ""))

            # Log success
            append_log({
                "timestamp": datetime.datetime.now().isoformat(),
                "source": "telegram_bot",
                "user_id": update.effective_user.id,
                "user_name": update.effective_user.username or update.effective_user.first_name,
                "status": "success",
                "slug": file_prefix,
                "data": item
            })

            # Send ONLY the one MP4 Reel video with the caption attached
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=constants.ChatAction.UPLOAD_VIDEO,
            )
            with open(mp4_path, "rb") as video_file:
                await update.message.reply_video(
                    video=video_file,
                    width=1080,
                    height=1920,
                    duration=12,
                    supports_streaming=True,
                    caption=caption_text,
                )

        except Exception as e:
            await update.message.reply_text(f"❌ Error rendering reel #{i}: `{e}`", parse_mode=constants.ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    text = update.message.text
    items, error = parse_text_to_items(text)

    if error:
        await update.message.reply_text(f"❌ {error}")
        return

    await update.message.reply_text(f"🚀 Processing {len(items)} reel(s)...")
    await render_and_send_items(update, context, items)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming JSON document attachments."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".json"):
        await update.message.reply_text("⚠️ Please send a `.json` file containing joke entries.")
        return

    await update.message.reply_text("📥 Downloading and parsing JSON file...")
    file_obj = await doc.get_file()
    file_bytes = await file_obj.download_as_bytearray()

    try:
        data = json.loads(file_bytes.decode("utf-8"))
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            await update.message.reply_text("❌ JSON content must be an object or an array of objects.")
            return
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse JSON file: `{e}`", parse_mode=constants.ParseMode.MARKDOWN)
        return

    await update.message.reply_text(f"🚀 Found {len(items)} item(s). Starting rendering...")
    await render_and_send_items(update, context, items)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        print("❌ TELEGRAM_BOT_TOKEN is not set.")
        print("👉 Add your token to '.env' or export it as an environment variable:")
        print("   TELEGRAM_BOT_TOKEN=\"123456789:ABCdefGHIjklMNOpqrsTUVwxyz\"")
        sys.exit(1)

    init_environment()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", send_help))
    app.add_handler(CommandHandler("help", send_help))
    app.add_handler(CommandHandler("test", handle_test))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("🤖 9to5 Telegram Bot is running! Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
