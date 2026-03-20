"""
Wallet loop — polls Helius every WALLET_CHECK_EVERY seconds.
When user's wallet makes a SWAP → auto-create position.
When smart wallet buys → alert + bundle check.
"""
import asyncio
import json
import logging
from datetime import datetime
from sqlalchemy import select
from database import async_session
from models import User, Position, SmartWallet, SmartWalletTx, SeenTx, FiredAlert
from services.helius import fetch_wallet_txs
from services.price import fetch_price
from utils import fmt_mcap, fmt_sol, fmt_x, dexscreener, solscan_token
from config import WALLET_CHECK_EVERY
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_PLAN = [
    {"x": 4,  "pct": 50, "label": "4x"},
    {"x": 8,  "pct": 30, "label": "8x"},
    {"x": 0,  "pct": 20, "label": "moon"},
]

BUNDLE_THRESHOLD = 3    # сколько смарт-кошельков = сигнал
BUNDLE_WINDOW    = 60   # минут


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
                # Личный кошелёк — авто-трекинг
                if user.mode == "wallet" and user.wallet:
                    await _process_personal_wallet(bot, user)

                # Смарт-кошельки — алерты + bundle
                await _process_smart_wallets(bot, user)

        except Exception as e:
            log.error(f"wallet_loop: {e}")


async def _process_personal_wallet(bot, user: User):
    txs = await fetch_wallet_txs(user.wallet)
    for tx in txs[:5]:
        sig = tx.get("signature", "")
        if not sig or await _tx_seen(sig):
            continue
        await _mark_seen(sig)

        # Найти купленный токен
        mint = _extract_bought_mint(tx, user.wallet)
        if not mint:
            continue

        # Уже есть активная позиция?
        async with async_session() as s:
            existing = await s.execute(
                select(Position).where(
                    Position.user_id  == user.user_id,
                    Position.contract == mint,
                    Position.status   == "active"
                )
            )
            if existing.first():
                continue

        data = await fetch_price(mint)
        if not data:
            continue

        sol_spent = _extract_sol_spent(tx, user.wallet)
        if sol_spent < 0.01:
            sol_spent = 0.5

        async with async_session() as s:
            pos = Position(
                user_id     = user.user_id,
                contract    = mint,
                symbol      = data["symbol"],
                name        = data["name"],
                entry_price = data["price"],
                entry_mcap  = data["mcap"],
                sol_in      = sol_spent,
                exit_plan   = json.dumps(DEFAULT_PLAN),
                source      = "wallet",
                status      = "active",
            )
            s.add(pos)
            await s.commit()
            await s.refresh(pos)
            pos_id = pos.id

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✎ Edit Plan",   callback_data=f"editplan:{pos_id}"),
            InlineKeyboardButton("✅ Keep Plan",  callback_data=f"keepplan:{pos_id}"),
        ]])

        await bot.send_message(
            user.user_id,
            f"🤖 *AUTO-TRACKED — ${data['symbol']}*\n\n"
            f"*{data['name']}*\n"
            f"📊 Mcap: {fmt_mcap(data['mcap'])}\n"
            f"💰 ~{fmt_sol(sol_spent)} in\n\n"
            f"📋 *Default plan set:*\n"
            f"  • 4x → sell 50%\n  • 8x → sell 30%\n  • rest → 🌙\n\n"
            f"Edit or confirm?",
            parse_mode="Markdown",
            reply_markup=kb
        )
        log.info(f"auto-track: {data['symbol']} → user {user.user_id}")
        await asyncio.sleep(0.5)


async def _process_smart_wallets(bot, user: User):
    from datetime import timedelta
    async with async_session() as s:
        result  = await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user.user_id)
        )
        wallets = result.scalars().all()

    if not wallets:
        return

    new_buys: dict[str, list] = {}  # contract → [wallet_info]

    for sw in wallets:
        txs = await fetch_wallet_txs(sw.address, limit=5)
        for tx in txs:
            sig = tx.get("signature", "")
            if not sig or await _tx_seen(sig):
                continue
            await _mark_seen(sig)

            mint = _extract_bought_mint(tx, sw.address)
            if not mint:
                continue

            sol = _extract_sol_spent(tx, sw.address)

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

            new_buys.setdefault(mint, []).append({
                "label": sw.label or sw.address[:8] + "...",
                "sol":   sol,
                "sig":   sig,
            })

        await asyncio.sleep(0.3)

    # Bundle check
    for mint, buyers in new_buys.items():
        # Считаем уникальных покупателей за последний час
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

        data = await fetch_price(mint)
        mcap = fmt_mcap(data["mcap"]) if data else "?"
        name = data["name"] if data else mint[:8] + "..."
        symbol = data["symbol"] if data else "???"

        wallet_lines = "\n".join(
            f"  🧠 {u.label or u.address[:8]+'...'} — {u.sol_amount:.2f} SOL"
            for u in unique[:5]
        )

        await bot.send_message(
            user.user_id,
            f"🚨 *BUNDLE SIGNAL — {name}* (${symbol})\n\n"
            f"*{len(unique)} smart wallets* bought in the last hour!\n\n"
            f"📊 Mcap: {mcap}\n"
            f"`{mint}`\n\n"
            f"*Who bought:*\n{wallet_lines}\n\n"
            f"[DexScreener]({dexscreener(mint)}) · [Solscan]({solscan_token(mint)})\n\n"
            f"⚡ _Strong signal — check before entry!_",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add Position", callback_data=f"quickadd:{mint}"),
                InlineKeyboardButton("📈 Chart",        url=dexscreener(mint)),
            ]])
        )
        log.info(f"bundle alert: {symbol} → user {user.user_id}")


# ── TX parsing helpers ────────────────────────────────────────────────────────

def _extract_bought_mint(tx: dict, wallet: str) -> str | None:
    for transfer in tx.get("tokenTransfers", []):
        mint    = transfer.get("mint", "")
        to_addr = transfer.get("toUserAccount", "")
        if mint and mint != SOL_MINT and wallet.lower() in to_addr.lower():
            return mint
    return None


def _extract_sol_spent(tx: dict, wallet: str) -> float:
    total = sum(
        t.get("amount", 0)
        for t in tx.get("nativeTransfers", [])
        if wallet.lower() in t.get("fromUserAccount", "").lower()
    )
    return abs(total) / 1e9


async def _was_alerted(key: str) -> bool:
    async with async_session() as s:
        return await s.get(FiredAlert, key) is not None


async def _mark_alerted(uid: int, key: str):
    async with async_session() as s:
        try:
            s.add(FiredAlert(user_id=uid, alert_key=key))
            await s.commit()
        except Exception:
            pass
