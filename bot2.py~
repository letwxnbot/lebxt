#!/usr/bin/env python3
# bot2.py - Restored Twxn's Prepaid Market (full layout + balance checks + admin broadcast)
import os
import asyncio
import math
from decimal import Decimal
from datetime import datetime
from typing import Optional, List

from dotenv import load_dotenv
load_dotenv(override=False)

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in env")

ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "8418864166").split(",") if x.strip())
PROXY_URL = os.getenv("PROXY_URL", "http://brd-customer-hl_ff068f80-zone-twxnproxy:txvts7556bi1@brd.superproxy.io:22225")
STOCK_INVITE_URL = os.getenv("STOCK_INVITE_URL", "https://t.me/+ntzN_J5td7c2ZGYx")
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@letwxn")

# Database selection: prefer DATABASE_URL, else uploaded file, else /tmp fallback
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_UPLOADED_DB = "/mnt/data/market.db"   # user uploaded file path
FALLBACK_DB = os.path.join(os.path.dirname(__file__), "data", "market.db")
if DATABASE_URL:
    DB_URL = DATABASE_URL
elif os.path.exists(LOCAL_UPLOADED_DB):
    DB_URL = f"sqlite:///{LOCAL_UPLOADED_DB}"
else:
    os.makedirs(os.path.dirname(FALLBACK_DB), exist_ok=True)
    DB_URL = f"sqlite:///{FALLBACK_DB}"

print("📁 DB_URL ->", DB_URL)

# --- SQLAlchemy models (use models.py if you have it) ---
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Numeric, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)
    username = Column(String)
    display_name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Wallet(Base):
    __tablename__ = "wallets"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    coin = Column(String)
    balance = Column(Numeric, default=0)
    deposit_address = Column(String, nullable=True)
    address_index = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Card(Base):
    __tablename__ = "cards"
    id = Column(Integer, primary_key=True)
    site = Column(String, index=True)
    bin = Column(String, index=True)
    cc_number = Column(String)
    exp = Column(String)
    encrypted_code = Column(Text, nullable=False)
    balance = Column(Numeric, default=0)
    price = Column(Numeric, default=0)
    currency = Column(String, default="USD")
    status = Column(String, default="in_stock")
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    card_id = Column(Integer)
    price_usd = Column(Numeric)
    coin_used = Column(String)
    coin_amount = Column(Numeric)
    status = Column(String, default="completed")
    created_at = Column(DateTime, default=datetime.utcnow)

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    referrer_id = Column(BigInteger, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    subject = Column(String)
    status = Column(String, default="open")
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)

# --- small helpers (replace enc/dec/derive with your real ones if present) ---
def money(v) -> str:
    return f"${Decimal(v):.2f}"

def enc_text(s: str) -> str:
    # If you have a real Fernet encryption, import/replace here
    return s

def dec_text(s: str) -> str:
    return s

# If you have a function that derives addresses from xpubs, plug it in here
def derive_addr_from_xpub(xpub: str, coin: str, index: int) -> str:
    return f"derived_{coin}_{index}"

def get_or_create_wallet(db, user_id: int, coin: str) -> Wallet:
    w = db.query(Wallet).filter(Wallet.user_id==user_id, Wallet.coin==coin).first()
    if w:
        return w
    w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"))
    db.add(w); db.commit(); db.refresh(w)
    return w

# --- Playwright balance checker ---
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse

SITE_SELECTORS = {
    "mygift": {
        "url": "https://mygift.giftcardmall.com/",
        "card_number": "#credit-card-number",
        "exp_month": "#expiration_month",
        "exp_year": "#expiration_year",
        "pin": "#credit-card-code",
        "submit": ".btn.ms-2.submit-btn.submit-btn-radius",
        "result_balance": ".value"
    },
    "securespend": {
        "url": "https://www.securespend.com/",
        "card_number": "#cardnumber",
        "exp_month": "#expirationMonth",
        "exp_year": "#expirationYear",
        "pin": "#cvv",
        "submit": "#form-block-button-submit",
        "result_balance": "#virtual-section-balance-amount"
    },
    "mybalancenow": {
        "url": "https://www.mybalancenow.com/",
        "card_number": "#cardnumber",
        "exp_month": "#expirationMonth",
        "exp_year": "#expirationYear",
        "pin": "#cvv",
        "submit": "#form-block-button-submit",
        "result_balance": "#virtual-section-balance-amount"
    }
}

