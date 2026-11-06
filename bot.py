"""
SMM Panel Telegram Bot - PRO (all inline buttons)
Install: pip install -r requirements.txt
Run:     python smm_bot_pro.py

User side : inline menu, force-join, orders, add funds (QR + screenshot),
            daily bonus, referral, redeem code.
Admin side: /admin opens a full inline panel - force channels add/remove,
            add/cut balance by user ID, welcome/daily bonus, referral % commission,
            add/delete services manually, min fund, gift all, gift codes,
            ban/unban, broadcast, stats.
"""
import asyncio, json, logging, os, sqlite3, time
import requests
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_URL = os.getenv("API_URL", "https://yourpanel.com/api/v2").strip()
API_KEY = os.getenv("API_KEY", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()]
CURRENCY = "₹"
SUPPORT = "@your_support_username"
QR_IMAGE = "qr.png"            # your payment QR image (same folder as this file)
PAYMENT_INFO = ("💳 Scan the QR and pay the exact amount.\n"
                "UPI: yourupi@bank")
DEFAULT_CHANNELS = []          # optional first-run channels e.g. ["@mychannel"]
# starting values - all of these can be changed later from the Admin Panel
DEFAULTS = {"welcome": 5, "daily": 1, "refpct": 5, "minfund": 50}
DB_FILE = os.getenv("DB_FILE", "bot.db")
# ==================================================

logging.basicConfig(level=logging.INFO)
PAGE = 8

# ---------------- database ----------------
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT,
  balance REAL DEFAULT 0, banned INTEGER DEFAULT 0, referred_by INTEGER,
  bonus_given INTEGER DEFAULT 0, last_daily INTEGER DEFAULT 0, joined INTEGER,
  ref_earned REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, name TEXT,
  category TEXT, rate REAL, min INTEGER, max INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, service_id INTEGER, service_name TEXT, link TEXT, qty INTEGER,
  charge REAL, panel_order INTEGER, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS funds(id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, amount REAL, proof TEXT, status TEXT DEFAULT 'pending',
  created INTEGER, proof_uid TEXT);
CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, amount REAL, uses_left INTEGER);
CREATE TABLE IF NOT EXISTS redeemed(code TEXT, user_id INTEGER, PRIMARY KEY(code,user_id));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
""")
for _alter in ("ALTER TABLE funds ADD COLUMN proof_uid TEXT",
               "ALTER TABLE users ADD COLUMN ref_earned REAL DEFAULT 0"):
    try:
        db.execute(_alter)
        db.commit()
    except sqlite3.OperationalError:
        pass


def q(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def setting(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return float(r["v"]) if r else float(DEFAULTS[k])


def get_str(k, default=""):
    r = q("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def support():
    s_ = get_str("support", SUPPORT).strip()
    return s_ if s_.startswith("@") else "@" + s_


def support_url():
    return f"https://t.me/{support().lstrip('@')}"


def mask(k):
    if not k:
        return "not set"
    return k[:4] + "••••" + k[-3:] if len(k) > 8 else "••••"


def set_setting(k, v):
    q("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))


MENU_DEFAULTS = {
    "btn_order": "🛒 New Order", "btn_fund": "💰 Add Funds",
    "btn_account": "👤 Account", "btn_orders": "📦 My Orders",
    "btn_daily": "🎁 Daily Bonus", "btn_refer": "👥 Refer & Earn",
    "btn_redeem": "🎟 Redeem Code", "btn_support": "🆘 Support",
}


def menu_label(key):
    return get_str(key, MENU_DEFAULTS[key])


def get_channels():
    r = q("SELECT v FROM settings WHERE k='channels'").fetchone()
    if r:
        return json.loads(r["v"])
    return [{"chat": c, "title": c, "url": f"https://t.me/{c.lstrip('@')}"}
            for c in DEFAULT_CHANNELS]


def save_channels(lst):
    set_setting("channels", json.dumps(lst))


def add_bal(uid, amt):
    q("UPDATE users SET balance=ROUND(balance+?,2) WHERE id=?", (amt, uid))


def user_row(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def fmt(x):
    return f"{CURRENCY}{x:g}"


# ---------------- panel API ----------------
def api(**params):
    params["key"] = get_str("api_key", API_KEY)
    r = requests.post(get_str("api_url", API_URL), data=params, timeout=40)
    r.raise_for_status()
    return r.json()


async def api_async(**params):
    return await asyncio.to_thread(api, **params)


# ---------------- UI helpers ----------------
async def show(update, text, kb=None):
    """Edit the current message (clean inline UX); send new one if not possible."""
    cq = update.callback_query
    if cq:
        try:
            return await cq.edit_message_text(text, reply_markup=kb)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            pass
    await update.effective_chat.send_message(text, reply_markup=kb)


async def send(update, text, kb=None):
    await update.effective_chat.send_message(text, reply_markup=kb)


def back_kb(label="🔙 Main Menu", data="home"):
    return M([[B(label, callback_data=data)]])


def cancel_kb():
    return M([[B("❌ Cancel", callback_data="home")]])


def home_kb(uid):
    rows = [
        [B(menu_label("btn_order"), callback_data="order"), B(menu_label("btn_fund"), callback_data="fund")],
        [B(menu_label("btn_account"), callback_data="acc"), B(menu_label("btn_orders"), callback_data="orders")],
        [B(menu_label("btn_daily"), callback_data="daily"), B(menu_label("btn_refer"), callback_data="refer")],
        [B(menu_label("btn_redeem"), callback_data="redeem"),
         B(menu_label("btn_support"), url=support_url())],
    ]
    if uid in ADMIN_IDS:
        rows.append([B("🛠 Admin Panel", callback_data="admin")])
    return M(rows)


def set_state(ctx, st):
    ctx.user_data["st"] = st


async def send_qr(chat, caption, kb=None):
    """Send the payment QR (uploaded via admin panel, else qr.png, else text only)."""
    fid = get_str("qr_file_id")
    if fid:
        try:
            return await chat.send_photo(fid, caption=caption, reply_markup=kb)
        except Exception as e:
            logging.warning("QR file_id failed: %s", e)
    try:
        with open(QR_IMAGE, "rb") as f:
            return await chat.send_photo(f, caption=caption, reply_markup=kb)
    except FileNotFoundError:
        pass
    await chat.send_message(caption, reply_markup=kb)


# ---------------- force join / registration ----------------
def ensure_user(u, ref=None):
    if not user_row(u.id):
        if ref and not user_row(ref):
            ref = None
        q("INSERT INTO users(id,username,referred_by,joined) VALUES(?,?,?,?)",
          (u.id, u.username, ref, int(time.time())))
    else:
        q("UPDATE users SET username=? WHERE id=?", (u.username, u.id))


async def is_joined(bot, uid):
    for ch in get_channels():
        try:
            m = await bot.get_chat_member(ch["chat"], uid)
            if m.status in ("left", "kicked"):
                return False
        except Exception as e:
            logging.warning("Force-join check failed for %s: %s", ch["chat"], e)
    return True


def join_kb():
    rows = [[B(f"📢 Join {ch.get('title', 'Channel')}", url=ch["url"])]
            for ch in get_channels()]
    rows.append([B("✅ I've Joined", callback_data="check")])
    return M(rows)


async def give_welcome(update, ctx, uid):
    u = user_row(uid)
    if u["bonus_given"]:
        return
    q("UPDATE users SET bonus_given=1 WHERE id=?", (uid,))
    w = setting("welcome")
    if w > 0:
        add_bal(uid, w)
        await send(update, f"🎉 Welcome bonus {fmt(w)} added to your balance!")
    if u["referred_by"]:
        try:
            await ctx.bot.send_message(
                u["referred_by"], "👥 A new friend joined via your link! You will earn "
                f"{setting('refpct'):g}% of every deposit they make.")
        except Exception:
            pass


async def prepare(update, ctx):
    """Registers the user, blocks banned users, enforces force-join."""
    u = update.effective_user
    ref = None
    m = update.message
    if m and m.text and m.text.startswith("/start") and ctx.args:
        if ctx.args[0].isdigit() and int(ctx.args[0]) != u.id:
            ref = int(ctx.args[0])
    ensure_user(u, ref)
    if u.id not in ADMIN_IDS:
        if get_str("maintenance", "0") == "1":
            notice = get_str("maintenance_text", "🛠 Bot is under maintenance. Please try again later.")
            await send(update, notice)
            return False
        if user_row(u.id)["banned"]:
            await send(update, "🚫 You are banned from using this bot.")
            return False
        if not await is_joined(ctx.bot, u.id):
            await show(update, "🔒 Access Locked\n\nJoin all channels below, then tap "
                               "“I've Joined” to continue.", join_kb())
            return False
    await give_welcome(update, ctx, u.id)
    return True


# ---------------- user screens ----------------
async def show_home(update, ctx):
    u = update.effective_user
    bal = user_row(u.id)["balance"]
    welcome = get_str("welcome_text", "👋 Welcome, {name}!")
    try:
        welcome = welcome.replace("{name}", u.first_name).replace("{balance}", f"{CURRENCY}{bal:.2f}")
    except Exception:
        pass
    await show(update,
               f"{welcome}\n\n💼 Balance: {CURRENCY}{bal:.2f}\n\nChoose an option below 👇",
               home_kb(u.id))


async def show_account(update, ctx):
    u = user_row(update.effective_user.id)
    n = q("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
    spent = q("SELECT SUM(charge) s FROM orders WHERE user_id=?", (u["id"],)).fetchone()["s"] or 0
    await show(update, f"👤 My Account\n\n🆔 ID: {u['id']}\n"
                       f"💼 Balance: {CURRENCY}{u['balance']:.2f}\n"
                       f"📦 Orders: {n}\n💸 Total spent: {CURRENCY}{spent:.2f}",
               M([[B("💰 Add Funds", callback_data="fund")],
                  [B("🔙 Main Menu", callback_data="home")]]))


async def show_orders(update, ctx):
    rows = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
             (update.effective_user.id,)).fetchall()
    if not rows:
        return await show(update, "📦 You have no orders yet.", back_kb())
    text = "📦 Last orders\n\n" + "\n\n".join(
        f"#{r['id']} • {r['service_name'][:30]}\nQty {r['qty']} • "
        f"{CURRENCY}{r['charge']:.2f} • {r['status']}" for r in rows)
    await show(update, text, back_kb())


async def do_daily(update, ctx):
    d = setting("daily")
    if d <= 0:
        return await show(update, "🎁 Daily bonus is currently disabled.", back_kb())
    u = user_row(update.effective_user.id)
    wait = u["last_daily"] + 86400 - int(time.time())
    if wait > 0:
        return await show(update, f"⏳ Already claimed.\nCome back in "
                                  f"{wait // 3600}h {wait % 3600 // 60}m.", back_kb())
    q("UPDATE users SET last_daily=? WHERE id=?", (int(time.time()), u["id"]))
    add_bal(u["id"], d)
    await show(update, f"🎁 Daily bonus {fmt(d)} added to your balance!", back_kb())


async def show_refer(update, ctx):
    me = await ctx.bot.get_me()
    uid = update.effective_user.id
    cnt = q("SELECT COUNT(*) c FROM users WHERE referred_by=?", (uid,)).fetchone()["c"]
    earned = user_row(uid)["ref_earned"] or 0
    await show(update, f"👥 Refer & Earn\n\nEarn {setting('refpct'):g}% commission on every "
                       f"deposit your referrals make - forever!\n\n🔗 Your link:\n"
                       f"https://t.me/{me.username}?start={uid}\n\n"
                       f"👥 Referrals: {cnt}\n💰 Total earned: {CURRENCY}{earned:.2f}",
               back_kb())


# ---- service browsing ----
def categories():
    return [r[0] for r in q("SELECT DISTINCT category FROM services WHERE active=1 "
                            "ORDER BY category")]


async def show_cats(update, page):
    cats = categories()
    if not cats:
        return await show(update, "No services available yet.", back_kb())
    rows = [[B(c[:45], callback_data=f"cat_{i}_0")]
            for i, c in enumerate(cats) if page * PAGE <= i < (page + 1) * PAGE]
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", callback_data=f"cats_{page - 1}"))
    if (page + 1) * PAGE < len(cats):
        nav.append(B("Next ➡️", callback_data=f"cats_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([B("🔙 Main Menu", callback_data="home")])
    await show(update, "📂 Choose a category:", M(rows))


async def show_cat(update, i, page):
    cats = categories()
    if i >= len(cats):
        return await show_cats(update, 0)
    cat = cats[i]
    total = q("SELECT COUNT(*) c FROM services WHERE active=1 AND category=?",
              (cat,)).fetchone()["c"]
    rows = q("SELECT * FROM services WHERE active=1 AND category=? ORDER BY id "
             "LIMIT ? OFFSET ?", (cat, PAGE, page * PAGE)).fetchall()
    kb = [[B(f"{s['id']} • {s['name'][:28]} • {CURRENCY}{s['rate']:.2f}/1k",
             callback_data=f"svc_{s['id']}")] for s in rows]
    nav = []
    if page > 0:
        nav.append(B("⬅️ Prev", callback_data=f"cat_{i}_{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(B("Next ➡️", callback_data=f"cat_{i}_{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([B("🔙 Categories", callback_data="cats_0")])
    await show(update, f"📂 {cat}", M(kb))


# ---------------- order flow (state machine) ----------------
async def start_order(update, ctx, sid):
    s = q("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
    if not s:
        return await show_cats(update, 0)
    ctx.user_data["ord"] = {"svc": dict(s)}
    set_state(ctx, "o_link")
    await show(update, f"🛍 {s['name']}\n\n💲 Price: {CURRENCY}{s['rate']:.2f} per 1000\n"
                       f"📉 Min: {s['min']}  📈 Max: {s['max']}\n\n"
                       "🔗 Send the link now:", cancel_kb())


async def place_order(update, ctx):
    cq = update.callback_query
    uid, o = cq.from_user.id, ctx.user_data.pop("ord", None)
    if not o or "charge" not in o:
        return await show_home(update, ctx)
    s, charge = o["svc"], o["charge"]
    cur = q("UPDATE users SET balance=ROUND(balance-?,2) WHERE id=? AND balance>=?",
            (charge, uid, charge))
    if cur.rowcount == 0:
        return await show(update, "❌ Insufficient balance.",
                          M([[B("💰 Add Funds", callback_data="fund")],
                             [B("🔙 Main Menu", callback_data="home")]]))
    try:
        r = await api_async(action="add", service=s["id"], link=o["link"], quantity=o["qty"])
    except Exception as e:
        r = {"error": str(e)}
    if "order" in r:
        q("INSERT INTO orders(user_id,service_id,service_name,link,qty,charge,"
          "panel_order,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
          (uid, s["id"], s["name"], o["link"], o["qty"], charge, r["order"],
           "Pending", int(time.time())))
        await show(update, f"✅ Order placed!\n\n🆔 Order ID: {r['order']}\n"
                           f"💸 Charged: {CURRENCY}{charge}", back_kb())
    else:
        add_bal(uid, charge)
        for a in ADMIN_IDS:  # real reason goes to admin only (e.g. panel balance low)
            try:
                await ctx.bot.send_message(
                    a, f"⚠️ Order failed\nUser: {uid}\nService: {s['id']} - {s['name']}\n"
                       f"Reason: {r.get('error', r)}")
            except Exception:
                pass
        await show(update, "❌ Order failed.\nYour balance has been refunded.\n\n"
                           f"🆘 Any help? Contact admin: {support()}",
                   M([[B("🆘 Contact Admin", url=support_url())],
                      [B("🔙 Main Menu", callback_data="home")]]))


# ---------------- add funds ----------------
async def start_fund(update, ctx):
    if get_str("qr_enabled", "1") != "1":
        return await show(update, "💳 Add Funds is temporarily unavailable. Please contact support.", back_kb())
    set_state(ctx, "f_amt")
    await show(update, f"💰 Add Funds\n\nEnter the amount you want to add "
                       f"(minimum {fmt(setting('minfund'))}):", cancel_kb())


async def fund_screenshot(update, ctx):
    m, u = update.message, update.effective_user
    photo = m.photo[-1]
    amt = ctx.user_data.pop("fund_amt", None)
    ctx.user_data.pop("st", None)
    if not amt:
        return await send(update, "Session expired. Please start again.", back_kb())
    if q("SELECT 1 FROM funds WHERE proof_uid=?", (photo.file_unique_id,)).fetchone():
        return await send(update, "❌ This screenshot was already used.\n"
                                  "Duplicate or fake screenshots are not allowed.", back_kb())
    fid = q("INSERT INTO funds(user_id,amount,proof,created,proof_uid) VALUES(?,?,?,?,?)",
            (u.id, amt, "screenshot", int(time.time()), photo.file_unique_id)).lastrowid
    cap = (f"💰 Fund request #{fid}\n👤 User: {u.id} (@{u.username})\n"
           f"💵 Amount: {CURRENCY}{amt}")
    kb = M([[B("✅ Approve", callback_data=f"fa_{fid}")],
            [B("❌ Reject: Fake", callback_data=f"frf_{fid}"),
             B("❌ Reject: Already used", callback_data=f"fru_{fid}")]])
    for a in ADMIN_IDS:
        try:
            await ctx.bot.send_photo(a, photo.file_id, caption=cap, reply_markup=kb)
        except Exception as e:
            logging.warning("Admin notify failed: %s", e)
    await send(update, f"✅ Screenshot received!\nRequest #{fid} sent to admin.\n"
                       "Your balance will be added after approval.", back_kb())


async def fund_decision(update, ctx):
    cq = update.callback_query
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("Admin only", show_alert=True)
    act, fid = cq.data.split("_")
    fid = int(fid)
    new = "approved" if act == "fa" else "rejected"
    cur = q("UPDATE funds SET status=? WHERE id=? AND status='pending'", (new, fid))
    if cur.rowcount == 0:
        return await cq.answer("Already processed", show_alert=True)
    f = q("SELECT * FROM funds WHERE id=?", (fid,)).fetchone()
    if act == "fa":
        add_bal(f["user_id"], f["amount"])
        ref = user_row(f["user_id"])["referred_by"]
        pct = setting("refpct")
        if ref and pct > 0:
            com = round(f["amount"] * pct / 100, 2)
            if com > 0:
                add_bal(ref, com)
                q("UPDATE users SET ref_earned=ROUND(ref_earned+?,2) WHERE id=?", (com, ref))
                try:
                    await ctx.bot.send_message(
                        ref, f"💰 Referral commission {CURRENCY}{com:g} ({pct:g}%) "
                             "received from your friend's deposit!")
                except Exception:
                    pass
        note = f"✅ Payment approved! {CURRENCY}{f['amount']} added to your balance."
    elif act == "frf":
        note = (f"❌ Fund request #{fid} rejected: your payment screenshot is FAKE. "
                "Please do not send fake screenshots.")
    else:
        note = (f"❌ Fund request #{fid} rejected: this screenshot was ALREADY USED. "
                "Send a new valid payment screenshot.")
    try:
        await ctx.bot.send_message(f["user_id"], note)
    except Exception:
        pass
    await cq.answer(new.upper())
    label = {"fa": "APPROVED ✅", "frf": "REJECTED ❌ (Fake)",
             "fru": "REJECTED ❌ (Already used)"}[act]
    text = f"Fund #{fid} {label}\nUser {f['user_id']} • {CURRENCY}{f['amount']}"
    try:
        await cq.edit_message_caption(text)
    except Exception:
        await cq.edit_message_text(text)


# ---------------- redeem ----------------
async def do_redeem(update, ctx, code):
    code, uid = code.upper(), update.effective_user.id
    row = q("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
    if not row or row["uses_left"] <= 0:
        return await send(update, "❌ Invalid or expired code. Try again or cancel.",
                          cancel_kb())
    if q("SELECT 1 FROM redeemed WHERE code=? AND user_id=?", (code, uid)).fetchone():
        return await send(update, "❌ You already used this code.", back_kb())
    ctx.user_data.pop("st", None)
    q("INSERT INTO redeemed VALUES(?,?)", (code, uid))
    q("UPDATE codes SET uses_left=uses_left-1 WHERE code=?", (code,))
    add_bal(uid, row["amount"])
    await send(update, f"🎁 {fmt(row['amount'])} added to your balance!", back_kb())


# ================= ADMIN PANEL =================
PROMPTS = {
    "addbal": "➕ Add Balance\n\nSend: user_id amount\nExample: 123456789 100",
    "cutbal": "➖ Cut Balance\n\nSend: user_id amount\nExample: 123456789 50",
    "set_welcome": "🎉 Welcome Bonus\n\nSend the new amount (0 = off):",
    "set_daily": "🎁 Daily Bonus\n\nSend the new amount (0 = off):",
    "set_refpct": "👥 Referral Commission %\n\nSend the percent a referrer earns on every "
                  "deposit of their referrals.\nExample: 5 or 10  (0 = off)",
    "svc_cat": "➕ Add Service (1/6)\n\n📂 Send the CATEGORY name.\nExample: Instagram Followers",
    "svc_id": "➕ Add Service (2/6)\n\n🆔 Send the SERVICE ID (from your SMM panel):",
    "svc_name": "➕ Add Service (3/6)\n\n🏷 Send the SERVICE NAME users will see:",
    "svc_price": "➕ Add Service (4/6)\n\n💲 Send the SELLING PRICE per 1000.\nExample: 45",
    "svc_min": "➕ Add Service (5/6)\n\n📉 Send the MINIMUM quantity:",
    "svc_max": "➕ Add Service (6/6)\n\n📈 Send the MAXIMUM quantity:",
    "delsvc": "🗑 Delete Service\n\nSend the Service ID to remove:",
    "set_minfund": "💳 Minimum Fund\n\nSend the new minimum add-fund amount:",
    "giftall": "🎁 Gift All Users\n\nSend the amount to add to EVERY user:",
    "code": "🎟 Create Gift Code\n\nSend: CODE amount uses\nExample: WELCOME10 10 50",
    "api_url": "🌐 API URL\n\nSend your SMM panel API URL.\nExample: https://yourpanel.com/api/v2",
    "api_key": "🔑 API Key\n\nSend your SMM panel API key.\n(Your message is deleted right after, for safety.)",
    "qr": "🖼 Payment QR\n\nSend your payment QR image as a PHOTO:",
    "paytext": ("📝 Payment Text\n\nSend the text shown under the QR (UPI ID, notes).\n"
                "Example:\nUPI: yourupi@bank\nPay the exact amount."),
    "support": "🆘 Support Contact\n\nSend the admin/support Telegram username.\nExample: @myadmin",
    "ban": "🚫 Ban User\n\nSend the user ID:",
    "unban": "✅ Unban User\n\nSend the user ID:",
    "user": "🔎 User Info\n\nSend the user ID:",
    "bc": "📣 Broadcast\n\nSend the message to deliver to all users:",
    "addch": ("📢 Add Force Channel\n\nAdd the bot as ADMIN in the channel first, then send:\n"
              "• Public: @channelname\n"
              "• Private: -100xxxxxxxxxx https://t.me/+invitelink"),
    "welcome_text": ("👋 Home Welcome Message\n\nSend the welcome text. You can use {name} and {balance}."),
    "maintenance_text": "🛠 Maintenance Notice\n\nSend the message users should see while maintenance is ON:",
    "btn_order": "🛒 New Order button label — send the new label:",
    "btn_fund": "💰 Add Funds button label — send the new label:",
    "btn_account": "👤 Account button label — send the new label:",
    "btn_orders": "📦 My Orders button label — send the new label:",
    "btn_daily": "🎁 Daily Bonus button label — send the new label:",
    "btn_refer": "👥 Refer & Earn button label — send the new label:",
    "btn_redeem": "🎟 Redeem Code button label — send the new label:",
    "btn_support": "🆘 Support button label — send the new label:",
}


def admin_kb():
    c = CURRENCY
    return M([
        [B("📊 Stats", callback_data="adm_stats"), B("➕ Add Service", callback_data="adm_svc")],
        [B("🗑 Delete Service", callback_data="adm_delsvc"), B("📢 Force Channels", callback_data="adm_ch")],
        [B("➕ Add Balance", callback_data="adm_addbal"), B("➖ Cut Balance", callback_data="adm_cutbal")],
        [B(f"🎉 Welcome: {c}{setting('welcome'):g}", callback_data="adm_set_welcome"),
         B(f"🎁 Daily: {c}{setting('daily'):g}", callback_data="adm_set_daily")],
        [B(f"👥 Referral: {setting('refpct'):g}%", callback_data="adm_set_refpct"),
         B(f"💳 Min Fund: {c}{setting('minfund'):g}", callback_data="adm_set_minfund")],
        [B("🎟 Create Code", callback_data="adm_code"), B("🎁 Gift All", callback_data="adm_giftall")],
        [B("🚫 Ban", callback_data="adm_ban"), B("✅ Unban", callback_data="adm_unban")],
        [B("🔎 User Info", callback_data="adm_user"), B("📣 Broadcast", callback_data="adm_bc")],
        [B("🔌 API Settings", callback_data="adm_api"), B("💳 Payment & QR", callback_data="adm_pay")],
        [B(f"🆘 Support: {support()}"[:40], callback_data="adm_support"),
         B("⚙️ Advanced Settings", callback_data="adm_advanced")],
        [B("🏠 Main Menu", callback_data="home")],
    ])


async def show_admin(update, ctx):
    users = q("SELECT COUNT(*) c FROM users").fetchone()["c"]
    pend = q("SELECT COUNT(*) c FROM funds WHERE status='pending'").fetchone()["c"]
    await show(update, f"🛠 Admin Panel\n\n👥 Users: {users}\n⏳ Pending funds: {pend}\n"
                       "\nTap a setting to change it 👇", admin_kb())


async def show_advanced(update, ctx):
    maintenance = "ON 🔴" if get_str("maintenance", "0") == "1" else "OFF 🟢"
    qr_enabled = "ON 🟢" if get_str("qr_enabled", "1") == "1" else "OFF 🔴"
    await show(update,
        f"⚙️ Advanced Settings\n\n🛠 Maintenance: {maintenance}\n💳 QR Payments: {qr_enabled}\n"
        f"\nWelcome message: {get_str('welcome_text', '👋 Welcome, {name}!')}\n"
        f"Maintenance notice: {get_str('maintenance_text', '🛠 Bot is under maintenance. Please try again later.')}\n\n"
        "Choose what you want to configure:",
        M([[B("🔁 Toggle Maintenance", callback_data="adm_toggle_maintenance"),
            B("💳 Toggle QR Payments", callback_data="adm_toggle_qr")],
          [B("👋 Edit Welcome Message", callback_data="adm_welcome_text")],
          [B("📝 Edit Maintenance Notice", callback_data="adm_maintenance_text")],
          [B("🔘 Customize Menu Buttons", callback_data="adm_menu_labels")],
          [B("🔙 Admin Panel", callback_data="admin")]]))


async def show_menu_labels(update, ctx):
    rows = [[B(MENU_DEFAULTS[k], callback_data=f"adm_{k}")] for k in MENU_DEFAULTS]
    rows.append([B("🔙 Advanced Settings", callback_data="adm_advanced")])
    await show(update, "🔘 Menu Button Customization\n\nSelect a button to rename it:", M(rows))


async def show_channels(update, ctx):
    chs = get_channels()
    kb = [[B(f"❌ Remove: {c.get('title', c['chat'])}"[:55], callback_data=f"adm_rmch_{i}")]
          for i, c in enumerate(chs)]
    kb.append([B("➕ Add Channel", callback_data="adm_addch")])
    kb.append([B("🔙 Admin Panel", callback_data="admin")])
    text = "📢 Force Join Channels\n\n" + (
        "\n".join(f"{i + 1}. {c.get('title', c['chat'])}" for i, c in enumerate(chs))
        if chs else "No channels set. Force-join is OFF.")
    await show(update, text, M(kb))


async def admin_action(update, ctx, d):
    cq = update.callback_query
    if d == "adm_stats":
        g = lambda s: q(s).fetchone()[0] or 0
        return await show(update,
            f"📊 Stats\n\n👥 Users: {g('SELECT COUNT(*) FROM users')}\n"
            f"📦 Orders: {g('SELECT COUNT(*) FROM orders')}\n"
            f"💵 Revenue: {CURRENCY}{g('SELECT SUM(charge) FROM orders'):.2f}\n"
            f"💼 Total user balance: {CURRENCY}{g('SELECT SUM(balance) FROM users'):.2f}\n"
            f"🛍 Services: {g('SELECT COUNT(*) FROM services WHERE active=1')}",
            back_kb("🔙 Admin Panel", "admin"))
    if d == "adm_svc":
        ctx.user_data["newsvc"] = {}
        set_state(ctx, "adm:svc_cat")
        return await show(update, PROMPTS["svc_cat"], back_kb("❌ Cancel", "admin"))
    if d == "adm_svcok":
        n = ctx.user_data.pop("newsvc", None)
        if not n or "max" not in n:
            return await show_admin(update, ctx)
        q("INSERT OR REPLACE INTO services(id,name,category,rate,min,max,active) "
          "VALUES(?,?,?,?,?,?,1)",
          (n["id"], n["name"], n["category"], n["rate"], n["min"], n["max"]))
        return await show(update, f"✅ Service added!\n\n📂 {n['category']}\n"
                                  f"🆔 {n['id']} • {n['name']}\n💲 {CURRENCY}{n['rate']:g}/1000",
                          M([[B("➕ Add Another", callback_data="adm_svc")],
                             [B("🛠 Admin Panel", callback_data="admin")]]))
    if d == "adm_advanced":
        return await show_advanced(update, ctx)
    if d == "adm_menu_labels":
        return await show_menu_labels(update, ctx)
    if d == "adm_toggle_maintenance":
        new_value = "0" if get_str("maintenance", "0") == "1" else "1"
        set_setting("maintenance", new_value)
        await cq.answer("Maintenance " + ("enabled" if new_value == "1" else "disabled"))
        return await show_advanced(update, ctx)
    if d == "adm_toggle_qr":
        new_value = "0" if get_str("qr_enabled", "1") == "1" else "1"
        set_setting("qr_enabled", new_value)
        await cq.answer("QR payments " + ("enabled" if new_value == "1" else "disabled"))
        return await show_advanced(update, ctx)
    if d == "adm_api":
        return await show_api_menu(update, ctx)
    if d == "adm_apitest":
        await show(update, "⏳ Testing connection...")
        try:
            r = await api_async(action="balance")
            if isinstance(r, dict) and "balance" in r:
                text = f"✅ Connected!\n\n💰 Panel balance: {r['balance']} {r.get('currency', '')}"
            else:
                text = f"❌ Panel replied with an error:\n{r.get('error', r) if isinstance(r, dict) else r}"
        except Exception as e:
            text = f"❌ Connection failed:\n{e}"
        return await show(update, text, M([[B("🔌 API Settings", callback_data="adm_api")],
                                           [B("🛠 Admin Panel", callback_data="admin")]]))
    if d == "adm_pay":
        return await show_pay_menu(update, ctx)
    if d == "adm_payprev":
        await send_qr(update.effective_chat,
                      f"👁 Preview\n\n{get_str('payment_info', PAYMENT_INFO)}")
        return await send(update, "This is what users see.",
                          M([[B("💳 Payment Settings", callback_data="adm_pay")]]))
    if d == "adm_ch":
        return await show_channels(update, ctx)
    if d.startswith("adm_rmch_"):
        chs = get_channels()
        i = int(d.split("_")[2])
        if i < len(chs):
            chs.pop(i)
            save_channels(chs)
        return await show_channels(update, ctx)
    act = d[4:]
    if act in PROMPTS:
        set_state(ctx, f"adm:{act}")
        return await show(update, PROMPTS[act], back_kb("❌ Cancel", "admin"))


async def show_api_menu(update, ctx):
    await show(update, f"🔌 API Settings\n\n🌐 URL: {get_str('api_url', API_URL)}\n"
                       f"🔑 Key: {mask(get_str('api_key', API_KEY))}\n\n"
                       "Used to place orders and sync order status.",
               M([[B("🌐 Set API URL", callback_data="adm_api_url"),
                   B("🔑 Set API Key", callback_data="adm_api_key")],
                  [B("🧪 Test Connection", callback_data="adm_apitest")],
                  [B("🔙 Admin Panel", callback_data="admin")]]))


async def show_pay_menu(update, ctx):
    if get_str("qr_file_id"):
        qr = "✅ Uploaded"
    elif os.path.exists(QR_IMAGE):
        qr = f"📁 Using file {QR_IMAGE}"
    else:
        qr = "❌ Not set"
    enabled = "ENABLED 🟢" if get_str("qr_enabled", "1") == "1" else "DISABLED 🔴"
    await show(update, f"💳 Payment Settings\n\n🖼 QR: {qr}\n💳 QR payments: {enabled}\n\n📝 Payment text:\n"
                       f"{get_str('payment_info', PAYMENT_INFO)}",
               M([[B("🖼 Set QR", callback_data="adm_qr"),
                   B("📝 Set Payment Text", callback_data="adm_paytext")],
                  [B("🔁 Enable/Disable QR Payments", callback_data="adm_toggle_qr")],
                  [B("👁 Preview", callback_data="adm_payprev")],
                  [B("🔙 Admin Panel", callback_data="admin")]]))


async def broadcast_task(bot, admin_chat, text):
    ok = 0
    for r in q("SELECT id FROM users WHERE banned=0").fetchall():
        try:
            await bot.send_message(r["id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await bot.send_message(admin_chat, f"📣 Broadcast finished: delivered to {ok} users.")


async def admin_input(update, ctx, act, text):
    done = back_kb("🛠 Admin Panel", "admin")
    retry = back_kb("❌ Cancel", "admin")

    async def err(t):
        await send(update, f"⚠️ {t}\n\nTry again:", retry)

    def ok():
        ctx.user_data.pop("st", None)

    async def step(nxt):
        set_state(ctx, f"adm:{nxt}")
        await send(update, PROMPTS[nxt], retry)

    parts = text.split()
    try:
        if act in ("addbal", "cutbal"):
            uid, amt = int(parts[0]), float(parts[1])
            row = user_row(uid)
            if not row or amt <= 0:
                return await err("User not found or invalid amount.")
            if act == "cutbal":
                if amt > row["balance"]:
                    return await err(f"User only has {CURRENCY}{row['balance']:.2f}.")
                add_bal(uid, -amt)
                note = f"➖ {CURRENCY}{amt:g} was deducted from your balance."
            else:
                add_bal(uid, amt)
                note = f"💰 {CURRENCY}{amt:g} was added to your balance."
            ok()
            try:
                await ctx.bot.send_message(uid, note)
            except Exception:
                pass
            return await send(update, f"✅ Done. User {uid} new balance: "
                                      f"{CURRENCY}{user_row(uid)['balance']:.2f}", done)
        if act.startswith("set_"):
            val = float(parts[0])
            key = act[4:]
            if val < 0 or (key == "refpct" and val > 100):
                return await err("Invalid value." if key != "refpct" else "Percent must be 0-100.")
            set_setting(key, val)
            ok()
            if key == "refpct":
                msg = f"✅ Referral commission set to {val:g}%"
            else:
                names = {"welcome": "Welcome bonus", "daily": "Daily bonus",
                         "minfund": "Minimum fund"}
                msg = f"✅ {names[key]} set to {fmt(val)}"
            return await send(update, msg, done)
        if act == "svc_cat":
            if not (2 <= len(text) <= 60):
                return await err("Category name must be 2-60 characters.")
            ctx.user_data["newsvc"] = {"category": text}
            return await step("svc_id")
        if act.startswith("svc_") or act == "delsvc":
            n = ctx.user_data.get("newsvc")
            if act != "delsvc" and not n:
                ok()
                return await send(update, "⚠️ Session expired. Start again.", done)
            if act == "svc_id":
                sid = int(parts[0])
                if sid <= 0:
                    return await err("Invalid service ID.")
                n["id"] = sid
                return await step("svc_name")
            if act == "svc_name":
                if not (2 <= len(text) <= 100):
                    return await err("Name must be 2-100 characters.")
                n["name"] = text
                return await step("svc_price")
            if act == "svc_price":
                price = float(parts[0])
                if price <= 0:
                    return await err("Price must be greater than 0.")
                n["rate"] = price
                return await step("svc_min")
            if act == "svc_min":
                mn = int(parts[0])
                if mn <= 0:
                    return await err("Minimum must be greater than 0.")
                n["min"] = mn
                return await step("svc_max")
            if act == "svc_max":
                mx = int(parts[0])
                if mx < n["min"]:
                    return await err(f"Maximum must be at least {n['min']}.")
                n["max"] = mx
                ok()
                exists = q("SELECT 1 FROM services WHERE id=?", (n["id"],)).fetchone()
                note = "\n\n⚠️ This ID already exists and will be replaced." if exists else ""
                return await send(update,
                    f"🧾 Review Service\n\n📂 Category: {n['category']}\n🆔 Service ID: {n['id']}\n"
                    f"🏷 Name: {n['name']}\n💲 Price/1000: {CURRENCY}{n['rate']:g}\n"
                    f"📉 Min: {n['min']}   📈 Max: {n['max']}{note}",
                    M([[B("✅ Submit", callback_data="adm_svcok"),
                        B("❌ Cancel", callback_data="admin")]]))
            if act == "delsvc":
                sid = int(parts[0])
                if q("DELETE FROM services WHERE id=?", (sid,)).rowcount == 0:
                    return await err("Service not found.")
                ok()
                return await send(update, f"🗑 Service {sid} deleted.", done)
        if act == "giftall":
            amt = float(parts[0])
            if amt <= 0:
                return await err("Amount must be positive.")
            q("UPDATE users SET balance=ROUND(balance+?,2) WHERE banned=0", (amt,))
            ok()
            return await send(update, f"✅ Gifted {fmt(amt)} to all users.", done)
        if act == "code":
            code, amt, uses = parts[0].upper(), float(parts[1]), int(parts[2])
            q("INSERT OR REPLACE INTO codes VALUES(?,?,?)", (code, amt, uses))
            ok()
            return await send(update, f"✅ Code {code}: {fmt(amt)}, {uses} uses.", done)
        if act in ("ban", "unban"):
            uid = int(parts[0])
            if not user_row(uid):
                return await err("User not found.")
            q("UPDATE users SET banned=? WHERE id=?", (1 if act == "ban" else 0, uid))
            ok()
            return await send(update, f"✅ User {uid} {'banned' if act == 'ban' else 'unbanned'}.", done)
        if act == "user":
            u = user_row(int(parts[0]))
            if not u:
                return await err("User not found.")
            n = q("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
            ok()
            return await send(update, f"🔎 User {u['id']} (@{u['username']})\n"
                                      f"Balance: {CURRENCY}{u['balance']:.2f}\nOrders: {n}\n"
                                      f"Banned: {bool(u['banned'])}\nReferred by: {u['referred_by']}",
                              done)
        if act == "api_url":
            if not parts[0].lower().startswith(("http://", "https://")):
                return await err("URL must start with http:// or https://")
            set_setting("api_url", parts[0])
            ok()
            return await send(update, "✅ API URL saved.",
                              M([[B("🧪 Test Connection", callback_data="adm_apitest")],
                                 [B("🔌 API Settings", callback_data="adm_api")]]))
        if act == "api_key":
            try:
                await update.message.delete()
            except Exception:
                pass
            set_setting("api_key", parts[0])
            ok()
            return await send(update, "✅ API key saved (your message was deleted for safety).",
                              M([[B("🧪 Test Connection", callback_data="adm_apitest")],
                                 [B("🔌 API Settings", callback_data="adm_api")]]))
        if act == "paytext":
            set_setting("payment_info", text)
            ok()
            return await send(update, "✅ Payment text updated.",
                              M([[B("👁 Preview", callback_data="adm_payprev")],
                                 [B("💳 Payment Settings", callback_data="adm_pay")]]))
        if act in ("welcome_text", "maintenance_text") or act in MENU_DEFAULTS:
            if not text or len(text) > 180:
                return await err("Text must be between 1 and 180 characters.")
            set_setting(act, text)
            ok()
            if act in MENU_DEFAULTS:
                return await send(update, "✅ Menu button label updated.",
                                  M([[B("🔘 More Menu Buttons", callback_data="adm_menu_labels")],
                                     [B("⚙️ Advanced Settings", callback_data="adm_advanced")]]))
            return await send(update, "✅ Text updated successfully.",
                              M([[B("⚙️ Advanced Settings", callback_data="adm_advanced")]]))
        if act == "support":
            u_ = parts[0].replace("https://t.me/", "").lstrip("@")
            if len(u_) < 4 or not u_.replace("_", "").isalnum():
                return await err("Send a valid Telegram username.")
            set_setting("support", "@" + u_)
            ok()
            return await send(update, f"✅ Support contact set to @{u_}", done)
        if act == "bc":
            ok()
            ctx.application.create_task(
                broadcast_task(ctx.bot, update.effective_chat.id, text))
            return await send(update, "📣 Broadcast started...", done)
        if act == "addch":
            raw = parts[0]
            if "t.me/" in raw:
                tail = raw.split("t.me/")[1].strip("/")
                if tail.startswith("+") or tail.startswith("joinchat"):
                    return await err("For private channels send: -100xxxxxxxxxx invite_link")
                raw = "@" + tail
            ref = int(raw) if raw.lstrip("-").isdigit() else raw
            try:
                c = await ctx.bot.get_chat(ref)
                me = await ctx.bot.get_chat_member(c.id, ctx.bot.id)
            except Exception as e:
                return await err(f"Cannot access channel: {e}")
            if me.status not in ("administrator", "creator"):
                return await err("Bot is not admin in that channel. Make it admin first.")
            url = parts[1] if len(parts) > 1 else (
                f"https://t.me/{c.username}" if c.username else None)
            if not url:
                return await err("Private channel needs an invite link after the ID.")
            chs = get_channels()
            if any(x["chat"] == c.id for x in chs):
                return await err("Channel already added.")
            chs.append({"chat": c.id, "title": c.title, "url": url})
            save_channels(chs)
            ok()
            return await send(update, f"✅ Channel added: {c.title}",
                              M([[B("📢 Channels", callback_data="adm_ch")],
                                 [B("🛠 Admin Panel", callback_data="admin")]]))
    except (IndexError, ValueError, KeyError):
        return await err("Invalid format.")


# ================= ROUTERS =================
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    d, uid = cq.data, cq.from_user.id

    if d.startswith(("fa_", "frf_", "fru_")):
        return await fund_decision(update, ctx)

    if d == "check":
        ensure_user(cq.from_user)
        if await is_joined(ctx.bot, uid):
            await cq.answer("✅ Verified!")
            await give_welcome(update, ctx, uid)
            return await show_home(update, ctx)
        return await cq.answer("❌ Please join all channels first, then tap again.",
                               show_alert=True)

    await cq.answer()
    if not await prepare(update, ctx):
        return
    ctx.user_data.pop("st", None)

    if d == "home":
        return await show_home(update, ctx)
    if d == "order":
        return await show_cats(update, 0)
    if d.startswith("cats_"):
        return await show_cats(update, int(d.split("_")[1]))
    if d.startswith("cat_"):
        _, i, p = d.split("_")
        return await show_cat(update, int(i), int(p))
    if d.startswith("svc_"):
        return await start_order(update, ctx, int(d.split("_")[1]))
    if d == "oc_yes":
        return await place_order(update, ctx)
    if d == "oc_no":
        ctx.user_data.pop("ord", None)
        return await show_home(update, ctx)
    if d == "fund":
        return await start_fund(update, ctx)
    if d == "acc":
        return await show_account(update, ctx)
    if d == "orders":
        return await show_orders(update, ctx)
    if d == "daily":
        return await do_daily(update, ctx)
    if d == "refer":
        return await show_refer(update, ctx)
    if d == "redeem":
        set_state(ctx, "redeem")
        return await show(update, "🎟 Redeem Code\n\nSend your gift code:", cancel_kb())

    if d == "admin" or d.startswith("adm_"):
        if uid not in ADMIN_IDS:
            return
        if d == "admin":
            return await show_admin(update, ctx)
        return await admin_action(update, ctx, d)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await prepare(update, ctx):
        return
    m = update.message
    text = (m.text or "").strip()
    st = ctx.user_data.get("st")
    uid = update.effective_user.id

    if st == "adm:qr" and uid in ADMIN_IDS:
        if m.photo:
            set_setting("qr_file_id", m.photo[-1].file_id)
            ctx.user_data.pop("st", None)
            return await send(update, "✅ Payment QR updated!",
                              M([[B("👁 Preview", callback_data="adm_payprev")],
                                 [B("💳 Payment Settings", callback_data="adm_pay")]]))
        return await send(update, "📸 Please send the QR as a photo.",
                          back_kb("❌ Cancel", "admin"))

    if st == "o_link":
        if len(text) < 4 or " " in text:
            return await send(update, "❌ Invalid link. Send again:", cancel_kb())
        ctx.user_data["ord"]["link"] = text
        set_state(ctx, "o_qty")
        s = ctx.user_data["ord"]["svc"]
        return await send(update, f"🔢 Send quantity ({s['min']} - {s['max']}):", cancel_kb())

    if st == "o_qty":
        o = ctx.user_data["ord"]
        s = o["svc"]
        if not text.isdigit() or not (s["min"] <= int(text) <= s["max"]):
            return await send(update, f"❌ Enter a number between {s['min']} and {s['max']}:",
                              cancel_kb())
        o["qty"] = int(text)
        o["charge"] = round(s["rate"] * o["qty"] / 1000, 2)
        ctx.user_data.pop("st", None)
        bal = user_row(uid)["balance"]
        return await send(update,
            f"🧾 Order Summary\n\n🛍 {s['name']}\n🔗 {o['link']}\n🔢 Quantity: {o['qty']}\n"
            f"💸 Charge: {CURRENCY}{o['charge']}\n💼 Your balance: {CURRENCY}{bal:.2f}",
            M([[B("✅ Confirm", callback_data="oc_yes"), B("❌ Cancel", callback_data="oc_no")]]))

    if st == "f_amt":
        try:
            amt = float(text)
            assert amt >= setting("minfund")
        except Exception:
            return await send(update, f"❌ Enter a valid amount (min {fmt(setting('minfund'))}):",
                              cancel_kb())
        ctx.user_data["fund_amt"] = amt
        set_state(ctx, "f_shot")
        caption = (f"💰 Amount to pay: {CURRENCY}{amt:g}\n\n"
                   f"{get_str('payment_info', PAYMENT_INFO)}\n\n"
                   "📸 After payment, send the payment SCREENSHOT here.")
        return await send_qr(update.effective_chat, caption, cancel_kb())

    if st == "f_shot":
        if m.photo:
            return await fund_screenshot(update, ctx)
        return await send(update, "📸 Please send the payment SCREENSHOT as a photo.",
                          cancel_kb())

    if st == "redeem" and text:
        return await do_redeem(update, ctx, text)

    if st and st.startswith("adm:") and uid in ADMIN_IDS and text:
        return await admin_input(update, ctx, st[4:], text)

    await show_home(update, ctx)


async def start(update, ctx):
    if await prepare(update, ctx):
        ctx.user_data.pop("st", None)
        await show_home(update, ctx)


async def cancel(update, ctx):
    ctx.user_data.pop("st", None)
    if await prepare(update, ctx):
        await show_home(update, ctx)


async def admin_cmd(update, ctx):
    if update.effective_user.id not in ADMIN_IDS:
        return
    ensure_user(update.effective_user)
    ctx.user_data.pop("st", None)
    await show_admin(update, ctx)


# ---------------- background: order status sync + auto refund ----------------
async def sync_orders(ctx: ContextTypes.DEFAULT_TYPE):
    rows = q("SELECT * FROM orders WHERE status IN ('Pending','In progress','Processing') "
             "LIMIT 100").fetchall()
    if not rows:
        return
    ids = ",".join(str(r["panel_order"]) for r in rows)
    try:
        res = await api_async(action="orders", orders=ids)
    except Exception as e:
        return logging.warning("sync failed: %s", e)
    for r in rows:
        d = res.get(str(r["panel_order"]))
        if not isinstance(d, dict) or "status" not in d or d["status"] == r["status"]:
            continue
        st = d["status"]
        q("UPDATE orders SET status=? WHERE id=?", (st, r["id"]))
        refund = 0.0
        if st == "Canceled":
            refund = r["charge"]
        elif st == "Partial":
            remains = int(float(d.get("remains") or 0))
            refund = round(r["charge"] * remains / r["qty"], 2)
        if refund > 0:
            add_bal(r["user_id"], refund)
        if st in ("Completed", "Canceled", "Partial"):
            note = f"📦 Order #{r['id']} is {st}."
            if refund > 0:
                note += f" {CURRENCY}{refund} refunded."
            try:
                await ctx.bot.send_message(r["user_id"], note)
            except Exception:
                pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Render Environment Variables.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(
        ((filters.TEXT & ~filters.COMMAND) | filters.PHOTO) & filters.ChatType.PRIVATE,
        on_message))
    app.job_queue.run_repeating(sync_orders, interval=300, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
