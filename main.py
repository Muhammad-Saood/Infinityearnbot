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
import uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel
from typing import Union
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@InfinityEarn2x")
BASE_URL = os.getenv("BASE_URL")
PORT = int(os.getenv("PORT", "8000"))
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID", "-1003095776330")

NOWPAY_API = "https://api.nowpayments.io/v1"
USDT_BSC_CODE = "usdtbsc"
PACKAGES = {10: 0.33, 20: 0.66, 50: 1.66, 100: 3.33, 200: 6.66, 500: 16.66, 1000: 33.33}
PACKAGE_DAYS = 60
MIN_WITHDRAWAL = 1.5

# Persistent storage
try:
    with open("users.json", "r") as f:
        users: Dict[int, Dict[str, Any]] = {int(k): v for k, v in json.load(f).items()}
except (FileNotFoundError, json.JSONDecodeError, ValueError):
    users: Dict[int, Dict[str, Any]] = {}

try:
    with open("processed_orders.json", "r") as f:
        processed_orders = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    processed_orders = set()

try:
    with open("next_address.json", "r") as f:
        next_deposit_address = json.load(f).get("address")
except (FileNotFoundError, json.JSONDecodeError):
    next_deposit_address = None

first_address_assigned = False
if "first_address_assigned" in users:
    first_address_assigned = users["first_address_assigned"]
    del users["first_address_assigned"]
else:
    with open("users.json", "w") as f:
        json.dump(users, f)

# Memory utilities
def save_users():
    global first_address_assigned
    users["first_address_assigned"] = first_address_assigned
    with open("users.json", "w") as f:
        json.dump(users, f)
    del users["first_address_assigned"]

def save_processed_orders():
    with open("processed_orders.json", "w") as f:
        json.dump(list(processed_orders), f)

def save_next_address(new_address: str):
    global next_deposit_address
    next_deposit_address = new_address
    with open("next_address.json", "w") as f:
        json.dump({"address": next_deposit_address}, f)

def ensure_user(uid: int, referrer_id: Optional[int] = None):
    if uid not in users:
        users[uid] = {
            "balance": 0.0,
            "verified": False,
            "referrer_id": referrer_id,
            "packages": [],
            "first_package_activated": False,
            "withdraw_state": None,
            "deposit_address": None
        }
        save_users()
    elif referrer_id and not users[uid].get("referrer_id"):
        users[uid]["referrer_id"] = referrer_id
        save_users()

def get_user(uid: int) -> Dict[str, Any]:
    ensure_user(uid)
    return users[uid]

def add_balance(uid: int, amount: float):
    if uid in users:
        users[uid]["balance"] = round(users[uid]["balance"] + amount, 8)
        save_users()

def deduct_balance(uid: int, amount: float) -> bool:
    if uid in users:
        cur = users[uid]["balance"]
        if cur + 1e-9 < amount:
            return False
        users[uid]["balance"] = round(cur - amount, 8)
        save_users()
        return True
    return False

def append_package(uid: int, pack: Dict[str, Any]):
    if uid in users:
        users[uid]["packages"].append(pack)
        save_users()

