import telebot
from telebot.types import ChatPermissions
import re
import json
import os

# আপনার বট টোকেন
BOT_TOKEN = "8349674951:AAHixzZs1AMt9JHz9iTHpGN3AgXUe4Haf_w"
bot = telebot.TeleBot(BOT_TOKEN)

# কাস্টম সেভ করা ওয়ার্ড রাখার JSON ফাইল
DB_FILE = "saved_keywords.json"

def load_keywords():
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_keywords(keywords):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)

# গালাগালি ও অশ্লীল শব্দের তালিকা (বাংলা ও ইংরেজি)
BAD_WORDS = [
    "madarchod", "banchod", "chuda", "magi", "khanki", "bessa", "bokachoda", 
    "gandi", "harami", "suor", "fuck", "bitch", "asshole", "bsdk", "gandu", 
    "chutiya", "chod", "bal", "balsal", "maderchod", "bhadwa", "lund", "randi",
    "খানকি", "মাগী", "চুদা", "বোকাচোদা", "মাদারচোদ", "বাল", "বেশ্যা", "হারামি"
]

# Buy/Sell ও Inbox সংক্রান্ত শব্দাবলি
BUY_SELL_INBOX_WORDS = [
    "buy", "sell", "inbox", "dm me", "ib asho", "ib", "inbox me", "contact me in inbox",
    "বিক্রি", "কিনুন", "ইনবক্স", "ইনবক্সে", "নক দিন", "ইনবক্সে আসুন", "dm", "সেল", "বাই",
    "offer", "discount", "price in ib", "dm for details", "inbox for details"
]

# টেক্সট ফিল্টার ফাংশন (স্টার ***, স্পেস ইত্যাদি বাইপাস ঠেকানোর জন্য)
def normalize_text(text):
    if not text:
        return ""
    # স্পেশাল ক্যারেক্টার ও স্পেস রিমুভ করে সাধারণ টেক্সটে রূপান্তর
    cleaned = re.sub(r'[\s\*_\-\.@#$%^&+=!?/\\|~`]', '', text.lower())
    return cleaned

def is_admin(chat_id, user_id):
    try:
        chat_member = bot.get_chat_member(chat_id, user_id)
        return chat_member.status in ['creator', 'administrator']
    except:
        return False

# ================== MUTE / UNMUTE কমান্ডসমূহ ==================

