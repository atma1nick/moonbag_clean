"""
Wallet loop — личный кошелёк + смарт-кошельки.
REST polling через Helius Enhanced Transactions API.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
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
MAX_TX_AGE_SEC   = 300  # игнорируем транзакции старше 5 минут (защита от дублей при рестарте)

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


def _classify_tx(tx: dict, wallet: str) -> tuple[str | None, str | None, float]:
    """
    Определяет тип транзакции относительно кошелька.
    Возвращает: ("buy"|"sell"|None, mint, sol_amount)

    Использует events.swap как приоритетный источник —
    он надёжнее tokenTransfers для сложных маршрутов (gmgn, photon, jupiter).
    """
    wallet_l = wallet.lower()

    # ── Способ 1: events.swap (gmgn/photon/jupiter всегда заполняют) ─────────
    swap = tx.get("events", {}).get("swap", {})
    if swap:
        native_in  = swap.get("nativeInput",  {}) or {}
        native_out = swap.get("nativeOutput", {}) or {}
        tok_inputs  = swap.get("tokenInputs",  []) or []
        tok_outputs = swap.get("tokenOutputs", []) or []

        # Кошелёк отдал токен, получил SOL → ПРОДАЖА
        for inp in tok_inputs:
            mint = inp.get("mint", "")
            if not mint or mint == SOL_MINT:
                continue
            frm = inp.get("userAccount", inp.get("fromUserAccount", "")).lower()
            if wallet_l in frm or not frm:
                sol_recv = float(native_out.get("amount", 0) or 0) / 1e9
                return ("sell", mint, sol_recv)

        # Кошелёк отдал SOL, получил токен → ПОКУПКА
        for out in tok_outputs:
            mint = out.get("mint", "")
            if not mint or mint == SOL_MINT:
                continue
            to = out.get("userAccount", out.get("toUserAccount", "")).lower()
            if wallet_l in to or not to:
                sol_spent = float(native_in.get("amount", 0) or 0) / 1e9
                return ("buy", mint, sol_spent)

    # ── Способ 2: tokenTransfers (fallback) ───────────────────────────────────
    bought_mint = None
    sold_mint   = None

    for t in tx.get("tokenTransfers", []):
        mint    = t.get("mint", "")
        to_acc  = t.get("toUserAccount",   "").lower()
        frm_acc = t.get("fromUserAccount", "").lower()
        if not mint or mint == SOL_MINT:
            continue
        if wallet_l in to_acc:
            bought_mint = mint
        if wallet_l in frm_acc:
            sold_mint = mint

    sol_out = sum(
        t.get("amount", 0) for t in tx.get("nativeTransfers", [])
        if wallet_l in t.get("fromUserAccount", "").lower()
    ) / 1e9

    sol_in = sum(
        t.get("amount", 0) for t in tx.get("nativeTransfers", [])
        if wallet_l in t.get("toUserAccount", "").lower()
    ) / 1e9

    if bought_mint and not sold_mint:
        return ("buy",  bought_mint, sol_out)
    if sold_mint and not bought_mint:
        return ("sell", sold_mint,   sol_in)

    return (None, None, 0.0)


def _is_fresh(tx: dict) -> bool:
    """Транзакция не старше MAX_TX_AGE_SEC секунд."""
    ts = tx.get("timestamp", 0)
    if not ts:
        return True  # нет timestamp → пропускаем фильтр
    age = datetime.now(timezone.utc).timestamp() - ts
    return age <= MAX_TX_AGE_SEC


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(bot):
    log.info("wallet_loop started")
    await asyncio.sleep(30)

    while True:
        await asyncio.sleep(WALLET_CHECK_EVERY)
        try:
            async with async_session() as s:
                users = (await s.execute(select(User))).scalars().all()

            for user in users:
                if user.mode == "wallet" and user.wallet:
                    await _process_personal_wallet(bot, user)
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
        if not sig:
            continue

        # Старые транзакции — помечаем и пропускаем без алерта
        if not _is_fresh(tx):
            if not await _tx_seen(sig):
                await _mark_seen(sig)
            continue

        if await _tx_seen(sig):
            continue
        await _mark_seen(sig)

        action, mint, sol_amount = _classify_tx(tx, user.wallet)

        if not mint or not action:
            continue

        data = await fetch_price(mint)
        if not data:
            continue

        log.info(f"wallet tx: {action} {data.get('symbol','?')} {sol_amount:.3f} SOL → user {user.user_id}")

        if action == "buy":
            await _handle_buy(bot, user, mint, data, sol_amount, currency, sol_price)
        elif action == "sell":
            await _handle_sell(bot, user, mint, data, sol_amount, currency, sol_price)

        await asyncio.sleep(0.3)


async def _handle_buy(bot, user, mint, data, sol_amount, currency, sol_price):
    """Покупка — создаём позицию или фиксируем DCA."""

    # Проверяем открытую позицию
    async with async_session() as s:
        pos = (await s.execute(
            select(Position).where(
                Position.user_id  == user.user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    usd_str = f" (≈${sol_amount * sol_price:.0f})" if sol_price and sol_amount else ""
    sol_str = fmt_sol(sol_amount) if sol_amount else "?"

    if pos:
        # DCA — обновляем sol_in позиции
        async with async_session() as s:
            p = await s.get(Position, pos.id)
            if p and sol_amount > 0:
                old_in    = p.sol_in
                old_price = p.entry_price
                # Средняя цена входа
                if old_price and data["price"]:
                    total_sol  = old_in + sol_amount
                    avg_price  = (old_in * old_price + sol_amount * data["price"]) / total_sol
                    p.entry_price = avg_price
                p.sol_in = (p.sol_in or 0) + sol_amount
                await s.commit()

        try:
            await bot.send_message(
                user.user_id,
                f"➕ *DCA — ${data['symbol']}*\n\n"
                f"Added to existing position.\n"
                f"Amount: *{sol_str}*{usd_str}\n"
                f"New avg entry: {fmt_mcap(data['mcap'])}\n"
                f"_Position updated._",
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
        p = Position(
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
        s.add(p)
        await s.commit()
        await s.refresh(p)
        pos_id = p.id

    # Превью тейков
    plan_lines = ""
    for l in plan:
        x   = l.get("x", 0)
        pct = l.get("pct", 0)
        if x and sol_amount:
            val   = sol_amount * (pct / 100) * x
            val_s = (f"${val * sol_price:.0f}" if currency == "USD" and sol_price
                     else fmt_sol(val, 2))
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
            f"📋 *Exit plan:*\n{plan_lines}\n"
            f"[DexScreener]({dexscreener(mint)})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✎ Edit Plan",  callback_data=f"editplan:{pos_id}"),
                InlineKeyboardButton("✅ Keep Plan", callback_data=f"keepplan:{pos_id}"),
            ]])
        )
        log.info(f"buy alert sent: {data['symbol']} → {user.user_id}")
    except Exception as e:
        log.error(f"buy alert: {e}")


async def _handle_sell(bot, user, mint, data, sol_received, currency, sol_price):
    """Продажа — закрываем позицию и уведомляем."""

    async with async_session() as s:
        pos = (await s.execute(
            select(Position).where(
                Position.user_id  == user.user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    if not pos:
        # Продали токен которого нет в боте — просто информируем
        try:
            await bot.send_message(
                user.user_id,
                f"💸 *SELL — ${data['symbol']}*\n\n"
                f"Received: *{fmt_sol(sol_received)}*\n"
                f"Mcap: {fmt_mcap(data['mcap'])}\n\n"
                f"_Not tracked in MoonBag._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # Считаем PnL
    cur_x   = data["price"] / pos.entry_price if pos.entry_price else 1.0
    pnl_sol = (sol_received - pos.sol_in) if sol_received else 0.0
    sign    = "+" if pnl_sol >= 0 else ""
    emoji   = "🟢" if pnl_sol >= 0 else "🔴"
    usd_str = f" (≈${sol_received * sol_price:.0f})" if sol_price and sol_received else ""

    # Автоматически закрываем позицию
    async with async_session() as s:
        p = await s.get(Position, pos.id)
        if p:
            p.status    = "closed"
            p.sol_out   = sol_received or 0
            p.exit_price= data["price"]
            p.closed_at = datetime.utcnow()
            await s.commit()

    # Запись в журнал
    from models import JournalEntry
    async with async_session() as s:
        s.add(JournalEntry(
            user_id     = user.user_id,
            position_id = pos.id,
            contract    = mint,
            symbol      = data["symbol"],
            sol_in      = pos.sol_in,
            sol_out     = sol_received or 0,
            pnl_sol     = pnl_sol,
            pnl_pct     = (cur_x - 1) * 100,
            exit_x      = cur_x,
            note        = "Auto-detected sell",
        ))
        await s.commit()

    try:
        await bot.send_message(
            user.user_id,
            f"{emoji} *SELL DETECTED — ${data['symbol']}*\n\n"
            f"Received: *{fmt_sol(sol_received)}*{usd_str}\n"
            f"Exit: *{fmt_x(cur_x)}*\n"
            f"PnL: *{sign}{fmt_sol(pnl_sol)}*\n\n"
            f"_Position closed and logged to journal._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📓 Journal", callback_data="do:journal"),
                InlineKeyboardButton("◀️ Menu",    callback_data="do:menu"),
            ]])
        )
        log.info(f"sell alert sent: {data['symbol']} {fmt_x(cur_x)} → {user.user_id}")
    except Exception as e:
        log.error(f"sell alert: {e}")


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
            if not sig:
                continue
            if not _is_fresh(tx):
                if not await _tx_seen(sig):
                    await _mark_seen(sig)
                continue
            if await _tx_seen(sig):
                continue
            await _mark_seen(sig)

            action, mint, sol = _classify_tx(tx, sw.address)
            if action != "buy" or not mint:
                continue

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
                f"*{len(unique)} wallets* in {BUNDLE_WINDOW}min!\n\n"
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
        except Exception as e:
            log.error(f"bundle: {e}")