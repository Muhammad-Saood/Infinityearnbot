import os
import json
import time
import hmac
import hashlib
import datetime as dt
from typing import Optional, Dict, Any, List
import requests
import asyncio

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv  # Optional, for local testing

# Load .env file for local testing (ignored on Koyeb)
load_dotenv()

# ----------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@InfinityEarn2x")
BASE_URL = os.getenv("BASE_URL")  # Can be None initially
PORT = int(os.getenv("PORT", "8000"))

if not firebase_admin._apps:
    try:
        # Load Firebase config from file
        with open("firebase.json", "r") as f:
            config_dict = json.load(f)
        cred = credentials.Certificate(config_dict)
        firebase_admin.initialize_app(cred)
    except FileNotFoundError:
        raise RuntimeError("❌ firebase.json not found in project directory")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"❌ Invalid JSON in firebase.json: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"❌ Failed to initialize Firebase: {str(e)}")
db = firestore.client()
NOWPAY_API = "https://api.nowpayments.io/v1"
USDT_BSC_CODE = "usdtbsc"
PACKAGES = {10: 0.33, 20: 0.66, 50: 1.66, 100: 3.33, 200: 6.66, 500: 16.66, 1000: 33.33}
PACKAGE_DAYS = 60
MIN_WITHDRAW = 3.0
COL_USERS = db.collection("users")
COL_DEPOSITS = db.collection("deposits")
COL_WITHDRAWS = db.collection("withdrawals")
ASK_WD_ADDR, ASK_WD_AMOUNT = range(2)

api = FastAPI()

# ----------------- FIREBASE UTILITIES -----------------
def user_ref(uid: int):
    return COL_USERS.document(str(uid))

def ensure_user(uid: int, referrer_id: Optional[int] = None):
    ref = user_ref(uid)
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict()
        if referrer_id and not data.get("referrer_id"):
            ref.update({"referrer_id": referrer_id})
        return
    ref.set({
        "balance": 0.0,
        "verified": False,
        "created_at": firestore.SERVER_TIMESTAMP,
        "referrer_id": referrer_id,
        "packages": [],
        "first_package_activated": False
    })

def get_user(uid: int) -> Dict[str, Any]:
    snap = user_ref(uid).get()
    if snap.exists:
        return snap.to_dict()
    ensure_user(uid)
    return user_ref(uid).get().to_dict()

def add_balance(uid: int, amount: float):
    ref = user_ref(uid)
    def txn(t):
        s = t.get(ref)
        cur = s.to_dict().get("balance", 0.0)
        t.update(ref, {"balance": round(cur + amount, 8)})
    db.run_transaction(txn)

def deduct_balance(uid: int, amount: float) -> bool:
    ref = user_ref(uid)
    try:
        def txn(t):
            s = t.get(ref)
            cur = s.to_dict().get("balance", 0.0)
            if cur + 1e-9 < amount:
                raise ValueError("INSUFFICIENT")
            t.update(ref, {"balance": round(cur - amount, 8)})
        db.run_transaction(txn)
        return True
    except ValueError:
        return False

def append_package(uid: int, pack: Dict[str, Any]):
    ref = user_ref(uid)
    def txn(t):
        s = t.get(ref)
        data = s.to_dict()
        packs = data.get("packages", [])
        packs.append(pack)
        t.update(ref, {"packages": packs})
    db.run_transaction(txn)

