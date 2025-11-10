#!/usr/bin/env python3
# bot1.py - Restored Twxn's Prepaid Market
# Full menu, admin broadcast, purchase flow with balance verification via Playwright + proxy.

import os
import math
import asyncio
from decimal import Decimal
from datetime import datetime
from typing import Optional, List, Dict

from dotenv import load_dotenv
load_dotenv(override=False)  # Render uses env vars; local .env is optional

# --- Config & Environment ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FERNET_KEY = os.getenv("FERNET_KEY") or ""
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS", "8418864166").split(",") if x.strip())
PROXY_URL = os.getenv("PROXY_URL", "http://brd-customer-hl_ff068f80-zone-twxnproxy:txvts7556bi1@brd.superproxy.io:22225")

# DB path priority:
# 1) DATABASE_URL env (Postgres) - if provided
# 2) Local desktop path (developer requested)
# 3) data/market.db inside project
# 4) /tmp/market.db (writable)
DATABASE_URL = os.getenv("DATABASE_URL")
LOCAL_DESKTOP_DB = "/Users/Twxn/Desktop/twxn/market.db"
DATA_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
TMP_DB = "/tmp/market.db"

if DATABASE_URL:
    DB_URL = DATABASE_URL
elif os.path.exists(LOCAL_DESKTOP_DB):
    DB_URL = f"sqlite:///{LOCAL_DESKTOP_DB}"
else:
    # ensure data dir exists
    os.makedirs(os.path.dirname(DATA_DB), exist_ok=True)
    DB_URL = f"sqlite:///{DATA_DB}"

print(f"📁 Using database URL: {DB_URL}")

# --- SQLAlchemy models ---
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Numeric, Text, DateTime, ForeignKey, func
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
    coin = Column(String)  # 'USD', 'BTC', 'LTC'
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
    balance = Column(Numeric, default=0)      # face value
    price = Column(Numeric, default=0)        # optional price override
    currency = Column(String, default="USD")
    status = Column(String, default="in_stock")  # in_stock, sold
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    card_id = Column(Integer)
    price_usd = Column(Numeric)
    coin_used = Column(String)     # 'USD'
    coin_amount = Column(Numeric)
    status = Column(String, default="completed")
    created_at = Column(DateTime, default=datetime.utcnow)

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)      # referred user
    referrer_id = Column(BigInteger, index=True)  # who referred them
    created_at = Column(DateTime, default=datetime.utcnow)

class ReferralBonus(Base):
    __tablename__ = "referral_bonuses"
    id = Column(Integer, primary_key=True)
    referrer_id = Column(BigInteger, index=True)
    referred_user_id = Column(BigInteger, index=True)
    amount_usd = Column(Numeric)   # credited USD
    txid = Column(String)          # origin transaction
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

# --- Crypto / deposit helpers (placeholders — use your original implementations) ---
# Keep your original deposit/derive functions in your codebase and import them; below are stubs
def derive_addr_from_xpub(xpub: str, coin: str, index: int) -> str:
    # placeholder: your real code uses bip_utils to derive addresses
    return f"DERIVED_{coin}_{index}"

def enc_text(s: str) -> str:
    # placeholder encryption - if you use Fernet, replace with your code
    return s

def dec_text(s: str) -> str:
    return s

def money(v) -> str:
    return f"${Decimal(v):.2f}"

# --- Aiogram setup ---
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN not set in environment")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- UI text & menus ---
STOCK_INVITE_URL = os.getenv("STOCK_INVITE_URL", "https://t.me/+ntzN_J5td7c2ZGYx")
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@letwxn")

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
            InlineKeyboardButton(text="👥 Referrals", callback_data="referrals")
        ],
        [
            InlineKeyboardButton(text="🆘 Support Ticket", callback_data="support"),
            InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin")  # admin gate check later
        ],
        [
            InlineKeyboardButton(text="💎 Twxn's Main Listings 💎", callback_data="shop:page:1")
        ]
    ])
    return kb

def back_home_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]])

# --- Helper DB functions ---
def get_or_create_wallet(db, user_id: int, coin: str) -> Wallet:
    w = db.query(Wallet).filter(Wallet.user_id==user_id, Wallet.coin==coin).first()
    if w: return w
    w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"))
    db.add(w); db.commit(); db.refresh(w)
    return w

