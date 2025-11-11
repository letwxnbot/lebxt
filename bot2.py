# bot2.py
# Main Telegram bot (aiogram v3) with:
# - Randomized 1-row then 2-per-row main menu
# - Deposit addresses shown (reuse if present)
# - Admin: Add Cards + Broadcast Stock
# - Buy flow verifies balance via internal helper: http://twxnhelp:8000/check_balance
#
# Env:
#   BOT_TOKEN=...
#   DATABASE_URL=postgresql://twin_jza0_user:...@dpg-d4973mm3jp1c73cua21g-a/twin_jza0
#   FERNET_KEY=base64url-fernet-key
#   STOCK_CHANNEL_ID=-100xxxxxxxx
#   SUPPORT_HANDLE=@letwxn
#   ADMIN_IDS=8418864166
#   HELPER_URL=http://twxnhelp:8000

import os
import math
import random
import asyncio
from decimal import Decimal
from typing import Optional, List, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cryptography.fernet import Fernet

# ====== Load env ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/market.db")
FERNET_KEY = os.getenv("FERNET_KEY", "")
STOCK_CHANNEL_ID = int(os.getenv("STOCK_CHANNEL_ID", "0"))
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@letwxn")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8418864166").split(",") if x.strip()}
HELPER_URL = os.getenv("HELPER_URL", "http://twxnhelp:8000")

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN")
if not FERNET_KEY:
    raise SystemExit("Missing FERNET_KEY")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

# ====== DB & models ======
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

# Import your models (must be in models.py and match your existing schema)
from models import Base, User, Wallet, Card, Order  # add others if needed
Base.metadata.create_all(bind=engine)

# ====== Aiogram ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ====== Helpers ======
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def enc_text(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def dec_text(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

def money(v) -> str:
    return f"${Decimal(v):.2f}"

def get_or_create_wallet(db, user_id: int, coin: str) -> Wallet:
    w = db.query(Wallet).filter(Wallet.user_id == user_id, Wallet.coin == coin).first()
    if w:
        return w
    w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"))
    db.add(w); db.commit(); db.refresh(w)
    return w

def home_message_text(usd_balance: Decimal, purchased: int, stock_count: int) -> str:
    return (
        "💳 *Welcome to Twxn’s Prepaid Market!*\n\n"
        "💰 *Account Info:*\n"
        f"• Account Balance: *{money(usd_balance)}*\n"
        f"• Purchased cards: *{purchased}*\n"
        f"• In stock now: *{stock_count}*\n\n"
        "📰 *Stock Updates:*\n"
        f"Join the channel for live updates.\n\n"
        "🆘 *Need Help?*\n"
        f"Open a *Support Ticket* below or reach out at {SUPPORT_HANDLE}"
    )

def home_menu_kb() -> InlineKeyboardMarkup:
    # remove any duplicate listing button text you didn’t want
    buttons = [
        InlineKeyboardButton(text="🧾 View Listings", callback_data="shop:page:1"),
        InlineKeyboardButton(text="💵 Deposit", callback_data="home:deposit"),
        InlineKeyboardButton(text="🛒 My Purchases", callback_data="home:orders"),
        InlineKeyboardButton(text="🎁 Referral", callback_data="home:referral"),
        InlineKeyboardButton(text="🆘 Support", callback_data="home:support"),
    ]
    random.shuffle(buttons)
    rows = []
    if buttons:
        rows.append([buttons[0]])              # first row: 1 button
        for i in range(1, len(buttons), 2):    # subsequent rows: 2 buttons
            rows.append(buttons[i:i+2])

    # Admin row last (single)
    # shown only to admins
    rows.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_home_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home:back")]
    ])

# ====== Balance Helper HTTP ======
async def helper_check_balance(number: str, exp: str, code: str) -> dict:
    """
    POST to twxnhelp:8000/check_balance with {number, exp, code}
    Returns dict like: {status: "ok"/"invalid"/"timeout"/"unknown"/"empty", balance: float}
    """
    url = f"{HELPER_URL.rstrip('/')}/check_balance"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json={"number": number, "exp": exp, "code": code}, timeout=30) as r:
                if r.status != 200:
                    text = await r.text()
                    return {"status": "error", "balance": 0.0, "raw": f"http {r.status}: {text}"}
                return await r.json()
        except asyncio.TimeoutError:
            return {"status": "timeout", "balance": 0.0}
        except Exception as e:
            return {"status": "error", "balance": 0.0, "raw": str(e)}

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


