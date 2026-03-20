import asyncio
import json
import logging
from sqlalchemy import select
from database import async_session
from models import Position, FiredAlert, User
from services.price import fetch_price, fetch_sol_price
from utils import fmt_mcap, fmt_sol, fmt_x, fmt_pct, calc_pnl, dexscreener
from config import PRICE_CHECK_EVERY
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)


async def _was_fired(key: str) -> bool:
    # FIX: alert_key теперь primary key — s.get работает корректно
    async with async_session() as s:
        return await s.get(FiredAlert, key) is not None


async def _mark_fired(user_id: int, key: str):
    async with async_session() as s:
        try:
            s.add(FiredAlert(alert_key=key, user_id=user_id))
            await s.commit()
        except Exception:
            pass


async def run(bot):
    log.info("price_loop started")
    await asyncio.sleep(10)

    async def _sol_updater():
        while True:
            await fetch_sol_price()
            await asyncio.sleep(300)
    asyncio.create_task(_sol_updater())

    while True:
        await asyncio.sleep(PRICE_CHECK_EVERY)
        try:
            async with async_session() as s:
                result    = await s.execute(select(Position).where(Position.status == "active"))
                positions = result.scalars().all()

            for pos in positions:
                try:
                    await _check_position(bot, pos)
                except Exception as e:
                    log.error(f"price_loop pos {pos.id}: {e}")
        except Exception as e:
            log.error(f"price_loop: {e}")


async def _check_position(bot, pos: Position):
    from services.price import get_cached_sol_price
    data = await fetch_price(pos.contract)
    if not data or not data.get("price") or not pos.entry_price:
        return

    cur_x            = data["price"] / pos.entry_price
    pnl_sol, pnl_pct = calc_pnl(pos.sol_in, cur_x)
    mcap_str         = fmt_mcap(data["mcap"])
    chg_1h           = data.get("price_change_1h", 0)
    sign             = "+" if pnl_sol >= 0 else ""

    # ── Take-profit alerts ────────────────────────────────────────────────
    plan = json.loads(pos.exit_plan) if pos.exit_plan else []
    for level in plan:
        x_target = level.get("x", 0)
        if not x_target or level.get("done") or level.get("skipped"):
            continue
        if cur_x < x_target:
            continue

        key = f"tp:{pos.id}:{x_target}"
        if await _was_fired(key):
            continue
        await _mark_fired(pos.user_id, key)

        pct           = level["pct"]
        sell_sol      = pos.sol_in * pct / 100
        recv_sol      = sell_sol * x_target

        txt = (
            f"🚀 *TAKE-PROFIT — ${pos.symbol}*\n\n"
            f"🎯 Target *{fmt_x(x_target)}* hit!\n"
            f"📊 Now: *{fmt_x(cur_x)}*  |  Mcap: {mcap_str}\n"
            f"⏱ 1h: {'+' if chg_1h>=0 else ''}{chg_1h:.1f}%\n\n"
            f"💰 *Your position:*\n"
            f"  SOL in: {fmt_sol(pos.sol_in)}\n"
            f"  PnL: *{sign}{fmt_sol(pnl_sol)}* ({sign}{pnl_pct:.1f}%)\n\n"
            f"📋 *Suggested:* sell {pct}% → ≈{fmt_sol(recv_sol)}\n\n"
            f"[DexScreener]({dexscreener(pos.contract)})"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Done — sold {pct}%", callback_data=f"done:{pos.id}:{x_target}"),
            InlineKeyboardButton("⏭ Skip",                 callback_data=f"skip:{pos.id}:{x_target}"),
        ]])
        try:
            await bot.send_message(pos.user_id, txt, parse_mode="Markdown",
                                   reply_markup=kb, disable_web_page_preview=True)
            log.info(f"TP alert sent: {pos.symbol} {x_target}x → {pos.user_id}")
        except Exception as e:
            log.error(f"TP send: {e}")

    # ── Stop-loss alert ───────────────────────────────────────────────────
    if pos.stop_loss and pos.stop_loss > 0 and data.get("mcap"):
        if data["mcap"] <= pos.stop_loss:
            key = f"sl:{pos.id}:{int(pos.stop_loss)}"
            if not await _was_fired(key):
                await _mark_fired(pos.user_id, key)
                drop = ((data["mcap"] - pos.entry_mcap) / pos.entry_mcap * 100) if pos.entry_mcap else 0
                txt = (
                    f"🛑 *STOP LOSS — ${pos.symbol}*\n\n"
                    f"Mcap hit {mcap_str} (SL: {fmt_mcap(pos.stop_loss)})\n"
                    f"📉 Drop from entry: {drop:.1f}%\n"
                    f"💸 PnL: *{sign}{fmt_sol(pnl_sol)}* ({sign}{pnl_pct:.1f}%)\n\n"
                    f"[DexScreener]({dexscreener(pos.contract)})"
                )
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔒 Close position", callback_data=f"closepos:{pos.id}"),
                    InlineKeyboardButton("❌ Dismiss",         callback_data=f"skip:{pos.id}:0"),
                ]])
                try:
                    await bot.send_message(pos.user_id, txt, parse_mode="Markdown",
                                           reply_markup=kb, disable_web_page_preview=True)
                    log.info(f"SL alert: {pos.symbol} → {pos.user_id}")
                except Exception as e:
                    log.error(f"SL send: {e}")
