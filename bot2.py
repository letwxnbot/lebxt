# bot2.py — Twxn’s Prepaid Market (Aiogram v3)
# - Admin panel restored (Add Cards, Broadcast, View Sales placeholder, Support Tickets placeholder)
# - Add Cards => auto price 40% of balance + broadcast to stock channel
# - Deposit/Wallet restore: BTC/LTC addresses generated from XPUB if missing (bip_utils best-effort)
# - Shop: live balance verify via helper (http://twxnhelp:8000/check_balance) before completing sale
# - Home menu: 1 button first row, then 2 per row (randomized), matches your requested layout

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

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/market.db")
FERNET_KEY = os.getenv("FERNET_KEY", "")
STOCK_CHANNEL_ID = int(os.getenv("STOCK_CHANNEL_ID", "0"))
SUPPORT_HANDLE = os.getenv("SUPPORT_HANDLE", "@letwxn")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8418864166").split(",") if x.strip()}
HELPER_URL = os.getenv("HELPER_URL", "http://twxnhelp:8000")

# XPUBs for address generation (restore wallet gen behavior)
BTC_XPUB = os.getenv("BTC_XPUB", "").strip()
LTC_XPUB = os.getenv("LTC_XPUB", "").strip()

if not BOT_TOKEN:
    raise SystemExit("❌ Missing BOT_TOKEN")
if not FERNET_KEY:
    raise SystemExit("❌ Missing FERNET_KEY")

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

# ====== DB & MODELS ======
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

# Your models must exist in models.py with these names
from models import Base, User, Wallet, Card, Order, Referral, ReferralBonus, SupportTicket
Base.metadata.create_all(bind=engine)

# ====== AIROGRAM ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ====== HELPERS ======
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
        "Join the channel for live updates.\n\n"
        "🆘 *Need Help?*\n"
        f"Open a *Support Ticket* below or reach out at {SUPPORT_HANDLE}"
    )

def home_menu_kb(is_admin_user: bool) -> InlineKeyboardMarkup:
    # main rows randomized: 1 button first row, 2 thereafter
    buttons = [
        InlineKeyboardButton(text="🧾 View Listings", callback_data="shop:page:1"),
        InlineKeyboardButton(text="💵 Deposit", callback_data="home:wallet"),
        InlineKeyboardButton(text="🛒 My Purchases", callback_data="home:orders"),
        InlineKeyboardButton(text="🎁 Referral", callback_data="home:referral"),
        InlineKeyboardButton(text="🆘 Support", callback_data="home:support"),
    ]
    random.shuffle(buttons)
    rows = []
    if buttons:
        rows.append([buttons[0]])              # 1 button row
        for i in range(1, len(buttons), 2):    # 2 per row afterward
            rows.append(buttons[i:i+2])
    if is_admin_user:
        rows.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def paginate(items: List[Card], page: int, per: int = 10) -> Tuple[List[Card], int, int]:
    total = len(items)
    pages = max(1, math.ceil(total / per))
    page = max(1, min(page, pages))
    start = (page - 1) * per
    end = start + per
    return items[start:end], page, pages

# ====== XPUB DERIVATION (best-effort) ======
def _derive_address_from_xpub(xpub: str, index: int, coin_type: str) -> Optional[str]:
    """
    Try to derive bech32 addresses with bip_utils; if unavailable, return None
    coin_type in {"BTC","LTC"} used only as a hint.
    """
    if not xpub:
        return None
    try:
        from bip_utils import Bip84, Bip84Coins
        coins = Bip84Coins.BITCOIN if coin_type == "BTC" else Bip84Coins.LITECOIN
        acct = Bip84.FromXPub(xpub, coins)
        return acct.Change(Bip84.Change.EXTERNAL).AddressIndex(index).PublicKey().ToAddress()
    except Exception:
        return None

def _next_address_index(db, user_id: int, coin: str) -> int:
    w = db.query(Wallet).filter(Wallet.user_id == user_id, Wallet.coin == coin).first()
    if not w:
        w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"), address_index=0)
        db.add(w); db.commit(); db.refresh(w)
    if w.address_index is None:
        w.address_index = 0
    return int(w.address_index)

def ensure_deposit_address(db, user_id: int, coin: str) -> Optional[str]:
    """
    Restore original behavior: if no address exists, derive one from XPUB and save it.
    Reuse existing address if already present.
    """
    w = db.query(Wallet).filter(Wallet.user_id == user_id, Wallet.coin == coin).first()
    if not w:
        w = Wallet(user_id=user_id, coin=coin, balance=Decimal("0"))
        db.add(w); db.commit(); db.refresh(w)

    if w.deposit_address:
        return w.deposit_address

    # Derive only if XPUB is set
    xpub = BTC_XPUB if coin == "BTC" else LTC_XPUB if coin == "LTC" else ""
    if not xpub:
        return None

    idx = _next_address_index(db, user_id, coin)
    addr = _derive_address_from_xpub(xpub, idx, coin)

    if addr:
        w.deposit_address = addr
        w.address_index = idx + 1
        db.commit(); db.refresh(w)
        return addr

    # Could not derive (lib missing or invalid xpub)
    return None

