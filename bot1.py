# bot1.py — Twxn's Prepaid Market (FINAL)
# BTC+LTC deposits (2 conf => USD), referrals (15%), support tickets (view/reply/resolve),
# shop (admin add posts to stock channel), identical back-to-home message, stock updates link.

import os
import math
import asyncio
import shutil
import datetime
from decimal import Decimal, ROUND_UP
from datetime import datetime
from typing import List, Dict, Optional

import requests
import aiohttp
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

# --- Load environment variables ---
load_dotenv()
print("BOT_TOKEN loaded:", os.getenv("BOT_TOKEN"))

# --- Import models from models.py ---
from models import Base, User, Wallet, Card, Order, Referral, ReferralBonus, SupportTicket

# --- Import balance tools (no circular import now) ---
from balance_pool import start_worker_pool, check_card_async, periodic_balance_refresh


# --- Compute rate helper (for now, fixed 1:1) ---
def compute_rate_for_card(card):
    """
    Compute rate multiplier for a card.
    You can expand this later to give discounts or fees.
    """
    return Decimal("1.0")  # just sell at full balance value for now
# =========================
# Database setup (must be BEFORE any asyncio tasks)
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()

# use /opt/render/project/src/data/market.db on Render, ./data locally
base_dir = os.path.dirname(os.path.abspath(__file__))
data_dir = os.path.join(base_dir, "data")
os.makedirs(data_dir, exist_ok=True)

db_path = os.path.join(data_dir, "market.db")
print(f"📦 Using SQLite DB at: {db_path}")

engine = create_engine(
    f"sqlite:///{db_path}",
    connect_args={"check_same_thread": False},
    echo=False,
    future=True
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
Base.metadata.create_all(bind=engine)

# =========================
# Daily Database Backup
# =========================
async def daily_db_backup(db_path: str, keep_days: int = 7):
    """Backs up SQLite DB once per 24h and prunes old backups."""
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    while True:
        try:
            now = datetime.datetime.now()
            backup_name = f"market_{now.strftime('%Y-%m-%d_%H-%M')}.db"
            backup_path = os.path.join(backup_dir, backup_name)

            shutil.copy2(db_path, backup_path)
            print(f"💾 Database backup saved: {backup_path}")

            # Remove old backups (older than keep_days)
            cutoff = now - datetime.timedelta(days=keep_days)
            for f in os.listdir(backup_dir):
                full_path = os.path.join(backup_dir, f)
                try:
                    if os.path.isfile(full_path):
                        ts = datetime.datetime.strptime(f.split("_")[1], "%Y-%m-%d")
                        if ts < cutoff:
                            os.remove(full_path)
                            print(f"🧹 Removed old backup: {f}")
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ Backup error: {e}")

        # wait 24 hours (86400 seconds)
        await asyncio.sleep(86400)

# =========================
# Start bot + background tasks
# =========================
from aiogram import Bot, Dispatcher
import asyncio
import os
# =========================
# Bot setup and /start handler
# =========================
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Create bot + dispatcher first
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

print("📋 Registered handlers before definition:", dp.message.handlers)

# Then define /start handler (AFTER dp exists)
@dp.message(Command("start"))
async def on_start(msg: types.Message):
    print("🚀 /start triggered!")

    db = SessionLocal()
    try:
        # ensure user exists
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(
                id=msg.from_user.id,
                username=msg.from_user.username,
                display_name=msg.from_user.full_name
            )
            db.add(u)
            db.commit()

        # get wallet + stats
        w_usd = get_or_create_wallet(db, msg.from_user.id, "USD")
        usd_balance = Decimal(w_usd.balance or 0)
        purchased = db.query(Order).filter(Order.user_id == msg.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status == "in_stock").count()

        # main text (your Twxn’s Market layout)
        text = (
            "💳 *Welcome to Twxn’s Prepaid Market!*\n\n"
            "💰 *Account Info:*\n"
            f"• Account Balance: *{money(usd_balance)}*\n"
            f"• Purchased cards: *{purchased}*\n"
            f"• In stock now: *{stock_count}*\n\n"
            "📰 *Stock Updates:*\n"
            f"[Join Here]({STOCK_INVITE_URL})\n\n"
            "🆘 *Need Help?*\n"
            "Open a *Support Ticket* below or reach out at @letwxn"
        )

        # main buttons
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Browse Cards", callback_data="shop:page:1")],
            [InlineKeyboardButton(text="💼 Wallet", callback_data="home:wallet")],
            [InlineKeyboardButton(text="🎁 Referrals", callback_data="referrals")],
            [InlineKeyboardButton(text="🧾 Orders", callback_data="orders")],
            [InlineKeyboardButton(text="🆘 Support", callback_data="support:start")]
        ])

        await msg.answer(text, parse_mode="Markdown", reply_markup=kb)
    finally:
        db.close()

print("📋 Registered handlers after definition:", dp.message.handlers)

# =========================================================
# 🧠 AUTO DATABASE FIXER — Prevents SQL errors on startup
# =========================================================
from sqlalchemy import inspect, text

