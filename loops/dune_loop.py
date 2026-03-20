"""
Dune loop — раз в 24 часа ищет новые smart money кошельки.
Предлагает юзеру добавить их с кнопками ➕ Add / Skip.
"""
import asyncio
import logging
from sqlalchemy import select
from database import async_session
from models import User, SmartWallet
from services.dune import get_top_traders
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger(__name__)
INTERVAL = 86400  # 24 часа


async def run(bot):
    log.info("dune_loop started")
    await asyncio.sleep(300)  # первый запуск через 5 минут после старта

    while True:
        try:
            await _discover_wallets(bot)
        except Exception as e:
            log.error(f"dune_loop: {e}")
        await asyncio.sleep(INTERVAL)


async def _discover_wallets(bot):
    top = await get_top_traders(days=7, min_trades=10, min_winrate=60.0)
    if not top:
        log.info("dune_loop: no results from Dune (key missing or query failed)")
        return

    async with async_session() as s:
        result = await s.execute(select(User))
        users  = result.scalars().all()

    for user in users:
        # Собираем уже добавленные адреса
        async with async_session() as s:
            existing = (await s.execute(
                select(SmartWallet).where(SmartWallet.user_id == user.user_id)
            )).scalars().all()
        known = {w.address for w in existing}

        # Новые кошельки которых ещё нет
        new_wallets = [w for w in top if w["address"] and w["address"] not in known][:3]
        if not new_wallets:
            continue

        lines = ""
        for w in new_wallets:
            addr = w["address"]
            lines += (
                f"\n🧠 `{addr[:8]}...{addr[-4:]}`\n"
                f"   WR: {w['winrate']}%  |  PnL: +{w['pnl_sol']:.1f} SOL  "
                f"|  {w['trades']} trades\n"
            )

        # Кнопки для каждого кошелька
        buttons = []
        for w in new_wallets:
            addr  = w["address"]
            label = f"{addr[:6]}... ({w['winrate']}% WR)"
            buttons.append([
                InlineKeyboardButton(f"➕ {label}", callback_data=f"dune_add:{addr}"),
                InlineKeyboardButton("Skip",        callback_data=f"dune_skip:{addr}"),
            ])
        buttons.append([InlineKeyboardButton("Skip all", callback_data="dune_skip:all")])

        try:
            await bot.send_message(
                user.user_id,
                f"🔍 *New Smart Money Found*\n\n"
                f"Dune Analytics found {len(new_wallets)} profitable wallets "
                f"from the last 7 days:\n{lines}\n"
                f"_Add them to get alerts on their buys?_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            log.info(f"dune discovery: {len(new_wallets)} wallets → user {user.user_id}")
        except Exception as e:
            log.error(f"dune discovery send: {e}")