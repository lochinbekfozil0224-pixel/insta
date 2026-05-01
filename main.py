#!/usr/bin/env python3
"""
Instagram Video Downloader Bot — FIXED VERSION
Asosiy tuzatishlar:
  - Instagram cookie qo'llab-quvvatlash (cookies.txt yoki env var)
  - nocheckcertificate — SSL xatolari bartaraf
  - Progress hook — fayl yo'li ishonchli topiladi
  - Aniq xato xabarlari
"""

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery

import yt_dlp

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7457477557:AAGUBa6qRiI1z67xgESMvWHJwC4bKHBNnCE")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "8135915671"))
DB_PATH    = os.environ.get("DB_PATH", "bot.db")

# Download papkasi — /tmp ishlataymiz (Railway uchun xavfsiz)
DL_DIR = Path("/tmp/igbot")
DL_DIR.mkdir(exist_ok=True, parents=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

pending_urls: dict[int, str] = {}

# ═══════════════════════════════════════════════════════
# COOKIES SETUP
# ═══════════════════════════════════════════════════════
COOKIES_PATH = "/tmp/ig_cookies.txt"

def setup_cookies() -> str | None:
    """
    Cookie manbalari (ustuvorlik tartibi):
    1. cookies.txt fayli (ishchi papkada)
    2. INSTAGRAM_COOKIES env var (base64 encoded)
    """
    if os.path.exists("cookies.txt"):
        log.info("Cookie: cookies.txt topildi")
        return "cookies.txt"

    env_cookies = os.environ.get("INSTAGRAM_COOKIES", "")
    if env_cookies:
        try:
            decoded = base64.b64decode(env_cookies).decode("utf-8")
            with open(COOKIES_PATH, "w") as f:
                f.write(decoded)
            log.info("Cookie: INSTAGRAM_COOKIES env vardan yuklandi")
            return COOKIES_PATH
        except Exception as e:
            log.error(f"Cookie decode xatosi: {e}")

    log.warning("Cookie yo'q — Instagram 403 qaytarishi mumkin!")
    return None

COOKIE_FILE = setup_cookies()

# ═══════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════
def get_db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id  INTEGER PRIMARY KEY,
                username TEXT,
                created  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        defaults = {
            "watermark":        "📸 @YourBot",
            "ad_enabled":       "1",
            "channel_id":       "@your_channel",
            "channel_url":      "https://t.me/your_channel",
            "sub_btn_text":     "📢 Obuna bo'l",
            "sub_btn_emoji_id": "",
            "sub_btn_style":    "success",
            "bot_btn_text":     "🔗 Kanalga o'tish",
            "bot_btn_emoji_id": "",
            "bot_btn_style":    "primary",
            "bot_btn_url":      "https://t.me/your_channel",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v)
            )

def gs(key: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""

def ss(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )

def add_user(uid: int, uname: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (uid, uname)
        )

def all_users() -> list[int]:
    with get_db() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users")]

def user_count() -> int:
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

# ═══════════════════════════════════════════════════════
# RAW TELEGRAM API (Bot API 9.4 styled buttons)
# ═══════════════════════════════════════════════════════
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def styled_kb(rows: list[list[dict]]) -> dict:
    keyboard = []
    for row in rows:
        kb_row = []
        for b in row:
            btn: dict = {"text": b["text"]}
            if "url"           in b: btn["url"]                  = b["url"]
            if "callback_data" in b: btn["callback_data"]         = b["callback_data"]
            if b.get("style"):       btn["style"]                 = b["style"]
            if b.get("emoji_id"):    btn["icon_custom_emoji_id"]  = b["emoji_id"]
            kb_row.append(btn)
        keyboard.append(kb_row)
    return {"inline_keyboard": keyboard}

async def raw_post(method: str, payload: dict) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API}/{method}", json=payload) as r:
            return await r.json()

async def raw_form(method: str, form: aiohttp.FormData) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API}/{method}", data=form) as r:
            return await r.json()

async def send_msg(chat_id: int, text: str, kb: dict | None = None) -> dict:
    p: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = json.dumps(kb)
    return await raw_post("sendMessage", p)

async def edit_msg(chat_id: int, msg_id: int, text: str) -> dict:
    return await raw_post("editMessageText", {
        "chat_id": chat_id, "message_id": msg_id,
        "text": text, "parse_mode": "HTML"
    })

