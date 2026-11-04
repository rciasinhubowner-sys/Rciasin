#!/usr/bin/env python3
# RC TOOLS Group Manager Bot
# Install: pip install -r requirements.txt
# Set BOT_TOKEN in environment; never publish your token.

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timezone

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Set it as an environment variable.")

DB_PATH = os.getenv("DB_PATH", "rc_tools_bot.sqlite3")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("rc-tools-bot")

# Add/remove terms to tune moderation for your group.
BAD_WORDS = [
    # Bangla (common variants; maintain this list for your community)
    "মাদারচোদ", "মাদারচুৎ", "চোদা", "চুদি", "চুদ", "খানকি", "খানকির",
    "বোকাচোদা", "হারামজাদা", "শুয়োরের বাচ্চা", "কুত্তার বাচ্চা",
    "মাইর", "মাগি", "বাল", "ধোন", "ভোদা", "মাল",
    # English
    "fuck", "fucking", "motherfucker", "mother fucker", "bitch",
    "bastard", "asshole", "dickhead", "shit", "cunt", "whore",
]
SEXUAL_EMOJIS = ["🍆", "🍑", "💦", "🖕"]
LINK_RE = re.compile(r"(?i)(https?://|www\.|t\.me/|telegram\.me/|[a-z0-9-]+\.(com|net|org|info|xyz|top|io|me)\b)")
INVITE_RE = re.compile(r"(?i)(t\.me/\+|joinchat/)")
DM_SELL_PHRASES = [
    "dm me", "inbox me", "check inbox", "message me", "pm me",
    "buy", "sell", "for sale", "buy sell", "contact me",
    "ইনবক্সে আসুন", "ইনবক্স করুন", "ইনবক্সে নক", "ডিএম করুন",
    "মেসেজ দিন", "কিনতে চাইলে", "বিক্রি করব", "কেনাবেচা",
    "ক্রয় করতে", "যোগাযোগ করুন", "নক দেন",
]
RC_NEGATIVE_PHRASES = [
    "rc tools scam", "rc tools fraud", "rc tools fake",
    "rc tools প্রতারক", "rc tools ভুয়া", "rc tools চোর",
]

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, name)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                original_permissions TEXT
            )
        """)

def clean_name(value):
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:60]

def is_group(message):
    return message.chat.type in ("group", "supergroup")

def is_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def is_exempt(message):
    # Telegram channel posts in a linked discussion group commonly have sender_chat.
    if message.sender_chat:
        return True
    return bool(message.from_user and is_admin(message.chat.id, message.from_user.id))

def reply_target(message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    return None

def normalize_for_filter(text):
    text = (text or "").lower()
    # Remove asterisks and separators used to disguise words: f*u*c*k, f-u-c-k.
    text = re.sub(r"[\s*._\-\u200b\u200c\u200d]+", "", text)
    return text

def contains_profanity(text):
    compact = normalize_for_filter(text)
    return any(normalize_for_filter(word) in compact for word in BAD_WORDS if word)

def moderation_reason(message):
    text = message.text or message.caption or ""
    low = text.lower()
    if contains_profanity(text):
        return "অশালীন ভাষা"
    if any(emoji in text for emoji in SEXUAL_EMOJIS):
        return "নিষিদ্ধ ইমোজি"
    if LINK_RE.search(text) or INVITE_RE.search(text):
        return "অনুমতি ছাড়া লিংক"
    if any(p in low for p in DM_SELL_PHRASES):
        return "DM / Buy-Sell প্রচারণা"
    if any(p in low for p in RC_NEGATIVE_PHRASES):
        return "গ্রুপের নির্ধারিত নিষিদ্ধ মন্তব্য"
    return None

def delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
        return True
    except Exception as exc:
        log.warning("Could not delete message %s: %s", message_id, exc)
        return False

def save_text_note(chat_id, name, content):
    with db() as con:
        con.execute("""
            INSERT INTO notes(chat_id,name,kind,content,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id,name) DO UPDATE SET
              kind=excluded.kind, content=excluded.content,
              source_chat_id=NULL, source_message_id=NULL,
              created_at=excluded.created_at
        """, (chat_id, name, "text", content, datetime.now(timezone.utc).isoformat()))

def save_media_note(chat_id, name, source_chat_id, source_message_id):
    with db() as con:
        con.execute("""
            INSERT INTO notes(chat_id,name,kind,source_chat_id,source_message_id,created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(chat_id,name) DO UPDATE SET
              kind=excluded.kind, content=NULL,
              source_chat_id=excluded.source_chat_id,
              source_message_id=excluded.source_message_id,
              created_at=excluded.created_at
        """, (chat_id, name, "media", source_chat_id, source_message_id,
              datetime.now(timezone.utc).isoformat()))

def get_note(chat_id, name):
    with db() as con:
        return con.execute("SELECT * FROM notes WHERE chat_id=? AND name=?",
                           (chat_id, name)).fetchone()

def get_notes(chat_id):
    with db() as con:
        return con.execute("SELECT name,kind FROM notes WHERE chat_id=? ORDER BY name",
                           (chat_id,)).fetchall()

def admin_only(message):
    if not message.from_user or not is_admin(message.chat.id, message.from_user.id):
        bot.reply_to(message, "⛔ এই কমান্ড শুধু গ্রুপ Admin ব্যবহার করতে পারবেন।")
        return False
    return True

@bot.message_handler(commands=["start", "help"])
def help_command(message):
    text = (
        "🤖 <b>RC TOOLS Group Bot</b>\\n\\n"
        "<b>Notes:</b>\\n"
        "<code>/add নাম</code> — কোনো মেসেজের Reply দিয়ে File/Video/Photo save করুন।\\n"
        "<code>/add নাম</code> তারপর পরের লাইনে Text — Text save করুন।\\n"
        "<code>/notes</code> — সব Saved Note দেখুন।\\n"
        "Saved নামটি আলাদা মেসেজে লিখলে Note পাঠানো হবে।\\n\\n"
        "<b>Moderation:</b> গালি, নির্দিষ্ট ইমোজি, লিংক ও DM/Buy-Sell প্রচারণা মুছে দেবে।\\n\\n"
        "<b>Admin:</b>\\n"
        "<code>/mute All</code>, <code>/unmute All</code>\\n"
        "<code>/mute</code> বা <code>/ban</code> — সদস্যের মেসেজে Reply দিয়ে\\n"
        "<code>/mute @username</code>, <code>/ban @username</code>\\n"
        "<code>/unmute</code>, <code>/unban</code> — Reply বা @username দিয়ে"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=["add"])
def add_note(message):
    if not is_group(message):
        return
    if not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "ব্যবহার: <code>/add নাম</code> — Text-এর জন্য পরের লাইনে Text লিখুন অথবা File-এর মেসেজে Reply করুন।")
        return
    arg = parts[1].strip()
    # If command is a reply, save the replied-to message (including media).
    if message.reply_to_message:
        name = clean_name(arg)
        if not name:
            bot.reply_to(message, "একটি Note নাম দিন।")
            return
        save_media_note(message.chat.id, name, message.chat.id,
                        message.reply_to_message.message_id)
        bot.reply_to(message, f"✅ <b>{name}</b> নামে মেসেজ/File save হয়েছে।")
        return
    # Text form: /add name\\ncontent
    lines = arg.split("\n", 1)
    if len(lines) < 2 or not lines[1].strip():
        bot.reply_to(message, "Text save করতে লিখুন:\\n<code>/add নাম</code>\\nআপনার Text")
        return
    name = clean_name(lines[0])
    content = lines[1].strip()
    if not name:
        bot.reply_to(message, "একটি Note নাম দিন।")
        return
    save_text_note(message.chat.id, name, content)
    bot.reply_to(message, f"✅ <b>{name}</b> নামে Text save হয়েছে।")

@bot.message_handler(commands=["notes"])
def notes_command(message):
    if not is_group(message):
        return
    rows = get_notes(message.chat.id)
    if not rows:
        bot.reply_to(message, "এখনো কোনো Note save করা হয়নি।")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        icon = "📝" if row["kind"] == "text" else "📎"
        markup.add(types.InlineKeyboardButton(
            f"{icon} {row['name']}",
            callback_data="note:" + str(abs(hash((message.chat.id, row["name"]))))[:10]
        ))
    # Keep callback lookup stable in this process and include the selected name in a short token map.
    # A new /notes command rebuilds the map.
    bot._rc_note_callbacks = getattr(bot, "_rc_note_callbacks", {})
    for row in rows:
        token = str(abs(hash((message.chat.id, row["name"]))))[:10]
        bot._rc_note_callbacks[token] = (message.chat.id, row["name"])
    bot.send_message(message.chat.id, "📚 <b>Saved Notes</b> — যেকোনো নামের বাটনে চাপ দিন:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("note:"))
def note_callback(call):
    token = call.data.split(":", 1)[1]
    mapping = getattr(bot, "_rc_note_callbacks", {})
    target = mapping.get(token)
    if not target:
        bot.answer_callback_query(call.id, "আবার /notes লিখুন।")
        return
    chat_id, name = target
    note = get_note(chat_id, name)
    if not note:
        bot.answer_callback_query(call.id, "Note পাওয়া যায়নি।")
        return
    bot.answer_callback_query(call.id, f"{name} পাঠানো হচ্ছে…")
    if note["kind"] == "text":
        content = note["content"] or ""
        # Telegram message limit is 4096 characters.
        bot.send_message(chat_id, content[:4096])
    else:
        try:
            bot.copy_message(chat_id, note["source_chat_id"], note["source_message_id"])
        except Exception as exc:
            log.exception("Note media send failed: %s", exc)
            bot.send_message(chat_id, "এই File/মেসেজটি আর পাওয়া যাচ্ছে না। মূল মেসেজটি মুছে ফেলা হয়ে থাকতে পারে।")

def resolve_user(message, arg):
    target = reply_target(message)
    if target:
        return target
    username = (arg or "").strip()
    if username.startswith("@"):
        try:
            chat = bot.get_chat(username)
            if chat.type == "private":
                return chat.id
        except Exception:
            return None
    return None

def set_member_mute(chat_id, user_id, muted):
    permissions = types.ChatPermissions(
        can_send_messages=not muted,
        can_send_audios=not muted,
        can_send_documents=not muted,
        can_send_photos=not muted,
        can_send_videos=not muted,
        can_send_video_notes=not muted,
        can_send_voice_notes=not muted,
        can_send_polls=not muted,
        can_send_other_messages=not muted,
        can_add_web_page_previews=not muted
    )
    bot.restrict_chat_member(chat_id, user_id, permissions=permissions)

@bot.message_handler(commands=["mute", "unmute", "ban", "unban"])
def member_admin_commands(message):
    if not is_group(message) or not admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    command = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "/mute" and arg.lower() == "all":
        try:
            chat = bot.get_chat(message.chat.id)
            old = chat.permissions
            old_data = old.to_dict() if old else {}
            with db() as con:
                con.execute("INSERT OR REPLACE INTO settings(chat_id,original_permissions) VALUES(?,?)",
                            (message.chat.id, json.dumps(old_data)))
            bot.set_chat_permissions(message.chat.id, types.ChatPermissions(
                can_send_messages=False, can_send_audios=False,
                can_send_documents=False, can_send_photos=False,
                can_send_videos=False, can_send_video_notes=False,
                can_send_voice_notes=False, can_send_polls=False,
                can_send_other_messages=False, can_add_web_page_previews=False
            ))
            bot.reply_to(message, "🔇 সাধারণ সদস্যদের জন্য Group Mute করা হয়েছে।")
        except Exception as exc:
            bot.reply_to(message, f"Mute করা যায়নি: {exc}")
        return

    if command == "/unmute" and arg.lower() == "all":
        try:
            with db() as con:
                row = con.execute("SELECT original_permissions FROM settings WHERE chat_id=?",
                                  (message.chat.id,)).fetchone()
            if row and row["original_permissions"]:
                data = json.loads(row["original_permissions"])
                bot.set_chat_permissions(message.chat.id, types.ChatPermissions(**data))
            else:
                bot.set_chat_permissions(message.chat.id, types.ChatPermissions(
                    can_send_messages=True, can_send_audios=True,
                    can_send_documents=True, can_send_photos=True,
                    can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True
                ))
            bot.reply_to(message, "🔊 সাধারণ সদস্যদের জন্য Group Unmute করা হয়েছে।")
        except Exception as exc:
            bot.reply_to(message, f"Unmute করা যায়নি: {exc}")
        return

    user_id = resolve_user(message, arg)
    if not user_id:
        bot.reply_to(message, "ব্যবহার: কমান্ডটি সদস্যের মেসেজে Reply দিয়ে লিখুন, অথবা @username দিন। Username দিয়ে কাজ করতে হলে Telegram-এ সেটি resolve হওয়া প্রয়োজন।")
        return
    if is_admin(message.chat.id, user_id):
        bot.reply_to(message, "Admin-কে এই কমান্ড প্রয়োগ করা যাবে না।")
        return

    try:
        if command == "/mute":
            set_member_mute(message.chat.id, user_id, True)
            bot.reply_to(message, "🔇 সদস্যকে Mute করা হয়েছে।")
        elif command == "/unmute":
            set_member_mute(message.chat.id, user_id, False)
            bot.reply_to(message, "🔊 সদস্যকে Unmute করা হয়েছে।")
        elif command == "/ban":
            bot.ban_chat_member(message.chat.id, user_id)
            bot.reply_to(message, "⛔ সদস্যকে শুধু এই Group থেকে Ban করা হয়েছে।")
        elif command == "/unban":
            bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
            bot.reply_to(message, "✅ সদস্যের Ban তুলে দেওয়া হয়েছে।")
    except Exception as exc:
        bot.reply_to(message, f"কাজটি করা যায়নি: {exc}")

@bot.message_handler(func=lambda m: is_group(m), content_types=[
    "text", "photo", "video", "document", "animation", "audio",
    "voice", "video_note", "sticker"
])
def moderation_and_note_lookup(message):
    if is_exempt(message):
        return

    # Run moderation only on user-generated text/captions.
    reason = moderation_reason(message)
    if reason:
        delete_message(message.chat.id, message.message_id)
        return

    # A saved note name sent as a plain text message returns the saved content/media.
    if message.text and not message.text.startswith("/"):
        name = clean_name(message.text)
        note = get_note(message.chat.id, name)
        if note:
            if note["kind"] == "text":
                bot.send_message(message.chat.id, (note["content"] or "")[:4096])
            else:
                try:
                    bot.copy_message(message.chat.id, note["source_chat_id"], note["source_message_id"])
                except Exception:
                    bot.reply_to(message, "এই Saved File পাওয়া যাচ্ছে না।")
            return

init_db()
log.info("RC TOOLS bot started.")
bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
