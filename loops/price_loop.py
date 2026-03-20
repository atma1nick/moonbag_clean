"""
Price loop — checks every PRICE_CHECK_EVERY seconds.
Fires take-profit alerts and stop-loss alerts.
Each alert includes: PnL, mcap, 1h change, links.
"""
import asyncio
import json
import logging
from sqlalchemy import select
from database import async_session
from models import Position, FiredAlert, User
from services.price import fetch_price, fetch_sol_price
from utils import fmt_mcap, fmt_sol, fmt_x, fmt_pct, calc_pnl, dexscreener, solscan_token
from config import PRICE_CHECK_EVERY
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)


async def _alert_key(pos_id: int, tag: str) -> str:
    return f"{pos_id}:{tag}"


async def _was_fired(key: str) -> bool:
    async with async_session() as s:
        return await s.get(FiredAlert, key) is not None


async def _mark_fired(user_id: int, key: str):
    async with async_session() as s:
        try:
            s.add(FiredAlert(user_id=user_id, alert_key=key))
            await s.commit()
        except Exception:
            pass


async def run(bot):
    log.info("price_loop started")
    await asyncio.sleep(10)

    # Update SOL price every 5 minutes in the background
    async def sol_price_updater():
        while True:
            await fetch_sol_price()
            await asyncio.sleep(300)
    asyncio.create_task(sol_price_updater())

    while True:
        await asyncio.sleep(PRICE_CHECK_EVERY)
        try:
            async with async_session() as s:
                result = await s.execute(
                    select(Position).where(Position.status == "active")
                )
                positions = result.scalars().all()

            for pos in positions:
                try:
                    await _check_position(bot, pos)
                except Exception as e:
                    log.error(f"price_loop pos {pos.id}: {e}")

        except Exception as e:
            log.error(f"price_loop outer: {e}")


async def _check_position(bot, pos: Position):
    data = await fetch_price(pos.contract)
    if not data or not data.get("price") or not pos.entry_price:
        return

    cur_x        = data["price"] / pos.entry_price
    pnl_sol, pnl_pct = calc_pnl(pos.sol_in, cur_x)
    mcap_str     = fmt_mcap(data["mcap"])
    chg_1h       = data.get("price_change_1h", 0)
    sign_pnl     = "+" if pnl_sol >= 0 else ""

    user = await _get_user(pos.user_id)
    currency = user.currency if user else "SOL"

    # ── Take-profit alerts ────────────────────────────────────────────────────
    plan = json.loads(pos.exit_plan) if pos.exit_plan else []

    for level in plan:
        x_target = level.get("x", 0)
        if not x_target or level.get("done") or level.get("skipped"):
            continue

        if cur_x < x_target:
            continue

        key = await _alert_key(pos.id, f"tp:{x_target}")
        if await _was_fired(key):
            continue

        await _mark_fired(pos.user_id, key)

        pnl_at_target = pos.sol_in * (x_target - 1)
        pct           = level["pct"]

        txt = (
            f"🚀 *TAKE-PROFIT HIT — ${pos.symbol}*\n\n"
            f"🎯 Target: *{fmt_x(x_target)}* — triggered!\n"
            f"📊 Current: *{fmt_x(cur_x)}*  |  Mcap: {mcap_str}\n"
            f"⏱ 1h change: {'+' if chg_1h>=0 else ''}{chg_1h:.1f}%\n\n"
            f"💰 *Your position:*\n"
            f"  SOL in: {fmt_sol(pos.sol_in)}\n"
            f"  PnL now: *{sign_pnl}{fmt_sol(pnl_sol)}* ({sign_pnl}{pnl_pct:.1f}%)\n"
            f"  Suggested sell: {pct}% → ≈{fmt_sol(pos.sol_in * pct/100 * cur_x)}\n\n"
            f"[DexScreener]({dexscreener(pos.contract)}) · "
            f"[Solscan]({solscan_token(pos.contract)})"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Done — sold {pct}%", callback_data=f"done:{pos.id}:{x_target}"),
            InlineKeyboardButton("⏭ Skip",                 callback_data=f"skip:{pos.id}:{x_target}"),
        ]])

        try:
            await bot.send_message(
                pos.user_id, txt,
                parse_mode="Markdown",
                reply_markup=kb,
                disable_web_page_preview=True
            )
            log.info(f"TP alert: {pos.symbol} {x_target}x → user {pos.user_id}")
        except Exception as e:
            log.error(f"TP alert send: {e}")

    # ── Stop-loss alert ───────────────────────────────────────────────────────
    if pos.stop_loss and pos.stop_loss > 0 and data.get("mcap"):
        if data["mcap"] <= pos.stop_loss:
            key = await _alert_key(pos.id, f"sl:{int(pos.stop_loss)}")
            if not await _was_fired(key):
                await _mark_fired(pos.user_id, key)

                drop_pct = ((data["mcap"] - pos.entry_mcap) / pos.entry_mcap * 100) if pos.entry_mcap else 0

                txt = (
                    f"🛑 *STOP LOSS — ${pos.symbol}*\n\n"
                    f"Mcap dropped to {mcap_str} (below your SL: {fmt_mcap(pos.stop_loss)})\n\n"
                    f"📉 Drop from entry: {drop_pct:.1f}%\n"
                    f"💸 Current PnL: *{sign_pnl}{fmt_sol(pnl_sol)}* ({sign_pnl}{pnl_pct:.1f}%)\n\n"
                    f"[DexScreener]({dexscreener(pos.contract)}) · "
                    f"[Solscan]({solscan_token(pos.contract)})"
                )

                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔒 Close position",   callback_data=f"closepos:{pos.id}"),
                    InlineKeyboardButton("❌ Dismiss",           callback_data=f"skip:{pos.id}:0"),
                ]])

                try:
                    await bot.send_message(
                        pos.user_id, txt,
                        parse_mode="Markdown",
                        reply_markup=kb,
                        disable_web_page_preview=True
                    )
                    log.info(f"SL alert: {pos.symbol} → user {pos.user_id}")
                except Exception as e:
                    log.error(f"SL alert send: {e}")


async def _get_user(uid: int):
    async with async_session() as s:
        return await s.get(User, uid)