def active_packages(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = dt.datetime.now(dt.UTC)
    return [p for p in user.get("packages", []) if dt.datetime.fromtimestamp(p["end_ts"], dt.UTC) > now]

# NOWPayments utilities
def get_min_amount():
    url = f"{NOWPAY_API}/min-amount"
    headers = {"x-api-key": NOWPAY_API_KEY}
    params = {"currency_from": USDT_BSC_CODE, "currency_to": USDT_BSC_CODE}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return float(resp.json().get('min_amount', 5.0))
    except Exception as e:
        logger.error(f"Failed to get min amount: {str(e)}")
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
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        logger.info(f"Created payment for user {user_id} with order_id {payload['order_id']}")
        return resp.json()
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        if hasattr(e.response, 'status_code'):
            logger.error(f"API error {e.response.status_code}: {error_msg}")
        else:
            logger.error(f"Network error: {error_msg}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error creating payment: {str(e)}")
        raise

def verify_nowpay_signature(raw_body: bytes, signature: str) -> bool:
    try:
        body = json.loads(raw_body.decode("utf-8"))
        sorted_body = json.dumps(body, separators=(",", ":"), sort_keys=True)
        digest = hmac.new(NOWPAY_IPN_SECRET.encode("utf-8"), sorted_body.encode("utf-8"), hashlib.sha512).hexdigest()
        return digest == signature
    except Exception as e:
        logger.error(f"Signature verification failed: {str(e)}")
        return False

# FastAPI endpoints
api = FastAPI()

@api.get("/")
def root():
    return {"ok": True}

class NowPaymentsIPN(BaseModel):
    payment_status: str
    actually_paid: Union[str, float, int]
    pay_amount: Union[str, float]
    order_id: str
    pay_address: str

@api.post("/ipn/nowpayments")
async def ipn_nowpayments(request: Request, x_nowpayments_sig: str = Header(None)):
    raw = await request.body()
    if not x_nowpayments_sig or not verify_nowpay_signature(raw, x_nowpayments_sig):
        raise HTTPException(status_code=400, detail="Bad signature")
    data = NowPaymentsIPN(**json.loads(raw.decode("utf-8")))
    status = (data.payment_status or "").lower()
    credited = float(data.actually_paid or data.pay_amount or 0.0)
    order_id = data.order_id
    pay_address = data.pay_address
    logger.info(f"Received IPN for order_id {order_id}, status {status}, credited {credited}, address {pay_address}")
    if status in {"finished", "confirmed"} and order_id and credited > 0:
        if order_id not in processed_orders:
            try:
                tg_id = int(str(order_id).split("-")[0])
                user = get_user(tg_id)
                if user.get("deposit_address") == pay_address:
                    add_balance(tg_id, credited)
                    await app.bot.send_message(chat_id=tg_id, text=f"{credited} USDT Deposit Successfully")
                    processed_orders.add(order_id)
                    save_processed_orders()
                    logger.info(f"Processed payment for order_id {order_id}, credited {credited} to user {tg_id}")
            except Exception as e:
                logger.error(f"Error processing payment for order_id {order_id}: {e}")
        else:
            logger.info(f"Duplicate payment notification for order_id {order_id} ignored")
    return {"ok": True}

@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await app.initialize()
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

@api.post("/force-address")
async def force_address(request: Request):
    global next_deposit_address
    try:
        new_address_data = nowpayments_create_payment(0)
        next_deposit_address = new_address_data["pay_address"]
        save_next_address(next_deposit_address)
        logger.info(f"Forced deposit address set: {next_deposit_address}")
        return {"status": "success", "address": next_deposit_address}
    except Exception as e:
        logger.error(f"Failed to force address: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create address: {str(e)}")

# Telegram bot handlers
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
    global first_address_assigned, next_deposit_address
    uid = update.effective_user.id
    user = get_user(uid)
    if not BASE_URL or not NOWPAY_API_KEY:
        await update.message.reply_text("Service not configured. Contact admin.")
        return
    logger.info(f"next_deposit_address: {next_deposit_address}, first_address_assigned: {first_address_assigned}")
    if not user.get("deposit_address"):
        if not first_address_assigned and next_deposit_address:
            user["deposit_address"] = next_deposit_address
            first_address_assigned = True
            save_users()
            await update.message.reply_text(f"Your permanent deposit address: {next_deposit_address}")
            try:
                new_address_data = nowpayments_create_payment(0)
                save_next_address(new_address_data["pay_address"])
                logger.info(f"New deposit address set for future users: {new_address_data['pay_address']}")
            except Exception as e:
                logger.error(f"Failed to request new deposit address: {str(e)}")
        elif next_deposit_address:
            user["deposit_address"] = next_deposit_address
            save_users()
            await update.message.reply_text(f"Your permanent deposit address: {next_deposit_address}")
            try:
                new_address_data = nowpayments_create_payment(0)
                save_next_address(new_address_data["pay_address"])
                logger.info(f"New deposit address set: {new_address_data['pay_address']}")
            except Exception as e:
                logger.error(f"Failed to request new deposit address: {str(e)}")
        else:
            await update.message.reply_text("No addresses available. Try again later. The system is working to obtain one.")
    else:
        await update.message.reply_text(f"Your permanent deposit address: {user['deposit_address']}")

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

async def cmd_my_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    count = sum(1 for u in users.values() if u.get("referrer_id") == uid and u.get("first_package_activated", False))
    await update.message.reply_text(f"Your qualified friends are {count}")

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
        user["withdraw_address"] = message_text
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
            qualified_friends = sum(1 for u in users.values() if u.get("referrer_id") == uid and u.get("first_package_activated", False))
            if ADMIN_CHANNEL_ID:
                message = f"New Withdrawal Request:\nUser ID: {uid}\nAddress: {user['withdraw_address']}\nAmount: {amount} USDT\nQualified Friends: {qualified_friends}"
                try:
                    await app.bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=message)
                except Exception as e:
                    logger.error(f"Failed to send notification to admin channel: {e}")
                    add_balance(uid, amount)
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

