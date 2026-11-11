#!/usr/bin/env python3
"""Wallet + referral + service-order Telegram bot.

This is a clean replacement for the Firebase/SMS/OTP functionality in the
original file. It does not collect phone numbers, read SMS, or forward OTPs.
"""
import os
import json
import time
import uuid
import logging
import threading
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_BOT_TOKEN_HERE")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "8418191600").split(",") if x.strip().isdigit()}
DB_FILE = Path(os.getenv("DB_FILE", "wallet_bot_data.json"))
QR_IMAGE_FILE = Path(os.getenv("QR_IMAGE_FILE", "payment_qr.png"))
DEFAULT_UPI_ID = os.getenv("UPI_ID", "")
DEFAULT_UPI_NAME = os.getenv("UPI_NAME", "Payment")
DEFAULT_REFERRAL_MODE = "fixed"       # fixed | percent
DEFAULT_REFERRAL_VALUE = "5.00"       # ₹5 fixed or 5 percent
CURRENCY = "₹"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("WalletBot")
LOCK = threading.RLock()

DEFAULT_DB = {
    "users": {},
    "deposits": {},
    "services": {},
    "orders": {},
    "settings": {
        "upi_id": DEFAULT_UPI_ID,
        "upi_name": DEFAULT_UPI_NAME,
        "qr_file": str(QR_IMAGE_FILE) if QR_IMAGE_FILE.exists() else "",
        "referral_mode": DEFAULT_REFERRAL_MODE,
        "referral_value": DEFAULT_REFERRAL_VALUE,
        "maintenance": False,
    },
    "banned": [],
}


def load_db():
    with LOCK:
        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid database")
        except (OSError, ValueError, json.JSONDecodeError):
            data = json.loads(json.dumps(DEFAULT_DB))
        for key, value in DEFAULT_DB.items():
            data.setdefault(key, json.loads(json.dumps(value)))
        return data


DB = load_db()


