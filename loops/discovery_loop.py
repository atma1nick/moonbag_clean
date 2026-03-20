"""
Discovery loop — заменяет dune_loop.py.
Раз в 24 часа:
  1. Ищет новые smart wallets через GMGN leaderboard
  2. Предлагает добавить с кнопками ➕/Skip
  3. Уведомляет о неактивных кошельках (30+ дней без сигналов)
"""
import asyncio
import logging
from sqlalchemy import select
from database import async_session
from models import User
from services.wallet_discovery import discover_new_wallets, get_inactive_wallets
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)
INTERVAL = 86400  # 24 часа


async def run(bot):
    log.info("discovery_loop started")
    await asyncio.sleep(600)  # первый запуск через 10 минут

    while True:
        try:
            await _run_discovery(bot)
        except Exception as e:
            log.error(f"discovery_loop: {e}")
        await asyncio.sleep(INTERVAL)


async def _run_discovery(bot):
    async with async_session() as s:
        users = (await s.execute(select(User))).scalars().all()

    for user in users:
        uid = user.user_id

        # ── Новые кошельки ────────────────────────────────────────────────
        try:
            new_wallets = await discover_new_wallets(uid)
            if new_wallets:
                lines = ""
                for w in new_wallets:
                    addr = w["address"]
                    lines += (
                        f"\n`{addr[:8]}...{addr[-4:]}`\n"
                        f"   WR: {w['winrate']:.0f}%  "
                        f"PnL: +{w['pnl_sol']:.1f} SOL  "
                        f"{w['trades']} trades\n"
                    )

                buttons = []
                for w in new_wallets:
                    addr  = w["address"]
                    short = f"{addr[:6]}..{addr[-3:]}"
                    buttons.append([
                        InlineKeyboardButton(
                            f"➕ {short} ({w['winrate']:.0f}%WR)",
                            callback_data=f"dune_add:{addr}"
                        ),
                        InlineKeyboardButton("Skip", callback_data=f"dune_skip:{addr}"),
                    ])
                buttons.append([
                    InlineKeyboardButton("Skip all", callback_data="dune_skip:all")
                ])

                await bot.send_message(
                    uid,
                    f"🔍 *New Smart Money Found*\n\n"
                    f"GMGN top traders this week:{lines}\n"
                    f"_Add them to your watchlist?_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                log.info(f"discovery: {len(new_wallets)} new wallets → user {uid}")
        except Exception as e:
            log.error(f"discovery new wallets user {uid}: {e}")

        # ── Неактивные кошельки ───────────────────────────────────────────
        try:
            inactive = await get_inactive_wallets(uid, days=30)
            if inactive:
                lines = "\n".join(
                    f"• {w['label']} — "
                    f"{'never active' if not w['last_active'] else str(w['days_silent']) + 'd silent'}"
                    for w in inactive[:5]
                )
                buttons = [[
                    InlineKeyboardButton("🗑 Clean up",    callback_data="sw:list_remove"),
                    InlineKeyboardButton("◀️ Keep all",    callback_data="disc:keep"),
                ]]
                await bot.send_message(
                    uid,
                    f"💤 *Inactive Smart Wallets*\n\n"
                    f"These wallets haven't made a buy in 30+ days:\n\n"
                    f"{lines}\n\n"
                    f"_Consider removing to reduce noise._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
        except Exception as e:
            log.error(f"discovery inactive user {uid}: {e}")

        await asyncio.sleep(2)