def parse_balance_text(text: str) -> Decimal:
    if not text:
        return Decimal("0")
    filtered = "".join(ch for ch in text if (ch.isdigit() or ch in ".,"))
    filtered = filtered.replace(",", "")
    if not filtered:
        return Decimal("0")
    return Decimal(filtered)

async def check_card_balance_playwright(site_key: str, card_number: str, exp: Optional[str]=None, pin: Optional[str]=None, proxy: Optional[str]=None) -> Decimal:
    selectors = SITE_SELECTORS.get(site_key)
    if not selectors:
        raise RuntimeError("Unsupported site")
    proxy_dict = None
    if proxy:
        p = urlparse(proxy)
        proxy_dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}", "username": p.username, "password": p.password}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(proxy=proxy_dict) if proxy_dict else await browser.new_context()
            page = await context.new_page()
            await page.goto(selectors["url"], timeout=60000)
            await page.wait_for_timeout(1500)
            # try to switch tab if needed
            try:
                if await page.query_selector("text='Check Balance'"):
                    await page.click("text='Check Balance'")
                    await page.wait_for_timeout(800)
                else:
                    tabs = await page.query_selector_all('[role="tab"]')
                    if tabs and len(tabs) >= 2:
                        await tabs[1].click()
                        await page.wait_for_timeout(800)
            except Exception:
                pass
            # wait for field
            try:
                await page.wait_for_selector(selectors["card_number"], timeout=20000)
            except PlaywrightTimeoutError:
                await context.close()
                raise RuntimeError("Card input not found")
            # fill inputs
            await page.fill(selectors["card_number"], card_number)
            if exp:
                mm, yy = exp[:2], exp[-2:]
                if selectors.get("exp_month"):
                    await page.fill(selectors["exp_month"], mm)
                if selectors.get("exp_year"):
                    await page.fill(selectors["exp_year"], yy)
            if pin and selectors.get("pin"):
                await page.fill(selectors["pin"], pin)
            await page.click(selectors["submit"])
            # wait for result
            try:
                await page.wait_for_selector(selectors["result_balance"], timeout=20000)
                text = await page.inner_text(selectors["result_balance"])
            except PlaywrightTimeoutError:
                text = await page.inner_text("body")
            await context.close()
            return parse_balance_text(text)
    except Exception as e:
        raise RuntimeError(f"Playwright error: {e}")

def bin_to_site_key(bin_str: str) -> str:
    if bin_str.startswith(("403446","435880","511332")):
        return "mygift"
    if bin_str.startswith("409758"):
        return "securespend"
    return "mygift"

# --- aiogram bot setup & UI ---
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def home_message_text(usd_balance: Decimal, purchased: int, stock_count: int) -> str:
    return (
        "💳 *Welcome to Twxn’s Prepaid Market!*\n\n"
        "💰 *Account Info:*\n"
        f"• Account Balance: *{money(usd_balance)}*\n"
        f"• Purchased cards: *{purchased}*\n"
        f"• In stock now: *{stock_count}*\n\n"
        "📰 *Stock Updates:*\n"
        f"[Join Here]({STOCK_INVITE_URL})\n\n"
        "🆘 *Need Help?*\n"
        f"Open a *Support Ticket* below or reach out at {SUPPORT_HANDLE}"
    )

def main_menu_kb(is_admin=False):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁 View Listings", callback_data="shop:page:1"),
            InlineKeyboardButton(text="💰 Make a Deposit", callback_data="home:wallet")
        ],
        [
            InlineKeyboardButton(text="📦 Purchase History", callback_data="history"),
        ],
        [
            InlineKeyboardButton(text="👥 Referrals", callback_data="referrals"),
            InlineKeyboardButton(text="🆘 Support Ticket", callback_data="support")
        ],
        [
            InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin")
        ],
        [
            InlineKeyboardButton(text="💎 Twxn's Main Listings 💎", callback_data="shop:page:1")
        ]
    ])
    return kb