async def del_msg(chat_id: int, msg_id: int):
    await raw_post("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

async def send_video(chat_id: int, path: str, caption: str, kb: dict | None = None) -> dict:
    form = aiohttp.FormData()
    form.add_field("chat_id",    str(chat_id))
    form.add_field("caption",    caption)
    form.add_field("parse_mode", "HTML")
    if kb: form.add_field("reply_markup", json.dumps(kb))
    with open(path, "rb") as f:
        form.add_field("video", f, filename="video.mp4", content_type="video/mp4")
    return await raw_form("sendVideo", form)

async def send_doc(chat_id: int, path: str, caption: str, kb: dict | None = None) -> dict:
    form = aiohttp.FormData()
    form.add_field("chat_id",    str(chat_id))
    form.add_field("caption",    caption)
    form.add_field("parse_mode", "HTML")
    if kb: form.add_field("reply_markup", json.dumps(kb))
    with open(path, "rb") as f:
        form.add_field("document", f, filename=Path(path).name)
    return await raw_form("sendDocument", form)

# ═══════════════════════════════════════════════════════
# VIDEO: YUKLAB OLISH
# ═══════════════════════════════════════════════════════
def is_insta(text: str) -> bool:
    return bool(re.search(
        r'https?://(www\.)?(instagram\.com|instagr\.am)/'
        r'(p|reel|tv|stories|share)/[\w\-]+',
        text
    ))

async def download_video(url: str, uid: int) -> tuple[str | None, str]:
    """
    Returns: (fayl_yoli, xato_turi)
    xato_turi: "" | "cookie" | "private" | "boshqa xato matni"
    """
    ts     = int(time.time())
    prefix = f"{uid}_{ts}"
    tmpl   = str(DL_DIR / f"{prefix}.%(ext)s")

    downloaded: list[str] = []

    def hook(d: dict):
        if d["status"] == "finished":
            fname = d.get("filename") or d.get("info_dict", {}).get("_filename", "")
            if fname:
                downloaded.append(fname)

    opts: dict = {
        "outtmpl":            tmpl,
        "format":             "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format":"mp4",
        "quiet":              True,
        "no_warnings":        True,
        "nocheckcertificate": True,          # ← SSL xatosini hal qiladi
        "progress_hooks":     [hook],
        "socket_timeout":     60,
        "retries":            3,
        "fragment_retries":   3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    # Cookie bor bo'lsa qo'shish
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE

    def _dl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    try:
        await asyncio.get_event_loop().run_in_executor(None, _dl)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "403" in err or "login" in err or "cookie" in err or "csrf" in err:
            return None, "cookie"
        if "private" in err or "unavailable" in err:
            return None, "private"
        log.error(f"yt-dlp xato: {e}")
        return None, str(e)[:120]
    except Exception as e:
        log.error(f"Kutilmagan xato: {e}")
        return None, str(e)[:120]

    # Fayl topish
    if downloaded and os.path.exists(downloaded[0]):
        return downloaded[0], ""

    # Glob orqali topish (zaxira)
    for f in DL_DIR.glob(f"{prefix}.*"):
        if f.suffix not in (".part", ".ytdl", ".tmp"):
            return str(f), ""

    return None, "topilmadi"

# ═══════════════════════════════════════════════════════
# VIDEO: WATERMARK (FFmpeg)
# ═══════════════════════════════════════════════════════
def add_watermark(inp: str, text: str) -> str:
    out  = inp.rsplit(".", 1)[0] + "_wm.mp4"
    safe = text.replace("'", r"\'").replace(":", r"\:").replace("\\", r"\\")

    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"

    drawtext = (
        f"drawtext=text='{safe}'"
        ":fontsize=30:fontcolor=white"
        ":x=(w-text_w)/2:y=h-th-30"
        ":box=1:boxcolor=black@0.65:boxborderw=10"
    )

    import subprocess
    cmd = [ffmpeg, "-i", inp, "-vf", drawtext, "-codec:a", "copy", "-y", out]

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=300)
        if res.returncode == 0 and os.path.exists(out):
            return out
        log.error(f"FFmpeg xato: {res.stderr.decode()[:200]}")
    except Exception as e:
        log.error(f"FFmpeg: {e}")

    return inp

# ═══════════════════════════════════════════════════════
# SUBSCRIPTION CHECK
# ═══════════════════════════════════════════════════════
async def is_subscribed(uid: int) -> bool:
    ch = gs("channel_id")
    if not ch or ch == "@your_channel":
        return True
    try:
        m = await bot.get_chat_member(ch, uid)
        return m.status not in ("left", "kicked")
    except Exception:
        return True

# ═══════════════════════════════════════════════════════
# FSM STATES
# ═══════════════════════════════════════════════════════
class S(StatesGroup):
    watermark     = State()
    channel_id    = State()
    channel_url   = State()
    sub_btn_text  = State()
    sub_btn_emoji = State()
    bot_btn_text  = State()
    bot_btn_url   = State()
    bot_btn_emoji = State()
    bot_btn_style = State()
    broadcast     = State()

# ═══════════════════════════════════════════════════════
# ADMIN PANELI
# ═══════════════════════════════════════════════════════
async def admin_home(uid: int):
    ad      = gs("ad_enabled") == "1"
    e       = gs("sub_btn_emoji_id")
    cookie  = "✅ Cookie bor" if (COOKIE_FILE and os.path.exists(COOKIE_FILE)) else "❌ Cookie yo'q"

    kb = styled_kb([
        [
            {"text": "📝 Watermark",    "callback_data": "ap_wm",     "style": "primary", "emoji_id": e},
            {"text": "📢 Kanal",        "callback_data": "ap_ch",     "style": "primary", "emoji_id": e},
        ],
        [
            {"text": "🔘 Obuna tugma",  "callback_data": "ap_subbtn", "style": "primary", "emoji_id": e},
            {"text": "🔗 Pastki tugma", "callback_data": "ap_botbtn", "style": "primary", "emoji_id": e},
        ],
        [
            {
                "text":          "✅ Reklama yoq" if not ad else "❌ Reklama o'chir",
                "callback_data": "ap_ad",
                "style":         "success" if not ad else "danger",
                "emoji_id":      e,
            },
        ],
        [
            {"text": "🍪 Cookie haqida", "callback_data": "ap_cookie", "style": "primary", "emoji_id": e},
            {"text": "📣 Broadcast",     "callback_data": "ap_bc",     "style": "danger",  "emoji_id": e},
        ],
    ])

    await send_msg(
        uid,
        f"🛠 <b>Admin Panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{user_count()}</b>\n"
        f"📢 Kanal: <code>{gs('channel_id')}</code>\n"
        f"📝 Watermark: <code>{gs('watermark')}</code>\n"
        f"🍪 Instagram: {cookie}\n"
        f"{'✅ Reklama yoniq' if ad else '❌ Reklama o\'chiq'}",
        kb,
    )

async def subbtn_menu(uid: int):
    e = gs("sub_btn_emoji_id")
    kb = styled_kb([
        [{"text": "📝 Matn",           "callback_data": "ap_sb_text",  "style": "primary", "emoji_id": e}],
        [{"text": "✨ Emoji ID",        "callback_data": "ap_sb_emoji", "style": "primary", "emoji_id": e}],
        [{"text": "🔗 URL",            "callback_data": "ap_sb_url",   "style": "primary", "emoji_id": e}],
        [{"text": "🔙 Orqaga",         "callback_data": "ap_back",     "style": "danger",  "emoji_id": e}],
    ])
    await send_msg(uid,
        f"🔘 <b>Obuna tugmasi</b>\n\n"
        f"📝 Matn: <code>{gs('sub_btn_text')}</code>\n"
        f"✨ Emoji ID: <code>{gs('sub_btn_emoji_id') or '(yo\'q)'}</code>\n"
        f"🎨 Stil: <code>{gs('sub_btn_style')}</code>\n"
        f"🔗 URL: <code>{gs('channel_url')}</code>",
        kb,
    )

async def botbtn_menu(uid: int):
    e = gs("sub_btn_emoji_id")
    kb = styled_kb([
        [{"text": "📝 Matn",           "callback_data": "ap_bb_text",  "style": "primary", "emoji_id": e}],
        [{"text": "✨ Emoji ID",        "callback_data": "ap_bb_emoji", "style": "primary", "emoji_id": e}],
        [{"text": "🔗 URL",            "callback_data": "ap_bb_url",   "style": "primary", "emoji_id": e}],
        [{"text": "🎨 Stil",           "callback_data": "ap_bb_style", "style": "primary", "emoji_id": e}],
        [{"text": "🔙 Orqaga",         "callback_data": "ap_back",     "style": "danger",  "emoji_id": e}],
    ])
    await send_msg(uid,
        f"🔗 <b>Pastki tugma (video ostida)</b>\n\n"
        f"📝 Matn: <code>{gs('bot_btn_text')}</code>\n"
        f"✨ Emoji ID: <code>{gs('bot_btn_emoji_id') or '(yo\'q)'}</code>\n"
        f"🎨 Stil: <code>{gs('bot_btn_style')}</code>\n"
        f"🔗 URL: <code>{gs('bot_btn_url')}</code>",
        kb,
    )

# ═══════════════════════════════════════════════════════
# VIDEO PROCESSING
# ═══════════════════════════════════════════════════════
async def process_video(uid: int, url: str):
    r      = await send_msg(uid, "⏳ Video yuklanmoqda...")
    msg_id = r.get("result", {}).get("message_id")

    path, err = await download_video(url, uid)

    if not path:
        if err == "cookie":
            text = (
                "❌ <b>Instagram cookie kerak!</b>\n\n"
                "Instagram endi login talab qilmoqda.\n"
                "Admin panel → 🍪 Cookie haqida"
            )
        elif err == "private":
            text = "❌ Bu post <b>yopiq</b> yoki o'chirilgan!"
        else:
            text = f"❌ Video yuklab bo'lmadi!\n<code>{err}</code>"

        if msg_id:
            await edit_msg(uid, msg_id, text)
        return

    final = path
    if gs("ad_enabled") == "1":
        wm = gs("watermark")
        if wm:
            if msg_id:
                await edit_msg(uid, msg_id, "🎬 Watermark qo'shilmoqda...")
            final = await asyncio.get_event_loop().run_in_executor(
                None, add_watermark, path, wm
            )

    if msg_id:
        await edit_msg(uid, msg_id, "📤 Yuborilmoqda...")

    e  = gs("bot_btn_emoji_id")
    kb = styled_kb([[{
        "text":     gs("bot_btn_text"),
        "url":      gs("bot_btn_url"),
        "style":    gs("bot_btn_style"),
        "emoji_id": e,
    }]])

    size = os.path.getsize(final)
    cap  = "✅ Mana sizning videongiz!"

    try:
        if size < 50 * 1024 * 1024:
            res = await send_video(uid, final, cap, kb)
        else:
            res = await send_doc(uid, final, cap + "\n<i>(Hajmi katta)</i>", kb)

        if res.get("ok"):
            if msg_id: await del_msg(uid, msg_id)
        else:
            if msg_id:
                await edit_msg(uid, msg_id, f"❌ Yuborishda xato: {res.get('description', '?')}")
    except Exception as ex:
        log.error(f"Send xato: {ex}")
        if msg_id:
            await edit_msg(uid, msg_id, "❌ Yuborishda xato yuz berdi!")
    finally:
        for p in {path, final}:
            try:
                if p and os.path.exists(p): os.remove(p)
            except Exception:
                pass

# ═══════════════════════════════════════════════════════
# BOT + DISPATCHER
# ═══════════════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# ═══════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    add_user(uid, msg.from_user.username or msg.from_user.first_name or "")

    if uid == ADMIN_ID:
        await admin_home(uid)
        return

    e  = gs("bot_btn_emoji_id")
    kb = styled_kb([[{
        "text": "📸 Instagram", "url": "https://instagram.com",
        "style": "primary", "emoji_id": e,
    }]])
    await send_msg(
        uid,
        f"👋 <b>Salom, {msg.from_user.first_name}!</b>\n\n"
        f"📸 Instagram <b>video</b> yoki <b>reel</b> havolasini yuboring\n"
        f"⬇️ Men uni yuklab, sizga yuboraman!",
        kb,
    )

@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("❌ Ruxsat yo'q!")
        return
    await admin_home(ADMIN_ID)

@dp.message(F.text)
async def handle_text(msg: Message, state: FSMContext):
    uid  = msg.from_user.id
    text = msg.text or ""
    cur  = await state.get_state()

    # ── Admin FSM ──────────────────────────────────────
    if cur and uid == ADMIN_ID:
        handled = True
        if   cur == S.watermark:     ss("watermark",        text)
        elif cur == S.channel_id:    ss("channel_id",       text)
        elif cur == S.channel_url:   ss("channel_url",      text)
        elif cur == S.sub_btn_text:  ss("sub_btn_text",     text)
        elif cur == S.sub_btn_emoji: ss("sub_btn_emoji_id", text)
        elif cur == S.bot_btn_text:  ss("bot_btn_text",     text)
        elif cur == S.bot_btn_url:   ss("bot_btn_url",      text)
        elif cur == S.bot_btn_emoji: ss("bot_btn_emoji_id", text)
        elif cur == S.bot_btn_style:
            if text not in ("primary", "success", "danger"):
                await msg.answer("❌ Faqat: primary | success | danger")
                return
            ss("bot_btn_style", text)
        elif cur == S.broadcast:
            users = all_users()
            ok = fail = 0
            await msg.answer(f"📣 Yuborilmoqda... ({len(users)} ta)")
            for u2 in users:
                try:
                    await bot.send_message(u2, text, parse_mode="HTML")
                    ok += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    fail += 1
            await msg.answer(f"✅ Broadcast tugadi!\n✅ {ok} | ❌ {fail}")
            await state.clear()
            await admin_home(ADMIN_ID)
            return
        else:
            handled = False

        if handled:
            await msg.answer("✅ Saqlandi!", parse_mode="HTML")
            await state.clear()
            await admin_home(ADMIN_ID)
        return

    # ── Instagram URL ──────────────────────────────────
    if is_insta(text):
        subbed = await is_subscribed(uid)
        if not subbed:
            pending_urls[uid] = text
            e  = gs("sub_btn_emoji_id")
            kb = styled_kb([
                [{
                    "text":     gs("sub_btn_text"),
                    "url":      gs("channel_url"),
                    "style":    gs("sub_btn_style"),
                    "emoji_id": e,
                }],
                [{
                    "text":          "✅ Obunani tekshirish",
                    "callback_data": "check_sub",
                    "style":         "success",
                    "emoji_id":      e,
                }],
            ])
            await send_msg(uid,
                "❗️ Videoni olish uchun kanalga <b>obuna bo'ling!</b>\n\n"
                "Obuna bo'lgach 👇 tugmani bosing.",
                kb,
            )
            return

        asyncio.create_task(process_video(uid, text))
        return

    await msg.answer("📸 Instagram video yoki reel havolasini yuboring!")

# ═══════════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════════
@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cq: CallbackQuery):
    uid = cq.from_user.id
    if await is_subscribed(uid):
        url = pending_urls.pop(uid, None)
        await cq.answer("✅ Obuna tasdiqlandi!")
        try: await cq.message.delete()
        except Exception: pass
        if url:
            asyncio.create_task(process_video(uid, url))
        else:
            await send_msg(uid, "📸 Havolani qayta yuboring.")
    else:
        await cq.answer("❌ Hali obuna bo'lmadingiz!", show_alert=True)