def ensure_db_schema(engine):
    """
    Ensures all required tables and columns exist.
    Adds missing columns to prevent runtime crashes.
    """
    required_schema = {
        "users": {
            "id": "BIGINT PRIMARY KEY",
            "username": "TEXT",
            "display_name": "TEXT",
            "created_at": "DATETIME"
        },
        "wallets": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "user_id": "BIGINT",
            "coin": "TEXT",
            "balance": "NUMERIC DEFAULT 0",
            "deposit_address": "TEXT",
            "address_index": "INTEGER",
            "created_at": "DATETIME"
        },
        "transactions": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "wallet_id": "INTEGER",
            "txid": "TEXT",
            "amount": "NUMERIC",
            "confirmations": "INTEGER DEFAULT 0",
            "status": "TEXT",
            "coin": "TEXT",
            "created_at": "DATETIME",
            "updated_at": "DATETIME"
        },
        "cards": {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "site": "TEXT",
    "bin": "TEXT",
    "cc_number": "TEXT",
    "exp": "TEXT",
    "cvv": "TEXT",
    "encrypted_code": "TEXT",
    "balance": "NUMERIC",
    "price": "NUMERIC",
    "currency": "TEXT",
    "status": "TEXT",
    "added_by": "BIGINT",
    "created_at": "DATETIME"
},

        "orders": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "user_id": "BIGINT",
            "card_id": "INTEGER",
            "price": "NUMERIC",
            "created_at": "DATETIME"
        },
        "support_tickets": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "user_id": "BIGINT",
            "subject": "TEXT",
            "content": "TEXT",
            "status": "TEXT",
            "created_at": "DATETIME",
            "closed_at": "DATETIME"
        }
    }

    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    with engine.connect() as conn:
        for table_name, columns in required_schema.items():
            if table_name not in existing_tables:
                # Create table if missing
                cols = ", ".join([f"{col} {ctype}" for col, ctype in columns.items()])
                conn.execute(text(f"CREATE TABLE {table_name} ({cols});"))
                print(f"✅ Created missing table: {table_name}")
            else:
                # Check for missing columns
                existing_cols = [c["name"] for c in inspector.get_columns(table_name)]
                for col, ctype in columns.items():
                    if col not in existing_cols:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col} {ctype};"))
                        print(f"⚙️ Added missing column '{col}' to '{table_name}'")
        conn.commit()

# HD derivation
from bip_utils import Bip44, Bip44Coins, Bip44Changes

from dotenv import load_dotenv
import os, sys

# Load .env if it exists, otherwise fall back to environment variables
load_dotenv(override=True)

# (optional) quick debug — safe version for both local and Render
print("[env] .env file checked (Render uses system vars if None)")
print(f"[env] BOT_TOKEN (env): {os.getenv('BOT_TOKEN')!r}")

BOT_TOKEN = os.getenv("BOT_TOKEN") or ""             # REQUIRED
FERNET_KEY = os.getenv("FERNET_KEY") or ""           # REQUIRED (Fernet base64 key)

# --- DATABASE PATH FIX FOR LOCAL MAC SETUP ---
from sqlalchemy import create_engine

# Store the database in a local "data" folder next to the script
base_dir = os.path.dirname(os.path.abspath(__file__))
db_dir = os.path.join(base_dir, "data")
os.makedirs(db_dir, exist_ok=True)
db_path = os.path.join(db_dir, "market.db")

engine = create_engine(
    "sqlite:////Users/Twxn/Desktop/twxn/data/market.db",
    connect_args={"check_same_thread": False},
    echo=False,
    future=True
)


print(f"✅ Using database at: {db_path}")

# Admin & stock
ADMIN_IDS        = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
STOCK_CHANNEL_ID = int(os.getenv("STOCK_CHANNEL_ID", "-1003283018931"))
SUPPORT_HANDLE   = os.getenv("SUPPORT_HANDLE", "@letwxn")

# Bot username (hardcode as confirmed; still fetched if empty)
BOT_USERNAME     = (os.getenv("BOT_USERNAME", "letwxnbot") or "letwxnbot").lstrip("@")

# XPUBs (confirmed)
BTC_XPUB         = (os.getenv("BTC_XPUB", "xpub6C1scCQwRsdQBXib13cZ8qLSZ4uXZc5wBr6M6QoNUp3RFapVN4HYhtQ27DwtiyGs48k28Wb169tNLrf7fFsR3E91reuRfu4hHwDMe5wcY8Z")).strip()
LTC_XPUB         = (os.getenv("LTC_XPUB", "xpub6DSZJ8CxRCzp14xeypaPe71Z4d7DZMC35w6RVcKqajSfvsSrNZetLEA9QqyvsUQTgK2jsrh7eQRoqTynvgVX4j7q3UTWAw8C5kLP9Vf5rqN")).strip()

# Rates poll & deposit poll intervals
RATES_INTERVAL   = int(os.getenv("RATES_INTERVAL", "60"))
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL", "20"))

# Private stock channel invite (provided)
STOCK_INVITE_URL = os.getenv("STOCK_INVITE_URL", "https://t.me/+ntzN_J5td7c2ZGYx")

if not BOT_TOKEN or not FERNET_KEY:
    raise SystemExit("❌ Missing BOT_TOKEN or FERNET_KEY in .env")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

BTC_USD_RATE = Decimal("68000.0")
LTC_USD_RATE = Decimal("80.0")

async def update_crypto_rates():
    """Fetch BTC and LTC USD prices every 3 minutes using CoinGecko."""
    global BTC_USD_RATE, LTC_USD_RATE
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,litecoin&vs_currencies=usd"
                ) as resp:
                    data = await resp.json()
                    BTC_USD_RATE = Decimal(str(data["bitcoin"]["usd"]))
                    LTC_USD_RATE = Decimal(str(data["litecoin"]["usd"]))
                    print(f"[💱 Rates Updated] BTC=${BTC_USD_RATE} | LTC=${LTC_USD_RATE}")
        except Exception as e:
            print(f"[Rate Update Error] {e}")
        await asyncio.sleep(180)  # every 3 minutes


# DB Setup
# =========================
from models import Base, User, Wallet, Card, Order, Referral, ReferralBonus, SupportTicket

SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
Base.metadata.create_all(bind=engine)
# Helpers
# =========================
def enc_text(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def dec_text(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

def money(v) -> str:
    return f"${Decimal(v):.2f}"

def paginate(items: List, page: int, per: int = 10):
    total = len(items)
    pages = max(1, math.ceil(total / per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    end = start + per
    return items[start:end], page, pages

def get_or_create_wallet(db, user_id: int, coin: str) -> Wallet:
    w = db.query(Wallet).filter(Wallet.user_id==user_id, Wallet.coin==coin).first()
    if w: return w
    w = Wallet(user_id=user_id, coin=coin, balance=0)
    db.add(w); db.commit()
    return w

# Address derivation from XPUB
def derive_addr_from_xpub(xpub: str, coin: str, index: int) -> str:
    if not xpub:
        raise RuntimeError(f"{coin}_XPUB not configured")
    if coin == "BTC":
        acc = Bip44.FromExtendedKey(xpub, Bip44Coins.BITCOIN)
    elif coin == "LTC":
        acc = Bip44.FromExtendedKey(xpub, Bip44Coins.LITECOIN)
    else:
        raise ValueError("Unsupported coin")
    return acc.Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()

def get_or_create_deposit_address(db, user_id: int, coin: str) -> str:
    w = db.query(Wallet).filter(Wallet.user_id==user_id, Wallet.coin==coin).first()
    if w and w.deposit_address:
        return w.deposit_address
    existing_max = db.query(Wallet).filter(Wallet.coin==coin, Wallet.address_index.isnot(None))\
        .order_by(Wallet.address_index.desc()).first()
    idx = (existing_max.address_index + 1) if (existing_max and existing_max.address_index is not None) else 0
    if coin == "BTC":
        addr = derive_addr_from_xpub(BTC_XPUB, "BTC", idx)
    elif coin == "LTC":
        addr = derive_addr_from_xpub(LTC_XPUB, "LTC", idx)
    else:
        raise ValueError("Unsupported coin")
    if not w:
        w = Wallet(user_id=user_id, coin=coin, balance=0, deposit_address=addr, address_index=idx)
        db.add(w)
    else:
        w.deposit_address = addr
        w.address_index = idx
    db.commit()
    return addr

# BlockCypher explorer (BTC/LTC)
def bc_tx_list(coin: str, address: str) -> List[Dict]:
    chain = "btc" if coin == "BTC" else "ltc"
    url = f"https://api.blockcypher.com/v1/{chain}/main/addrs/{address}"
    try:
        r = requests.get(url, timeout=12)
        if r.status_code != 200:
            return []
        data = r.json()
        txrefs = data.get("txrefs", []) + data.get("unconfirmed_txrefs", [])
        out = []
        for t in txrefs:
            txid = t.get("tx_hash")
            conf = t.get("confirmations", 0)
            value = Decimal(t.get("value", 0)) / Decimal(1e8)
            out.append({"txid": txid, "amount": value, "confirmations": conf})
        return out
    except Exception:
        return []

# FX helpers
def usd_to_coin_amount(price_usd: Decimal, coin: str) -> Decimal:
    if coin == "LTC":
        if LTC_USD_RATE <= 0: raise RuntimeError("LTC_USD_RATE must be > 0")
        return (Decimal(price_usd) / LTC_USD_RATE).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
    if coin == "BTC":
        if BTC_USD_RATE <= 0: raise RuntimeError("BTC_USD_RATE must be > 0")
        return (Decimal(price_usd) / BTC_USD_RATE).quantize(Decimal("0.00000001"), rounding=ROUND_UP)
    raise ValueError("Unsupported coin")

@dp.message(Command("start"))
async def on_start(msg: types.Message):
    print("🚀 /start triggered!")

    db = SessionLocal()
    try:
        # ensure user exists
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(
                id=msg.from_user.id,
                username=msg.from_user.username,
                display_name=msg.from_user.full_name
            )
            db.add(u)
            db.commit()

        # get wallet + stats
        w_usd = get_or_create_wallet(db, msg.from_user.id, "USD")
        usd_balance = Decimal(w_usd.balance or 0)
        purchased = db.query(Order).filter(Order.user_id == msg.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status == "in_stock").count()

        # check admin
        is_admin = msg.from_user.id in ADMIN_IDS

        # main text
        text = (
            "💳 *Welcome to Twxn’s Prepaid Market!*\n\n"
            "💰 *Account Info:*\n"
            f"• Account Balance: *{money(usd_balance)}*\n"
            f"• Purchased cards: *{purchased}*\n"
            f"• In stock now: *{stock_count}*\n\n"
            "📰 *Stock Updates:*\n"
            f"[Join Here]({STOCK_INVITE_URL})\n\n"
            "🆘 *Need Help?*\n"
            "Open a *Support Ticket* below or reach out at @letwxn"
        )

        # user buttons
        kb_buttons = [
            [InlineKeyboardButton(text="🛒 Browse Cards", callback_data="shop:page:1")],
            [InlineKeyboardButton(text="💼 Wallet", callback_data="home:wallet")],
            [InlineKeyboardButton(text="🎁 Referrals", callback_data="referrals")],
            [InlineKeyboardButton(text="🧾 Orders", callback_data="orders")],
            [InlineKeyboardButton(text="🆘 Support", callback_data="support:start")]
        ]

        # add admin panel
        if is_admin:
            kb_buttons.append([InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin:home")])

        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        await msg.answer(text, parse_mode="Markdown", reply_markup=kb)
    finally:
        db.close()

    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_home_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home_back")]]
    )

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Card", callback_data="adm:add")],
            [InlineKeyboardButton(text="📋 View Stock", callback_data="adm:view:1")],
            [InlineKeyboardButton(text="💬 View Support Tickets", callback_data="admin:view_tickets")],
            [InlineKeyboardButton(text="💸 Load USD Balance", callback_data="adm:load")],
            [InlineKeyboardButton(text="🧾 View Orders", callback_data="adm:orders:1")],
            [InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home_back")],
        ]
    )

# =========================
# Home Screen (exact layout) + /start + Back
# =========================
def home_message_text(usd_balance: Decimal, purchased: int, stock_count: int) -> str:
    return (
        "💳 *Welcome to Twxn's Prepaid Market, Twxn!*\n\n"
        "💰 *Account Info:*\n"
        f"• Account Balance: *{money(usd_balance)}*\n"
        f"• Purchased cards: *{purchased}*\n"
        f"• In stock now: *{stock_count}*\n\n"
        "📰 *Stock Updates:*\n"
        f"[Join Here]({STOCK_INVITE_URL})\n\n"
        "🆘 *Need Help?*\n"
        "Open a *Support Ticket* below or reach out at @letwxn"
    )

@dp.message(Command("start"))
async def on_start(msg: types.Message):
    db = SessionLocal()
    try:
        # ensure user
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(id=msg.from_user.id, username=msg.from_user.username, display_name=msg.from_user.full_name)
            db.add(u); db.commit()

        # referral capture: /start ref_<id>
        try:
            if msg.text and " " in msg.text:
                _, param = msg.text.split(" ", 1)
                param = param.strip()
                if param.startswith("ref_"):
                    ref_id = int(param.replace("ref_", ""))
                    if ref_id != msg.from_user.id:
                        exists = db.query(Referral).filter(Referral.user_id==msg.from_user.id).first()
                        if not exists:
                            db.add(Referral(user_id=msg.from_user.id, referrer_id=ref_id)); db.commit()
        except Exception:
            pass

        # wallets
        w_usd = get_or_create_wallet(db, msg.from_user.id, "USD")
        get_or_create_wallet(db, msg.from_user.id, "BTC")
        get_or_create_wallet(db, msg.from_user.id, "LTC")

        purchased = db.query(func.count(Order.id)).filter(Order.user_id==msg.from_user.id).scalar() or 0
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
        usd_balance = Decimal(w_usd.balance or 0)
    finally:
        db.close()

    text = home_message_text(usd_balance, purchased, stock_count)
    await msg.answer(text, parse_mode="Markdown", disable_web_page_preview=True,
                     reply_markup=home_menu_keyboard(msg.from_user.id in ADMIN_IDS))

@dp.callback_query(F.data == "home_back")
async def home_back_cb(cq: types.CallbackQuery):
    uid = cq.from_user.id
    db = SessionLocal()
    try:
        w_usd = get_or_create_wallet(db, uid, "USD")
        purchased = db.query(func.count(Order.id)).filter(Order.user_id==uid).scalar() or 0
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
        text = home_message_text(Decimal(w_usd.balance or 0), purchased, stock_count)
    finally:
        db.close()
    await cq.message.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True,
                               reply_markup=home_menu_keyboard(uid in ADMIN_IDS))
    await cq.answer()

# =========================
# Wallet & Deposits
# =========================
@dp.callback_query(F.data == "home:wallet")
async def wallet_inline(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        w_btc = get_or_create_wallet(db, cq.from_user.id, "BTC")
        w_ltc = get_or_create_wallet(db, cq.from_user.id, "LTC")
        usd = Decimal(w_usd.balance or 0)
        btc = Decimal(w_btc.balance or 0)
        ltc = Decimal(w_ltc.balance or 0)
    finally:
        db.close()
    txt = (
        "🏦 *Make a Deposit*\n\n"
        f"USD: {money(usd)}\n"
        f"BTC: {btc:.8f} (~{money(btc * BTC_USD_RATE)})\n"
        f"LTC: {ltc:.8f} (~{money(ltc * LTC_USD_RATE)})\n\n"
        "Choose a coin below. Your USD wallet is credited after *2 confirmations*."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔸 Deposit BTC", callback_data="deposit:BTC"),
         InlineKeyboardButton(text="🔹 Deposit LTC", callback_data="deposit:LTC")],
        [InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home_back")]
    ])
    await cq.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("deposit:"))
async def on_deposit_coin(cq: types.CallbackQuery):
    await cq.answer()
    coin = cq.data.split(":")[1]  # BTC | LTC
    db = SessionLocal()
    try:
        addr = get_or_create_deposit_address(db, cq.from_user.id, coin)
    except Exception as e:
        await cq.message.answer(f"❌ Could not generate deposit address for {coin}: {e}", reply_markup=back_home_button())
        db.close()
        return
    finally:
        db.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:wallet")]
    ])
    await cq.message.answer(f"📥 Send *{coin}* to:\n`{addr}`\n\nWe credit your *USD* wallet after 2 confirmations.", parse_mode="Markdown", reply_markup=kb)