# Setup & Run with Lifespan
api = FastAPI()
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

@asynccontextmanager
async def lifespan(app):
    global next_deposit_address
    # Startup logic
    if BASE_URL:
        webhook_url = f"{BASE_URL}/telegram/webhook"
        try:
            await app.bot.set_webhook(webhook_url)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {str(e)}")
    else:
        logger.warning("BASE_URL not set. Use /set-webhook to configure Telegram webhook.")
    if next_deposit_address is None:
        logger.info("Attempting to create initial deposit address...")
        attempt = 0
        while next_deposit_address is None:
            attempt += 1
            try:
                new_address_data = nowpayments_create_payment(0)
                next_deposit_address = new_address_data["pay_address"]
                with open("next_address.json", "w") as f:
                    json.dump({"address": next_deposit_address}, f)
                logger.info(f"Initial deposit address set after attempt {attempt}: {next_deposit_address}")
            except requests.exceptions.RequestException as e:
                if hasattr(e.response, 'status_code'):
                    logger.error(f"API error {e.response.status_code} on attempt {attempt}: {str(e)}. Retrying in 60 seconds...")
                else:
                    logger.error(f"Network error on attempt {attempt}: {str(e)}. Retrying in 60 seconds...")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Unexpected error on attempt {attempt}: {str(e)}. Retrying in 60 seconds...")
                await asyncio.sleep(60)
    yield
    # Shutdown logic
    save_users()
    save_processed_orders()
    save_next_address(next_deposit_address)
    await app.stop()

api.router.lifespan = lifespan

@api.get("/")
def root():
    return {"ok": True}

class NowPaymentsIPN(BaseModel):
    payment_status: str
    actually_paid: Union[str, float, int]
    pay_amount: Union[str, float]
    order_id: str
    pay_address: str

@api.post("/ipn/nowpayments")
async def ipn_nowpayments(request: Request, x_nowpayments_sig: str = Header(None)):
    raw = await request.body()
    if not x_nowpayments_sig or not verify_nowpay_signature(raw, x_nowpayments_sig):
        raise HTTPException(status_code=400, detail="Bad signature")
    data = NowPaymentsIPN(**json.loads(raw.decode("utf-8")))
    status = (data.payment_status or "").lower()
    credited = float(data.actually_paid or data.pay_amount or 0.0)
    order_id = data.order_id
    pay_address = data.pay_address
    logger.info(f"Received IPN for order_id {order_id}, status {status}, credited {credited}, address {pay_address}")
    if status in {"finished", "confirmed"} and order_id and credited > 0:
        if order_id not in processed_orders:
            try:
                tg_id = int(str(order_id).split("-")[0])
                user = get_user(tg_id)
                if user.get("deposit_address") == pay_address:
                    add_balance(tg_id, credited)
                    await app.bot.send_message(chat_id=tg_id, text=f"{credited} USDT Deposit Successfully")
                    processed_orders.add(order_id)
                    save_processed_orders()
                    logger.info(f"Processed payment for order_id {order_id}, credited {credited} to user {tg_id}")
            except Exception as e:
                logger.error(f"Error processing payment for order_id {order_id}: {e}")
        else:
            logger.info(f"Duplicate payment notification for order_id {order_id} ignored")
    return {"ok": True}

@api.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await app.initialize()
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

@api.post("/force-address")
async def force_address(request: Request):
    global next_deposit_address
    try:
        new_address_data = nowpayments_create_payment(0)
        next_deposit_address = new_address_data["pay_address"]
        save_next_address(next_deposit_address)
        logger.info(f"Forced deposit address set: {next_deposit_address}")
        return {"status": "success", "address": next_deposit_address}
    except Exception as e:
        logger.error(f"Failed to force address: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create address: {str(e)}")

# Run the application
if __name__ == "__main__":
    missing = []
    for name in ["BOT_TOKEN", "NOWPAY_API_KEY", "NOWPAY_IPN_SECRET"]:
        if not globals().get(name):
            missing.append(name)
    if missing:
        raise RuntimeError(f"Missing required config values: {', '.join(missing)}")
    config = uvicorn.Config(api, host="0.0.0.0", port=PORT, log_level="info", workers=1)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