# ====== Handlers ======
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    db = SessionLocal()
    try:
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(id=msg.from_user.id, username=msg.from_user.username, display_name=msg.from_user.full_name)
            db.add(u); db.commit()
        w_usd = get_or_create_wallet(db, msg.from_user.id, "USD")
        purchased = db.query(Order).filter(Order.user_id == msg.from_user.id).count()
        stock = db.query(Card).filter(Card.status == "in_stock").count()
        await msg.answer(
            home_message_text(Decimal(w_usd.balance or 0), purchased, stock),
            parse_mode="Markdown",
            reply_markup=home_menu_kb()
        )
    finally:
        db.close()

@dp.callback_query(F.data == "home:back")
async def on_home_back(cq: types.CallbackQuery):
    db = SessionLocal()
    try:
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        purchased = db.query(Order).filter(Order.user_id == cq.from_user.id).count()
        stock = db.query(Card).filter(Card.status == "in_stock").count()
        await cq.message.edit_text(
            home_message_text(Decimal(w_usd.balance or 0), purchased, stock),
            parse_mode="Markdown",
            reply_markup=home_menu_kb()
        )
    finally:
        db.close()

# ====== Deposit (show existing or blank) ======
def ensure_deposit_address(db, user_id: int, coin: str) -> Optional[str]:
    w = db.query(Wallet).filter(Wallet.user_id == user_id, Wallet.coin == coin).first()
    if not w:
        w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"))
        db.add(w); db.commit(); db.refresh(w)
    return w.deposit_address  # do not generate here (keep your original logic/language unchanged)

@dp.callback_query(F.data == "home:deposit")
async def on_deposit(cq: types.CallbackQuery):
    db = SessionLocal()
    try:
        btc = ensure_deposit_address(db, cq.from_user.id, "BTC")
        ltc = ensure_deposit_address(db, cq.from_user.id, "LTC")

        btc_line = f"`{btc}`" if btc else "_No BTC address set yet._"
        ltc_line = f"`{ltc}`" if ltc else "_No LTC address set yet._"

        txt = (
            "💵 *Make a Deposit*\n\n"
            f"*BTC Address:*\n{btc_line}\n\n"
            f"*LTC Address:*\n{ltc_line}\n\n"
            "Your USD wallet is credited after *2 confirmations*. "
            "Send only the correct coin to the matching address."
        )
        await cq.message.edit_text(txt, parse_mode="Markdown", reply_markup=back_home_button())
    finally:
        db.close()

# ====== Shop (simple list & buy) ======
def compute_rate_for_card(card: Card) -> Decimal:
    # Customize if you need discounts/fees
    # If card.price is set, you could prefer price over balance * rate.
    return Decimal("1.0")

def shop_kb_page(cards: List[Card], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    for c in cards:
        label = f"{c.site or '—'} • {c.bin} • {money(c.balance)} • id {c.id}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"shop:buy:{c.id}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅ Prev", callback_data=f"shop:page:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="Next ➡", callback_data=f"shop:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def paginate(items: List[Card], page: int, per: int = 10) -> Tuple[List[Card], int, int]:
    total = len(items)
    pages = max(1, math.ceil(total / per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    end = start + per
    return items[start:end], page, pages

@dp.callback_query(F.data.startswith("shop:page:"))
async def shop_page(cq: types.CallbackQuery):
    _, _, page_s = cq.data.split(":")
    page = int(page_s)
    db = SessionLocal()
    try:
        all_cards = db.query(Card).filter(Card.status == "in_stock").order_by(Card.id.desc()).all()
        subset, page, pages = paginate(all_cards, page, 10)
        await cq.message.edit_text("🧾 *Available Cards*", parse_mode="Markdown",
                                   reply_markup=shop_kb_page(subset, page, pages))
    finally:
        db.close()

@dp.callback_query(F.data.startswith("shop:buy:"))
async def shop_buy_request(cq: types.CallbackQuery):
    _, _, cid_s = cq.data.split(":")
    cid = int(cid_s)
    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status != "in_stock":
            await cq.message.answer("🚫 Card not available.", reply_markup=back_home_button())
            return
        rate = compute_rate_for_card(card)
        sale_price = (Decimal(card.price or 0) or (Decimal(card.balance or 0) * rate)).quantize(Decimal("0.01"))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Confirm {money(sale_price)}", callback_data=f"shop:confirm:{cid}:{sale_price}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="shop:page:1")]
        ])
        await cq.message.answer(
            f"⚠️ Confirm purchase of card id:{cid} for *{money(sale_price)}* ?",
            parse_mode="Markdown",
            reply_markup=kb
        )
    finally:
        db.close()