# =========================
# Orders view
# =========================
@dp.callback_query(F.data == "home:orders")
async def my_orders_inline(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        orders = db.query(Order).filter(Order.user_id==cq.from_user.id).order_by(Order.id.desc()).limit(20).all()
    finally:
        db.close()
    if not orders:
        await cq.message.edit_text("🧾 No purchases yet.", reply_markup=back_home_button()); return
    lines = ["📦 *Your Orders*\n"]
    for o in orders:
        lines.append(f"• #{o.id} — {money(o.price_usd)} — {o.status} — {o.created_at.strftime('%Y-%m-%d %H:%M')}")
    await cq.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_home_button())

# =========================
# Shop (list & purchase)
# =========================
PAGE_SIZE = 10

def compute_rate_for_card(card: Card) -> Decimal:
    # 32% for BINs starting with 409758, else 40%
    try:
        bin_str = (card.bin or "").replace(" ", "")
        if bin_str.startswith("409758"):
            return Decimal("0.40")
    except Exception:
        pass
    return Decimal("0.40")

def format_card_line(idx: int, card: Card) -> str:
    face = Decimal(card.balance or 0)
    rate = compute_rate_for_card(card)
    cur = (card.currency or "USD").upper()
    return f"{idx}. {card.bin} {cur}${face:.2f} at {int(rate * 100)}% |"

