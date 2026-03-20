"""
Wallet loop — два режима:
1. Personal wallet (mode=wallet): отслеживает покупки юзера на gmgn/Photon/Trojan
2. Smart wallets: отслеживает покупки tracked кошельков + bundle detector

Интервал: WALLET_CHECK_EVERY секунд (default 90s)
API: Helius Enhanced Transactions
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from sqlalchemy import select
from database import async_session
from models import User, Position, SmartWallet, SmartWalletTx, SeenTx, FiredAlert
from services.helius import fetch_wallet_txs
from services.price import fetch_price, get_cached_sol_price
from utils import fmt_mcap, fmt_sol, fmt_usd, fmt_x, dexscreener
from config import WALLET_CHECK_EVERY
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)

SOL_MINT         = "So11111111111111111111111111111111111111112"
BUNDLE_THRESHOLD = 3
BUNDLE_WINDOW    = 60   # минут

DEFAULT_PLAN = [
    {"x": 4,  "pct": 50, "label": "4x"},
    {"x": 8,  "pct": 30, "label": "8x"},
    {"x": 0,  "pct": 20, "label": "moon"},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _tx_seen(sig: str) -> bool:
    async with async_session() as s:
        return await s.get(SeenTx, sig) is not None

async def _mark_seen(sig: str):
    async with async_session() as s:
        try:
            s.add(SeenTx(sig=sig))
            await s.commit()
        except Exception:
            pass

async def _was_alerted(key: str) -> bool:
    async with async_session() as s:
        return await s.get(FiredAlert, key) is not None

async def _mark_alerted(uid: int, key: str):
    async with async_session() as s:
        try:
            s.add(FiredAlert(alert_key=key, user_id=uid))
            await s.commit()
        except Exception:
            pass

def _get_user_plan(user: User) -> list:
    """Берём дефолтный план юзера или стандартный."""
    if user.default_plan:
        try:
            return json.loads(user.default_plan)
        except Exception:
            pass
    return DEFAULT_PLAN


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(bot):
    log.info("wallet_loop started")
    await asyncio.sleep(30)

    while True:
        await asyncio.sleep(WALLET_CHECK_EVERY)
        try:
            async with async_session() as s:
                result = await s.execute(select(User))
                users  = result.scalars().all()

            for user in users:
                if user.mode == "wallet" and user.wallet:
                    await _process_personal_wallet(bot, user)
                await _process_smart_wallets(bot, user)

        except Exception as e:
            log.error(f"wallet_loop: {e}")


# ── Personal wallet — автотрекинг покупок юзера ───────────────────────────────

async def _process_personal_wallet(bot, user: User):
    txs = await fetch_wallet_txs(user.wallet, limit=10)
    if not txs:
        return

    sol_price = get_cached_sol_price()
    currency  = user.currency or "SOL"

    for tx in txs:
        sig = tx.get("signature", "")
        if not sig or await _tx_seen(sig):
            continue
        await _mark_seen(sig)

        # Определяем: это покупка или продажа?
        mint    = _bought_mint(tx, user.wallet)
        is_sell = False
        if not mint:
            mint    = _sold_mint(tx, user.wallet)
            is_sell = True
        if not mint:
            continue

        data = await fetch_price(mint)
        if not data:
            continue

        sol_amount = _sol_spent(tx, user.wallet) if not is_sell else _sol_received(tx, user.wallet)
        if sol_amount < 0.001:
            sol_amount = 0.0

        # ── ПОКУПКА — создаём позицию ────────────────────────────────────
        if not is_sell:
            # Проверяем — нет ли уже активной позиции по этому токену
            async with async_session() as s:
                existing = await s.execute(
                    select(Position).where(
                        Position.user_id  == user.user_id,
                        Position.contract == mint,
                        Position.status   == "active"
                    )
                )
                if existing.first():
                    # Позиция есть — это дополнительная покупка (DCA)
                    # Просто сообщаем об этом
                    await _send_dca_alert(bot, user, mint, data, sol_amount, currency, sol_price)
                    continue

            plan = _get_user_plan(user)

            async with async_session() as s:
                pos = Position(
                    user_id     = user.user_id,
                    contract    = mint,
                    symbol      = data["symbol"],
                    name        = data["name"],
                    entry_price = data["price"],
                    entry_mcap  = data["mcap"],
                    sol_in      = sol_amount or 0.5,
                    exit_plan   = json.dumps(plan),
                    source      = "wallet",
                    status      = "active",
                )
                s.add(pos)
                await s.commit()
                await s.refresh(pos)
                pos_id = pos.id

            await _send_buy_alert(bot, user, pos_id, mint, data,
                                  sol_amount, plan, currency, sol_price)

        # ── ПРОДАЖА — обновляем позицию если есть ────────────────────────
        else:
            await _handle_wallet_sell(bot, user, mint, data, sol_amount, currency, sol_price)

        await asyncio.sleep(0.3)


async def _send_buy_alert(bot, user, pos_id, mint, data, sol_amount, plan, currency, sol_price):
    """Алерт о новой покупке с полной инфой."""
    sol_str = fmt_sol(sol_amount) if sol_amount else "unknown"

    # Стоимость в USD
    usd_str = ""
    if sol_price and sol_amount:
        usd_str = f" (≈{fmt_usd(sol_amount * sol_price)})"

    # Ожидаемые тейки
    plan_preview = ""
    for l in plan:
        x = l.get("x", 0)
        pct = l.get("pct", 0)
        if x and sol_amount:
            val = sol_amount * (pct / 100) * x
            val_str = fmt_usd(val * sol_price) if currency == "USD" and sol_price else fmt_sol(val, 2)
            lbl = l.get("label", f"{x}x")
            plan_preview += f"  • {lbl} → sell {pct}% ≈ {val_str}\n"
        elif not x:
            plan_preview += f"  • 🌙 Moonbag → hold {pct}%\n"

    await bot.send_message(
        user.user_id,
        f"🤖 *AUTO-TRACKED BUY — ${data['symbol']}*\n\n"
        f"📛 {data['name']}\n"
        f"💰 Spent: *{sol_str}*{usd_str}\n"
        f"📊 Entry mcap: *{fmt_mcap(data['mcap'])}*\n"
        f"💧 Liquidity: {fmt_mcap(data['liquidity'])}\n\n"
        f"📋 *Auto exit plan:*\n{plan_preview}\n"
        f"[DexScreener]({dexscreener(mint)})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✎ Edit Plan",  callback_data=f"editplan:{pos_id}"),
            InlineKeyboardButton("✅ Keep Plan", callback_data=f"keepplan:{pos_id}"),
        ]])
    )
    log.info(f"auto-buy alert: {data['symbol']} → user {user.user_id}")


async def _send_dca_alert(bot, user, mint, data, sol_amount, currency, sol_price):
    """Юзер докупил токен у которого уже есть позиция."""
    sol_str = fmt_sol(sol_amount) if sol_amount else "?"
    usd_str = f" (≈{fmt_usd(sol_amount * sol_price)})" if sol_price and sol_amount else ""

    await bot.send_message(
        user.user_id,
        f"➕ *DCA DETECTED — ${data['symbol']}*\n\n"
        f"You bought more of an existing position.\n"
        f"Amount: *{sol_str}*{usd_str}\n"
        f"Current mcap: *{fmt_mcap(data['mcap'])}*\n\n"
        f"_Position SOL-in not updated automatically.\n"
        f"Edit your position manually if needed._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 View Position", callback_data="do:pos"),
        ]])
    )


async def _handle_wallet_sell(bot, user, mint, data, sol_received, currency, sol_price):
    """Юзер продал токен — обновляем позицию."""
    async with async_session() as s:
        result = await s.execute(
            select(Position).where(
                Position.user_id  == user.user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )
        pos = result.scalars().first()

    if not pos:
        return  # нет позиции — не трекаем

    cur_x   = data["price"] / pos.entry_price if pos.entry_price else 0
    pnl_sol = sol_received - pos.sol_in if sol_received else 0
    sol_str = fmt_sol(sol_received) if sol_received else "?"
    sign    = "+" if pnl_sol >= 0 else ""
    emoji   = "🟢" if pnl_sol >= 0 else "🔴"

    await bot.send_message(
        user.user_id,
        f"{emoji} *SELL DETECTED — ${data['symbol']}*\n\n"
        f"Received: *{sol_str}*\n"
        f"Exit: *{fmt_x(cur_x)}*\n"
        f"PnL: *{sign}{fmt_sol(pnl_sol)}*\n\n"
        f"_Close position in the bot to log it to journal._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔒 Close position", callback_data=f"closepos:{pos.id}"),
        ]])
    )
    log.info(f"sell detected: {data['symbol']} → user {user.user_id}")


# ── Smart wallets ─────────────────────────────────────────────────────────────

async def _process_smart_wallets(bot, user: User):
    async with async_session() as s:
        result  = await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user.user_id)
        )
        wallets = result.scalars().all()

    if not wallets:
        return

    new_buys: dict[str, list] = {}

    for sw in wallets:
        txs = await fetch_wallet_txs(sw.address, limit=5)
        for tx in txs:
            sig = tx.get("signature", "")
            if not sig or await _tx_seen(sig):
                continue
            await _mark_seen(sig)

            mint = _bought_mint(tx, sw.address)
            if not mint:
                continue

            sol = _sol_spent(tx, sw.address)

            async with async_session() as s:
                try:
                    s.add(SmartWalletTx(
                        user_id    = user.user_id,
                        address    = sw.address,
                        contract   = mint,
                        label      = sw.label,
                        action     = "buy",
                        sol_amount = sol,
                        tx_sig     = sig,
                    ))
                    await s.commit()
                except Exception:
                    pass

            new_buys.setdefault(mint, []).append(sw)

        await asyncio.sleep(0.3)

    # Bundle check
    for mint in new_buys:
        cutoff = datetime.utcnow() - timedelta(minutes=BUNDLE_WINDOW)
        async with async_session() as s:
            result = await s.execute(
                select(SmartWalletTx).where(
                    SmartWalletTx.user_id  == user.user_id,
                    SmartWalletTx.contract == mint,
                    SmartWalletTx.action   == "buy",
                    SmartWalletTx.seen_at  >= cutoff,
                )
            )
            recent = result.scalars().all()

        unique = list({r.address: r for r in recent}.values())
        if len(unique) < BUNDLE_THRESHOLD:
            continue

        key = f"bundle:{user.user_id}:{mint}"
        if await _was_alerted(key):
            continue
        await _mark_alerted(user.user_id, key)

        data   = await fetch_price(mint)
        mcap   = fmt_mcap(data["mcap"]) if data else "?"
        name   = data["name"]   if data else mint[:8] + "..."
        symbol = data["symbol"] if data else "???"
        liq    = fmt_mcap(data["liquidity"]) if data else "?"

        lines = "\n".join(
            f"  🧠 {w.label or w.address[:8]+'...'} — {w.sol_amount:.2f} SOL"
            for w in unique[:5]
        )

        await bot.send_message(
            user.user_id,
            f"🚨 *BUNDLE SIGNAL — {name}* (${symbol})\n\n"
            f"*{len(unique)} smart wallets* bought in the last hour!\n\n"
            f"📊 Mcap: {mcap}  |  Liq: {liq}\n"
            f"`{mint}`\n\n"
            f"*Who bought:*\n{lines}\n\n"
            f"[DexScreener]({dexscreener(mint)})\n\n"
            f"⚡ _Strong signal — verify before entry!_",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add Position", callback_data=f"quickadd:{mint}"),
                InlineKeyboardButton("📈 Chart",        url=dexscreener(mint)),
            ]])
        )
        log.info(f"bundle alert: {symbol} → {user.user_id}")


# ── TX parsers ────────────────────────────────────────────────────────────────

def _bought_mint(tx: dict, wallet: str) -> str | None:
    """Токен который кошелёк ПОЛУЧИЛ (покупка)."""
    for t in tx.get("tokenTransfers", []):
        mint = t.get("mint", "")
        to   = t.get("toUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in to.lower():
            return mint
    return None


def _sold_mint(tx: dict, wallet: str) -> str | None:
    """Токен который кошелёк ОТДАЛ (продажа)."""
    for t in tx.get("tokenTransfers", []):
        mint  = t.get("mint", "")
        frm   = t.get("fromUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in frm.lower():
            return mint
    return None


def _sol_spent(tx: dict, wallet: str) -> float:
    """SOL ушедший из кошелька."""
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("fromUserAccount", "").lower()
    )
    return abs(total) / 1e9


def _sol_received(tx: dict, wallet: str) -> float:
    """SOL пришедший в кошелёк."""
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("toUserAccount", "").lower()
    )
    return abs(total) / 1e9