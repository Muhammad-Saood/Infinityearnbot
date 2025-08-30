import os
import json
import time
import hmac
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, List
import requests
import asyncio
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler
from telegram.ext import filters
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn
from pydantic import BaseModel
from typing import Union
from dotenv import load_dotenv

# Load .env file for local testing (ignored on Koyeb)
load_dotenv()

# ----------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@InfinityEarn2x")
BASE_URL = os.getenv("BASE_URL")  # Can be None initially
PORT = int(os.getenv("PORT", "8000"))
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID", "-1003095776330")  # ID of the private channel for admin notifications

NOWPAY_API = "https://api.nowpayments.io/v1"
USDT_BSC_CODE = "usdtbsc"
PACKAGES = {10: 0.33, 20: 0.66, 50: 1.66, 100: 3.33, 200: 6.66, 500: 16.66, 1000: 33.33}
PACKAGE_DAYS = 60
MIN_WITHDRAWAL = 1.5  # Minimum withdrawal amount

# In-memory storage for testing
users: Dict[int, Dict[str, Any]] = {}  # uid: {"balance": 0.0, "verified": False, "referrer_id": None, "packages": [], "first_package_activated": False, "withdraw_state": None}

# Persistent storage for processed orders
try:
    with open("processed_orders.json", "r") as f:
        processed_orders = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    processed_orders = set()  # Default to empty set if file doesn’t exist or is invalid

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

api = FastAPI()

# ----------------- MEMORY UTILITIES -----------------
def ensure_user(uid: int, referrer_id: Optional[int] = None):
    if uid in users:
        data = users[uid]
        if referrer_id and not data.get("referrer_id"):
            data["referrer_id"] = referrer_id
        return
    users[uid] = {
        "balance": 0.0,
        "verified": False,
        "referrer_id": referrer_id,
        "packages": [],
        "first_package_activated": False,
        "withdraw_state": None  # New field for withdrawal state: None, "address", or "amount"
    }

def get_user(uid: int) -> Dict[str, Any]:
    if uid in users:
        return users[uid]
    ensure_user(uid)
    return users[uid]

def add_balance(uid: int, amount: float):
    if uid in users:
        users[uid]["balance"] = round(users[uid]["balance"] + amount, 8)

def deduct_balance(uid: int, amount: float) -> bool:
    if uid in users:
        cur = users[uid]["balance"]
        if cur + 1e-9 < amount:
            return False
        users[uid]["balance"] = round(cur - amount, 8)
        return True
    return False

def append_package(uid: int, pack: Dict[str, Any]):
    if uid in users:
        users[uid]["packages"].append(pack)

