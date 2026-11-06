"""
SMM Panel Telegram Bot (PRO)
Install: pip install "python-telegram-bot[job-queue]==20.7" requests
Run:     python smm_bot_pro.py

Features: force-join channels, service import from panel API (with markup),
order flow (service > link > quantity), add funds + admin approve/reject,
welcome / daily / referral / gift-code bonuses, admin add/cut balance,
ban/unban, broadcast, stats, auto order-status sync with auto refund.
"""
import asyncio, functools, logging, sqlite3, time
import requests
from telegram import (Update, InlineKeyboardButton as Btn,
                      InlineKeyboardMarkup as Markup, ReplyKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ConversationHandler,
                          ContextTypes, filters)

# ===================== CONFIG =====================
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
API_URL = "https://yourpanel.com/api/v2"
API_KEY = "YOUR_SMM_PANEL_API_KEY"
ADMIN_IDS = [123456789]
FORCE_CHANNELS = ["@channel1", "@channel2"]   # bot must be ADMIN in these. [] = off
CURRENCY = "₹"
RATE_MULTIPLIER = 1.0     # panel USD -> your currency (e.g. 90). 1.0 = no change
DEFAULT_MARKUP = 20       # percent profit on top of panel rate (change: /setmarkup)
WELCOME_BONUS = 5.0       # one-time free balance after joining channels
REFERRAL_BONUS = 3.0      # given to referrer when new user joins
DAILY_BONUS = 1.0         # free bonus every 24h
MIN_FUND = 50.0
SUPPORT = "@your_support_username"
QR_IMAGE = "qr.png"       # put your payment QR image in the same folder as this file
PAYMENT_INFO = ("💳 Scan the QR and pay the exact amount.\n"
                "UPI: yourupi@bank\n\n"
                "📸 After payment, send the payment SCREENSHOT here.")
DB_FILE = "bot.db"
# ==================================================

logging.basicConfig(level=logging.INFO)
PAGE = 8
MENU = [["🛒 New Order", "💰 Add Funds"], ["👤 Account", "📦 My Orders"],
        ["🎁 Free Bonus", "👥 Refer & Earn"], ["🆘 Support"]]
MENU_RE = ("^(🛒 New Order|💰 Add Funds|👤 Account|📦 My Orders|"
           "🎁 Free Bonus|👥 Refer & Earn|🆘 Support)$")
TXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_RE)
LINK, QTY, CONFIRM = range(3)
F_AMOUNT, F_PROOF = range(10, 12)

# ---------------- database ----------------
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT,
  balance REAL DEFAULT 0, banned INTEGER DEFAULT 0, referred_by INTEGER,
  bonus_given INTEGER DEFAULT 0, last_daily INTEGER DEFAULT 0, joined INTEGER);
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
try:  # for databases created by the older version
    db.execute("ALTER TABLE funds ADD COLUMN proof_uid TEXT")
    db.commit()
except sqlite3.OperationalError:
    pass