# --- Playwright-based balance checker (site-specific selectors) ---
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Site selectors map — adjust if the site changes
SITE_SELECTORS = {
    "mygift": {
        "url": "https://mygift.giftcardmall.com/",
        "card_number": "#credit-card-number",
        "exp_month": "#expiration_month",
        "exp_year": "#expiration_year",
        "pin": "#credit-card-code",
        "submit": ".btn.ms-2.submit-btn.submit-btn-radius",
        "result_balance": ".value"  # may need adjustment
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

async def check_card_balance_playwright(site_key: str, card_number: str, exp: Optional[str] = None, pin: Optional[str] = None, proxy: Optional[str] = None) -> Decimal:
    """Use Playwright to open site and check balance. Returns Decimal balance."""
    selectors = SITE_SELECTORS.get(site_key)
    if not selectors:
        raise RuntimeError("Unsupported site for balance check")

    # proxy should be like: http://user:pass@host:port
    pw_proxy = None
    if proxy:
        # Playwright expects a dict for proxy when launching context
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        pw_proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}", "username": parsed.username, "password": parsed.password}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(proxy=pw_proxy) if pw_proxy else await browser.new_context()
            page = await context.new_page()
            await page.goto(selectors["url"], timeout=60000)
            await page.wait_for_timeout(1500)

            # try to reveal check-balance tab if default shows register
            try:
                if await page.query_selector("text='Check Balance'"):
                    try:
                        await page.click("text='Check Balance'")
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
                else:
                    tabs = await page.query_selector_all('[role="tab"]')
                    if tabs and len(tabs) >= 2:
                        try:
                            await tabs[1].click()
                            await page.wait_for_timeout(800)
                        except Exception:
                            pass
            except Exception:
                pass

            # wait for card input
            try:
                await page.wait_for_selector(selectors["card_number"], timeout=20000)
            except PlaywrightTimeoutError as e:
                await context.close()
                raise RuntimeError(f"Card input not found: {e}")

            # fill
            await page.fill(selectors["card_number"], card_number)
            if exp:
                mm, yy = exp[:2], exp[-2:]
                if selectors.get("exp_month"):
                    await page.fill(selectors["exp_month"], mm)
                if selectors.get("exp_year"):
                    await page.fill(selectors["exp_year"], yy)
            if pin and selectors.get("pin"):
                await page.fill(selectors["pin"], pin)

            # submit
            await page.click(selectors["submit"])

            # wait for result
            try:
                await page.wait_for_selector(selectors["result_balance"], timeout=20000)
                text = await page.inner_text(selectors["result_balance"])
            except PlaywrightTimeoutError:
                # fallback: read body text and attempt to extract
                text = await page.inner_text("body")

            await context.close()
            bal = parse_balance_text(text)
            return bal
    except Exception as e:
        raise RuntimeError(f"Playwright check failed: {e}")

# Determine site_key by BIN or explicit site stored in card.site
def bin_to_site_key(bin_str: str) -> str:
    # bins starting with given patterns map to mygift / securespend / mybalancenow
    if bin_str.startswith(("403446", "435880", "511332")):
        return "mygift"
    if bin_str.startswith("409758"):
        # prefer securespend then mybalancenow
        return "securespend"
    return "mygift"

# --- Handlers: /start and home menu ---
@dp.message(Command("start"))
async def on_start(msg: types.Message):
    db = SessionLocal()
    try:
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(id=msg.from_user.id, username=msg.from_user.username, display_name=msg.from_user.full_name)
            db.add(u); db.commit()
        usd_w = get_or_create_wallet(db, msg.from_user.id, "USD")
        usd_balance = Decimal(usd_w.balance or 0)
        purchased = db.query(Order).filter(Order.user_id==msg.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
    finally:
        db.close()

    await msg.answer(home_message_text(usd_balance, purchased, stock_count), parse_mode="Markdown", reply_markup=main_menu_kb(is_admin=(msg.from_user.id in ADMIN_IDS)))

# Back to home handler
@dp.callback_query(lambda c: c.data == "home:back")
async def back_to_home(cq: types.CallbackQuery):
    await cq.answer()
    # mirror start behavior
    db = SessionLocal()
    try:
        usd_w = get_or_create_wallet(db, cq.from_user.id, "USD")
        usd_balance = Decimal(usd_w.balance or 0)
        purchased = db.query(Order).filter(Order.user_id==cq.from_user.id).count()
        stock_count = db.query(Card).filter(Card.status=="in_stock").count()
    finally:
        db.close()
    await cq.message.edit_text(home_message_text(usd_balance, purchased, stock_count), parse_mode="Markdown", reply_markup=main_menu_kb(is_admin=(cq.from_user.id in ADMIN_IDS)))

# --- View Listings & Buying flow ---
@dp.callback_query(lambda c: c.data and c.data.startswith("shop:page:"))
async def shop_page_cb(cq: types.CallbackQuery):
    await cq.answer()
    _, _, page = cq.data.split(":")
    page = int(page)
    db = SessionLocal()
    try:
        items = db.query(Card).filter(Card.status=="in_stock").all()
        per = 8
        total = len(items)
        pages = max(1, math.ceil(total / per))
        page = max(1, min(page, pages))
        start = (page-1)*per
        slice_items = items[start:start+per]
    finally:
        db.close()

    kb_rows = []
    for card in slice_items:
        price = card.price or card.balance
        kb_rows.append([InlineKeyboardButton(text=f"🔹 {card.site or 'Card'} ${Decimal(price):.2f}", callback_data=f"shop:buy:{card.id}")])
    # navigation
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"shop:page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"shop:page:{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows + ([nav] if nav else []) + [[InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]])
    await cq.message.edit_text("💎 Twxn's Main Listings 💎", reply_markup=kb)

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
        price_usd = card.price if card.price and card.price > 0 else card.balance
        sale_price = Decimal(price_usd)
    finally:
        db.close()

    # Before showing the confirm button, check live balance
    await cq.message.answer("🧠 Verifying card balance before purchase...")
    try:
        site_key = bin_to_site_key(card.bin or "")
        # We pass CC#, exp if stored in card.exp (format MM/YY or MMYY) - common pattern
        exp = card.exp if card.exp else None
        # Some cards may not have pin stored; pass None
        proxy = PROXY_URL
        live_bal = await check_card_balance_playwright(site_key, card.cc_number, exp=exp, pin=None, proxy=proxy)
    except Exception as e:
        await cq.message.answer(f"⚠️ Could not verify balance automatically: {e}\nProceeding with stored balance.", reply_markup=back_home_button())
        live_bal = None

    # If live balance differs, prompt user
    db = SessionLocal()
    try:
        card = db.get(Card, cid)  # refresh
        stored_bal = Decimal(card.balance or 0)
        if live_bal is not None and live_bal != stored_bal:
            # update listing
            card.balance = live_bal
            db.commit()
            sale_price = live_bal if not card.price or card.price == 0 else card.price
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Confirm ${Decimal(sale_price):.2f}", callback_data=f"shop:confirm:{cid}:{sale_price}"),
                 InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
            ])
            await cq.message.answer(f"⚠️ Card balance changed from {money(stored_bal)} to {money(live_bal)}. Do you still want to buy for {money(sale_price)}?", parse_mode="Markdown", reply_markup=kb)
            return
        else:
            # proceed to ask for confirmation using stored sale_price
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Confirm {money(sale_price)}", callback_data=f"shop:confirm:{cid}:{sale_price}"),
                 InlineKeyboardButton(text="❌ Cancel", callback_data=f"shop:cancel:{cid}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
            ])
            await cq.message.answer(f"⚠️ Confirm purchase of card id:{cid} for *{money(sale_price)}* ?", parse_mode="Markdown", reply_markup=kb)
    finally:
        db.close()

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
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        if Decimal(w_usd.balance or 0) < sale_price:
            await cq.message.answer(f"❌ Not enough USD balance. Need {money(sale_price)}.", reply_markup=back_home_button())
            return
        # Deduct & finalize
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

