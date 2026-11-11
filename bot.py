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
from flask import Flask, jsonify
from werkzeug.serving import make_server
import qrcode

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

# Lightweight HTTP server for Render health checks / web-service port binding.
web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "Wallet Telegram Bot is running", 200

@web_app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "wallet-telegram-bot"}), 200


def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    server = make_server("0.0.0.0", port, web_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="flask-health-server", daemon=True)
    thread.start()
    log.info("Flask health server listening on port %s", port)
    return server

DEFAULT_DB = {
    "users": {},
    "deposits": {},
    "services": {},
    "orders": {},
    "transactions": [],
    "settings": {
        "upi_id": DEFAULT_UPI_ID,
        "upi_name": DEFAULT_UPI_NAME,
        "qr_file": str(QR_IMAGE_FILE) if QR_IMAGE_FILE.exists() else "",
        "referral_mode": DEFAULT_REFERRAL_MODE,
        "referral_value": DEFAULT_REFERRAL_VALUE,
        "maintenance": False,
        "firebase_config": {"project_id": "", "database_url": "", "storage_bucket": ""},
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
        data.setdefault("transactions", [])
        data.setdefault("settings", {}).setdefault("firebase_config", {"project_id": "", "database_url": "", "storage_bucket": ""})
        for service in data.get("services", {}).values():
            service.setdefault("example_text", "")
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


def credit(uid, amount, total_added=False, referral=False, note="Wallet credit"):
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
        DB.setdefault("transactions", []).append({
            "id": uuid.uuid4().hex[:10].upper(), "user_id": uid,
            "type": "credit", "amount": str(amount), "note": note,
            "created_at": int(time.time())
        })
        save_db()


def debit(uid, amount, note="Wallet debit"):
    amount = money(amount)
    uid = str(uid)
    with LOCK:
        user = DB["users"].get(uid)
        if not user or money(user.get("balance", 0)) < amount:
            return False
        user["balance"] = str(money(user.get("balance", 0)) - amount)
        DB.setdefault("transactions", []).append({
            "id": uuid.uuid4().hex[:10].upper(), "user_id": uid,
            "type": "debit", "amount": str(amount), "note": note,
            "created_at": int(time.time())
        })
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
         InlineKeyboardButton("📜 History", callback_data="history")],
        [InlineKeyboardButton("👥 Refer & Earn", callback_data="refer"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]])