def q(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get_markup():
    r = q("SELECT v FROM settings WHERE k='markup'").fetchone()
    return float(r["v"]) if r else DEFAULT_MARKUP


def sell_rate(panel_rate):
    return round(panel_rate * RATE_MULTIPLIER * (1 + get_markup() / 100), 4)


def add_bal(uid, amt):
    q("UPDATE users SET balance=ROUND(balance+?,2) WHERE id=?", (amt, uid))


def user_row(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


# ---------------- panel API ----------------
def api(**params):
    params["key"] = API_KEY
    r = requests.post(API_URL, data=params, timeout=40)
    r.raise_for_status()
    return r.json()


async def api_async(**params):
    return await asyncio.to_thread(api, **params)


# ---------------- helpers ----------------
async def send(update, text, markup=None):
    await update.effective_chat.send_message(text, reply_markup=markup)


async def show(update, text, markup=None):
    cq = update.callback_query
    if cq:
        try:
            return await cq.edit_message_text(text, reply_markup=markup)
        except Exception:
            pass
    await send(update, text, markup)


def main_menu():
    return ReplyKeyboardMarkup(MENU, resize_keyboard=True)


async def is_joined(bot, uid):
    for ch in FORCE_CHANNELS:
        try:
            m = await bot.get_chat_member(ch, uid)
            if m.status in ("left", "kicked"):
                return False
        except Exception as e:
            logging.warning("Force-join check failed for %s: %s", ch, e)
    return True


def join_markup():
    rows = [[Btn(f"📢 Join {c}", url=f"https://t.me/{c.lstrip('@')}")]
            for c in FORCE_CHANNELS]
    rows.append([Btn("✅ I Joined", callback_data="checkjoin")])
    return Markup(rows)


async def give_welcome(update, ctx, uid):
    u = user_row(uid)
    if u["bonus_given"]:
        return
    q("UPDATE users SET bonus_given=1 WHERE id=?", (uid,))
    if WELCOME_BONUS:
        add_bal(uid, WELCOME_BONUS)
        await send(update, f"🎉 Welcome bonus {CURRENCY}{WELCOME_BONUS} added!")
    ref = u["referred_by"]
    if ref and REFERRAL_BONUS:
        add_bal(ref, REFERRAL_BONUS)
        try:
            await ctx.bot.send_message(
                ref, f"👥 New referral joined! {CURRENCY}{REFERRAL_BONUS} added.")
        except Exception:
            pass


def guard(fn):
    """Registers user, blocks banned users, enforces force-join."""
    @functools.wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        ref = None
        msg = update.message
        if msg and msg.text and msg.text.startswith("/start") and ctx.args:
            if ctx.args[0].isdigit() and int(ctx.args[0]) != u.id:
                ref = int(ctx.args[0])
        if not user_row(u.id):
            if ref and not user_row(ref):
                ref = None
            q("INSERT INTO users(id,username,referred_by,joined) VALUES(?,?,?,?)",
              (u.id, u.username, ref, int(time.time())))
        row = user_row(u.id)
        if row["banned"]:
            await send(update, "🚫 You are banned.")
            return
        if not await is_joined(ctx.bot, u.id):
            if update.callback_query:
                await update.callback_query.answer()
            await send(update, "⚠️ Join our channels to use the bot:", join_markup())
            return
        await give_welcome(update, ctx, u.id)
        return await fn(update, ctx)
    return wrapper


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update, ctx):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, ctx)
    return wrapper


# ---------------- basic handlers ----------------
@guard
async def start(update, ctx):
    await send(update, "👋 Welcome to SMM Panel Bot!\nChoose an option below.",
               main_menu())


async def check_join_cb(update, ctx):
    cq = update.callback_query
    if await is_joined(ctx.bot, cq.from_user.id):
        await cq.answer("Verified ✅")
        if not user_row(cq.from_user.id):
            q("INSERT INTO users(id,username,joined) VALUES(?,?,?)",
              (cq.from_user.id, cq.from_user.username, int(time.time())))
        await give_welcome(update, ctx, cq.from_user.id)
        await send(update, "✅ Verified! Use the menu below.", main_menu())
    else:
        await cq.answer("❌ You haven't joined all channels.", show_alert=True)