def shop_menu_keyboard(cards: List[Card], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    start_idx = (page - 1) * PAGE_SIZE
    for i, card in enumerate(cards, start=1):
        display_text = f"{start_idx + i}. {card.bin} ${Decimal(card.balance or 0):.2f}"
        rows.append([
            InlineKeyboardButton(text=display_text, callback_data=f"shop:noop:{card.id}"),
            InlineKeyboardButton(text="🛒 Purchase", callback_data=f"shop:buy:{card.id}")
        ])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⏮️ First", callback_data=f"shop:page:1"))
        nav.append(InlineKeyboardButton(text="◀️ Back", callback_data=f"shop:page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"shop:page:{page+1}"))
        nav.append(InlineKeyboardButton(text="⏭️ Last", callback_data=f"shop:page:{pages}"))
    rows.append(nav if nav else [InlineKeyboardButton(text="⬅️ Back", callback_data="home_back")])
    rows.append([
        InlineKeyboardButton(text="💳 Deposit", callback_data="home:wallet"),
        InlineKeyboardButton(text="🔁 Refresh", callback_data=f"shop:page:{page}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def show_shop_page(user_id: int, page: int, ctx_message: Optional[types.Message] = None):
    db = SessionLocal()
    try:
        q = db.query(Card).filter(Card.status == "in_stock").order_by(Card.balance.desc())
        total = q.count()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, pages))
        items = q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

        w_usd = get_or_create_wallet(db, user_id, "USD")
        w_btc = get_or_create_wallet(db, user_id, "BTC")
        w_ltc = get_or_create_wallet(db, user_id, "LTC")
        usd_bal = Decimal(w_usd.balance or 0)
        btc_bal = Decimal(w_btc.balance or 0)
        ltc_bal = Decimal(w_ltc.balance or 0)
    finally:
        db.close()

    txt_lines = []
    txt_lines.append("💎 Twxn's Main Listings 💎\n")
    txt_lines.append("Your Balance:")
    txt_lines.append(f"💵 USD: {money(usd_bal)}")
    txt_lines.append(f"• BTC: {btc_bal:.8f} ({money(btc_bal * BTC_USD_RATE)})")
    txt_lines.append(f"• LTC: {ltc_bal:.8f} ({money(ltc_bal * LTC_USD_RATE)})")
    txt_lines.append("")
    if items:
        for idx, c in enumerate(items, start=1 + (page-1)*PAGE_SIZE):
            txt_lines.append(format_card_line(idx, c))
    else:
        txt_lines.append("No cards available right now.")
    updated = datetime.utcnow().strftime("%H:%M:%S UTC")
    txt_lines.append("")
    txt_lines.append(f"Page {page}/{pages} | Updated: {updated}")
    text = "\n".join(txt_lines)

    kb = shop_menu_keyboard(items, page, pages)
    if ctx_message:
        await ctx_message.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data == "home:shop")
async def shop_home_cb(cq: types.CallbackQuery):
    await cq.answer()
    await show_shop_page(cq.from_user.id, 1, cq.message)

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:page:"))
async def shop_page_cb(cq: types.CallbackQuery):
    await cq.answer()
    page = int(cq.data.split(":")[2])
    await show_shop_page(cq.from_user.id, page, cq.message)

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:noop:"))
async def shop_noop_cb(cq: types.CallbackQuery):
    await cq.answer()

from balance_pool import check_card_async
from decimal import Decimal

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:buy:"))
async def shop_buy_request_cb(cq: types.CallbackQuery):
    await cq.answer()
    _, _, cid = cq.data.split(":")
    cid = int(cid)
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status != "in_stock":
            await cq.message.answer("🚫 Card not available or sold.", reply_markup=back_home_button())
            return

        # ✅ Check live balance before confirming
        try:
            current_balance = await check_card_async(
                card.id,
                card.bin,
                card.cc_number,
                card.exp,
                dec_text(card.encrypted_code)
            )
        except RuntimeError as e:
            if "CAPTCHA_OR_BLOCK_DETECTED" in str(e):
                await cq.message.answer("⚠️ Temporarily unable to verify card balance. Please try again soon.", reply_markup=back_home_button())
                return
            else:
                await cq.message.answer(f"⚠️ Error checking card balance: {e}", reply_markup=back_home_button())
                return

        old_balance = Decimal(card.balance or 0)
        if abs(current_balance - old_balance) > Decimal("0.01"):
            # 🔄 Balance changed — update and ask user to confirm again
            card.balance = current_balance
            db.commit()
            await cq.message.answer(
                f"💳 Card balance changed!\n\n"
                f"Old: ${old_balance:.2f}\nNew: ${current_balance:.2f}\n\n"
                "Would you like to continue with this new balance?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Continue", callback_data=f"shop:buy:{cid}")],
                        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")]
                    ]
                )
            )
            return

        # ✅ Continue normal pricing if balance unchanged
        rate = compute_rate_for_card(card)
        sale_price = (Decimal(card.balance) * rate).quantize(Decimal("0.01"))
    finally:
        db.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Confirm {money(sale_price)}", callback_data=f"shop:confirm:{cid}:{sale_price}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")
        ],
        [InlineKeyboardButton(text="⬅️ Back", callback_data=f"shop:page:1")]
    ])

    await cq.message.answer(
        f"⚠️ Confirm purchase of card ID `{cid}` for *{money(sale_price)}* ?",
        parse_mode="Markdown",
        reply_markup=kb
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("shop:confirm:"))
async def shop_buy_confirm_cb(cq: types.CallbackQuery):
    await cq.answer()
    _, _, cid, price_str = cq.data.split(":")
    cid = int(cid)
    sale_price = Decimal(price_str)
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status != "in_stock":
            await cq.message.answer("🚫 Card not available.", reply_markup=back_home_button())
            return

        # ✅ Check live balance before selling
        if card.bin in ["403446", "435880", "511332", "409758"]:
            try:
                new_balance = await check_card_async(card.bin, card.cc_number, card.exp, dec_text(card.encrypted_code))
                print(f"💳 Live balance for card {card.id}: {new_balance}")
                if new_balance != card.balance:
                    diff = new_balance - card.balance
                    card.balance = new_balance
                    db.commit()
                    await cq.message.answer(
                        f"⚠️ The balance changed to ${new_balance:.2f}.\n"
                        "Do you still want to continue?",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Yes", callback_data=f"shop:confirm:{cid}:{new_balance}")],
                            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")]
                        ])
                    )
                    return
            except Exception as e:
                print(f"❌ Balance check failed for card {cid}: {e}")
                await cq.message.answer(f"⚠️ Could not verify card balance. Proceed anyway?", 
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Continue Anyway", callback_data=f"shop:confirm:{cid}:{sale_price}")],
                        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")]
                    ]))
                return

        # ✅ Regular flow after successful check
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        if Decimal(w_usd.balance or 0) < sale_price:
            await cq.message.answer(f"❌ Not enough USD balance. Need {money(sale_price)}.", reply_markup=back_home_button())
            return

        # Deduct & finalize purchase
        w_usd.balance = Decimal(w_usd.balance or 0) - sale_price
        card.status = "sold"
        order = Order(user_id=cq.from_user.id, card_id=card.id, price_usd=sale_price, coin_used="USD", coin_amount=sale_price)
        db.add(order)
        db.commit()

        code = dec_text(card.encrypted_code)
        msg = (
            f"✅ Purchase complete!\n\n"
            f"Card id: {card.id}\n"
            f"Site: {card.site or '—'}\n"
            f"BIN: {card.bin}\n"
            f"CC: {card.cc_number}\n"
            f"EXP: {card.exp}\n"
            f"CODE: `{code}`\n\n"
            f"Paid: {money(sale_price)}\nOrder ID: {order.id}"
        )
        await cq.message.answer(msg, parse_mode="Markdown", reply_markup=back_home_button())

    finally:
        db.close()

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:cancel:"))
async def shop_buy_cancel_cb(cq: types.CallbackQuery):
    await cq.answer("Purchase canceled.")
    await cq.message.answer("Purchase canceled.", reply_markup=back_home_button())