def back_home_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]])

# --- handlers ---
@dp.message(Command("start"))
async def on_start(msg: types.Message):
    db = SessionLocal()
    try:
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(id=msg.from_user.id, username=msg.from_user.username, display_name=msg.from_user.full_name)
            db.add(u); db.commit(); db.refresh(u)
        usd_w = get_or_create_wallet(db, msg.from_user.id, "USD")
        usd_balance = Decimal(usd_w.balance or 0)
        purchased = db.query(Order).filter(Order.user_id==msg.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
    finally:
        db.close()
    await msg.answer(home_message_text(usd_balance, purchased, stock_count), parse_mode="Markdown", reply_markup=main_menu_kb(is_admin=(msg.from_user.id in ADMIN_IDS)))

@dp.callback_query(lambda c: c.data == "home:back")
async def back_to_home(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        usd_w = get_or_create_wallet(db, cq.from_user.id, "USD")
        usd_balance = Decimal(usd_w.balance or 0)
        purchased = db.query(Order).filter(Order.user_id==cq.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
    finally:
        db.close()
    await cq.message.edit_text(home_message_text(usd_balance, purchased, stock_count), parse_mode="Markdown", reply_markup=main_menu_kb(is_admin=(cq.from_user.id in ADMIN_IDS)))

# listings & buy (pages)
@dp.callback_query(lambda c: c.data and c.data.startswith("shop:page:"))
async def shop_page_cb(cq: types.CallbackQuery):
    await cq.answer()
    _,_,page_s = cq.data.split(":")
    page = int(page_s)
    db = SessionLocal()
    try:
        items = db.query(Card).filter(Card.status=="in_stock").all()
        per = 8
        total = len(items)
        pages = max(1, math.ceil(total/per))
        page = max(1, min(page, pages))
        start = (page-1)*per
        page_items = items[start:start+per]
    finally:
        db.close()
    kb_rows = []
    for card in page_items:
        price = card.price if card.price and card.price>0 else card.balance
        kb_rows.append([InlineKeyboardButton(text=f"🔹 {card.site or 'Card'} ${Decimal(price):.2f}", callback_data=f"shop:buy:{card.id}")])
    nav = []
    if page>1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"shop:page:{page-1}"))
    if page<pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"shop:page:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows + ([nav] if nav else []) + [[InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]])
    await cq.message.edit_text("💎 Twxn's Main Listings 💎", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:buy:"))
async def shop_buy_request_cb(cq: types.CallbackQuery):
    await cq.answer()
    _,_,cid_s = cq.data.split(":")
    cid = int(cid_s)
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status!="in_stock":
            await cq.message.answer("🚫 Card not available.", reply_markup=back_home_button())
            return
        sale_price = card.price if card.price and card.price>0 else card.balance
    finally:
        db.close()
    # verify balance live
    await cq.message.answer("🧠 Verifying card balance before purchase...")
    try:
        site_key = bin_to_site_key(card.bin or "")
        exp = card.exp if card.exp else None
        live_bal = await check_card_balance_playwright(site_key, card.cc_number, exp=exp, pin=None, proxy=PROXY_URL)
    except Exception as e:
        live_bal = None
        await cq.message.answer(f"⚠️ Auto-check failed: {e}\nProceeding with stored balance.", reply_markup=back_home_button())
    # prompt accordingly
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        stored_bal = Decimal(card.balance or 0)
        if live_bal is not None and live_bal != stored_bal:
            card.balance = live_bal
            db.commit()
            sale_price = live_bal if not card.price or card.price==0 else card.price
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Confirm {money(sale_price)}", callback_data=f"shop:confirm:{cid}:{sale_price}"),
                 InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
            ])
            await cq.message.answer(f"⚠️ Card balance changed from {money(stored_bal)} to {money(live_bal)}. Buy for {money(sale_price)}?", parse_mode="Markdown", reply_markup=kb)
            return
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Confirm {money(sale_price)}", callback_data=f"shop:confirm:{cid}:{sale_price}"),
                 InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
            ])
            await cq.message.answer(f"⚠️ Confirm purchase of card id:{cid} for *{money(sale_price)}* ?", parse_mode="Markdown", reply_markup=kb)
    finally:
        db.close()

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:confirm:"))
async def shop_confirm_cb(cq: types.CallbackQuery):
    await cq.answer()
    _,_,cid_s,price_s = cq.data.split(":")
    cid = int(cid_s)
    sale_price = Decimal(price_s)
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status!="in_stock":
            await cq.message.answer("🚫 Card not available.", reply_markup=back_home_button()); return
        w = get_or_create_wallet(db, cq.from_user.id, "USD")
        if Decimal(w.balance or 0) < sale_price:
            await cq.message.answer(f"❌ Not enough USD balance. Need {money(sale_price)}.", reply_markup=back_home_button()); return
        w.balance = Decimal(w.balance or 0) - sale_price
        card.status = "sold"
        order = Order(user_id=cq.from_user.id, card_id=card.id, price_usd=sale_price, coin_used="USD", coin_amount=sale_price)
        db.add(order); db.commit()
        code = dec_text(card.encrypted_code)
        await cq.message.answer(f"✅ Purchase complete!\n\nCard id: {card.id}\nSite: {card.site}\nBIN: {card.bin}\nCC: {card.cc_number}\nEXP: {card.exp}\nCODE: `{code}`\n\nPaid: {money(sale_price)}\nOrder ID: {order.id}", parse_mode="Markdown", reply_markup=back_home_button())
    finally:
        db.close()