@dp.callback_query(F.data == "ap_cookie")
async def cb_cookie(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    status = "✅ Cookie fayli bor va ishlayapti" if (COOKIE_FILE and os.path.exists(COOKIE_FILE)) \
             else "❌ Cookie yo'q — Instagram 403 berishi mumkin"

    await send_msg(ADMIN_ID,
        f"🍪 <b>Instagram Cookie Sozlash</b>\n\n"
        f"Holat: {status}\n\n"
        f"<b>Cookie olish:</b>\n"
        f"1️⃣ Chrome/Firefox ga <b>«Get cookies.txt LOCALLY»</b> extensionini o'rnating\n"
        f"2️⃣ instagram.com ga kiring (login qiling)\n"
        f"3️⃣ Extension ikonkasini bosib cookies.txt ni yuklab oling\n\n"
        f"<b>Railway ga yuklash:</b>\n"
        f"A) Railway → Variables → INSTAGRAM_COOKIES ga base64 encoded cookies.txt qo'ying\n"
        f"B) Yoki Railway Volume yarating va cookies.txt faylini yuklang\n\n"
        f"<b>Base64 qilish (terminalda):</b>\n"
        f"<code>base64 -i cookies.txt</code>"
    )
    await cq.answer()

@dp.callback_query(F.data == "ap_wm")
async def cb_wm(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.watermark)
    await send_msg(ADMIN_ID, f"📝 Yangi watermark:\nHozirgi: <code>{gs('watermark')}</code>")
    await cq.answer()

@dp.callback_query(F.data == "ap_ch")
async def cb_ch(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.channel_id)
    await send_msg(ADMIN_ID,
        f"📢 Kanal ID yuboring (<code>@mychannel</code>):\n"
        f"Hozirgi: <code>{gs('channel_id')}</code>\n\n"
        f"⚠️ Bot kanalda <b>admin</b> bo'lishi shart!"
    )
    await cq.answer()

@dp.callback_query(F.data == "ap_subbtn")
async def cb_subbtn(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await subbtn_menu(ADMIN_ID); await cq.answer()

@dp.callback_query(F.data == "ap_botbtn")
async def cb_botbtn(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await botbtn_menu(ADMIN_ID); await cq.answer()

@dp.callback_query(F.data == "ap_sb_text")
async def cb_sb_text(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.sub_btn_text)
    await send_msg(ADMIN_ID, f"📝 Obuna tugmasi matni:\nHozirgi: <code>{gs('sub_btn_text')}</code>")
    await cq.answer()

@dp.callback_query(F.data == "ap_sb_emoji")
async def cb_sb_emoji(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.sub_btn_emoji)
    await send_msg(ADMIN_ID,
        "✨ Premium emoji ID:\n\n"
        "@getidsbot ga emoji yuboring → ID ni oling\n"
        "⚠️ Bot egasida <b>Telegram Premium</b> kerak!"
    )
    await cq.answer()

@dp.callback_query(F.data == "ap_sb_url")
async def cb_sb_url(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.channel_url)
    await send_msg(ADMIN_ID, f"🔗 Kanal URL:\nHozirgi: <code>{gs('channel_url')}</code>")
    await cq.answer()

@dp.callback_query(F.data == "ap_bb_text")
async def cb_bb_text(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.bot_btn_text)
    await send_msg(ADMIN_ID, f"📝 Pastki tugma matni:\nHozirgi: <code>{gs('bot_btn_text')}</code>")
    await cq.answer()

@dp.callback_query(F.data == "ap_bb_emoji")
async def cb_bb_emoji(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.bot_btn_emoji)
    await send_msg(ADMIN_ID, "✨ Pastki tugma emoji ID:\n@getidsbot ga emoji yuboring")
    await cq.answer()

@dp.callback_query(F.data == "ap_bb_url")
async def cb_bb_url(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.bot_btn_url)
    await send_msg(ADMIN_ID, f"🔗 Pastki tugma URL:\nHozirgi: <code>{gs('bot_btn_url')}</code>")
    await cq.answer()

@dp.callback_query(F.data == "ap_bb_style")
async def cb_bb_style(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.bot_btn_style)
    await send_msg(ADMIN_ID,
        f"🎨 Stil yuboring:\n\n"
        f"<code>primary</code> — 🔵 Ko'k\n"
        f"<code>success</code> — 🟢 Yashil\n"
        f"<code>danger</code>  — 🔴 Qizil\n\n"
        f"Hozirgi: <code>{gs('bot_btn_style')}</code>"
    )
    await cq.answer()

@dp.callback_query(F.data == "ap_ad")
async def cb_ad(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    new = "0" if gs("ad_enabled") == "1" else "1"
    ss("ad_enabled", new)
    await cq.answer(f"Reklama {'yoqildi ✅' if new == '1' else 'o\'chirildi ❌'}", show_alert=True)
    await admin_home(ADMIN_ID)

@dp.callback_query(F.data == "ap_bc")
async def cb_bc(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID: return
    await state.set_state(S.broadcast)
    await send_msg(ADMIN_ID,
        f"📣 Broadcast xabarini yuboring:\n"
        f"👥 <b>{user_count()}</b> ta foydalanuvchi\n"
        f"HTML qo'llab-quvvatlanadi."
    )
    await cq.answer()

@dp.callback_query(F.data == "ap_back")
async def cb_back(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID: return
    await admin_home(ADMIN_ID); await cq.answer()

# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
async def main():
    init_db()
    log.info("✅ Bot ishga tushdi!")
    log.info(f"🍪 Cookie: {COOKIE_FILE or 'yo\'q'}")
    log.info(f"💾 DB: {DB_PATH}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