# =========================
# Referrals (fixed)
# =========================
@dp.callback_query(F.data == "home:referrals")
async def referrals_view(cq: types.CallbackQuery):
    await cq.answer()
    uid = cq.from_user.id
    db = SessionLocal()
    try:
        count = db.query(func.count(Referral.id)).filter(Referral.referrer_id==uid).scalar() or 0
        total_earned = db.query(func.coalesce(func.sum(ReferralBonus.amount_usd), 0)).filter(ReferralBonus.referrer_id==uid).scalar() or 0
    finally:
        db.close()
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    txt = (
        "👥 *Your Referral Program*\n\n"
        f"🔗 Invite link:\n`{link}`\n\n"
        f"👤 Total referrals: *{count}*\n"
        f"💰 Total earned: *{money(total_earned)}*\n\n"
        "You earn *15%* of each referral's credited deposits."
    )
    await cq.message.edit_text(txt, parse_mode="Markdown", reply_markup=back_home_button())

# =========================
# Support Tickets (user) + Admin (view/reply/resolve)
# =========================
ADMIN_STATE: Dict[int, Dict] = {}

@dp.callback_query(F.data == "support:new")
async def support_new(cq: types.CallbackQuery):
    await cq.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="support:cancel")]
    ])
    await cq.message.edit_text(
        "🆘 *Create Support Ticket*\n\n"
        "Please type your issue below (a single message). Include order IDs if relevant.\n\n"
        "_You can cancel anytime._",
        parse_mode="Markdown", reply_markup=kb
    )
    ADMIN_STATE[cq.from_user.id] = {"mode": "await_support_text"}

@dp.callback_query(F.data == "support:cancel")
async def support_cancel(cq: types.CallbackQuery):
    ADMIN_STATE.pop(cq.from_user.id, None)
    await cq.message.edit_text("❌ Support ticket canceled.", reply_markup=back_home_button())
    await cq.answer()

@dp.message(lambda m: ADMIN_STATE.get(m.from_user.id,{}).get("mode")=="await_support_text")
async def support_receive_text(msg: types.Message):
    ADMIN_STATE.pop(msg.from_user.id, None)
    db = SessionLocal()
    try:
        t = SupportTicket(user_id=msg.from_user.id, status="open", subject=(msg.text or "").strip())
        db.add(t); db.commit(); db.refresh(t)
        await msg.answer(
            f"✅ Ticket *#{t.id}* created. Our team will reply here.\nYou can add more messages by typing.",
            parse_mode="Markdown",
            reply_markup=back_home_button()
        )
        # notify admins (simple)
        admin_text = (
            f"🆘 *New Support Ticket* #{t.id}\n"
            f"👤 User: `{msg.from_user.id}` @{msg.from_user.username or '—'}\n"
            f"📝 Message:\n{msg.text or ''}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id, admin_text, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Reply", callback_data=f"admin:reply_ticket:{t.id}"),
                         InlineKeyboardButton(text="✅ Resolve", callback_data=f"admin:resolve_ticket:{t.id}")]
                    ])
                )
            except Exception:
                pass
    finally:
        db.close()

@dp.callback_query(F.data == "admin:view_tickets")
async def admin_view_tickets(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("🚫 Not authorized.", show_alert=True)
    db = SessionLocal()
    try:
        tickets = db.query(SupportTicket).filter(SupportTicket.status=="open").order_by(SupportTicket.created_at.desc()).all()
    finally:
        db.close()
    if not tickets:
        return await cq.message.edit_text("✅ No open support tickets right now.", reply_markup=admin_kb())
    t = tickets[0]
    text = (
        f"🆘 *Support Ticket #{t.id}*\n"
        f"👤 User ID: `{t.user_id}`\n"
        f"🕒 Opened: {t.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"💬 Message:\n_{t.subject or '(no message)'}_\n\n"
        f"📌 Status: *{t.status.upper()}*"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Reply", callback_data=f"admin:reply_ticket:{t.id}"),
             InlineKeyboardButton(text="✅ Resolve", callback_data=f"admin:resolve_ticket:{t.id}")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:panel")]
        ]
    )
    await cq.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await cq.answer()