@dp.callback_query(lambda c: c.data and c.data.startswith("shop:cancel:"))
async def shop_cancel_cb(cq: types.CallbackQuery):
    await cq.answer("Purchase canceled.")
    await cq.message.answer("Purchase canceled.", reply_markup=back_home_button())

# history/referrals/support
@dp.callback_query(lambda c: c.data == "history")
async def history_cb(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        orders = db.query(Order).filter(Order.user_id==cq.from_user.id).all()
        if not orders:
            await cq.message.answer("No purchase history.", reply_markup=back_home_button()); return
        lines = [f"#{o.id} card {o.card_id} paid {money(o.price_usd)} {o.created_at.date()}" for o in orders]
    finally:
        db.close()
    await cq.message.answer("📦 Purchase History:\n\n" + "\n".join(lines), reply_markup=back_home_button())

@dp.callback_query(lambda c: c.data == "referrals")
async def referrals_cb(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        count = db.query(Referral).filter(Referral.referrer_id==cq.from_user.id).count()
    finally:
        db.close()
    await cq.message.answer(f"You have referred {count} users.", reply_markup=back_home_button())

@dp.callback_query(lambda c: c.data == "support")
async def support_cb(cq: types.CallbackQuery):
    await cq.answer()
    await cq.message.answer("Please describe your issue and our support team will reply.", reply_markup=back_home_button())

# admin panel + broadcast
ADMIN_BROADCAST_STATE = set()

@dp.callback_query(lambda c: c.data == "admin")
async def admin_panel_cb(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Broadcast Message", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🧾 View Orders", callback_data="admin:orders")],
        [InlineKeyboardButton(text="👥 View Users", callback_data="admin:users")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])
    await cq.message.answer("⚙️ Admin Panel", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "admin:broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    ADMIN_BROADCAST_STATE.add(cq.from_user.id)
    await cq.message.answer("✉️ Send the message to broadcast (or /cancel).")

@dp.message()
async def admin_broadcast_receive(msg: types.Message):
    if msg.from_user.id in ADMIN_BROADCAST_STATE:
        if msg.text and msg.text.strip().lower() == "/cancel":
            ADMIN_BROADCAST_STATE.discard(msg.from_user.id)
            await msg.answer("Broadcast canceled.", reply_markup=types.ReplyKeyboardRemove()); return
        db = SessionLocal()
        try:
            users = db.query(User).all()
        finally:
            db.close()
        count = 0
        for u in users:
            try:
                await bot.send_message(u.id, f"📢 Broadcast from Twxn:\n\n{msg.text}")
                count += 1
            except Exception:
                pass
        ADMIN_BROADCAST_STATE.discard(msg.from_user.id)
        await msg.answer(f"Broadcast sent to {count} users.", reply_markup=types.ReplyKeyboardRemove())

@dp.callback_query(lambda c: c.data == "admin:users")
async def admin_users_cb(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    db = SessionLocal()
    try:
        users = db.query(User).limit(200).all()
        lines = [f"{u.id} — {u.username or ''} — {u.display_name or ''}" for u in users]
    finally:
        db.close()
    await cq.message.answer("👥 Users:\n\n" + "\n".join(lines), reply_markup=back_home_button())

@dp.callback_query(lambda c: c.data == "admin:orders")
async def admin_orders_cb(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    db = SessionLocal()
    try:
        orders = db.query(Order).order_by(Order.id.desc()).limit(100).all()
        lines = [f"#{o.id}: user {o.user_id} card {o.card_id} ${o.price_usd}" for o in orders]
    finally:
        db.close()
    await cq.message.answer("🧾 Orders:\n\n" + "\n".join(lines), reply_markup=back_home_button())

# wallet/deposit shortcut (keeps language same)
@dp.callback_query(lambda c: c.data == "home:wallet")
async def wallet_cb(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        btc = get_or_create_wallet(db, cq.from_user.id, "BTC")
        ltc = get_or_create_wallet(db, cq.from_user.id, "LTC")
        usd_v = Decimal(usd.balance or 0)
        btc_v = Decimal(btc.balance or 0)
        ltc_v = Decimal(ltc.balance or 0)
    finally:
        db.close()
    text = (
        "🏦 *Make a Deposit*\n\n"
        f"USD: {money(usd_v)}\n"
        f"BTC: {btc_v:.8f}\n"
        f"LTC: {ltc_v:.8f}\n\n"
        "Choose a coin below. Your USD wallet is credited after *2 confirmations*."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔸 Deposit BTC", callback_data="deposit:BTC"),
         InlineKeyboardButton(text="🔹 Deposit LTC", callback_data="deposit:LTC")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])
    await cq.message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("deposit:"))
async def deposit_coin_cb(cq: types.CallbackQuery):
    await cq.answer()
    coin = cq.data.split(":")[1]
    db = SessionLocal()
    try:
        w = db.query(Wallet).filter(Wallet.user_id==cq.from_user.id, Wallet.coin==coin).first()
        if not w:
            addr = derive_addr_from_xpub(os.getenv("BTC_XPUB") if coin=="BTC" else os.getenv("LTC_XPUB"), coin, 0)
            w = Wallet(user_id=cq.from_user.id, coin=coin, balance=0, deposit_address=addr, address_index=0)
            db.add(w); db.commit(); db.refresh(w)
        addr = w.deposit_address
    finally:
        db.close()
    await cq.message.answer(f"📥 Send *{coin}* to:\n`{addr}`\n\nWe credit your *USD* wallet after 2 confirmations.", parse_mode="Markdown", reply_markup=back_home_button())

# dbstatus for debugging
@dp.message(Command("dbstatus"))
async def dbstatus_cmd(msg: types.Message):
    db = SessionLocal()
    try:
        u = db.query(User).count()
        c = db.query(Card).count()
        o = db.query(Order).count()
        w = db.query(Wallet).count()
    finally:
        db.close()
    await msg.answer(f"DB Status:\nUsers: {u}\nCards: {c}\nOrders: {o}\nWallets: {w}")

# startup / run
async def on_startup():
    print("✅ Bot startup (bot2.py) - DB:", DB_URL)

async def main():
    await on_startup()
    me = await bot.get_me()
    print("🤖 Running as:", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as err:
        print("Fatal:", err)
        raise
