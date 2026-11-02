import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import ReplyParameters, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rcbot")

# ⚠️ নতুন token নিলে শুধু নিচের লাইনে বদলান
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8349674951:AAE0x7Lvr3M6_TYaDFi6fD-f0gHPkLJXEhg"
WARN_DELETE_AFTER = 2  # সতর্কবার্তা কত সেকেন্ড পর নিজে মুছবে

DB = sqlite3.connect("rcbot.db", check_same_thread=False)
DB.executescript(
    """
CREATE TABLE IF NOT EXISTS items(
  chat_id INTEGER, name TEXT, kind TEXT, file_id TEXT, caption TEXT, text TEXT,
  PRIMARY KEY(chat_id, name));
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, triggers TEXT, reply TEXT);
CREATE TABLE IF NOT EXISTS words(
  chat_id INTEGER, kind TEXT, word TEXT, PRIMARY KEY(chat_id, kind, word));
CREATE TABLE IF NOT EXISTS settings(
  chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key));
"""
)

RESERVED = {"add", "rm", "rc", "rclist", "rcdel", "notes", "list", "addword", "rmword",
            "mediablock", "start", "help"}

DEFAULT_WORDS = {
    "abuse": [
        "fuck", "motherfucker", "bitch", "bastard", "asshole", "shit",
        "মাদারচোদ", "বোকাচোদা", "খানকি", "মাগি", "হারামি", "কুত্তার বাচ্চা", "চুদি", "চোদা",
    ],
    "sexual": [
        "porn", "xxx", "nude", "nudes", "blowjob", "sexy", "18+",
        "পর্ন", "সেক্স", "চোদাচুদি", "নগ্ন", "ন্যাংটা",
    ],
    "competitor": [],
}
KIND_ALIAS = {"abuse": "abuse", "gali": "abuse", "গালি": "abuse",
              "sexual": "sexual", "sex": "sexual", "competitor": "competitor", "comp": "competitor"}

# /rc এর জন্য সমার্থক শব্দ-গ্রুপ (ইচ্ছামতো বাড়াতে পারেন)
SYNONYMS = [
    {"banana", "কলা", "🍌"},
    {"cat", "বিড়াল", "🐱", "🐈"},
    {"dog", "কুকুর", "🐶", "🐕"},
]
STOP = {"emoji", "sticker", "pic", "photo", "delete", "message", "msg", "bot", "admin"}

LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|wa\.me/"
    r"|\b[\w-]+\.(?:com|net|org|me|xyz|io|in|link|ly|gg|cc|top|site|online|app|bd|co)\b"
    r"|@[A-Za-z][\w]{3,})",
    re.I,
)
INVITE_RE = re.compile(r"(inbox|ইনবক্স|\bdm\b|ডিএম|whats\s*app|হোয়াটসঅ্যাপ|(?:\+?88)?01[3-9]\d{8})", re.I)
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

_cache: dict = {}
_tasks: set = set()


# ---------------------------------------------------------------- helpers
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\u200b-\u200f\u2060\ufeff\ufe0f]", "", s)


def word_regex(w: str):
    parts = [f"(?:{re.escape(ch)}|[*#@$])" for ch in norm(w).replace(" ", "")]
    return re.compile(r"[\W_]{0,2}".join(parts)) if parts else None


def get_words(chat_id: int, kind: str):
    rows = DB.execute("SELECT word FROM words WHERE chat_id=? AND kind=?", (chat_id, kind)).fetchall()
    return DEFAULT_WORDS[kind] + [r[0] for r in rows]


def match_words(text: str, words) -> bool:
    t = norm(text)
    for w in words:
        rx = word_regex(w)
        if rx and rx.search(t):
            return True
    return False


