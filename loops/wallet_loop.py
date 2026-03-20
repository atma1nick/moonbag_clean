"""
Wallet loop — ТОЛЬКО смарт-кошельки + bundle detector.
Личный кошелёк юзера обрабатывается ТОЛЬКО через helius_ws.py
чтобы не было дублирования алертов.
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
from utils import fmt_mcap, fmt_sol, dexscreener
from config import WALLET_CHECK_EVERY
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)

SOL_MINT         = "So11111111111111111111111111111111111111112"
BUNDLE_THRESHOLD = 3
BUNDLE_WINDOW    = 60  # минут

DEFAULT_PLAN = [
    {"x": 4,  "pct": 50, "label": "4x"},
    {"x": 8,  "pct": 30, "label": "8x"},
    {"x": 0,  "pct": 20, "label": "moon"},
]


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
    if user.default_plan:
        try:
            return json.loads(user.default_plan)
        except Exception:
            pass
    return DEFAULT_PLAN


async def run(bot):
    log.info("wallet_loop started — smart wallets only")
    await asyncio.sleep(60)  # даём helius_ws подняться первым

    while True:
        await asyncio.sleep(WALLET_CHECK_EVERY)
        try:
            async with async_session() as s:
                result = await s.execute(select(User))
                users  = result.scalars().all()

            for user in users:
                # Личный кошелёк — НЕ обрабатываем здесь, только в helius_ws
                # Смарт-кошельки — обрабатываем
                await _process_smart_wallets(bot, user)

        except Exception as e:
            log.error(f"wallet_loop: {e}")


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
        try:
            await bot.send_message(
                user.user_id,
                f"🚨 *BUNDLE SIGNAL — {name}* (${symbol})\n\n"
                f"*{len(unique)} wallets* in {BUNDLE_WINDOW}min!\n\n"
                f"📊 Mcap: {mcap}  |  Liq: {liq}\n"
                f"`{mint}`\n\n{lines}\n\n"
                f"[Chart]({dexscreener(mint)})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add Position", callback_data=f"quickadd:{mint}"),
                    InlineKeyboardButton("📈 Chart",        url=dexscreener(mint)),
                ]])
            )
        except Exception as e:
            log.error(f"bundle send: {e}")


def _bought_mint(tx: dict, wallet: str) -> str | None:
    for t in tx.get("tokenTransfers", []):
        mint = t.get("mint", "")
        to   = t.get("toUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in to.lower():
            return mint
    return None


def _sol_spent(tx: dict, wallet: str) -> float:
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("fromUserAccount", "").lower()
    )
    return abs(total) / 1e9