def save_db():
    with LOCK:
        tmp = DB_FILE.with_suffix(DB_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(DB, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DB_FILE)


def money(value):
    try:
        n = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not n.is_finite():
            raise InvalidOperation
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid amount")


def fmt(value):
    return f"{CURRENCY}{money(value):,.2f}"


def is_admin(uid):
    return uid in ADMIN_IDS


def ensure_user(tg_user, referrer=None):
    uid = str(tg_user.id)
    with LOCK:
        if uid not in DB["users"]:
            DB["users"][uid] = {
                "name": tg_user.full_name or "User",
                "username": tg_user.username or "",
                "balance": "0.00",
                "total_added": "0.00",
                "referral_earnings": "0.00",
                "referral_count": 0,
                "referred_by": str(referrer) if referrer and str(referrer) != uid else None,
                "referral_rewarded_deposits": [],
                "created_at": int(time.time()),
            }
            save_db()
        else:
            DB["users"][uid]["name"] = tg_user.full_name or DB["users"][uid].get("name", "User")
            DB["users"][uid]["username"] = tg_user.username or ""
            save_db()
    return DB["users"][uid]


def credit(uid, amount, total_added=False, referral=False):
    amount = money(amount)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    uid = str(uid)
    with LOCK:
        user = DB["users"].setdefault(uid, {
            "name": f"User {uid}", "username": "", "balance": "0.00",
            "total_added": "0.00", "referral_earnings": "0.00",
            "referral_count": 0, "referred_by": None,
            "referral_rewarded_deposits": [], "created_at": int(time.time())
        })
        user["balance"] = str(money(user.get("balance", 0)) + amount)
        if total_added:
            user["total_added"] = str(money(user.get("total_added", 0)) + amount)
        if referral:
            user["referral_earnings"] = str(money(user.get("referral_earnings", 0)) + amount)
        save_db()


def debit(uid, amount):
    amount = money(amount)
    uid = str(uid)
    with LOCK:
        user = DB["users"].get(uid)
        if not user or money(user.get("balance", 0)) < amount:
            return False
        user["balance"] = str(money(user.get("balance", 0)) - amount)
        save_db()
        return True


def referral_link(bot_username, uid):
    return f"https://t.me/{bot_username}?start={uid}"


def referral_reward(deposit_amount):
    settings = DB["settings"]
    value = money(settings.get("referral_value", "5"))
    if settings.get("referral_mode") == "percent":
        return (deposit_amount * value / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value


def user_blocked(uid):
    return str(uid) in {str(x) for x in DB.get("banned", [])}


async def guard(update, context):
    uid = update.effective_user.id
    if user_blocked(uid) and not is_admin(uid):
        if update.callback_query:
            await update.callback_query.answer("Your account is blocked.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ Your account is blocked. Contact admin.")
        return True
    if DB["settings"].get("maintenance") and not is_admin(uid):
        if update.effective_message:
            await update.effective_message.reply_text("🛠 Bot is under maintenance. Please try later.")
        return True
    return False


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Wallet", callback_data="wallet"),
         InlineKeyboardButton("➕ Add Funds", callback_data="deposit")],
        [InlineKeyboardButton("🛍 Services", callback_data="services"),
         InlineKeyboardButton("👥 Refer & Earn", callback_data="refer")],
        [InlineKeyboardButton("📜 My Orders", callback_data="orders"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    ref = context.args[0] if context.args else None
    user = ensure_user(update.effective_user, ref)
    await update.effective_message.reply_text(
        f"👋 Welcome, {escape(user.get('name', 'User'))}!\n\n"
        "Use the buttons below to manage your wallet and services.",
        reply_markup=main_keyboard(),
    )


async def info_text(uid):
    user = DB["users"].get(str(uid), {})
    return (
        f"👤 <b>{escape(user.get('name', 'User'))}</b>\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💰 Wallet balance: <b>{fmt(user.get('balance', 0))}</b>\n"
        f"🎁 Referral earnings: <b>{fmt(user.get('referral_earnings', 0))}</b>\n"
        f"👥 Referral count: <b>{int(user.get('referral_count', 0))}</b>\n"
        f"➕ Total funds added: <b>{fmt(user.get('total_added', 0))}</b>"
    )


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    ensure_user(update.effective_user)
    target = update.effective_user.id
    if context.args and is_admin(target):
        try:
            target = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Usage: /info [user_id]")
            return
    await update.effective_message.reply_text(await info_text(target), parse_mode=ParseMode.HTML)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await guard(update, context):
        return
    await q.answer()
    uid = q.from_user.id
    ensure_user(q.from_user)
    key = q.data

    if key == "wallet":
        await q.message.reply_text(await info_text(uid), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
    elif key == "deposit":
        settings = DB["settings"]
        upi = settings.get("upi_id") or "Not configured"
        qr = settings.get("qr_file")
        msg = f"➕ <b>ADD FUNDS</b>\n\nUPI ID: <code>{escape(upi)}</code>\nPay and then send:\n<code>/deposit AMOUNT UTR</code>\n\nExample: <code>/deposit 100 123456789012</code>\nYour request will be reviewed by admin."
        if qr and Path(qr).is_file():
            with open(qr, "rb") as f:
                await q.message.reply_photo(f, caption=msg, parse_mode=ParseMode.HTML)
        else:
            await q.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif key == "refer":
        me = await context.bot.get_me()
        user = DB["users"][str(uid)]
        await q.message.reply_text(
            f"👥 <b>REFER & EARN</b>\n\nYour link:\n<code>{referral_link(me.username, uid)}</code>\n\n"
            f"Successful referrals: {user.get('referral_count', 0)}\nReward: {escape(DB['settings']['referral_mode'])} — {fmt(DB['settings']['referral_value'])}"
            + ("%" if DB["settings"]["referral_mode"] == "percent" else ""),
            parse_mode=ParseMode.HTML,
        )
    elif key == "services":
        await show_services(q.message, uid)
    elif key == "orders":
        rows = [o for o in DB["orders"].values() if str(o["user_id"]) == str(uid)]
        rows = sorted(rows, key=lambda x: x["created_at"], reverse=True)[:15]
        if not rows:
            await q.message.reply_text("You have no orders yet.")
        else:
            body = "\n\n".join(
                f"🧾 <code>{escape(o['id'])}</code> · {escape(o['service'])}\n"
                f"Users: {o['quantity']} · Total: {fmt(o['total'])} · {escape(o['status'])}"
                for o in rows
            )
            await q.message.reply_text("<b>YOUR ORDERS</b>\n\n" + body, parse_mode=ParseMode.HTML)
    elif key == "help":
        await q.message.reply_text(
            "Commands:\n/deposit amount UTR — request a wallet top-up\n/info — wallet details\n/services — browse services\n\n"
            "Admin: /addbalance, /deduct, /approve, /reject, /addservice, /delservice, /setref, /setupi, /setqr, /ban, /unban, /gift"
        )


async def show_services(message, uid):
    services = DB["services"]
    if not services:
        await message.reply_text("No services are available right now.")
        return
    rows = []
    for sid, s in services.items():
        rows.append([InlineKeyboardButton(
            f"{s['name']} — {fmt(s['price_per_user'])}/user",
            callback_data=f"buy:{sid}"
        )])
    await message.reply_text(
        "🛍 Choose a service. Total price = price per user × quantity.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def services_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    ensure_user(update.effective_user)
    await show_services(update.effective_message, update.effective_user.id)


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if await guard(update, context):
        return
    await q.answer()
    sid = q.data.split(":", 1)[1]
    service = DB["services"].get(sid)
    if not service or not service.get("active", True):
        await q.message.reply_text("This service is no longer available.")
        return
    context.user_data["buy_service"] = sid
    await q.message.reply_text(
        f"Selected: {escape(service['name'])}\nPrice per user: {fmt(service['price_per_user'])}\n"
        "Send the quantity (number of users) to place your order."
    )


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    ensure_user(update.effective_user)
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /deposit AMOUNT UTR\nExample: /deposit 100 123456789012")
        return
    try:
        amount = money(context.args[0])
        if amount < Decimal("1") or amount > Decimal("100000"):
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a valid amount between ₹1 and ₹100000.")
        return
    utr = context.args[1].strip()
    if len(utr) < 6 or len(utr) > 40:
        await update.effective_message.reply_text("Please send a valid UTR/reference number.")
        return
    if any(d.get("utr") == utr for d in DB["deposits"].values()):
        await update.effective_message.reply_text("This UTR has already been submitted.")
        return
    did = uuid.uuid4().hex[:10].upper()
    DB["deposits"][did] = {
        "id": did, "user_id": update.effective_user.id,
        "amount": str(amount), "utr": utr, "status": "pending",
        "created_at": int(time.time()),
    }
    save_db()
    await update.effective_message.reply_text(f"✅ Deposit request submitted.\nRequest ID: {did}\nAmount: {fmt(amount)}\nWait for admin approval.")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                aid,
                f"💳 <b>NEW DEPOSIT</b>\nID: <code>{did}</code>\nUser: <code>{update.effective_user.id}</code>\n"
                f"Amount: <b>{fmt(amount)}</b>\nUTR: <code>{escape(utr)}</code>\n\n"
                f"/approve {did}\n/reject {did}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.warning("Admin notification failed: %s", exc)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    uid = update.effective_user.id
    ensure_user(update.effective_user)
    sid = context.user_data.get("buy_service")
    if not sid:
        return
    context.user_data.pop("buy_service", None)
    try:
        quantity = int((update.effective_message.text or "").strip())
        if quantity < 1 or quantity > 100000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Please send a whole number from 1 to 100000.")
        return
    service = DB["services"].get(sid)
    if not service or not service.get("active", True):
        await update.effective_message.reply_text("Service unavailable.")
        return
    total = money(service["price_per_user"]) * quantity
    if not debit(uid, total):
        await update.effective_message.reply_text(f"Insufficient balance. Required: {fmt(total)}")
        return
    oid = uuid.uuid4().hex[:10].upper()
    DB["orders"][oid] = {
        "id": oid, "user_id": uid, "service_id": sid,
        "service": service["name"], "quantity": quantity,
        "unit_price": str(money(service["price_per_user"])),
        "total": str(total), "status": "paid",
        "created_at": int(time.time()),
    }
    save_db()
    await update.effective_message.reply_text(
        f"✅ Order placed!\nOrder ID: {oid}\nService: {service['name']}\n"
        f"Users: {quantity}\nDeducted: {fmt(total)}\nWallet balance: {fmt(DB['users'][str(uid)]['balance'])}"
    )
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🛍 New order {oid}\nUser: {uid}\n{service['name']} × {quantity}\nTotal: {fmt(total)}")
        except Exception:
            pass


# -------------------- ADMIN COMMANDS --------------------
def admin_only(update):
    return is_admin(update.effective_user.id)


async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 2:
        return await update.effective_message.reply_text("Usage: /addbalance USER_ID AMOUNT")
    try:
        target, amount = int(context.args[0]), money(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await update.effective_message.reply_text("Invalid user ID or amount.")
    credit(target, amount)
    await update.effective_message.reply_text(f"Added {fmt(amount)} to {target}.")
    try:
        await context.bot.send_message(target, f"🎉 Admin added {fmt(amount)} to your wallet.")
    except Exception:
        pass


async def deduct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 2:
        return await update.effective_message.reply_text("Usage: /deduct USER_ID AMOUNT")
    try:
        target, amount = int(context.args[0]), money(context.args[1])
    except ValueError:
        return await update.effective_message.reply_text("Invalid user ID or amount.")
    if not debit(target, amount):
        return await update.effective_message.reply_text("User not found or insufficient balance.")
    await update.effective_message.reply_text(f"Deducted {fmt(amount)} from {target}.")


async def gift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    # Reply to a user's message with /gift 10, or use /gift USER_ID 10.
    if len(context.args) == 1 and update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
        raw_amount = context.args[0]
    elif len(context.args) == 2:
        try:
            target = int(context.args[0])
        except ValueError:
            return await update.effective_message.reply_text("Invalid user ID.")
        raw_amount = context.args[1]
    else:
        return await update.effective_message.reply_text(
            "Usage: reply to a user and send /gift 10, OR /gift USER_ID 10"
        )
    try:
        amount = money(raw_amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await update.effective_message.reply_text("Invalid amount.")
    credit(target, amount)
    await update.effective_message.reply_text(f"🎁 Gifted {fmt(amount)} to {target}.")
    try:
        await context.bot.send_message(target, f"🎁 Admin gifted {fmt(amount)} to your wallet.")
    except Exception:
        pass


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1 or context.args[0] not in DB["deposits"]:
        return await update.effective_message.reply_text("Usage: /approve REQUEST_ID")
    dep = DB["deposits"][context.args[0]]
    if dep["status"] != "pending":
        return await update.effective_message.reply_text("This request is already processed.")
    dep["status"] = "approved"
    dep["approved_at"] = int(time.time())
    credit(dep["user_id"], dep["amount"], total_added=True)
    # Reward each approved top-up; count each referred user only once.
    referred = DB["users"].get(str(dep["user_id"]), {})
    referrer = referred.get("referred_by")
    reward = Decimal("0.00")
    if referrer and referrer in DB["users"] and dep["id"] not in referred.get("referral_rewarded_deposits", []):
        reward = referral_reward(money(dep["amount"]))
        if reward > 0:
            credit(referrer, reward, referral=True)
        referred.setdefault("referral_rewarded_deposits", []).append(dep["id"])
        if not referred.get("referral_counted", False):
            referred["referral_counted"] = True
            DB["users"][referrer]["referral_count"] = int(DB["users"][referrer].get("referral_count", 0)) + 1
    save_db()
    await update.effective_message.reply_text(f"Approved {dep['id']} — {fmt(dep['amount'])}.")
    try:
        await context.bot.send_message(dep["user_id"], f"✅ Deposit approved. {fmt(dep['amount'])} added to your wallet.")
        if referrer and reward > 0:
            await context.bot.send_message(int(referrer), f"🎉 Referral reward received: {fmt(reward)}")
    except Exception:
        pass


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1 or context.args[0] not in DB["deposits"]:
        return await update.effective_message.reply_text("Usage: /reject REQUEST_ID")
    dep = DB["deposits"][context.args[0]]
    if dep["status"] != "pending":
        return await update.effective_message.reply_text("This request is already processed.")
    dep["status"] = "rejected"
    save_db()
    await update.effective_message.reply_text(f"Rejected {dep['id']}.")
    try:
        await context.bot.send_message(dep["user_id"], f"❌ Deposit request {dep['id']} was rejected. Contact admin if needed.")
    except Exception:
        pass


async def addservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) < 2:
        return await update.effective_message.reply_text('Usage: /addservice SERVICE_NAME PRICE_PER_USER\nExample: /addservice Telegram 2.50')
    name = " ".join(context.args[:-1]).strip()
    try:
        price = money(context.args[-1])
        if price <= 0:
            raise ValueError
    except ValueError:
        return await update.effective_message.reply_text("Price must be greater than zero.")
    sid = uuid.uuid4().hex[:8]
    DB["services"][sid] = {"name": name[:60], "price_per_user": str(price), "active": True}
    save_db()
    await update.effective_message.reply_text(f"Service added: {name}\nID: {sid}\nPrice/user: {fmt(price)}")


async def delservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1 or context.args[0] not in DB["services"]:
        return await update.effective_message.reply_text("Usage: /delservice SERVICE_ID")
    del DB["services"][context.args[0]]
    save_db()
    await update.effective_message.reply_text("Service removed.")


async def setref_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Usage: /setref fixed|percent VALUE (admin only)")
    if len(context.args) != 2 or context.args[0] not in ("fixed", "percent"):
        return await update.effective_message.reply_text("Usage: /setref fixed 5 OR /setref percent 10")
    try:
        value = money(context.args[1])
        if value <= 0 or (context.args[0] == "percent" and value > 100):
            raise ValueError
    except ValueError:
        return await update.effective_message.reply_text("Invalid reward value.")
    DB["settings"]["referral_mode"] = context.args[0]
    DB["settings"]["referral_value"] = str(value)
    save_db()
    await update.effective_message.reply_text(f"Referral reward set: {context.args[0]} {value}")


async def setupi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if not context.args:
        return await update.effective_message.reply_text("Usage: /setupi yourupi@bank")
    DB["settings"]["upi_id"] = context.args[0][:100]
    save_db()
    await update.effective_message.reply_text("UPI ID updated.")


async def setqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    photo = update.effective_message.photo
    if not photo:
        return await update.effective_message.reply_text("Send a QR image with caption /setqr")
    file = await context.bot.get_file(photo[-1].file_id)
    await file.download_to_drive(custom_path=str(QR_IMAGE_FILE))
    DB["settings"]["qr_file"] = str(QR_IMAGE_FILE)
    save_db()
    await update.effective_message.reply_text("Payment QR updated.")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.effective_message.reply_text("Usage: /ban USER_ID")
    uid = context.args[0]
    if uid not in DB["banned"]:
        DB["banned"].append(uid)
        save_db()
    await update.effective_message.reply_text(f"Blocked {uid}.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1:
        return await update.effective_message.reply_text("Usage: /unban USER_ID")
    uid = context.args[0]
    DB["banned"] = [x for x in DB["banned"] if str(x) != uid]
    save_db()
    await update.effective_message.reply_text(f"Unblocked {uid}.")


async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) != 1 or context.args[0] not in ("on", "off"):
        return await update.effective_message.reply_text("Usage: /maintenance on|off")
    DB["settings"]["maintenance"] = context.args[0] == "on"
    save_db()
    await update.effective_message.reply_text(f"Maintenance: {context.args[0]}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    pending = sum(1 for x in DB["deposits"].values() if x["status"] == "pending")
    total_users = len(DB["users"])
    total_orders = len(DB["orders"])
    await update.effective_message.reply_text(
        f"📊 Stats\nUsers: {total_users}\nOrders: {total_orders}\nPending deposits: {pending}"
    )


async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN environment variable before starting the bot.")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("services", services_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("deduct", deduct_cmd))
    app.add_handler(CommandHandler("gift", gift_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("addservice", addservice_cmd))
    app.add_handler(CommandHandler("delservice", delservice_cmd))
    app.add_handler(CommandHandler("setref", setref_cmd))
    app.add_handler(CommandHandler("setupi", setupi_cmd))
    app.add_handler(CommandHandler("setqr", setqr_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(wallet|deposit|refer|services|orders|help)$"))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/setqr(?:@\w+)?$"), setqr_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.add_error_handler(error_handler)
    log.info("Wallet bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