def get_setting(chat_id: int, key: str) -> str:
    r = DB.execute("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key)).fetchone()
    return r[0] if r else ""


async def temp_reply(ctx, chat_id: int, text: str, delay: int = WARN_DELETE_AFTER):
    try:
        sent = await ctx.bot.send_message(chat_id, text)
    except TelegramError:
        return

    async def _later():
        await asyncio.sleep(delay)
        try:
            await sent.delete()
        except TelegramError:
            pass

    t = asyncio.create_task(_later())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def is_admin(ctx, chat_id: int, user_id: int) -> bool:
    key = ("a", chat_id, user_id)
    c = _cache.get(key)
    if c and time.time() - c[0] < 60:
        return c[1]
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        ok = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        ok = False
    _cache[key] = (time.time(), ok)
    return ok


async def linked_info(ctx, chat_id: int):
    key = ("l", chat_id)
    c = _cache.get(key)
    if c and time.time() - c[0] < 600:
        return c[1]
    lid = uname = None
    try:
        chat = await ctx.bot.get_chat(chat_id)
        lid = chat.linked_chat_id
        if lid:
            uname = (await ctx.bot.get_chat(lid)).username
    except TelegramError:
        pass
    _cache[key] = (time.time(), (lid, uname))
    return lid, uname


async def sender_is_admin(msg, ctx) -> bool:
    if msg.sender_chat and msg.sender_chat.id == msg.chat_id:  # anonymous admin
        return True
    if msg.from_user:
        return await is_admin(ctx, msg.chat_id, msg.from_user.id)
    return False


def extract_media(m):
    if not m:
        return None
    if m.photo:
        return "photo", m.photo[-1].file_id
    for k in ("video", "document", "audio", "voice", "animation", "sticker", "video_note"):
        obj = getattr(m, k, None)
        if obj:
            return k, obj.file_id
    return None


SENDERS = {
    "photo": "send_photo", "video": "send_video", "document": "send_document",
    "audio": "send_audio", "voice": "send_voice", "animation": "send_animation",
    "sticker": "send_sticker", "video_note": "send_video_note",
}


def clean_name(s: str) -> str:
    s = norm(s).strip().lstrip("/").strip("[]").split("@")[0]
    return s[:32]


def media_blob(msg) -> str:
    """টেক্সট + ক্যাপশন + স্টিকারের ইমোজি/সেটের নাম + ফাইলের নাম"""
    parts = [msg.text or "", msg.caption or ""]
    if msg.sticker:
        parts += [msg.sticker.emoji or "", msg.sticker.set_name or ""]
    if msg.document and msg.document.file_name:
        parts.append(msg.document.file_name)
    if msg.video and msg.video.file_name:
        parts.append(msg.video.file_name)
    if msg.animation and msg.animation.file_name:
        parts.append(msg.animation.file_name)
    return " ".join(p for p in parts if p)


def parse_rc(desc: str):
    """বর্ণনা থেকে (triggers, reply) বের করে।
    ফরম্যাট ১:  banana, 🍌, কলা | উত্তর
    ফরম্যাট ২:  ...banana এর ইমোজি দিলে ... বলবে 'উত্তর'"""
    if "|" in desc:
        left, right = desc.split("|", 1)
        trig = [x.strip() for x in re.split(r"[,،\n]", left) if x.strip()]
        reply = right.strip()
    else:
        qs = [i for i, ch in enumerate(desc) if ch in "'\"‘’“”"]
        if len(qs) >= 2:
            reply = desc[qs[0] + 1: qs[-1]].strip()
            outside = desc[: qs[0]]
        else:
            reply, outside = "", desc
        trig = EMOJI_RE.findall(outside)
        trig += re.findall(r"([A-Za-z]{3,})", outside)
        trig += re.findall(r"(\S+?)\s*(?:এর|র)?\s*(?:ইমোজি|স্টিকার|ছবি|পিক|emoji|sticker)", outside, re.I)
        trig = [t for t in trig if norm(t) not in STOP]
    out = []
    for t in trig:
        t = norm(t).strip()
        if t and t not in out:
            out.append(t)
    for group in SYNONYMS:  # সমার্থক শব্দ যোগ
        if any(t in group for t in out):
            out += [g for g in group if g not in out]
    return out, reply


# ---------------------------------------------------------------- moderation
async def violation(msg, ctx):
    """(True, reply_text|None) অথবা None"""
    chat_id = msg.chat_id
    text = msg.text or msg.caption or ""
    blob = media_blob(msg)
    ents = (msg.entities or []) + (msg.caption_entities or [])
    _, uname = await linked_info(ctx, chat_id)

    clean = text
    if uname:
        clean = re.sub(rf"(?:https?://)?(?:t\.me/|telegram\.me/)?@?{re.escape(uname)}", "", text, flags=re.I)

    # ১) লিংক / ইউজারনেম / ইনবক্স / আমন্ত্রণ
    if LINK_RE.search(clean) or INVITE_RE.search(clean):
        return True, None
    for e in ents:
        if e.type == "text_link" and not (uname and uname.lower() in (e.url or "").lower()):
            return True, None

    # ২) /mediablock চালু থাকলে সদস্যদের ছবি/ভিডিও/ফাইল মুছবে (স্ক্রিনশট/ভিডিওতে আমন্ত্রণ ঠেকাতে)
    if get_setting(chat_id, "mediablock") == "on" and (
        msg.photo or msg.video or msg.animation or msg.video_note or msg.document
    ):
        return True, None

    # ৩) গালাগালি, sexual, প্রতিদ্বন্দ্বী টুল
    if blob:
        if match_words(blob, get_words(chat_id, "abuse")):
            return True, "গালাগালি নিষেধ"
        if match_words(blob, get_words(chat_id, "sexual")):
            return True, None
        nb = norm(blob)
        if any(norm(c) in nb for c in get_words(chat_id, "competitor")):
            return True, None

        # ৪) /rc নিয়ম
        nb = norm(blob)
        for trig_json, reply in DB.execute("SELECT triggers, reply FROM rules WHERE chat_id=?", (chat_id,)):
            if any(t and t in nb for t in json.loads(trig_json)):
                return True, (reply or None)
    return None


async def moderate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    # লিংক করা চ্যানেলের মেসেজ ও অ্যাডমিনদের মেসেজ ছোঁয়া হবে না
    if msg.is_automatic_forward or (msg.from_user and msg.from_user.id == 777000):
        return
    lid, _ = await linked_info(ctx, msg.chat_id)
    if msg.sender_chat and (msg.sender_chat.id == lid or msg.sender_chat.id == msg.chat_id):
        return
    if msg.from_user and msg.from_user.is_bot:
        return
    if await sender_is_admin(msg, ctx):
        return

    res = await violation(msg, ctx)
    if not res:
        return
    try:
        await msg.delete()
    except TelegramError as e:
        log.warning("delete failed (bot কে 'Delete messages' permission দিন): %s", e)
        return
    if res[1]:
        await temp_reply(ctx, msg.chat_id, res[1])
    raise ApplicationHandlerStop


# ---------------------------------------------------------------- commands
async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    parts = (msg.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await msg.reply_text("ফাইল/ভিডিওতে রিপ্লাই করে: /add নাম\nমেসেজ সেভ করতে: /add নাম আপনার মেসেজ")
        return
    name = clean_name(parts[1])
    if not name or name in RESERVED:
        await msg.reply_text("এই নাম ব্যবহার করা যাবে না।")
        return
    rest = parts[2].strip() if len(parts) > 2 else ""
    rep = msg.reply_to_message
    media = extract_media(rep)
    if media:
        kind, fid = media
        cap = rest or (rep.caption or "")
        DB.execute("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?)", (msg.chat_id, name, kind, fid, cap, ""))
        await msg.reply_text(f"✅ সেভ হয়েছে ({kind}) — ব্যবহার: /{name}")
    elif rest or (rep and rep.text):
        txt = rest or rep.text
        DB.execute("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?)", (msg.chat_id, name, "text", "", "", txt))
        await msg.reply_text(f"✅ মেসেজ সেভ হয়েছে — ব্যবহার: {name}")
    else:
        await msg.reply_text("ফাইলে রিপ্লাই করুন, অথবা নামের পরে মেসেজ লিখুন।")
        return
    DB.commit()


async def rm_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    if not ctx.args:
        await msg.reply_text("ব্যবহার: /rm নাম")
        return
    name = clean_name(ctx.args[0])
    cur = DB.execute("DELETE FROM items WHERE chat_id=? AND name=?", (msg.chat_id, name))
    DB.commit()
    await msg.reply_text("🗑 রিমুভ হয়েছে (/notes থেকেও মুছে গেছে)" if cur.rowcount else "এই নামে কিছু পাওয়া যায়নি")


ICONS = {"text": "💬", "photo": "🖼", "video": "🎬", "document": "📄", "audio": "🎵",
         "voice": "🎤", "animation": "🎞", "sticker": "😀", "video_note": "⭕"}


async def notes_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    rows = DB.execute("SELECT name, kind FROM items WHERE chat_id=? ORDER BY kind='text' DESC, name",
                      (msg.chat_id,)).fetchall()
    if not rows:
        await msg.reply_text("📭 কিছু সেভ করা নেই।")
        return
    lines = [f"{ICONS.get(k, '📎')} {n if k == 'text' else '/' + n}" for n, k in rows]
    chunk, size = ["📒 সেভ করা নাম (মোট %d):\n" % len(rows)], 0
    for ln in lines:
        if size + len(ln) > 3800:
            await msg.reply_text("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(ln)
        size += len(ln) + 1
    await msg.reply_text("\n".join(chunk))


async def rc_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text(
            "ব্যবহার (যেকোনো একটা):\n\n"
            "১) /rc banana, 🍌, কলা | আরে ভাই কলা ইমোজি দেও কেন নিষেধ আছে তো 😁\n\n"
            "২) /rc কেউ banana এর ইমোজি দিলে ডিলেট করে বলবা 'আরে ভাই কলা ইমোজি দেও কেন নিষেধ আছে তো 😁'"
        )
        return
    trig, reply = parse_rc(parts[1])
    if not trig:
        await msg.reply_text("ট্রিগার শব্দ/ইমোজি বুঝতে পারিনি। ১ নম্বর ফরম্যাট (শব্দ, শব্দ | উত্তর) ব্যবহার করুন।")
        return
    DB.execute("INSERT INTO rules(chat_id, triggers, reply) VALUES(?,?,?)",
               (msg.chat_id, json.dumps(trig, ensure_ascii=False), reply))
    DB.commit()
    await msg.reply_text(f"✅ নিয়ম যোগ হয়েছে\nট্রিগার: {', '.join(trig)}\nউত্তর: {reply or '(চুপচাপ ডিলেট)'}")


async def rclist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    rows = DB.execute("SELECT id, triggers, reply FROM rules WHERE chat_id=?", (msg.chat_id,)).fetchall()
    await msg.reply_text(
        "\n\n".join(f"#{i}  {', '.join(json.loads(t))}\n→ {r or '(চুপচাপ ডিলেট)'}" for i, t, r in rows)
        or "কোনো /rc নিয়ম নেই।"
    )


async def rcdel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await msg.reply_text("ব্যবহার: /rcdel নম্বর  (নম্বর দেখতে /rclist)")
        return
    cur = DB.execute("DELETE FROM rules WHERE chat_id=? AND id=?", (msg.chat_id, int(ctx.args[0])))
    DB.commit()
    await msg.reply_text("🗑 মুছে গেছে" if cur.rowcount else "পাওয়া যায়নি")


async def addword_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    if len(ctx.args) < 2 or ctx.args[0].lower() not in KIND_ALIAS:
        await msg.reply_text("ব্যবহার: /addword abuse|sexual|competitor শব্দ")
        return
    kind = KIND_ALIAS[ctx.args[0].lower()]
    word = " ".join(ctx.args[1:]).strip()
    DB.execute("INSERT OR IGNORE INTO words VALUES(?,?,?)", (msg.chat_id, kind, word))
    DB.commit()
    await msg.reply_text(f"✅ যোগ হয়েছে ({kind}): {word}")


async def rmword_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    if len(ctx.args) < 2 or ctx.args[0].lower() not in KIND_ALIAS:
        await msg.reply_text("ব্যবহার: /rmword abuse|sexual|competitor শব্দ")
        return
    kind = KIND_ALIAS[ctx.args[0].lower()]
    DB.execute("DELETE FROM words WHERE chat_id=? AND kind=? AND word=?",
               (msg.chat_id, kind, " ".join(ctx.args[1:]).strip()))
    DB.commit()
    await msg.reply_text("🗑 রিমুভ হয়েছে")


async def mediablock_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not await sender_is_admin(msg, ctx):
        return
    if not ctx.args or ctx.args[0].lower() not in ("on", "off"):
        await msg.reply_text("ব্যবহার: /mediablock on | off\n(on থাকলে সদস্যদের ছবি/ভিডিও/ফাইল মুছে যাবে)")
        return
    v = ctx.args[0].lower()
    DB.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (msg.chat_id, "mediablock", v))
    DB.commit()
    await msg.reply_text(f"মিডিয়া ব্লক: {v}")


# ---------------------------------------------------------------- saved item trigger
async def trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    t = (msg.text or "").strip()
    if not t or len(t) > 100:
        return
    slash = t.startswith("/")
    name = clean_name(t.split()[0]) if slash else clean_name(t)
    if not name or name in RESERVED:
        return
    row = DB.execute("SELECT kind, file_id, caption, text FROM items WHERE chat_id=? AND name=?",
                     (msg.chat_id, name)).fetchone()
    if not row:
        return
    kind, fid, cap, txt = row
    if kind != "text" and not slash:
        return  # ফাইল শুধু /নাম দিয়ে আসবে; মেসেজ শুধু নাম লিখলেই আসবে
    target = msg.reply_to_message or msg
    rp = ReplyParameters(message_id=target.message_id, allow_sending_without_reply=True)
    try:
        if kind == "text":
            await ctx.bot.send_message(msg.chat_id, txt, reply_parameters=rp)
        else:
            kw = {"caption": cap} if cap and kind not in ("sticker", "video_note") else {}
            await getattr(ctx.bot, SENDERS[kind])(msg.chat_id, fid, reply_parameters=rp, **kw)
    except TelegramError as e:
        log.warning("send failed: %s", e)


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "আমাকে গ্রুপে অ্যাডমিন বানান (Delete messages permission সহ)।\n\n"
        "/add নাম — ফাইল/ভিডিওতে রিপ্লাই করে সেভ\n"
        "/add নাম মেসেজ — মেসেজ সেভ\n"
        "/rm নাম — রিমুভ | /notes — সব নাম দেখুন\n"
        "/rc ... — কাস্টম নিয়ম | /rclist | /rcdel নম্বর\n"
        "/addword abuse|sexual|competitor শব্দ | /rmword ...\n"
        "/mediablock on|off"
    )


def keep_alive():
    """Render এর জন্য ছোট ওয়েব সার্ভার (port বাইন্ডিং + UptimeRobot ping)"""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    port = int(os.getenv("PORT", "10000"))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), H).serve_forever, daemon=True).start()


def main():
    keep_alive()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, moderate), group=-1)

    grp = filters.ChatType.GROUPS
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd, filters=grp))
    app.add_handler(CommandHandler("rm", rm_cmd, filters=grp))
    app.add_handler(CommandHandler(["notes", "list"], notes_cmd, filters=grp))
    app.add_handler(CommandHandler("rc", rc_cmd, filters=grp))
    app.add_handler(CommandHandler("rclist", rclist_cmd, filters=grp))
    app.add_handler(CommandHandler("rcdel", rcdel_cmd, filters=grp))
    app.add_handler(CommandHandler("addword", addword_cmd, filters=grp))
    app.add_handler(CommandHandler("rmword", rmword_cmd, filters=grp))
    app.add_handler(CommandHandler("mediablock", mediablock_cmd, filters=grp))
    app.add_handler(MessageHandler(grp & filters.TEXT, trigger), group=1)

    log.info("Bot চালু হয়েছে")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