# ====== BALANCE HELPER HTTP ======
async def helper_check_balance(number: str, exp: str, code: str) -> dict:
    """
    POST to twxnhelp /check_balance; returns {status, balance}
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

# ====== PRICING ======
def compute_rate_for_card(card: Card) -> Decimal:
    # Sell at 40% of face value (as requested)
    return Decimal("0.40")

# ====== /start with referral capture ======
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    db = SessionLocal()
    try:
        u = db.get(User, msg.from_user.id)
        if not u:
            u = User(id=msg.from_user.id, username=msg.from_user.username, display_name=msg.from_user.full_name)
            db.add(u); db.commit()

        # Capture referral: /start ref_<id>
        try:
            if msg.text and " " in msg.text:
                _, param = msg.text.split(" ", 1)
                param = param.strip()
                if param.startswith("ref_"):
                    ref_id = int(param.replace("ref_", ""))
                    if ref_id != msg.from_user.id:
                        ex = db.query(Referral).filter(Referral.user_id == msg.from_user.id).first()
                        if not ex:
                            db.add(Referral(user_id=msg.from_user.id, referrer_id=ref_id)); db.commit()
        except Exception:
            pass

        # Ensure wallets exist
        w_usd = get_or_create_wallet(db, msg.from_user.id, "USD")
        purchased = db.query(Order).filter(Order.user_id == msg.from_user.id).count()
        stock = db.query(Card).filter(Card.status == "in_stock").count()
        await msg.answer(
            home_message_text(Decimal(w_usd.balance or 0), purchased, stock),
            parse_mode="Markdown",
            reply_markup=home_menu_kb(is_admin(msg.from_user.id))
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
            reply_markup=home_menu_kb(is_admin(cq.from_user.id))
        )
    finally:
        db.close()

# ====== WALLET / DEPOSIT (original style with coin choice) ======
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
        f"BTC: {btc:.8f}\n"
        f"LTC: {ltc:.8f}\n\n"
        "Choose a coin below. Your USD wallet is credited after *2 confirmations*."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔸 Deposit BTC", callback_data="deposit:BTC"),
         InlineKeyboardButton(text="🔹 Deposit LTC", callback_data="deposit:LTC")],
        [InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home:back")]
    ])
    await cq.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("deposit:"))
async def on_deposit_coin(cq: types.CallbackQuery):
    await cq.answer()
    coin = cq.data.split(":")[1]  # BTC | LTC
    db = SessionLocal()
    try:
        addr = ensure_deposit_address(db, cq.from_user.id, coin)
        if not addr:
            await cq.message.answer(
                f"⚠️ Could not generate {coin} address (missing XPUB or derivation lib).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="home:wallet")]])
            )
            return
    finally:
        db.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:wallet")]
    ])
    await cq.message.answer(
        f"📥 Send *{coin}* to:\n`{addr}`\n\nWe credit your *USD* wallet after 2 confirmations.",
        parse_mode="Markdown",
        reply_markup=kb
    )

# ====== SHOP ======
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
            await cq.message.answer("🚫 Card not available.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="shop:page:1")]]))
            return
        rate = compute_rate_for_card(card)
        # Prefer explicit card.price if you ever set it; otherwise 40% rule
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
            await cq.message.answer("🚫 Card not available.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="shop:page:1")]]))
            return

        # Check buyer USD balance
        w_usd = get_or_create_wallet(db, cq.from_user.id, "USD")
        if Decimal(w_usd.balance or 0) < sale_price:
            await cq.message.answer(f"❌ Not enough USD balance. Need {money(sale_price)}.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="shop:page:1")]]))
            return

        # Live verification via helper
        number = card.cc_number or ""
        exp = (card.exp or "").replace(" ", "")
        # If your PIN/CODE is the gift code, it's stored encrypted
        code = ""
        try:
            code = dec_text(card.encrypted_code)
        except Exception:
            pass

        chk = await helper_check_balance(number, exp, code)
        status = chk.get("status")
        new_balance = Decimal(str(chk.get("balance", "0")))
        if status in ("invalid", "timeout", "error", "unknown") or new_balance <= 0:
            await cq.message.answer("❌ Card failed verification. Purchase canceled.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back", callback_data="shop:page:1")]]))
            return

        # If balance changed, update listing and ask to reconfirm
        if new_balance != Decimal(card.balance or 0):
            card.balance = new_balance
            db.commit()
            await cq.message.answer(f"ℹ️ Card balance updated to {money(new_balance)}. Please re-open listings to reconfirm.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧾 View Listings", callback_data="shop:page:1")]]))
            return

        # Deduct & finalize sale
        w_usd.balance = Decimal(w_usd.balance or 0) - sale_price
        card.status = "sold"
        order = Order(user_id=cq.from_user.id, card_id=card.id, price_usd=sale_price, coin_used="USD", coin_amount=sale_price, status="completed")
        db.add(order)
        db.commit(); db.refresh(order)

        # Reveal the code to buyer
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
        await cq.message.answer(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Home", callback_data="home:back")]]))

    finally:
        db.close()

# ====== ADMIN PANEL (same options restored) ======
class AddCardSG(StatesGroup):
    waiting_line = State()

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Cards", callback_data="admin:add")],
        [InlineKeyboardButton(text="📢 Broadcast Stock Count", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🗂 View Sales (coming soon)", callback_data="admin:view_sales")],
        [InlineKeyboardButton(text="🎫 Support Tickets (coming soon)", callback_data="admin:support")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="home:back")]
    ])

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

            # Broadcast to stock channel
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

@dp.callback_query(F.data == "admin:view_sales")
async def admin_view_sales(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not allowed.", show_alert=True)
    await cq.answer("Coming soon.")

@dp.callback_query(F.data == "admin:support")
async def admin_support(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer("Not allowed.", show_alert=True)
    await cq.answer("Coming soon.")

# ====== RUN ======
async def main():
    me = await bot.get_me()
    print(f"🤖 Running as: @{me.username}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