@guard
async def account(update, ctx):
    u = user_row(update.effective_user.id)
    n = q("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
    await send(update, f"👤 Account\n\nID: {u['id']}\n"
                       f"Balance: {CURRENCY}{u['balance']:.2f}\nTotal orders: {n}")


@guard
async def support(update, ctx):
    await send(update, f"🆘 Support: {SUPPORT}")


@guard
async def refer(update, ctx):
    me = await ctx.bot.get_me()
    uid = update.effective_user.id
    cnt = q("SELECT COUNT(*) c FROM users WHERE referred_by=?", (uid,)).fetchone()["c"]
    await send(update, f"👥 Refer & Earn {CURRENCY}{REFERRAL_BONUS} per friend!\n\n"
                       f"Your link:\nhttps://t.me/{me.username}?start={uid}\n\n"
                       f"Referrals: {cnt}")


@guard
async def daily(update, ctx):
    u = user_row(update.effective_user.id)
    wait = u["last_daily"] + 86400 - int(time.time())
    if wait > 0:
        return await send(update, f"⏳ Come back in {wait // 3600}h {wait % 3600 // 60}m.")
    q("UPDATE users SET last_daily=? WHERE id=?", (int(time.time()), u["id"]))
    add_bal(u["id"], DAILY_BONUS)
    await send(update, f"🎁 Daily bonus {CURRENCY}{DAILY_BONUS} added!\n"
                       "Have a gift code? Use /redeem CODE")


@guard
async def redeem(update, ctx):
    if not ctx.args:
        return await send(update, "Usage: /redeem CODE")
    code = ctx.args[0].upper()
    uid = update.effective_user.id
    row = q("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
    if not row or row["uses_left"] <= 0:
        return await send(update, "❌ Invalid or expired code.")
    if q("SELECT 1 FROM redeemed WHERE code=? AND user_id=?", (code, uid)).fetchone():
        return await send(update, "❌ You already used this code.")
    q("INSERT INTO redeemed VALUES(?,?)", (code, uid))
    q("UPDATE codes SET uses_left=uses_left-1 WHERE code=?", (code,))
    add_bal(uid, row["amount"])
    await send(update, f"🎁 {CURRENCY}{row['amount']} added to your balance!")


@guard
async def my_orders(update, ctx):
    rows = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
             (update.effective_user.id,)).fetchall()
    if not rows:
        return await send(update, "No orders yet.")
    await send(update, "\n\n".join(
        f"#{r['id']} • {r['service_name'][:30]}\nQty {r['qty']} • "
        f"{CURRENCY}{r['charge']:.2f} • {r['status']}" for r in rows))


# ---------------- service browsing ----------------
def categories():
    return [r[0] for r in q("SELECT DISTINCT category FROM services WHERE active=1 "
                            "ORDER BY category")]


def nav_row(prefix, page, total, suffix=""):
    row = []
    if page > 0:
        row.append(Btn("⬅️", callback_data=f"{prefix}{page - 1}{suffix}"))
    if (page + 1) * PAGE < total:
        row.append(Btn("➡️", callback_data=f"{prefix}{page + 1}{suffix}"))
    return row


@guard
async def new_order(update, ctx):
    await show_cats(update, 0)


async def show_cats(update, page):
    cats = categories()
    if not cats:
        return await show(update, "No services available yet.")
    rows = [[Btn(c[:45], callback_data=f"cat_{i}_0")]
            for i, c in enumerate(cats) if page * PAGE <= i < (page + 1) * PAGE]
    nav = nav_row("cats_", page, len(cats))
    if nav:
        rows.append(nav)
    await show(update, "📂 Choose a category:", Markup(rows))


@guard
async def cb_cats(update, ctx):
    await update.callback_query.answer()
    await show_cats(update, int(update.callback_query.data.split("_")[1]))


@guard
async def cb_cat(update, ctx):
    await update.callback_query.answer()
    _, i, page = update.callback_query.data.split("_")
    i, page = int(i), int(page)
    cats = categories()
    if i >= len(cats):
        return
    cat = cats[i]
    total = q("SELECT COUNT(*) c FROM services WHERE active=1 AND category=?",
              (cat,)).fetchone()["c"]
    rows = q("SELECT * FROM services WHERE active=1 AND category=? ORDER BY id "
             "LIMIT ? OFFSET ?", (cat, PAGE, page * PAGE)).fetchall()
    kb = [[Btn(f"{s['id']} • {s['name'][:28]} • {CURRENCY}{sell_rate(s['rate']):.2f}/1k",
               callback_data=f"svc_{s['id']}")] for s in rows]
    nav = nav_row("cat_", page, total, "")
    nav = [Btn(b.text, callback_data=f"cat_{i}_{b.callback_data[4:]}") for b in nav]
    if nav:
        kb.append(nav)
    kb.append([Btn("🔙 Categories", callback_data="cats_0")])
    await show(update, f"📂 {cat}", Markup(kb))


# ---------------- order conversation ----------------
@guard
async def svc_selected(update, ctx):
    cq = update.callback_query
    await cq.answer()
    s = q("SELECT * FROM services WHERE id=?", (int(cq.data.split("_")[1]),)).fetchone()
    if not s:
        return
    ctx.user_data["svc"] = dict(s)
    await send(update, f"🛍 {s['name']}\n\nPrice: {CURRENCY}{sell_rate(s['rate']):.2f} per 1000\n"
                       f"Min: {s['min']} • Max: {s['max']}\n\n"
                       "🔗 Send the link (or /cancel):")
    return LINK


async def got_link(update, ctx):
    link = update.message.text.strip()
    if len(link) < 4 or " " in link:
        await send(update, "❌ Invalid link. Send again:")
        return LINK
    ctx.user_data["link"] = link
    s = ctx.user_data["svc"]
    await send(update, f"🔢 Send quantity ({s['min']} - {s['max']}):")
    return QTY


async def got_qty(update, ctx):
    t = update.message.text.strip()
    s = ctx.user_data["svc"]
    if not t.isdigit() or not (s["min"] <= int(t) <= s["max"]):
        await send(update, f"❌ Enter a number between {s['min']} and {s['max']}:")
        return QTY
    qty = int(t)
    charge = round(sell_rate(s["rate"]) * qty / 1000, 2)
    ctx.user_data.update(qty=qty, charge=charge)
    bal = user_row(update.effective_user.id)["balance"]
    await send(update,
               f"🧾 Order Summary\n\nService: {s['name']}\nLink: {ctx.user_data['link']}\n"
               f"Quantity: {qty}\nCharge: {CURRENCY}{charge}\nYour balance: {CURRENCY}{bal:.2f}",
               Markup([[Btn("✅ Confirm", callback_data="ok"),
                        Btn("❌ Cancel", callback_data="no")]]))
    return CONFIRM


async def confirm_order(update, ctx):
    cq = update.callback_query
    await cq.answer()
    if cq.data == "no":
        await cq.edit_message_text("❌ Order cancelled.")
        return ConversationHandler.END
    uid, d = cq.from_user.id, ctx.user_data
    s, charge = d["svc"], d["charge"]
    cur = q("UPDATE users SET balance=ROUND(balance-?,2) WHERE id=? AND balance>=?",
            (charge, uid, charge))
    if cur.rowcount == 0:
        await cq.edit_message_text("❌ Insufficient balance. Tap 💰 Add Funds.")
        return ConversationHandler.END
    try:
        r = await api_async(action="add", service=s["id"], link=d["link"],
                            quantity=d["qty"])
    except Exception as e:
        r = {"error": str(e)}
    if "order" in r:
        q("INSERT INTO orders(user_id,service_id,service_name,link,qty,charge,"
          "panel_order,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
          (uid, s["id"], s["name"], d["link"], d["qty"], charge, r["order"],
           "Pending", int(time.time())))
        await cq.edit_message_text(f"✅ Order placed!\nPanel ID: {r['order']}\n"
                                   f"Charged: {CURRENCY}{charge}")
    else:
        add_bal(uid, charge)  # refund
        await cq.edit_message_text(f"❌ Order failed: {r.get('error', r)}\n"
                                   "Your balance was refunded.")
    ctx.user_data.clear()
    return ConversationHandler.END


async def conv_cancel(update, ctx):
    ctx.user_data.clear()
    await send(update, "❌ Cancelled. Tap a menu button again.", main_menu())
    return ConversationHandler.END


# ---------------- add funds conversation ----------------
@guard
async def fund_start(update, ctx):
    await send(update, f"💰 Enter amount to add (min {CURRENCY}{MIN_FUND}) or /cancel:")
    return F_AMOUNT


async def fund_amount(update, ctx):
    try:
        amt = float(update.message.text.strip())
        assert amt >= MIN_FUND
    except Exception:
        await send(update, f"❌ Enter a valid amount (min {MIN_FUND}):")
        return F_AMOUNT
    ctx.user_data["fund_amt"] = amt
    caption = f"💰 Amount to pay: {CURRENCY}{amt}\n\n{PAYMENT_INFO}\n\n(Cancel: /cancel)"
    try:
        with open(QR_IMAGE, "rb") as f:
            await update.effective_chat.send_photo(f, caption=caption)
    except FileNotFoundError:
        logging.warning("QR image %s not found", QR_IMAGE)
        await send(update, caption)
    return F_PROOF


async def fund_need_photo(update, ctx):
    await send(update, "📸 Please send the payment SCREENSHOT as a photo (or /cancel).")
    return F_PROOF


async def fund_proof(update, ctx):
    m, u = update.message, update.effective_user
    photo = m.photo[-1]
    amt = ctx.user_data.get("fund_amt")
    if not amt:
        await send(update, "Session expired. Tap 💰 Add Funds again.", main_menu())
        return ConversationHandler.END
    # same screenshot already submitted before? (auto reject)
    if q("SELECT 1 FROM funds WHERE proof_uid=?", (photo.file_unique_id,)).fetchone():
        ctx.user_data.pop("fund_amt", None)
        await send(update, "❌ This screenshot was already used. Fake/duplicate "
                           "screenshots are not allowed.", main_menu())
        return ConversationHandler.END
    ctx.user_data.pop("fund_amt")
    fid = q("INSERT INTO funds(user_id,amount,proof,created,proof_uid) VALUES(?,?,?,?,?)",
            (u.id, amt, "screenshot", int(time.time()), photo.file_unique_id)).lastrowid
    cap = (f"💰 Fund request #{fid}\nUser: {u.id} (@{u.username})\n"
           f"Amount: {CURRENCY}{amt}")
    kb = Markup([[Btn("✅ Approve", callback_data=f"fa_{fid}")],
                 [Btn("❌ Reject: Fake", callback_data=f"frf_{fid}"),
                  Btn("❌ Reject: Already used", callback_data=f"fru_{fid}")]])
    for a in ADMIN_IDS:
        try:
            await ctx.bot.send_photo(a, photo.file_id, caption=cap, reply_markup=kb)
        except Exception as e:
            logging.warning("Admin notify failed: %s", e)
    await send(update, f"✅ Screenshot received. Request #{fid} sent to admin.\n"
                       "Balance will be added after approval.", main_menu())
    return ConversationHandler.END


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
        note = f"✅ Payment approved! {CURRENCY}{f['amount']} added to your balance."
    elif act == "frf":
        note = (f"❌ Fund request #{fid} rejected: your payment screenshot is FAKE. "
                "Do not send fake screenshots.")
    else:
        note = (f"❌ Fund request #{fid} rejected: this screenshot was ALREADY USED. "
                "Send a new valid payment screenshot.")
    try:
        await ctx.bot.send_message(f["user_id"], note)
    except Exception:
        pass
    await cq.answer(new.upper())
    reason = {"fa": "APPROVED", "frf": "REJECTED (Fake)", "fru": "REJECTED (Already used)"}[act]
    text = f"Fund #{fid} {reason}\nUser {f['user_id']} • {CURRENCY}{f['amount']}"
    try:
        await cq.edit_message_caption(text)
    except Exception:
        await cq.edit_message_text(text)


# ---------------- admin commands ----------------
ADMIN_HELP = """🛠 Admin Commands
/import - import services from panel API
/setmarkup 25 - set profit %
/addbal uid amt - add balance
/cutbal uid amt - deduct balance
/gift uid amt - gift balance
/giftall amt - gift everyone
/createcode CODE amt uses - gift code
/ban uid  /unban uid
/user uid - user info
/broadcast text
/stats"""


@admin_only
async def admin_cmd(update, ctx):
    await send(update, ADMIN_HELP)


@admin_only
async def import_cmd(update, ctx):
    await send(update, "⏳ Importing services...")
    try:
        data = await api_async(action="services")
    except Exception as e:
        return await send(update, f"❌ API error: {e}")
    q("UPDATE services SET active=0")
    for s in data:
        q("INSERT OR REPLACE INTO services(id,name,category,rate,min,max,active) "
          "VALUES(?,?,?,?,?,?,1)",
          (int(s["service"]), s["name"], s.get("category", "Other"),
           float(s["rate"]), int(s["min"]), int(s["max"])))
    await send(update, f"✅ Imported {len(data)} services (markup {get_markup()}%).")


@admin_only
async def setmarkup(update, ctx):
    try:
        v = float(ctx.args[0])
    except Exception:
        return await send(update, "Usage: /setmarkup 25")
    q("INSERT OR REPLACE INTO settings VALUES('markup',?)", (str(v),))
    await send(update, f"✅ Markup set to {v}%")


async def _balance_cmd(update, ctx, sign, label):
    try:
        uid, amt = int(ctx.args[0]), float(ctx.args[1])
    except Exception:
        return await send(update, "Usage: /cmd user_id amount")
    if not user_row(uid):
        return await send(update, "User not found.")
    add_bal(uid, sign * amt)
    await send(update, f"✅ {label} {CURRENCY}{amt} — user {uid}. "
                       f"New balance: {CURRENCY}{user_row(uid)['balance']:.2f}")
    try:
        msg = {"add": f"💰 {CURRENCY}{amt} added to your balance.",
               "cut": f"➖ {CURRENCY}{amt} deducted from your balance.",
               "gift": f"🎁 You received a gift of {CURRENCY}{amt}!"}[label]
        await ctx.bot.send_message(uid, msg)
    except Exception:
        pass


@admin_only
async def addbal(update, ctx):
    await _balance_cmd(update, ctx, 1, "add")


@admin_only
async def cutbal(update, ctx):
    await _balance_cmd(update, ctx, -1, "cut")


@admin_only
async def gift(update, ctx):
    await _balance_cmd(update, ctx, 1, "gift")


@admin_only
async def giftall(update, ctx):
    try:
        amt = float(ctx.args[0])
    except Exception:
        return await send(update, "Usage: /giftall amount")
    q("UPDATE users SET balance=ROUND(balance+?,2) WHERE banned=0", (amt,))
    await send(update, f"✅ Gifted {CURRENCY}{amt} to all users.")


@admin_only
async def createcode(update, ctx):
    try:
        code, amt, uses = ctx.args[0].upper(), float(ctx.args[1]), int(ctx.args[2])
    except Exception:
        return await send(update, "Usage: /createcode CODE amount uses")
    q("INSERT OR REPLACE INTO codes VALUES(?,?,?)", (code, amt, uses))
    await send(update, f"✅ Code {code}: {CURRENCY}{amt}, {uses} uses.")


@admin_only
async def ban(update, ctx):
    q("UPDATE users SET banned=1 WHERE id=?", (int(ctx.args[0]),))
    await send(update, "🚫 Banned.")


@admin_only
async def unban(update, ctx):
    q("UPDATE users SET banned=0 WHERE id=?", (int(ctx.args[0]),))
    await send(update, "✅ Unbanned.")


@admin_only
async def user_info(update, ctx):
    u = user_row(int(ctx.args[0])) if ctx.args and ctx.args[0].isdigit() else None
    if not u:
        return await send(update, "User not found.")
    await send(update, f"ID {u['id']} @{u['username']}\nBalance {CURRENCY}{u['balance']}\n"
                       f"Banned: {bool(u['banned'])}\nReferred by: {u['referred_by']}")


@admin_only
async def broadcast(update, ctx):
    text = " ".join(ctx.args)
    if not text:
        return await send(update, "Usage: /broadcast message")
    ok = 0
    for r in q("SELECT id FROM users WHERE banned=0").fetchall():
        try:
            await ctx.bot.send_message(r["id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await send(update, f"📢 Sent to {ok} users.")


@admin_only
async def stats(update, ctx):
    g = lambda s: q(s).fetchone()[0] or 0
    pending = g("SELECT COUNT(*) FROM funds WHERE status='pending'")
    await send(update,
               f"📊 Stats\nUsers: {g('SELECT COUNT(*) FROM users')}\n"
               f"Orders: {g('SELECT COUNT(*) FROM orders')}\n"
               f"Revenue: {CURRENCY}{g('SELECT SUM(charge) FROM orders'):.2f}\n"
               f"Pending funds: {pending}\n"
               f"Total user balance: {CURRENCY}{g('SELECT SUM(balance) FROM users'):.2f}")


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


# ---------------- main ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    menu_fallback = MessageHandler(filters.Regex(MENU_RE), conv_cancel)
    fallbacks = [CommandHandler("cancel", conv_cancel), menu_fallback]

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(svc_selected, pattern=r"^svc_\d+$")],
        states={LINK: [MessageHandler(TXT, got_link)],
                QTY: [MessageHandler(TXT, got_qty)],
                CONFIRM: [CallbackQueryHandler(confirm_order, pattern="^(ok|no)$")]},
        fallbacks=fallbacks, allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Add Funds$"), fund_start)],
        states={F_AMOUNT: [MessageHandler(TXT, fund_amount)],
                F_PROOF: [MessageHandler(filters.PHOTO, fund_proof),
                          MessageHandler(TXT, fund_need_photo)]},
        fallbacks=fallbacks, allow_reentry=True))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CallbackQueryHandler(check_join_cb, pattern="^checkjoin$"))
    app.add_handler(CallbackQueryHandler(cb_cats, pattern=r"^cats_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_cat, pattern=r"^cat_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(fund_decision, pattern=r"^f(a|rf|ru)_\d+$"))

    for pat, fn in [("^🛒 New Order$", new_order), ("^👤 Account$", account),
                    ("^📦 My Orders$", my_orders), ("^🎁 Free Bonus$", daily),
                    ("^👥 Refer & Earn$", refer), ("^🆘 Support$", support)]:
        app.add_handler(MessageHandler(filters.Regex(pat), fn))

    for name, fn in [("admin", admin_cmd), ("import", import_cmd),
                     ("setmarkup", setmarkup), ("addbal", addbal),
                     ("cutbal", cutbal), ("gift", gift), ("giftall", giftall),
                     ("createcode", createcode), ("ban", ban), ("unban", unban),
                     ("user", user_info), ("broadcast", broadcast),
                     ("stats", stats)]:
        app.add_handler(CommandHandler(name, fn))

    app.job_queue.run_repeating(sync_orders, interval=300, first=30)
    app.run_polling()


if __name__ == "__main__":
    main()