@dp.callback_query(F.data.startswith("shop:confirm:"))
async def shop_buy_confirm(cq: types.CallbackQuery):
    _, _, cid_s, price_s = cq.data.split(":")
    cid = int(cid_s)
    sale_price = Decimal(price_s)

    db = SessionLocal()
    try:
        card = db.get(Card, cid)
        if not card or card.status != "in_stock":
            await cq.message.answer("🚫 Card not available.", reply_markup=back_home_button())
            return

        # Check user balance
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        if Decimal(w_usd.balance or 0) < sale_price:
            await cq.message.answer(f"❌ Not enough USD balance. Need {money(sale_price)}.", reply_markup=back_home_button())
            return

        # ===== PRE-PURCHASE BALANCE VERIFY via helper =====
        number = card.cc_number or ""
        exp = (card.exp or "").replace(" ", "")
        code = ""
        try:
            # If your code (PIN) is stored inside encrypted_code, you can parse it
            # For now we assume the “code” is in encrypted_code OR not needed
            code = dec_text(card.encrypted_code)
        except Exception:
            pass

        chk = await helper_check_balance(number, exp, code)
        status = chk.get("status")
        new_balance = Decimal(str(chk.get("balance", "0")))
        if status in ("invalid", "timeout", "error", "unknown") or new_balance <= 0:
            await cq.message.answer("❌ Card failed verification. Purchase canceled.", reply_markup=back_home_button())
            return

        # If balance changed, update the listing BEFORE charging
        if new_balance != Decimal(card.balance or 0):
            card.balance = new_balance
            db.commit()
            await cq.message.answer(f"ℹ️ Card balance updated to {money(new_balance)}. Please re-confirm from listings.", reply_markup=back_home_button())
            return

        # Charge & finalize
        w_usd.balance = Decimal(w_usd.balance or 0) - sale_price
        card.status = "sold"
        order = Order(user_id=cq.from_user.id, card_id=card.id, price_usd=sale_price, coin_used="USD", coin_amount=sale_price)
        db.add(order)
        db.commit(); db.refresh(order)

        # Reveal code
        code_val = ""
        try:
            code_val = dec_text(card.encrypted_code)
        except Exception:
            code_val = "(encrypted)"

        msg = (
            f"✅ Purchase complete!\n\n"
            f"Card id: {card.id}\n"
            f"Site: {card.site or '—'}\n"
            f"BIN: {card.bin}\n"
            f"CC: {card.cc_number}\n"
            f"EXP: {card.exp}\n"
            f"CODE: `{code_val}`\n\n"
            f"Paid: {money(sale_price)}\nOrder ID: {order.id}"
        )
        await cq.message.answer(msg, parse_mode="Markdown", reply_markup=back_home_button())

    finally:
        db.close()

# ====== Admin ======
@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not allowed.", show_alert=True)
    await cq.message.edit_text("👑 Admin Panel", reply_markup=admin_menu_kb())

@dp.callback_query(F.data == "admin:add")
async def admin_add_cards_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not allowed.", show_alert=True)
    await state.set_state(AddCardSG.waiting_line)
    fmt = ("Send one line per card in this format:\n"
           "`SITE|BIN|CC_NUMBER|EXP|PIN/CODE|BALANCE`")
    await cq.message.edit_text("➕ *Add Cards*\n\n" + fmt, parse_mode="Markdown")

@dp.message(AddCardSG.waiting_line)
async def admin_add_cards_receive(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    line = msg.text.strip()
    try:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 6:
            raise ValueError("Need 6 fields")
        site, bin6, cc, exp, code, bal_s = parts
        bal = Decimal(bal_s)

        db = SessionLocal()
        try:
            c = Card(
                site=site, bin=bin6, cc_number=cc, exp=exp,
                encrypted_code=enc_text(code),
                balance=bal, currency="USD", status="in_stock",
                added_by=msg.from_user.id
            )
            db.add(c); db.commit(); db.refresh(c)

            # broadcast stock update
            try:
                if STOCK_CHANNEL_ID != 0:
                    await bot.send_message(STOCK_CHANNEL_ID, f"🆕 New card: {site} {bin6} • {money(bal)} • id {c.id}")
            except Exception:
                pass

            await msg.answer(f"✅ Card #{c.id} added.")
        finally:
            db.close()
        await state.clear()
    except Exception as e:
        await msg.answer(f"❌ Failed to add: {e}\nFormat: `SITE|BIN|CC|EXP|PIN/CODE|BALANCE`", parse_mode="Markdown")

@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_stock(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not allowed.", show_alert=True)
    db = SessionLocal()
    try:
        cnt = db.query(Card).filter(Card.status == "in_stock").count()
    finally:
        db.close()
    if STOCK_CHANNEL_ID == 0:
        return await cq.answer("STOCK_CHANNEL_ID not set.", show_alert=True)
    await bot.send_message(STOCK_CHANNEL_ID, f"📦 Stock live: {cnt} card(s).")
    await cq.answer("Sent.")

# ====== Run ======
async def main():
    me = await bot.get_me()
    print(f"🤖 Running as: @{me.username}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