@bot.message_handler(commands=['mute'])
def mute_handler(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return

    text = message.text.strip().lower()
    
    # /mute all হ্যান্ডেল করা
    if text == "/mute all":
        try:
            bot.set_chat_permissions(message.chat.id, ChatPermissions(can_send_messages=False))
            bot.reply_to(message, "🔇 গ্রুপ এর সবাইকে Mute করা হয়েছে!")
        except Exception as e:
            bot.reply_to(message, f"❌ সমস্যা হয়েছে: {e}")
        return

    # রিপ্লাই দিয়ে ইউজার mute করা
    if not message.reply_to_message:
        bot.reply_to(message, "⚠️ যাকে মিউট করতে চান তার মেসেজে রিপ্লাই দিয়ে `/mute` লিখুন অথবা সবাইকে মিউট করতে `/mute all` লিখুন।", parse_mode="Markdown")
        return

    target_user = message.reply_to_message.from_user
    if is_admin(message.chat.id, target_user.id):
        bot.reply_to(message, "⚠️ অ্যাডমিনকে মিউট করা যাবে না।")
        return

    try:
        bot.restrict_chat_member(message.chat.id, target_user.id, permissions=ChatPermissions(can_send_messages=False))
        bot.reply_to(message, f"🔇 {target_user.first_name} কে মিউট করা হয়েছে!")
    except Exception as e:
        bot.reply_to(message, f"❌ মিউট করতে ব্যর্থ: {e}")


@bot.message_handler(commands=['unmute'])
def unmute_handler(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return

    text = message.text.strip().lower()

    # /unmute all হ্যান্ডেল করা
    if text == "/unmute all":
        try:
            bot.set_chat_permissions(
                message.chat.id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            bot.reply_to(message, "🔊 গ্রুপ এর সবাইকে Unmute করা হয়েছে!")
        except Exception as e:
            bot.reply_to(message, f"❌ সমস্যা হয়েছে: {e}")
        return

    # রিপ্লাই দিয়ে ইউজার unmute করা
    if not message.reply_to_message:
        bot.reply_to(message, "⚠️ যাকে আনমিউট করতে চান তার মেসেজে রিপ্লাই দিয়ে `/unmute` লিখুন অথবা সবাইকে আনমিউট করতে `/unmute all` লিখুন।", parse_mode="Markdown")
        return

    target_user = message.reply_to_message.from_user
    try:
        bot.restrict_chat_member(
            message.chat.id,
            target_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        bot.reply_to(message, f"🔊 {target_user.first_name} কে আনমিউট করা হয়েছে!")
    except Exception as e:
        bot.reply_to(message, f"❌ আনমিউট করতে ব্যর্থ: {e}")


# ================== /add এবং /rc কমান্ডসমূহ ==================

@bot.message_handler(commands=['add'])
def add_keyword_handler(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "⚠️ ব্যবহারের নিয়ম: `/add [message name]`", parse_mode="Markdown")
        return

    keyword = parts[1].strip().lower()
    keywords = load_keywords()

    if keyword not in keywords:
        keywords.append(keyword)
        save_keywords(keywords)
        bot.reply_to(message, f"✅ নাম `{keyword}` সেভ করা হয়েছে! এখন থেকে কেউ এটা লিখলে মেসেজ ডিলিট হয়ে যাবে।", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"⚠️ `{keyword}` ইতিমধ্যে সেভ করা আছে।", parse_mode="Markdown")


@bot.message_handler(commands=['rc'])
def list_saved_handler(message):
    keywords = load_keywords()
    if not keywords:
        bot.reply_to(message, "📂 বর্তমানে কোনো নাম বা কিওয়ার্ড সেভ করা নেই।")
        return

    text = "📋 **সেভ করা মেসেজ/নামের তালিকা (ক্লিক করলেই কপি হবে):**\n\n"
    for kw in keywords:
        text += f"• <code>{kw}</code>\n"

    bot.reply_to(message, text, parse_mode="HTML")


# ================== মেসেজ ফিল্টারিং ও API ট্র্রিগার ==================

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'voice', 'audio'])
def handle_all_messages(message):
    # শুধু টেক্সট বা ক্যাপশন ফিল্টার করব
    content = message.text or message.caption
    if not content:
        return

    cleaned_content = normalize_text(content)
    raw_lower = content.lower()

    # ১. API ট্রিগার (কেউ যদি "api" লিখে)
    if re.search(r'\bapi\b', raw_lower):
        user = message.from_user
        user_id = user.id
        user_name = user.first_name + (f" {user.last_name}" if user.last_name else "")
        
        # অ্যাডমিনদের লিস্ট ও মেনশন তৈরি
        try:
            admins = bot.get_chat_administrators(message.chat.id)
            admin_mentions = []
            for adm in admins:
                if not adm.user.is_bot:
                    admin_mentions.append(f"[{adm.user.first_name}](tg://user?id={adm.user.id})")
            
            mentions_text = ", ".join(admin_mentions) if admin_mentions else "Admins"
            reply_msg = (
                f"👤 **User ID:** `{user_id}`\n"
                f"📛 **Name:** {user_name}\n\n"
                f"{mentions_text} এই admin ওর api টা enble করে দাও 🙄"
            )
            bot.reply_to(message, reply_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Error fetching admins: {e}")
        return

    # অ্যাডমিনদের মেসেজ ফিল্টার করার দরকার নেই
    if is_admin(message.chat.id, message.from_user.id):
        return

    # ২. গালাগালি ফিল্টারিং (সাধারণ ও *** বা স্পেসযুক্ত সব ফর্ম্যাট)
    for bad in BAD_WORDS:
        norm_bad = normalize_text(bad)
        if norm_bad in cleaned_content or bad in raw_lower:
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            return

    # ৩. Buy/Sell এবং Inbox প্রমোশন মেসেজ ফিল্টারিং
    for bs in BUY_SELL_INBOX_WORDS:
        if bs in raw_lower or normalize_text(bs) in cleaned_content:
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            return

    # ৪. কাস্টম সেভ করা কিওয়ার্ড ফিল্টারিং (/add এর মাধ্যমে যা যুক্ত হয়েছে)
    saved_kws = load_keywords()
    for kw in saved_kws:
        if kw in raw_lower or normalize_text(kw) in cleaned_content:
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            return

print("Bot is running...")
bot.infinity_polling()