def panel_text(uid):
    user = DB["users"].get(str(uid), {})
    return (f"👋 Welcome, {escape(user.get('name', 'User'))}!\n\n"
            f"💰 Wallet: <b>{fmt(user.get('balance', 0))}</b>\n"
            "Choose an option below.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    ref = context.args[0] if context.args else None
    user = ensure_user(update.effective_user, ref)
    await update.effective_message.reply_text(
        panel_text(update.effective_user.id),
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
        await q.message.edit_text(await info_text(uid), parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    elif key == "deposit":
        context.user_data["deposit_step"] = "amount"
        await q.message.edit_text(
            "➕ <b>ADD FUNDS</b>\n\nSend the amount you want to add (₹1–₹100000).\n"
            "Then pay using the QR and upload your payment screenshot. Admin will approve or reject it.",
            parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    elif key == "home":
        context.user_data.pop("deposit_step", None)
        context.user_data.pop("deposit_amount", None)
        context.user_data.pop("buy_service", None)
        await q.message.edit_text(panel_text(uid), parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
    elif key == "refer":
        me = await context.bot.get_me()
        user = DB["users"][str(uid)]
        await q.message.edit_text(
            f"👥 <b>REFER & EARN</b>\n\nYour link:\n<code>{referral_link(me.username, uid)}</code>\n\n"
            f"Successful referrals: {user.get('referral_count', 0)}\nReward: {escape(DB['settings']['referral_mode'])} — {fmt(DB['settings']['referral_value'])}"
            + ("%" if DB['settings']['referral_mode'] == "percent" else ""),
            parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    elif key == "services":
        await show_services(q.message, uid)
    elif key in ("history", "orders"):
        deposits = [d for d in DB["deposits"].values() if str(d.get("user_id")) == str(uid)]
        orders = [o for o in DB["orders"].values() if str(o.get("user_id")) == str(uid)]
        entries = []
        for d in deposits:
            entries.append((d.get("created_at", 0), f"💳 Add funds · {fmt(d.get('amount', 0))} · {escape(d.get('status', 'pending').upper())} · <code>{escape(d.get('id', ''))}</code>"))
        for o in orders:
            entries.append((o.get("created_at", 0), f"🛍 {escape(o.get('service', 'Service'))} × {o.get('quantity', 0)} · {fmt(o.get('total', 0))} · {escape(o.get('status', 'paid').upper())} · <code>{escape(o.get('id', ''))}</code>"))
        for tx in DB.get("transactions", []):
            if str(tx.get("user_id")) == str(uid):
                sign = "+" if tx.get("type") == "credit" else "−"
                entries.append((tx.get("created_at", 0), f"💼 Wallet {escape(tx.get('type', 'change'))} · {sign}{fmt(tx.get('amount', 0))} · {escape(tx.get('note', ''))}"))
        entries.sort(key=lambda x: x[0], reverse=True)
        body = "\n\n".join(item for _, item in entries[:20]) or "No wallet or order history yet."
        await q.message.edit_text("📜 <b>HISTORY</b>\n\n" + body, parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
    elif key == "help":
        await q.message.edit_text(
            "Use the buttons to manage your wallet, deposits, services and history.\n\n"
            "Admin commands: /addservice, /editservice, /delservice, /setupi, /setref, /firebase_set, /firebase_status, /stats",
            reply_markup=back_keyboard())


async def show_services(message, uid):
    rows = []
    lines = ["🛍 <b>SERVICES</b>", "Choose a service below. Total = price × quantity."]
    for sid, service in DB["services"].items():
        if service.get("active", True):
            rows.append([InlineKeyboardButton(
                f"{service['name']} — {fmt(service['price_per_user'])}/user",
                callback_data=f"buy:{sid}"
            )])
            example = service.get("example_text", "")
            if example:
                lines.append(f"\n<b>{escape(service['name'])}</b>\nExample/info: {escape(example[:350])}")
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    if len(rows) == 1:
        lines = ["No services are available right now."]
    markup = InlineKeyboardMarkup(rows)
    try:
        await message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=markup)


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
    message_text = (update.effective_message.text or "").strip()

    # Admin add-service wizard: name -> price -> informational example text.
    admin_step = context.user_data.get("admin_service_step")
    if admin_step and is_admin(uid):
        if admin_step == "name":
            if not message_text or len(message_text) > 60:
                await update.effective_message.reply_text("Send a service name (1–60 characters).")
                return
            context.user_data["new_service_name"] = message_text
            context.user_data["admin_service_step"] = "price"
            await update.effective_message.reply_text("Now send price per unit, e.g. 2.50")
            return
        if admin_step == "price":
            try:
                price = money(message_text)
                if price <= 0:
                    raise ValueError
            except ValueError:
                await update.effective_message.reply_text("Enter a valid price greater than zero.")
                return
            context.user_data["new_service_price"] = str(price)
            context.user_data["admin_service_step"] = "example"
            await update.effective_message.reply_text(
                "Send an optional example/description for this service. It will only be displayed as information; the bot will not read SMS, detect OTPs, or route messages. Send - to skip."
            )
            return
        if admin_step == "example":
            name = context.user_data.pop("new_service_name", "Service")
            price = context.user_data.pop("new_service_price", "0")
            example = "" if message_text == "-" else message_text[:1000]
            sid = uuid.uuid4().hex[:8]
            DB["services"][sid] = {
                "name": name, "price_per_user": price, "example_text": example,
                "active": True, "created_at": int(time.time())
            }
            save_db()
            context.user_data.pop("admin_service_step", None)
            await update.effective_message.reply_text(f"✅ Service added\nName: {escape(name)}\nID: {sid}\nPrice/unit: {fmt(price)}")
            return

    step = context.user_data.get("deposit_step")
    if step == "amount":
        try:
            amount = money(message_text)
            if amount < Decimal("1") or amount > Decimal("100000"):
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("Enter a valid amount between ₹1 and ₹100000.")
            return
        upi = DB["settings"].get("upi_id", "")
        if not upi:
            await update.effective_message.reply_text("Payment UPI is not configured. Please contact admin.")
            return
        name = DB["settings"].get("upi_name", "Payment")
        from urllib.parse import quote
        uri = f"upi://pay?pa={quote(upi)}&pn={quote(name)}&am={amount:.2f}&cu=INR"
        qr_path = Path(f"deposit_qr_{uid}.png")
        qrcode.make(uri).save(qr_path)
        context.user_data["deposit_amount"] = str(amount)
        context.user_data["deposit_step"] = "screenshot"
        with qr_path.open("rb") as photo:
            await update.effective_message.reply_photo(
                photo, caption=f"💳 Pay {fmt(amount)} using this QR.\nAfter payment, upload the screenshot here."
            )
        return

    sid = context.user_data.get("buy_service")
    if not sid:
        return
    context.user_data.pop("buy_service", None)
    try:
        quantity = int(message_text)
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
    if not debit(uid, total, note=f"Service order {service['name']}"):
        await update.effective_message.reply_text(f"Insufficient balance. Required: {fmt(total)}")
        return
    oid = uuid.uuid4().hex[:10].upper()
    DB["orders"][oid] = {
        "id": oid, "user_id": uid, "service_id": sid,
        "service": service["name"], "quantity": quantity,
        "unit_price": str(money(service["price_per_user"])),
        "total": str(total), "status": "paid", "created_at": int(time.time()),
    }
    save_db()
    await update.effective_message.reply_text(
        f"✅ Order placed!\nOrder ID: {oid}\nService: {service['name']}\n"
        f"Quantity: {quantity}\nDeducted: {fmt(total)}\nWallet balance: {fmt(DB['users'][str(uid)]['balance'])}"
    )
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🛍 New order {oid}\nUser: {uid}\n{service['name']} × {quantity}\nTotal: {fmt(total)}")
        except Exception:
            pass


async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await guard(update, context):
        return
    if context.user_data.get("deposit_step") != "screenshot":
        return
    uid = update.effective_user.id
    amount = context.user_data.get("deposit_amount")
    if not amount:
        context.user_data.pop("deposit_step", None)
        await update.effective_message.reply_text("Deposit session expired. Please start Add Funds again.")
        return
    photo = update.effective_message.photo[-1]
    unique_id = photo.file_unique_id
    if any(d.get("screenshot_unique_id") == unique_id for d in DB["deposits"].values()):
        await update.effective_message.reply_text("This exact screenshot has already been submitted. Please contact admin if this is an error.")
        return
    did = uuid.uuid4().hex[:10].upper()
    DB["deposits"][did] = {
        "id": did, "user_id": uid, "amount": amount, "utr": "",
        "screenshot_file_id": photo.file_id, "screenshot_unique_id": unique_id,
        "status": "pending", "created_at": int(time.time()),
    }
    save_db()
    context.user_data.pop("deposit_step", None)
    context.user_data.pop("deposit_amount", None)
    await update.effective_message.reply_text(f"✅ Payment screenshot submitted.\nRequest: {did}\nAmount: {fmt(amount)}\nAdmin will review it shortly.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"depapprove:{did}"), InlineKeyboardButton("❌ Reject", callback_data=f"depreject:{did}")]])
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(aid, photo=photo.file_id,
                caption=f"💳 NEW PAYMENT REVIEW\nRequest: {did}\nUser ID: {uid}\nAmount: {fmt(amount)}\nStatus: PENDING",
                reply_markup=keyboard)
        except Exception as exc:
            log.warning("Admin screenshot notification failed: %s", exc)


async def deposit_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True)
        return
    action, did = q.data.split(":", 1)
    dep = DB["deposits"].get(did)
    if not dep:
        await q.answer("Request not found", show_alert=True)
        return
    if dep.get("status") != "pending":
        await q.answer("Already processed", show_alert=True)
        return
    if action == "depapprove":
        dep["status"] = "approved"
        dep["approved_at"] = int(time.time())
        credit(dep["user_id"], dep["amount"], total_added=True, note="Approved deposit")
        referred = DB["users"].get(str(dep["user_id"]), {})
        referrer = referred.get("referred_by")
        reward = Decimal("0.00")
        if referrer and referrer in DB["users"] and did not in referred.get("referral_rewarded_deposits", []):
            reward = referral_reward(money(dep["amount"]))
            if reward > 0:
                credit(referrer, reward, referral=True, note="Referral reward")
            referred.setdefault("referral_rewarded_deposits", []).append(did)
            if not referred.get("referral_counted", False):
                referred["referral_counted"] = True
                DB["users"][referrer]["referral_count"] = int(DB["users"][referrer].get("referral_count", 0)) + 1
        await q.answer("Approved")
        await q.edit_message_caption(caption=f"✅ APPROVED\nRequest: {did}\nUser: {dep['user_id']}\nAmount: {fmt(dep['amount'])}", reply_markup=None)
        try:
            await context.bot.send_message(dep["user_id"], f"✅ Your payment was approved. {fmt(dep['amount'])} added to your wallet.")
            if referrer and reward > 0:
                await context.bot.send_message(int(referrer), f"🎉 Referral reward received: {fmt(reward)}")
        except Exception:
            pass
    else:
        dep["status"] = "rejected"
        dep["rejected_at"] = int(time.time())
        await q.answer("Rejected")
        await q.edit_message_caption(caption=f"❌ REJECTED\nRequest: {did}\nUser: {dep['user_id']}\nAmount: {fmt(dep['amount'])}", reply_markup=None)
        try:
            await context.bot.send_message(dep["user_id"], f"❌ Your payment request {did} was rejected. Please contact admin if you think this is a mistake.")
        except Exception:
            pass
    save_db()


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
    credit(target, amount, note="Admin balance credit")
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
    if not debit(target, amount, note="Admin deduction"):
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
    credit(target, amount, note="Admin gift")
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
    credit(dep["user_id"], dep["amount"], total_added=True, note="Approved deposit")
    # Reward each approved top-up; count each referred user only once.
    referred = DB["users"].get(str(dep["user_id"]), {})
    referrer = referred.get("referred_by")
    reward = Decimal("0.00")
    if referrer and referrer in DB["users"] and dep["id"] not in referred.get("referral_rewarded_deposits", []):
        reward = referral_reward(money(dep["amount"]))
        if reward > 0:
            credit(referrer, reward, referral=True, note="Referral reward")
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
    context.user_data["admin_service_step"] = "name"
    context.user_data.pop("new_service_name", None)
    context.user_data.pop("new_service_price", None)
    await update.effective_message.reply_text("🛠 Add new service\nStep 1/3: Send the service name.")


async def editservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) < 3:
        return await update.effective_message.reply_text("Usage: /editservice ID NEW_NAME NEW_PRICE")
    sid = context.args[0]
    service = DB["services"].get(sid)
    if not service:
        return await update.effective_message.reply_text("Service ID not found.")
    try:
        price = money(context.args[-1])
        if price <= 0:
            raise ValueError
    except ValueError:
        return await update.effective_message.reply_text("Invalid price.")
    service["name"] = " ".join(context.args[1:-1])[:60]
    service["price_per_user"] = str(price)
    save_db()
    await update.effective_message.reply_text(f"Updated {sid}: {service['name']} — {fmt(price)}/unit")


async def firebase_set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    if len(context.args) < 2:
        return await update.effective_message.reply_text(
            "Usage: /firebase_set project_id|database_url|storage_bucket VALUE\n"
            "This stores Firebase project configuration only; it does not collect SMS/OTP data."
        )
    key = context.args[0].lower()
    if key not in ("project_id", "database_url", "storage_bucket"):
        return await update.effective_message.reply_text("Allowed keys: project_id, database_url, storage_bucket")
    value = " ".join(context.args[1:]).strip()
    if len(value) > 500:
        return await update.effective_message.reply_text("Value is too long.")
    DB["settings"].setdefault("firebase_config", {})[key] = value
    save_db()
    await update.effective_message.reply_text(f"Firebase {key} saved. Add service-account credentials securely as a Render secret for live Firebase connectivity.")


async def firebase_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return await update.effective_message.reply_text("Admin only.")
    cfg = DB["settings"].get("firebase_config", {})
    await update.effective_message.reply_text(
        "🔥 Firebase configuration\n"
        f"Project ID: {'set' if cfg.get('project_id') else 'not set'}\n"
        f"Database URL: {'set' if cfg.get('database_url') else 'not set'}\n"
        f"Storage bucket: {'set' if cfg.get('storage_bucket') else 'not set'}\n"
        f"Service account secret: {'set' if os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON') else 'not set'}\n\n"
        "This bot stores configuration only; no SMS/OTP reading or forwarding is enabled."
    )


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
    start_web_server()
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
    app.add_handler(CommandHandler("editservice", editservice_cmd))
    app.add_handler(CommandHandler("delservice", delservice_cmd))
    app.add_handler(CommandHandler("firebase_set", firebase_set_cmd))
    app.add_handler(CommandHandler("firebase_status", firebase_status_cmd))
    app.add_handler(CommandHandler("setref", setref_cmd))
    app.add_handler(CommandHandler("setupi", setupi_cmd))
    app.add_handler(CommandHandler("setqr", setqr_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(wallet|deposit|refer|services|orders|history|help|home)$"))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(deposit_review_callback, pattern=r"^(depapprove|depreject):"))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/setqr(?:@\w+)?$"), setqr_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, deposit_screenshot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.add_error_handler(error_handler)
    log.info("Wallet bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