def active_packages(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = dt.datetime.now(dt.UTC)
    out = []
    for p in user.get("packages", []):
        if dt.datetime.fromtimestamp(p["end_ts"], dt.UTC) > now:
            out.append(p)
    return out

# ----------------- NOWPAYMENTS -----------------
def get_min_amount():
    url = f"{NOWPAY_API}/min-amount"
    headers = {"x-api-key": NOWPAY_API_KEY}
    params = {"currency_from": USDT_BSC_CODE, "currency_to": USDT_BSC_CODE}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return float(resp.json().get('min_amount', 5.0))
    except Exception:
        return 5.0

def nowpayments_create_payment(user_id: int) -> Dict[str, Any]:
    if not BASE_URL:
        raise ValueError("BASE_URL not set for payment creation")
    url = f"{NOWPAY_API}/payment"
    headers = {"x-api-key": NOWPAY_API_KEY, "Content-Type": "application/json"}
    min_amt = get_min_amount()
    payload = {
        "price_amount": min_amt,
        "price_currency": USDT_BSC_CODE,
        "pay_currency": USDT_BSC_CODE,
        "order_id": f"{user_id}-{int(time.time())}",
        "ipn_callback_url": f"{BASE_URL}/ipn/nowpayments"
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    logger.info(f"Created payment for user {user_id} with order_id {payload['order_id']}")
    return resp.json()

def verify_nowpay_signature(raw_body: bytes, signature: str) -> bool:
    try:
        body = json.loads(raw_body.decode("utf-8"))
        sorted_body = json.dumps(body, separators=(",", ":"), sort_keys=True)
        digest = hmac.new(NOWPAY_IPN_SECRET.encode("utf-8"), sorted_body.encode("utf-8"), hashlib.sha512).hexdigest()
        return digest == signature
    except Exception:
        return False

# ----------------- FASTAPI ENDPOINTS -----------------
@api.get("/")
def root():
    return {"ok": True}

# Pydantic model for NowPayments IPN data
class NowPaymentsIPN(BaseModel):
    payment_status: str
    actually_paid: Union[str, float, int]
    pay_amount: Union[str, float]
    order_id: str

@api.post("/ipn/nowpayments")
async def ipn_nowpayments(request: Request, x_nowpayments_sig: str = Header(None)):
    raw = await request.body()
    if not x_nowpayments_sig or not verify_nowpay_signature(raw, x_nowpayments_sig):
        raise HTTPException(status_code=400, detail="Bad signature")
    data = NowPaymentsIPN(**json.loads(raw.decode("utf-8")))
    status = (data.payment_status or "").lower()
    credited = float(data.actually_paid or data.pay_amount or 0.0)
    order_id = data.order_id
    logger.info(f"Received IPN for order_id {order_id}, status {status}, credited {credited}")
    if status in {"finished", "confirmed"} and order_id and credited > 0:
        if order_id not in processed_orders:
            try:
                tg_id = int(str(order_id).split("-")[0])
                add_balance(tg_id, credited)
                await app.bot.send_message(chat_id=tg_id, text=f"{credited} USDT Deposit Successfully")
                processed_orders.add(order_id)
                with open("processed_orders.json", "w") as f:
                    json.dump(list(processed_orders), f)
                logger.info(f"Processed payment for order_id {order_id}, credited {credited} to user {tg_id}")
            except Exception as e:
                logger.error(f"Error processing payment for order_id {order_id}: {e}")
        else:
            logger.info(f"Duplicate payment notification for order_id {order_id} ignored")
    return {"ok": True}

@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await app.process_update(Update.de_json(update, app.bot))
    return {"ok": True}

@api.get("/set-webhook")
async def set_webhook():
    if not BASE_URL:
        raise HTTPException(status_code=400, detail="BASE_URL not set in environment variables")
    webhook_url = f"{BASE_URL}/telegram/webhook"
    try:
        await app.bot.set_webhook(webhook_url)
        return {"status": "Webhook set successfully", "webhook_url": webhook_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set webhook: {str(e)}")

# ----------------- TELEGRAM BOT HANDLERS -----------------
WELCOME_TEXT = (
    'Welcome to "Infinity Earn 2x" platform where you can:\n\n'
    '👉 Invest 10 USDT and earn 0.33 USDT daily for 60 days.\n'
    '👉 Invest 20 USDT and earn 0.66 USDT daily for 60 days.\n'
    '👉 Invest 50 USDT and earn 1.66 USDT daily for 60 days.\n'
    '👉 Invest 100 USDT and earn 3.33 USDT daily for 60 days.\n'
    '👉 Invest 200 USDT and earn 6.66 USDT daily for 60 days.\n'
    '👉 Invest 500 USDT and earn 16.66 USDT daily for 60 days.\n'
    '👉 Invest 1000 USDT and earn 33.33 USDT daily for 60 days.\n\n'
    '🎁 You can also get 10% bonus on first deposit of your friend if your friend joined by your referral link.\n\n'
    'Deposit your balance, select your package by sending commands from the menu, and start your earning journey. You can also select multiple packages one by one to boost your earning.'
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    referrer = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref"):
            try:
                referrer = int(arg[3:])
                if referrer == update.effective_user.id:
                    referrer = None
            except Exception:
                referrer = None
    ensure_user(uid, referrer)
    kb = [
        [InlineKeyboardButton("📢 Telegram Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")]
    ]
    await update.message.reply_text(WELCOME_TEXT, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not BASE_URL or not NOWPAY_API_KEY:
        await update.message.reply_text("Service not configured. Contact admin.")
        return
    try:
        pay = nowpayments_create_payment(uid)
        pay_address = pay.get("pay_address") or pay.get("wallet_address") or pay.get("payment_address")
        if not pay_address:
            inv = pay.get("invoice_url") or pay.get("payment_url") or pay.get("url")
            if inv:
                await update.message.reply_text(f"{inv}\n\n(Open and pay on BSC/USDT)")
                return
            await update.message.reply_text("Could not get deposit address. Try again later.")
            return
         await update.message.reply_text(f"Your receiving address of USDT on BSC (Binance Smart Chain) is given below 👇:")   
        await update.message.reply_text(f" {pay_address}")
    except Exception as e:
        await update.message.reply_text(f"Error creating deposit address: {e}")

def packages_keyboard():
    rows = [
        [InlineKeyboardButton("10 USDT", callback_data="pkg:10"),
         InlineKeyboardButton("20 USDT", callback_data="pkg:20"),
         InlineKeyboardButton("50 USDT", callback_data="pkg:50")],
        [InlineKeyboardButton("100 USDT", callback_data="pkg:100"),
         InlineKeyboardButton("200 USDT", callback_data="pkg:200"),
         InlineKeyboardButton("500 USDT", callback_data="pkg:500")],
        [InlineKeyboardButton("1000 USDT", callback_data="pkg:1000")]
    ]
    return InlineKeyboardMarkup(rows)

async def cmd_packages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Select a package:", reply_markup=packages_keyboard())

async def cb_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user = get_user(uid)
    price = int(q.data.split(":")[1])
    if price not in PACKAGES:
        await q.edit_message_text("Invalid package.")
        return
    if user.get("balance", 0.0) + 1e-9 < price:
        await q.edit_message_text("Insufficient balance for selected package")
        return
    if not deduct_balance(uid, float(price)):
        await q.edit_message_text("Insufficient balance for selected package")
        return
    daily = PACKAGES[price]
    now = dt.datetime.now(dt.UTC)
    end = now + dt.timedelta(days=PACKAGE_DAYS)
    pack = {
        "name": f"{price} USDT",
        "price": float(price),
        "daily": float(daily),
        "start_ts": int(now.timestamp()),
        "end_ts": int(end.timestamp()),
        "last_claim_date": None
    }
    append_package(uid, pack)
    if not user.get("first_package_activated"):
        refid = user.get("referrer_id")
        if refid:
            bonus = round(price * 0.10, 8)
            add_balance(refid, bonus)
        user["first_package_activated"] = True
    await q.edit_message_text(f"Your {price} USDT package has been activated for {PACKAGE_DAYS} days.")

async def cmd_daily_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    packs = active_packages(user)
    if not packs:
        await update.message.reply_text("No active packages.")
        return
    today = dt.datetime.now(dt.UTC).date().isoformat()
    total = 0.0
    changed = False
    for p in packs:
        if p.get("last_claim_date") == today:
            continue
        total += float(p["daily"])
        p["last_claim_date"] = today
        changed = True
    if total <= 0:
        await update.message.reply_text("You already claimed today.")
        return
    if changed:
        add_balance(uid, round(total, 8))
    await update.message.reply_text(f"Daily reward added: {round(total,8)} USDT")

async def cmd_my_packages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    packs = active_packages(user)
    names = [p["name"] for p in packs]
    if not names:
        await update.message.reply_text("You have no active packages.")
        return
    if len(names) == 1:
        await update.message.reply_text(f"Your package is {names[0]}")
    else:
        await update.message.reply_text(f"Your packages are {', '.join(names)}")

async def cmd_my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    bal = round(user.get("balance", 0.0), 8)
    await update.message.reply_text(f"Your current balance is {bal} USDT")

async def cmd_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref{uid}"
    await update.message.reply_text(link)

# ----------------- NEW: MY TEAM COMMAND -----------------
async def cmd_my_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    count = sum(1 for u in users.values() if u.get("referrer_id") == uid and u.get("first_package_activated", False))
    await update.message.reply_text(f"Your qualified friends are {count}")

# ----------------- NEW: WITHDRAWAL SYSTEM -----------------
async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    user["withdraw_state"] = "address"
    await update.message.reply_text("Enter your Binance ID")

async def handle_withdraw_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    message_text = update.message.text.strip()

    if user.get("withdraw_state") == "address":
        user["withdraw_state"] = "amount"
        user["withdraw_address"] = message_text  # Store the address
        await update.message.reply_text("Enter your withdrawal amount.")
    elif user.get("withdraw_state") == "amount":
        try:
            amount = float(message_text)
            if amount < MIN_WITHDRAWAL:
                await update.message.reply_text(f"Insufficient withdrawal amount. Minimum is {MIN_WITHDRAWAL} USDT.")
                user["withdraw_state"] = None
                user["withdraw_address"] = None
                return
            if not deduct_balance(uid, amount):
                await update.message.reply_text("Insufficient balance for withdrawal.")
                user["withdraw_state"] = None
                user["withdraw_address"] = None
                return
            # Calculate qualified friends
            qualified_friends = sum(1 for u in users.values() if u.get("referrer_id") == uid and u.get("first_package_activated", False))
            # Notify admin channel
            if ADMIN_CHANNEL_ID:
                message = f"New Withdrawal Request:\nUser ID: {uid}\nAddress: {user['withdraw_address']}\nAmount: {amount} USDT\nQualified Friends: {qualified_friends}"
                try:
                    await app.bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=message)
                except Exception as e:
                    logger.error(f"Failed to send notification to admin channel: {e}")
                    add_balance(uid, amount)  # Refund if notification fails
                    await update.message.reply_text("Withdrawal request failed. Contact admin.")
                    user["withdraw_state"] = None
                    user["withdraw_address"] = None
                    return
            await update.message.reply_text("Withdraw Successful! Your balance credited to your Binance account within 24 hours.")
            user["withdraw_state"] = None
            user["withdraw_address"] = None
        except ValueError:
            await update.message.reply_text("Invalid amount. Please enter a valid number.")
            user["withdraw_state"] = "amount"

# ----------------- SETUP & RUN -----------------
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("deposit", cmd_deposit))
app.add_handler(CommandHandler("packages", cmd_packages))
app.add_handler(CallbackQueryHandler(cb_package, pattern=r"^pkg:\d+$"))
app.add_handler(CommandHandler("daily_reward", cmd_daily_reward))
app.add_handler(CommandHandler("my_packages", cmd_my_packages))
app.add_handler(CommandHandler("my_balance", cmd_my_balance))
app.add_handler(CommandHandler("referral_link", cmd_referral_link))
app.add_handler(CommandHandler("my_team", cmd_my_team))
app.add_handler(CommandHandler("withdraw", cmd_withdraw))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_input))

async def initialize_app():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        await app.initialize()
        if BASE_URL:
            webhook_url = f"{BASE_URL}/telegram/webhook"
            await app.bot.set_webhook(webhook_url)
            logger.info(f"Webhook set to {webhook_url}")
        else:
            logger.warning("BASE_URL not set. Running FastAPI server only. Use /set-webhook to configure Telegram webhook.")
    except Exception as e:
        logger.error(f"Error initializing app: {e}")
        raise

if __name__ == "__main__":
    missing = []
    for name in ["BOT_TOKEN", "NOWPAY_API_KEY", "NOWPAY_IPN_SECRET"]:
        if not globals().get(name):
            missing.append(name)
    if missing:
        raise RuntimeError(f"Missing required config values: {', '.join(missing)}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(initialize_app())
        uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="info", workers=1)
    finally:
        loop.close()
