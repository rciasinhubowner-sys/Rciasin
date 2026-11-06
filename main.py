# -*- coding: utf-8 -*-
"""
Telegram Group Moderation Bot
-----------------------------
- গালাগালি (বাংলা / English / Banglish, *** দিয়ে লুকানো সহ) অটো ডিলিট
- Buy/Sell/Inbox টাইপ স্প্যাম অটো ডিলিট
- "api" লিখলে Admin দের মেনশন
- /add  /del  /rc  (সেভ করা মেসেজ/ফাইল ব্লক লিস্ট)
- /mute  /unmute  /mute all  /unmute all

Bot কে গ্রুপে Admin করতে হবে (Delete messages + Restrict members পারমিশন সহ)।
টোকেন কোডে লিখবেন না: .env ফাইলে  BOT_TOKEN=xxxx  অথবা environment variable দিন।
"""

import json
import logging
import os
import re
import time
import unicodedata
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN") or "8895394574:AAHidk1gLFp4y8Q089rlxKDwZoJvxoMswqE"  # পরে এখান থেকে সরিয়ে .env ব্যবহার করুন

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("modbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ----------------------------------------------------------------------------
# টেক্সট নরমালাইজেশন
# ----------------------------------------------------------------------------
_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_SEP = re.compile(r"[\s.,!?;:\"'()\[\]{}<>/\\|~^`=+\-_…।·]+")
_MASKCH = "*#%@$•"
_SPLIT_MASK = re.compile(r"[*#%@$•]+|[^*#%@$•]+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_ZW)
    return s.casefold()


def sq(s: str) -> str:
    """একই ইংরেজি অক্ষর পরপর থাকলে একটা রাখে (fuuuck -> fuck)।"""
    return re.sub(r"([a-z])\1+", r"\1", s)


def sq3(s: str) -> str:
    """৩+ বার পরপর থাকলে একটা রাখে (শুধু ছোট শব্দের জন্য, ball ঠিক থাকে)।"""
    return re.sub(r"([a-z])\1{2,}", r"\1", s)


def key_of(name: str) -> str:
    return re.sub(r"\s+", " ", norm(name)).strip()


# ----------------------------------------------------------------------------
# গালাগালির লিস্ট (এখানে নিজের মতো শব্দ যোগ/বাদ দিতে পারেন)
# SUBSTR: শব্দটা মেসেজের যেকোনো জায়গায় থাকলেই ধরা পড়বে (লম্বা, নিরাপদ শব্দ)
# WORDS : পুরো শব্দ হুবহু মিললে ধরা পড়বে (ছোট শব্দ, যাতে ভুল ধরা না পড়ে)
# ----------------------------------------------------------------------------
SUBSTR_RAW = [
    # English
    "fuck", "motherfuck", "bitch", "asshole", "bastard", "dickhead", "pussy",
    "cunt", "whore", "slut", "nigger", "nigga", "faggot", "shitty", "bullshit",
    # Banglish
    "madarchod", "madarchud", "maderchod", "bokachoda", "bokachuda", "khanki",
    "khankir", "haramjada", "haramzada", "kuttarbachcha", "shuorerbachcha",
    "chudbo", "chudir", "chodbo",
    # বাংলা
    "মাদারচোদ", "মাদারচুদ", "বোকাচোদা", "বোকাচুদা", "চুদ", "খানকি", "খানকী",
    "বেশ্যা", "হারামি", "হারামজাদা", "হারামজাদি", "শুয়োর", "শুয়ার",
    "কুত্তার বাচ্চা", "কুত্তির বাচ্চা", "গান্ডু", "গাণ্ডু", "ঢেমনা", "ভোদা",
    "পুটকি", "বাইনচোদ", "চোদনা", "চোদাচুদি",
]
WORDS_RAW = [
    # English
    "shit", "dick", "cock", "fag", "fags", "stfu", "fck", "fuk", "fuq", "slut",
    # Banglish
    "chod", "chud", "chuda", "chodna", "magi", "magir", "gandu", "harami",
    "bal", "baler", "bessha", "bessa", "tor maa", "toor maa",
    # বাংলা  ("চোদ্দ" = ১৪, তাই "চোদ" শুধু পুরো শব্দ হিসেবে ধরা হয়)
    "চোদ", "চোদা", "চোদে", "চোদি", "চোদন", "বাল", "বালের", "বালে", "বালটা",
    "মাগি", "মাগী", "মাগির", "মাগীর",
]
SUBSTR = [x for x in {sq(norm(w)) for w in SUBSTR_RAW}]
WORDS = {norm(w) for w in WORDS_RAW}
ALL_PLAIN = {norm(w) for w in SUBSTR_RAW + WORDS_RAW}  # *** মাস্কিং চেকের জন্য


def _mask_regex(tok: str) -> re.Pattern:
    parts = []
    for m in _SPLIT_MASK.finditer(tok):
        s = m.group()
        if s[0] in _MASKCH:
            n = len(s)
            parts.append(".{%d,%d}" % (n, n + 2))
        else:
            parts.append(re.escape(s))
    return re.compile("".join(parts))


def has_profanity(text: str) -> bool:
    t = norm(text)
    if not t.strip():
        return False

    # 1) f***, মা***চোদ, $hit, f*ck টাইপ মাস্ক করা শব্দ
    for tok in (x for x in _SEP.split(t) if x):
        if any(c in _MASKCH for c in tok) and any(c not in _MASKCH for c in tok):
            pat = _mask_regex(tok)
            if any(pat.fullmatch(w) for w in ALL_PLAIN):
                return True

    # 2) সাধারণ চেক (leet: sh1t, f*ck নয়, @ss, ইত্যাদি)
    leet = t.translate(_LEET)
    raw_tokens = [x for x in _SEP.split(leet) if x]
    tokens = [sq3(x) for x in raw_tokens]
    if any(tok in WORDS for tok in tokens):
        return True
    full = sq(leet)
    if any(s in full for s in SUBSTR):
        return True

    # 3) আলাদা আলাদা অক্ষর: "f u c k", "f.u.c.k"
    run = []
    for tok in raw_tokens + [""]:
        if len(tok) == 1:
            run.append(tok)
            continue
        if len(run) >= 3:
            joined = sq3("".join(run))
            if joined in WORDS or any(s in joined for s in SUBSTR):
                return True
        run = []

    # 4) স্পেস/চিহ্ন বাদ দিয়ে জোড়া লাগালে (শুধু লম্বা শব্দ)
    squashed = sq(re.sub(r"[^a-z0-9\u0980-\u09ff]", "", leet))
    if any(len(s) >= 6 and s in squashed for s in SUBSTR):
        return True
    return False


# ----------------------------------------------------------------------------
# Buy / Sell / Inbox স্প্যাম
# ----------------------------------------------------------------------------
_SPAM_PATTERNS = [
    # English / Banglish
    r"in\s?box", r"inbx", r"\binb\b", r"\bib\b", r"\bdm\b", r"\bpm me\b",
    r"\bbuy(ing|er|ers)?\b", r"\bsell(ing|er|ers)?\b", r"\bfor sale\b",
    r"whats\s?app", r"\bwa\.me\b", r"contact me", r"\b(msg|message|text) me\b",
    r"\bk?nock me\b", r"\bbikri\b", r"\bkinbo\b", r"\bkinte\b", r"\bkinben\b",
    r"\bbeche\b", r"\bbechbo\b",
    # বাংলা
    "ইনবক্স", "ইনবাক্স", "ইনবক্সে", "বিক্রি", "বিক্রয়", "বেচি", "বেচবো",
    "কিনবো", "কিনবেন", "কিনুন", "কিনতে চাই", "যোগাযোগ করুন",
    "নক দিন", "নক করুন", "নক দাও", "হোয়াটসঅ্যাপ", "হোয়াটসএপ",
]
SPAM_RE = re.compile("|".join(f"(?:{norm(p)})" for p in _SPAM_PATTERNS))


def is_spam(text: str) -> bool:
    return bool(SPAM_RE.search(norm(text)))


API_RE = re.compile(r"(?<![a-z0-9])api(?![a-z0-9])")

# ----------------------------------------------------------------------------
# ডাটা সংরক্ষণ (data.json)
# ----------------------------------------------------------------------------
DATA_FILE = Path(__file__).with_name("data.json")


def _load() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


DATA = _load()


def save_data() -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(DATA, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def chat_store(chat_id: int) -> dict:
    st = DATA.setdefault(str(chat_id), {})
    st.setdefault("saved", {})
    return st


# ----------------------------------------------------------------------------
# হেল্পার
# ----------------------------------------------------------------------------
_admin_cache: dict = {}
_api_last: dict = {}


async def get_admins(bot, chat_id: int):
    c = _admin_cache.get(chat_id)
    if c and time.time() - c[0] < 60:
        return c[1]
    admins = await bot.get_chat_administrators(chat_id)
    _admin_cache[chat_id] = (time.time(), admins)
    return admins


async def user_is_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        return any(a.user.id == user_id for a in await get_admins(bot, chat_id))
    except TelegramError as e:
        log.warning("admin list আনা যায়নি: %s", e)
        return False


async def sender_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if msg.sender_chat and msg.sender_chat.id == msg.chat_id:  # anonymous admin
        return True
    if not update.effective_user:
        return False
    return await user_is_admin(context.bot, msg.chat_id, update.effective_user.id)


async def try_delete(bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except TelegramError as e:
        log.warning("ডিলিট করা যায়নি (%s): %s", message_id, e)
        return False


def mention(user) -> str:
    return f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'


def fingerprint(msg):
    """মেসেজের ধরন + শনাক্তকারী (টেক্সট বা file_unique_id)।"""
    if msg.text:
        return "text", key_of(msg.text)
    for kind in ("sticker", "animation", "video_note", "video", "voice", "audio", "document"):
        obj = getattr(msg, kind, None)
        if obj:
            return kind, obj.file_unique_id
    if msg.photo:
        return "photo", msg.photo[-1].file_unique_id
    return None


TYPE_ICON = {
    "text": "💬", "sticker": "🏷", "animation": "🎞", "video": "🎬",
    "video_note": "⭕", "voice": "🎤", "audio": "🎵", "document": "📄", "photo": "🖼",
}


async def send_chunks(msg, lines: list[str], header: str = ""):
    buf = header
    for line in lines:
        if len(buf) + len(line) + 1 > 3500:
            await msg.reply_html(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        await msg.reply_html(buf)


# ----------------------------------------------------------------------------
# মেইন মডারেশন হ্যান্ডলার
# ----------------------------------------------------------------------------
async def handle_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    now = time.time()
    k = (msg.chat_id, user.id)
    if now - _api_last.get(k, 0) < 30:  # স্প্যাম ঠেকাতে ৩০ সেকেন্ড কুলডাউন
        return
    _api_last[k] = now
    try:
        admins = [a.user for a in await get_admins(context.bot, msg.chat_id) if not a.user.is_bot]
    except TelegramError:
        return
    mentions = " ".join(mention(u) for u in admins[:30])
    text = (
        f"{mentions}\n\n"
        f"👤 Name: {mention(user)}\n"
        f"🆔 User ID: <code>{user.id}</code>\n\n"
        f"এই admin, ওর api টা enable করে দাও 🙄"
    )
    await msg.reply_html(text)


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.sender_chat or not msg.from_user or msg.from_user.is_bot:
        return
    chat_id, user = msg.chat_id, msg.from_user
    saved = chat_store(chat_id)["saved"]
    text = msg.text or msg.caption or ""

    # নাম লিখলে সেভ করা মূল মেসেজ ডিলিট
    if msg.text:
        entry = saved.get(key_of(msg.text))
        if entry:
            await try_delete(context.bot, chat_id, entry["message_id"])

    # "api" লিখলে Admin দের মেনশন
    if text and API_RE.search(norm(text)):
        await handle_api(update, context)

    # Admin দের মেসেজ ফিল্টার হয় না
    if await user_is_admin(context.bot, chat_id, user.id):
        return

    # সেভ করা (/add) মেসেজ/ফাইল আবার কেউ দিলে ডিলিট
    fp = fingerprint(msg)
    if fp:
        for e in saved.values():
            if e["type"] == fp[0] and e["key"] == fp[1]:
                await try_delete(context.bot, chat_id, msg.message_id)
                return

    if text and (has_profanity(text) or is_spam(text)):
        await try_delete(context.bot, chat_id, msg.message_id)


# ----------------------------------------------------------------------------
# কমান্ড
# ----------------------------------------------------------------------------
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(update, context):
        return
    name = " ".join(context.args).strip()
    target = msg.reply_to_message
    if not name or not target:
        await msg.reply_text(
            "ব্যবহার: কোনো মেসেজ/ফাইল/ভয়েসে রিপ্লাই করে লিখুন:\n/add নাম"
        )
        return
    fp = fingerprint(target)
    if not fp:
        await msg.reply_text("এই ধরনের মেসেজ সেভ করা যাবে না।")
        return
    store = chat_store(msg.chat_id)
    store["saved"][key_of(name)] = {
        "name": name,
        "type": fp[0],
        "key": fp[1],
        "message_id": target.message_id,
    }
    save_data()
    await msg.reply_html(
        f"✅ সেভ হয়েছে: <code>{escape(name)}</code>\n"
        f"এখন কেউ <code>{escape(name)}</code> লিখলে ঐ মেসেজ ডিলিট হবে, "
        f"আর একই মেসেজ/ফাইল আবার দিলেও মুছে যাবে।"
    )


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(update, context):
        return
    name = " ".join(context.args).strip()
    if not name:
        await msg.reply_text("ব্যবহার: /del নাম")
        return
    saved = chat_store(msg.chat_id)["saved"]
    if saved.pop(key_of(name), None):
        save_data()
        await msg.reply_text("🗑 লিস্ট থেকে সরানো হয়েছে।")
    else:
        await msg.reply_text("এই নামে কিছু পাওয়া যায়নি।")


async def cmd_rc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    saved = chat_store(msg.chat_id)["saved"]
    if not saved:
        await msg.reply_text("কোনো মেসেজ সেভ করা নেই।")
        return
    lines = [
        f"{TYPE_ICON.get(e['type'], '•')} <code>{escape(e['name'])}</code>"
        for e in saved.values()
    ]
    await send_chunks(
        msg, lines, header="📂 <b>সেভ করা নাম</b> (ট্যাপ করলেই কপি হবে)\n\n"
    )


_PERM_FIELDS = [
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes",
    "can_send_polls", "can_send_other_messages", "can_add_web_page_previews",
    "can_change_info", "can_invite_users", "can_pin_messages", "can_manage_topics",
]


def _perms(value: bool, **over) -> ChatPermissions:
    d = {f: value for f in _PERM_FIELDS}
    d.update(over)
    return ChatPermissions(**d)


MUTED = _perms(False)
OPEN = _perms(True, can_change_info=False, can_pin_messages=False, can_manage_topics=False)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _mute(update, context, mute=True)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _mute(update, context, mute=False)


async def _mute(update: Update, context: ContextTypes.DEFAULT_TYPE, mute: bool):
    msg = update.effective_message
    bot, chat_id = context.bot, msg.chat_id
    if not await sender_is_admin(update, context):
        return
    cmd = "mute" if mute else "unmute"

    try:
        # /mute all  |  /unmute all
        if context.args and context.args[0].lower() == "all":
            store = chat_store(chat_id)
            if mute:
                chat = await bot.get_chat(chat_id)
                p = chat.permissions
                if p and p.can_send_messages:  # আগের পারমিশন মনে রাখি
                    store["prev_perms"] = {
                        f: getattr(p, f) for f in _PERM_FIELDS if getattr(p, f, None) is not None
                    }
                    save_data()
                await bot.set_chat_permissions(chat_id, MUTED)
                await msg.reply_text("🔇 সবাইকে mute করা হয়েছে (শুধু Admin লিখতে পারবে)।")
            else:
                prev = store.pop("prev_perms", None)
                save_data()
                perms = ChatPermissions(**prev) if prev else OPEN
                await bot.set_chat_permissions(chat_id, perms)
                await msg.reply_text("🔊 সবার mute তুলে নেওয়া হয়েছে।")
            return

        # /mute  (রিপ্লাই করে)
        target = msg.reply_to_message
        if not target or not target.from_user:
            await msg.reply_text(f"ব্যবহার: কারো মেসেজে রিপ্লাই করে /{cmd}\nসবার জন্য: /{cmd} all")
            return
        tuser = target.from_user
        if await user_is_admin(bot, chat_id, tuser.id):
            await msg.reply_text("Admin কে mute/unmute করা যায় না।")
            return
        await bot.restrict_chat_member(chat_id, tuser.id, permissions=MUTED if mute else OPEN)
        icon = "🔇" if mute else "🔊"
        word = "mute" if mute else "unmute"
        await msg.reply_html(f"{icon} {mention(tuser)} কে {word} করা হয়েছে।")
    except TelegramError as e:
        await msg.reply_text(f"❌ হয়নি: {e}\nBot কে Admin করে 'Restrict members' পারমিশন দিয়েছেন তো?")


# ----------------------------------------------------------------------------
def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN পাওয়া যায়নি। .env ফাইলে BOT_TOKEN=... লিখুন।")

    app = Application.builder().token(TOKEN).build()
    grp = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE

    app.add_handler(CommandHandler("add", cmd_add, filters=grp))
    app.add_handler(CommandHandler("del", cmd_del, filters=grp))
    app.add_handler(CommandHandler("rc", cmd_rc, filters=grp))
    app.add_handler(CommandHandler("mute", cmd_mute, filters=grp))
    app.add_handler(CommandHandler("unmute", cmd_unmute, filters=grp))
    # edit করে ফিল্টার ফাঁকি দেওয়া ঠেকাতে edited message ও ধরা হয়
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            moderate,
        )
    )

    log.info("Bot চালু হয়েছে...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