@dp.callback_query(F.data.startswith("admin:reply_ticket:"))
async def admin_reply_ticket(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("🚫 Not authorized.", show_alert=True)
    tid = int(cq.data.split(":")[-1])
    db = SessionLocal()
    try:
        t = db.get(SupportTicket, tid)
    finally:
        db.close()
    if not t:
        return await cq.answer("❌ Ticket not found.", show_alert=True)
    ADMIN_STATE[cq.from_user.id] = {"mode":"reply_ticket", "ticket_id": tid, "user_id": t.user_id}
    await cq.message.edit_text(
        f"💬 *Replying to ticket #{tid}*\n\n"
        f"👤 User ID: `{t.user_id}`\n"
        f"🕒 Opened: {t.created_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"📝 Message:\n_{t.subject or '(no message)'}_\n\n"
        f"✏️ Please type your reply below:",
        parse_mode="Markdown"
    )
    await cq.answer()

@dp.message(lambda m: ADMIN_STATE.get(m.from_user.id,{}).get("mode")=="reply_ticket")
async def handle_ticket_reply(msg: types.Message):
    st = ADMIN_STATE.pop(msg.from_user.id, None)
    if not st: return
    user_id = st.get("user_id")
    tid = st.get("ticket_id")
    if not user_id:
        return await msg.answer("⚠️ Error: No user linked to this ticket.", reply_markup=admin_kb())
    try:
        await bot.send_message(user_id, f"💬 *Support Reply (Ticket #{tid}):*\n\n{msg.text}", parse_mode="Markdown")
        await msg.answer(f"✅ Reply sent to user {user_id}.", reply_markup=admin_kb())
    except Exception as e:
        await msg.answer(f"⚠️ Could not send reply: {e}", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("admin:resolve_ticket:"))
async def admin_resolve_ticket(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("🚫 Not authorized.", show_alert=True)
    tid = int(cq.data.split(":")[-1])
    db = SessionLocal()
    try:
        t = db.get(SupportTicket, tid)
        if not t:
            return await cq.answer("❌ Ticket not found.", show_alert=True)
        t.status = "closed"
        t.closed_at = datetime.utcnow()
        db.commit()
        await cq.answer(f"✅ Ticket #{tid} marked as resolved.", show_alert=True)
    finally:
        db.close()
    await admin_view_tickets(cq)

# =========================
# Admin: Add/View/Remove/Load/Orders (with stock post on add)
# =========================
@dp.callback_query(F.data == "admin:panel")
async def admin_panel_open(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("🚫 Not authorized.", show_alert=True)
    await cq.message.edit_text("⚙️ *Admin Panel*\nChoose an option below:", parse_mode="Markdown", reply_markup=admin_kb())
    await cq.answer()

@dp.callback_query(F.data == "adm:add")
async def adm_add(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        return await cq.answer("🚫 Not authorized.", show_alert=True)
    ADMIN_STATE[cq.from_user.id] = {"mode": "add_card"}
    prompt = ("Send card info in one line:\n"
              "`BIN | CC Number | EXP | Code | Balance | Price | Site`\n"
              "Example:\n`541275 | 4111 1111 1111 1111 | 12/27 | 123 | 100.00 | 85.00 | amazon`")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:panel")]])
    await cq.message.edit_text(prompt, parse_mode="Markdown", reply_markup=kb)
    await cq.answer()

@dp.message(lambda m: ADMIN_STATE.get(m.from_user.id,{}).get("mode")=="add_card")
async def adm_add_receive(msg: types.Message):
    if msg.from_user.id not in ADMIN_IDS: return
    parts = [p.strip() for p in (msg.text or "").split("|")]
    if len(parts) < 6:
        await msg.answer("❌ Invalid. Need: BIN | CC Number | EXP | Code | Balance | Price | [Site]"); return
    binv, ccnum, exp, code, balance, price = parts[:6]
    site_val = parts[6] if len(parts) >= 7 else None
    try:
        bal = Decimal(balance); prc = Decimal(price)
    except:
        await msg.answer("❌ Balance/Price must be numbers."); return
    db = SessionLocal()
    try:
        card = Card(site=site_val, bin=binv, cc_number=ccnum, exp=exp,
                    encrypted_code=enc_text(code), balance=bal, price=prc, added_by=msg.from_user.id)
        db.add(card); db.commit(); db.refresh(card)
        await msg.answer(f"✅ Card added id:{card.id}", reply_markup=admin_kb())
    finally:
        db.close()
    # Stock channel post (NEW cards only)
    if STOCK_CHANNEL_ID:
        try:
            pretty_site = site_val or "—"
            s_msg = (
                "💳 *New card added!*\n"
                f"🗃️ BIN: `{binv}`\n"
                f"💵 Balance: *{money(balance)}*\n"
                f"💰 Price: *{money(price)}*\n"
            )
            await bot.send_message(int(STOCK_CHANNEL_ID), s_msg, parse_mode="Markdown")
        except Exception as e:
            print(f"⚠️ Error posting to stock channel: {e}")
    ADMIN_STATE.pop(msg.from_user.id, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("adm:view:"))
async def adm_view(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS: return await cq.answer("🚫 Not authorized.", show_alert=True)
    page = int(cq.data.split(":")[2])
    db = SessionLocal()
    try:
        all_cards = db.query(Card).order_by(Card.id.desc()).all()
        items, page, pages = paginate(all_cards, page, 10)
        lines = ["📋 *All Cards*\n"]
        for c in items:
            lines.append(f"id:{c.id} | {c.site or '—'} | BIN {c.bin} | {c.cc_number} | {c.exp} | Face {money(c.balance)} | Price {money(c.price)} | {c.status}")
    finally:
        db.close()
    nav = []
    if page > 1: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"adm:view:{page-1}"))
    if page < pages: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"adm:view:{page+1}"))
    if not nav: nav = [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:panel")]
    kb = InlineKeyboardMarkup(inline_keyboard=[nav])
    await cq.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)
    await cq.answer()

@dp.callback_query(F.data == "adm:load")
async def adm_load(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS: return await cq.answer("🚫 Not authorized.", show_alert=True)
    ADMIN_STATE[cq.from_user.id] = {"mode": "load_balance"}
    await cq.message.edit_text("Send: `user_id | USD amount` (e.g., `123456789 | 25.00`)", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="admin:panel")]]))
    await cq.answer()