# --- Purchase history, referrals, support ---
@dp.callback_query(lambda c: c.data == "history")
async def purchase_history_cb(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        orders = db.query(Order).filter(Order.user_id==cq.from_user.id).all()
        if not orders:
            await cq.message.answer("You have no purchase history.", reply_markup=back_home_button())
            return
        lines = []
        for o in orders:
            lines.append(f"Order {o.id} — Card {o.card_id} — Paid {money(o.price_usd)} — {o.created_at.date()}")
        text = "📦 Your Purchase History:\n\n" + "\n".join(lines)
    finally:
        db.close()
    await cq.message.answer(text, reply_markup=back_home_button())

@dp.callback_query(lambda c: c.data == "referrals")
async def referrals_cb(cq: types.CallbackQuery):
    await cq.answer()
    db = SessionLocal()
    try:
        refs = db.query(Referral).filter(Referral.referrer_id==cq.from_user.id).count()
    finally:
        db.close()
    await cq.message.answer(f"You have referred {refs} users.", reply_markup=back_home_button())

@dp.callback_query(lambda c: c.data == "support")
async def support_cb(cq: types.CallbackQuery):
    await cq.answer()
    await cq.message.answer("Please describe your issue and our support team will reply.", reply_markup=back_home_button())

# --- Admin Panel ---
@dp.callback_query(lambda c: c.data == "admin")
async def admin_panel_cb(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Broadcast Message", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🧾 View Orders", callback_data="admin:orders")],
        [InlineKeyboardButton(text="👥 View Users", callback_data="admin:users")],
        [InlineKeyboardButton(text="💵 Adjust Balance", callback_data="admin:adjust")],
        [InlineKeyboardButton(text="➕ Add Cards", callback_data="admin:addcard")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])
    await cq.message.answer("⚙️ Admin Panel", reply_markup=kb)

