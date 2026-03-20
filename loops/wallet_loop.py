"""
Wallet loop — два режима работы:
1. Личный кошелёк юзера: REST polling через Helius каждые WALLET_CHECK_EVERY сек
   (WebSocket в helius_ws.py работает как дополнение если есть платный план)
2. Смарт-кошельки: polling + bundle detector
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
from utils import fmt_mcap, fmt_sol, fmt_x, dexscreener
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
    if getattr(user, 'default_plan', None):
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
                # Личный кошелёк — REST polling (работает на любом Helius плане)
                if user.mode == "wallet" and user.wallet:
                    await _process_personal_wallet(bot, user)

                # Смарт-кошельки
                await _process_smart_wallets(bot, user)

        except Exception as e:
            log.error(f"wallet_loop: {e}")


# ── Личный кошелёк ────────────────────────────────────────────────────────────

async def _process_personal_wallet(bot, user: User):
    txs = await fetch_wallet_txs(user.wallet, limit=10)
    if not txs:
        return

    sol_price = get_cached_sol_price()
    currency  = getattr(user, 'currency', None) or "SOL"

    for tx in txs:
        sig = tx.get("signature", "")
        if not sig or await _tx_seen(sig):
            continue
        await _mark_seen(sig)

        # Покупка?
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

        sol_amount = (_sol_spent(tx, user.wallet) if not is_sell
                      else _sol_received(tx, user.wallet))
        if sol_amount < 0.001:
            sol_amount = 0.0

        if not is_sell:
            await _handle_buy(bot, user, mint, data, sol_amount, currency, sol_price)
        else:
            await _handle_sell(bot, user, mint, data, sol_amount, currency, sol_price)

        await asyncio.sleep(0.3)


async def _handle_buy(bot, user, mint, data, sol_amount, currency, sol_price):
    # Уже есть позиция?
    async with async_session() as s:
        existing = (await s.execute(
            select(Position).where(
                Position.user_id  == user.user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    if existing:
        # DCA — докупка
        sol_str = fmt_sol(sol_amount) if sol_amount else "?"
        usd_str = f" (≈${sol_amount * sol_price:.0f})" if sol_price and sol_amount else ""
        try:
            await bot.send_message(
                user.user_id,
                f"➕ *DCA — ${data['symbol']}*\n\n"
                f"Added to existing position.\n"
                f"Amount: *{sol_str}*{usd_str}\n"
                f"Mcap: {fmt_mcap(data['mcap'])}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 View Position", callback_data="do:pos"),
                ]])
            )
        except Exception as e:
            log.error(f"DCA alert: {e}")
        return

    # Новая позиция
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

    sol_str = fmt_sol(sol_amount) if sol_amount else "?"
    usd_str = f" (≈${sol_amount * sol_price:.0f})" if sol_price and sol_amount else ""

    # Превью плана
    plan_lines = ""
    for l in plan:
        x = l.get("x", 0)
        pct = l.get("pct", 0)
        if x and sol_amount:
            val = sol_amount * (pct / 100) * x
            val_s = f"${val * sol_price:.0f}" if currency == "USD" and sol_price else fmt_sol(val, 2)
            plan_lines += f"  • {l.get('label', f'{x}x')} → sell {pct}% ≈ {val_s}\n"
        elif not x:
            plan_lines += f"  • 🌙 Hold {pct}%\n"

    try:
        await bot.send_message(
            user.user_id,
            f"🤖 *AUTO-TRACKED — ${data['symbol']}*\n\n"
            f"*{data['name']}*\n"
            f"💰 Spent: *{sol_str}*{usd_str}\n"
            f"📊 Entry mcap: *{fmt_mcap(data['mcap'])}*\n"
            f"💧 Liq: {fmt_mcap(data['liquidity'])}\n\n"
            f"📋 *Auto exit plan:*\n{plan_lines}\n"
            f"[DexScreener]({dexscreener(mint)})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✎ Edit Plan",  callback_data=f"editplan:{pos_id}"),
                InlineKeyboardButton("✅ Keep Plan", callback_data=f"keepplan:{pos_id}"),
            ]])
        )
        log.info(f"buy alert: {data['symbol']} → {user.user_id}")
    except Exception as e:
        log.error(f"buy alert send: {e}")


async def _handle_sell(bot, user, mint, data, sol_received, currency, sol_price):
    # Находим позицию
    async with async_session() as s:
        pos = (await s.execute(
            select(Position).where(
                Position.user_id  == user.user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    if not pos:
        return  # нет позиции — не трекаем

    cur_x   = data["price"] / pos.entry_price if pos.entry_price else 0
    pnl_sol = (sol_received - pos.sol_in) if sol_received else 0
    sign    = "+" if pnl_sol >= 0 else ""
    emoji   = "🟢" if pnl_sol >= 0 else "🔴"
    sol_str = fmt_sol(sol_received) if sol_received else "?"
    usd_str = f" (≈${sol_received * sol_price:.0f})" if sol_price and sol_received else ""

    try:
        await bot.send_message(
            user.user_id,
            f"{emoji} *SELL DETECTED — ${data['symbol']}*\n\n"
            f"Received: *{sol_str}*{usd_str}\n"
            f"Exit: *{fmt_x(cur_x)}*\n"
            f"PnL: *{sign}{fmt_sol(pnl_sol)}*\n\n"
            f"_Tap Close to log it to journal._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔒 Close position", callback_data=f"closepos:{pos.id}"),
                InlineKeyboardButton("◀️ Menu",           callback_data="do:menu"),
            ]])
        )
        log.info(f"sell alert: {data['symbol']} → {user.user_id}")
    except Exception as e:
        log.error(f"sell alert send: {e}")


# ── Смарт-кошельки ────────────────────────────────────────────────────────────

async def _process_smart_wallets(bot, user: User):
    async with async_session() as s:
        wallets = (await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user.user_id)
        )).scalars().all()

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
            recent = (await s.execute(
                select(SmartWalletTx).where(
                    SmartWalletTx.user_id  == user.user_id,
                    SmartWalletTx.contract == mint,
                    SmartWalletTx.action   == "buy",
                    SmartWalletTx.seen_at  >= cutoff,
                )
            )).scalars().all()

        unique = list({r.address: r for r in recent}.values())
        if len(unique) < BUNDLE_THRESHOLD:
            continue

        key = f"bundle:{user.user_id}:{mint}"
        if await _was_alerted(key):
            continue
        await _mark_alerted(user.user_id, key)

        data   = await fetch_price(mint)
        mcap   = fmt_mcap(data["mcap"]) if data else "?"
        symbol = data["symbol"] if data else "???"
        name   = data["name"]   if data else mint[:8]
        liq    = fmt_mcap(data["liquidity"]) if data else "?"

        lines = "\n".join(
            f"  🧠 {w.label or w.address[:8]+'...'} — {w.sol_amount:.2f} SOL"
            for w in unique[:5]
        )

        try:
            await bot.send_message(
                user.user_id,
                f"🚨 *BUNDLE — {name}* (${symbol})\n\n"
                f"*{len(unique)} smart wallets* in {BUNDLE_WINDOW}min!\n\n"
                f"📊 Mcap: {mcap}  |  Liq: {liq}\n"
                f"`{mint}`\n\n{lines}\n\n"
                f"[DexScreener]({dexscreener(mint)})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add Position", callback_data=f"quickadd:{mint}"),
                    InlineKeyboardButton("📈 Chart",        url=dexscreener(mint)),
                ]])
            )
            log.info(f"bundle: {symbol} → {user.user_id}")
        except Exception as e:
            log.error(f"bundle send: {e}")


# ── TX parsers ────────────────────────────────────────────────────────────────

def _bought_mint(tx: dict, wallet: str) -> str | None:
    for t in tx.get("tokenTransfers", []):
        mint = t.get("mint", "")
        to   = t.get("toUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in to.lower():
            return mint
    return None


def _sold_mint(tx: dict, wallet: str) -> str | None:
    for t in tx.get("tokenTransfers", []):
        mint = t.get("mint", "")
        frm  = t.get("fromUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in frm.lower():
            return mint
    return None


def _sol_spent(tx: dict, wallet: str) -> float:
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("fromUserAccount", "").lower()
    )
    return abs(total) / 1e9


def _sol_received(tx: dict, wallet: str) -> float:
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("toUserAccount", "").lower()
    )
    return abs(total) / 1e9