@dp.message(lambda m: ADMIN_STATE.get(m.from_user.id,{}).get("mode")=="load_balance")
async def adm_load_receive(msg: types.Message):
    if msg.from_user.id not in ADMIN_IDS: return
    parts = [p.strip() for p in (msg.text or "").split("|")]
    if len(parts) < 2 or not parts[0].isdigit():
        await msg.answer("Invalid format. Use: `user_id | USD amount`", parse_mode="Markdown"); return
    uid = int(parts[0])
    try:
        amt = Decimal(parts[1])
    except:
        await msg.answer("Amount must be a number."); return
    db = SessionLocal()
    try:
        w = get_or_create_wallet(db, uid, "USD")
        w.balance = Decimal(w.balance or 0) + amt
        db.commit()
        await msg.answer(f"✅ Loaded {money(amt)} to user {uid}. New USD balance: {money(w.balance)}", reply_markup=admin_kb())
    finally:
        db.close()
    ADMIN_STATE.pop(msg.from_user.id, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("adm:orders:"))
async def adm_orders(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS: return await cq.answer("🚫 Not authorized.", show_alert=True)
    page = int(cq.data.split(":")[2])
    db = SessionLocal()
    try:
        all_orders = db.query(Order).order_by(Order.id.desc()).all()
        items, page, pages = paginate(all_orders, page, 10)
        lines = ["📦 *Orders*\n"]
        for o in items:
            lines.append(f"#{o.id} | user {o.user_id} | card {o.card_id} | {money(o.price_usd)} | {o.status} | {o.created_at.strftime('%Y-%m-%d %H:%M')}")
    finally:
        db.close()
    nav = []
    if page > 1: nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"adm:orders:{page-1}"))
    if page < pages: nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"adm:orders:{page+1}"))
    if not nav: nav = [InlineKeyboardButton(text="⬅️ Back", callback_data="admin:panel")]
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[nav])
    await cq.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=reply_kb)
    await cq.answer()

# =========================
# Background loops: deposits & rates
# =========================
async def monitor_deposits_loop():
    while True:
        try:
            db = SessionLocal()
            wallets = db.query(Wallet).filter(Wallet.deposit_address.isnot(None), Wallet.coin.in_(("BTC","LTC"))).all()
            for w in wallets:
                addr = w.deposit_address
                if not addr:
                    continue
                txs = bc_tx_list(w.coin, addr)
                for t in txs:
                    txid = t.get("txid")
                    if not txid:
                        continue
                    amount = Decimal(t.get("amount", 0))
                    confs = int(t.get("confirmations", 0) or 0)
                    existing = db.query(Transaction).filter(Transaction.txid==txid, Transaction.wallet_id==w.id).first()
                    if not existing:
                        existing = Transaction(wallet_id=w.id, txid=txid, amount=amount, confirmations=confs, status="pending", coin=w.coin)
                        db.add(existing); db.commit()
                    else:
                        if existing.confirmations != confs:
                            existing.confirmations = confs
                            existing.updated_at = datetime.utcnow()
                            db.commit()
                    # Credit after 2 confirmations
                    if existing.status != "confirmed" and existing.confirmations >= 2:
                        usd_delta = (amount * (LTC_USD_RATE if w.coin=="LTC" else BTC_USD_RATE)).quantize(Decimal("0.01"))
                        usd_wallet = get_or_create_wallet(db, w.user_id, "USD")
                        usd_wallet.balance = Decimal(usd_wallet.balance or 0) + usd_delta
                        existing.status = "confirmed"
                        existing.updated_at = datetime.utcnow()
                        db.commit()
                        # Referral 15% bonus
                        try:
                            ref = db.query(Referral).filter(Referral.user_id==w.user_id).first()
                            if ref:
                                bonus_usd = (usd_delta * Decimal("0.15")).quantize(Decimal("0.01"))
                                ref_usd = get_or_create_wallet(db, ref.referrer_id, "USD")
                                ref_usd.balance = Decimal(ref_usd.balance or 0) + bonus_usd
                                db.add(ReferralBonus(referrer_id=ref.referrer_id, referred_user_id=w.user_id, amount_usd=bonus_usd, txid=existing.txid))
                                db.commit()
                                try:
                                    await bot.send_message(ref.referrer_id, f"🎉 Referral bonus credited: {money(bonus_usd)} (15% of referral deposit).")
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        try:
                            await bot.send_message(w.user_id, f"💰 Deposit credited: {amount} {w.coin} (~{money(usd_delta)}). Your USD wallet was credited (2 conf).")
                        except Exception:
                            pass
            db.close()
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)

async def update_rates_loop():
    global BTC_USD_RATE, LTC_USD_RATE
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,litecoin&vs_currencies=usd"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    BTC_USD_RATE = Decimal(str(data["bitcoin"]["usd"]))
                    LTC_USD_RATE = Decimal(str(data["litecoin"]["usd"]))
        except Exception:
            pass
        await asyncio.sleep(RATES_INTERVAL)

# =========================
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command

async def main():
    me = await bot.get_me()
    print(f"🤖 Bot username: @{me.username}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