def active_packages(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    now = dt.datetime.utcnow()
    out = []
    for p in user.get("packages", []):
        if dt.datetime.fromtimestamp(p["end_ts"]) > now:
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

@api.post("/ipn/nowpayments")
async def ipn_nowpayments(request: Request, x_nowpayments_sig: str = Header(None)):
    raw = await request.body()
    if not x_nowpayments_sig or not verify_nowpay_signature(raw, x_nowpayments_sig):
        raise HTTPException(status_code=400, detail="Bad signature")
    data = BaseModel(**json.loads(raw.decode("utf-8")))
    status = (data.payment_status or "").lower()
    credited = float(data.actually_paid or data.pay_amount or 0.0)
    if status in {"finished", "confirmed"} and data.order_id and credited > 0:
        try:
            tg_id = int(str(data.order_id).split("-")[0])
        except Exception:
            return {"ok": True}
        COL_DEPOSITS.add({
            "user_id": tg_id,
            "payment_id": data.payment_id,
            "amount": credited,
            "currency": data.pay_currency,
            "status": status,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        add_balance(tg_id, credited)
        try:
            await app.bot.send_message(chat_id=tg_id, text=f"{credited} USDT Deposit Successfully")
        except Exception:
            pass
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
    'Join our Telegram Channel for latest announcements and verify your account to start your earning now.'
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    uid = update.effective_user.id
    ensure_user(uid, referrer)
    kb = [
        [InlineKeyboardButton("📢 Telegram Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("✅ Verify", callback_data="verify_channel")]
    ]
    await update.message.reply_text(WELCOME_TEXT, reply_markup=InlineKeyboardMarkup(kb))

async def cb_verify_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=uid)
        joined = member.status in ("member", "administrator", "creator")
    except Exception:
        joined = False
    if not joined:
        await q.edit_message_text("Join our channel and verify first.")
        return
    refobj = user_ref(uid)
    refobj.update({"verified": True})
    await q.edit_message_text(
        "Congratulations!\n"
        "You have been verified. Deposit your balance, select your package by sending commands from the menu, and start your earning journey. "
        "You can also select multiple packages one by one to boost your earning."
    )

async def cmd_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not user.get("verified"):
        await update.message.reply_text("Please join our channel and verify first.")
        return
    if not BASE_URL or not NOWPAY_API_KEY:
        await update.message.reply_text("Service not configured. Contact admin.")
        return
    try:
        pay = nowpayments_create_payment(uid)
        pay_address = pay.get("pay_address") or pay.get("wallet_address") or pay.get("payment_address")
        if not pay_address:
            inv = pay.get("invoice_url") or pay.get("payment_url") or pay.get("url")
            if inv:
                await update.message.reply_text(f"Your receiving address of USDT on BSC (Binance Smart Chain) is:\n{inv}\n\n(Open and pay on BSC/USDT)")
                return
            await update.message.reply_text("Could not get deposit address. Try again later.")
            return
        COL_DEPOSITS.add({
            "user_id": uid,
            "payment_response": pay,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        await update.message.reply_text(f"Your receiving address of USDT on BSC (Binance Smart Chain) is\n{pay_address}")
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
    if not user.get("verified"):
        await q.edit_message_text("Please join our channel and verify first.")
        return
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
    now = dt.datetime.utcnow()
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
    fresher = get_user(uid)
    if not fresher.get("first_package_activated"):
        refid = fresher.get("referrer_id")
        if refid:
            bonus = round(price * 0.10, 8)
            add_balance(int(refid), bonus)
        user_ref(uid).update({"first_package_activated": True})
    await q.edit_message_text(f"Package activated: {price} USDT for {PACKAGE_DAYS} days.")

async def cmd_daily_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    packs = active_packages(user)
    if not packs:
        await update.message.reply_text("No active packages.")
        return
    today = dt.datetime.utcnow().date().isoformat()
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
        ref = user_ref(uid)
        def txn(t):
            s = t.get(ref)
            data = s.to_dict()
            existing = data.get("packages", [])
            idx = {(pp["name"], pp["start_ts"]): i for i, pp in enumerate(existing)}
            for p in packs:
                key = (p["name"], p["start_ts"])
                i = idx.get(key)
                if i is not None:
                    existing[i] = p
            t.update(ref, {"packages": existing})
        db.run_transaction(txn)
        add_balance(uid, round(total, 8))
    await update.message.reply_text(f"Daily reward added: {round(total,8)} USDT")

async def cmd_my_packages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
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
    user = get_user(update.effective_user.id)
    bal = round(user.get("balance", 0.0), 8)
    await update.message.reply_text(f"Your current balance is {bal} USDT")

async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter your USDT withdrawal address on BSC (Binance Smart Chain).", reply_markup=ReplyKeyboardRemove())
    return ASK_WD_ADDR

def is_bsc_address(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42

async def ask_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not is_bsc_address(addr):
        await update.message.reply_text("Invalid BSC address. Try again.")
        return ASK_WD_ADDR
    context.user_data["wd_addr"] = addr
    await update.message.reply_text("Enter your withdrawal amount.")
    return ASK_WD_AMOUNT

async def finalize_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text.strip()
    try:
        amount = float(txt)
    except Exception:
        await update.message.reply_text("Enter a valid number.")
        return ASK_WD_AMOUNT
    if amount < MIN_WITHDRAW:
        await update.message.reply_text("Insufficient withdrawal amount.")
        return ConversationHandler.END
    if not deduct_balance(uid, amount):
        await update.message.reply_text("Insufficient balance.")
        return ConversationHandler.END
    COL_WITHDRAWS.add({
        "user_id": uid,
        "address": context.user_data.get("wd_addr"),
        "amount": float(amount),
        "status": "pending",
        "created_at": firestore.SERVER_TIMESTAMP
    })
    await update.message.reply_text("Withdrawal Successful! Your withdrawal will be credited to your wallet within 24 hours.")
    return ConversationHandler.END

async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Withdrawal cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def cmd_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref{uid}"
    await update.message.reply_text(link)

# ----------------- SETUP & RUN -----------------
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CallbackQueryHandler(cb_verify_channel, pattern="^verify_channel$"))
app.add_handler(CommandHandler("deposit", cmd_deposit))
app.add_handler(CommandHandler("packages", cmd_packages))
app.add_handler(CallbackQueryHandler(cb_package, pattern=r"^pkg:\d+$"))
app.add_handler(CommandHandler("daily_reward", cmd_daily_reward))
app.add_handler(CommandHandler("my_packages", cmd_my_packages))
app.add_handler(CommandHandler("my_balance", cmd_my_balance))
withdraw_conv = ConversationHandler(
    entry_points=[CommandHandler("withdraw", cmd_withdraw)],
    states={
        ASK_WD_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_withdraw_amount)],
        ASK_WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_withdraw)],
    },
    fallbacks=[CommandHandler("cancel", cancel_withdraw)],
)
app.add_handler(withdraw_conv)
app.add_handler(CommandHandler("referral_link", cmd_referral_link))

async def initialize_app():
    await app.initialize()
    if BASE_URL:
        webhook_url = f"{BASE_URL}/telegram/webhook"
        await app.bot.set_webhook(webhook_url)
        print(f"Webhook set to {webhook_url}")
    else:
        print("BASE_URL not set. Running FastAPI server only. Use /set-webhook to configure Telegram webhook.")

if __name__ == "__main__":
    missing = []
    for name in ["BOT_TOKEN", "NOWPAY_API_KEY", "NOWPAY_IPN_SECRET"]:
        if not globals().get(name):
            missing.append(name)
    if missing:
        raise RuntimeError(f"Missing required config values: {', '.join(missing)}")
    # Initialize Application and set webhook
    asyncio.run(initialize_app())
    # Run FastAPI server
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="info", workers=1)