# Broadcast flow state
ADMIN_BROADCAST_WAITING = {}

@dp.callback_query(lambda c: c.data == "admin:broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    ADMIN_BROADCAST_WAITING[cq.from_user.id] = True
    await cq.message.answer("✉️ Send the message you want to broadcast to all users. Send /cancel to abort.")

@dp.message()
async def admin_broadcast_receive(msg: types.Message):
    # If admin is in waiting state, treat the incoming message as broadcast content
    if msg.from_user.id in ADMIN_BROADCAST_WAITING:
        if msg.text and msg.text.strip().lower() == "/cancel":
            ADMIN_BROADCAST_WAITING.pop(msg.from_user.id, None)
            await msg.answer("Broadcast canceled.", reply_markup=main_menu_kb(is_admin=(msg.from_user.id in ADMIN_IDS)))
            return
        # grab all users
        db = SessionLocal()
        try:
            users = db.query(User).all()
            count = 0
            for u in users:
                try:
                    await bot.send_message(u.id, f"📢 Broadcast from Twxn:\n\n{msg.text}")
                    count += 1
                except Exception:
                    pass
        finally:
            db.close()
        ADMIN_BROADCAST_WAITING.pop(msg.from_user.id, None)
        await msg.answer(f"Broadcast sent to {count} users.", reply_markup=main_menu_kb(is_admin=(msg.from_user.id in ADMIN_IDS)))

# Admin: view users / orders (simple listing)
@dp.callback_query(lambda c: c.data == "admin:users")
async def admin_users_cb(cq: types.CallbackQuery):
    await cq.answer()
    if cq.from_user.id not in ADMIN_IDS:
        await cq.message.answer("🚫 Admins only.", reply_markup=back_home_button()); return
    db = SessionLocal()
    try:
        users = db.query(User).all()
        lines = [f"{u.id} — {u.username or ''} — {u.display_name or ''}" for u in users[:200]]
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

# --- Wallet / Deposit quick handler (kept language and behavior) ---
@dp.callback_query(lambda c: c.data == "home:wallet")
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
        f"BTC: {btc:.8f}\n"
        f"LTC: {ltc:.8f}\n\n"
        "Choose a coin below. Your USD wallet is credited after *2 confirmations*."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔸 Deposit BTC", callback_data="deposit:BTC"),
         InlineKeyboardButton(text="🔹 Deposit LTC", callback_data="deposit:LTC")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])
    await cq.message.answer(txt, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("deposit:"))
async def on_deposit_coin(cq: types.CallbackQuery):
    await cq.answer()
    coin = cq.data.split(":")[1]  # BTC | LTC
    db = SessionLocal()
    try:
        # use existing get_or_create_deposit_address logic if you have it; else derive here
        w = db.query(Wallet).filter(Wallet.user_id==cq.from_user.id, Wallet.coin==coin).first()
        if not w:
            # placeholder: derive new address index
            idx = 0
            addr = derive_addr_from_xpub(os.getenv("BTC_XPUB" if coin=="BTC" else "LTC_XPUB"), coin, idx)
            w = Wallet(user_id=cq.from_user.id, coin=coin, balance=0, deposit_address=addr, address_index=idx)
            db.add(w); db.commit(); db.refresh(w)
        addr = w.deposit_address
    finally:
        db.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])
    await cq.message.answer(f"📥 Send *{coin}* to:\n`{addr}`\n\nWe credit your *USD* wallet after 2 confirmations.", parse_mode="Markdown", reply_markup=kb)

# --- Startup / main ---
async def on_startup():
    print("✅ Bot is starting...")

async def main():
    await on_startup()
    # Print bot identity to logs
    me = await bot.get_me()
    print(f"🤖 Bot username: @{me.username}")
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print("Fatal error:", e)
        raise
