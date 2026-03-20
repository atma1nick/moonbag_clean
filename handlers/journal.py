from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func
from database import async_session
from models import JournalEntry
from utils import fmt_sol, fmt_pct, fmt_x
from handlers.base import get_user


async def show_journal(msg: Message, uid: int):
    async with async_session() as s:
        result = await s.execute(
            select(JournalEntry)
            .where(JournalEntry.user_id == uid)
            .order_by(JournalEntry.created_at.desc())
            .limit(20)
        )
        entries = result.scalars().all()

    if not entries:
        await msg.reply_text(
            "📓 *Journal is empty*\n\nClose your first position to see stats here.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
            ]])
        )
        return

    # Stats
    total_in   = sum(e.sol_in  for e in entries)
    total_out  = sum(e.sol_out for e in entries)
    total_pnl  = total_out - total_in
    wins       = sum(1 for e in entries if e.pnl_sol >= 0)
    winrate    = (wins / len(entries)) * 100 if entries else 0
    best_x     = max((e.exit_x for e in entries), default=0)

    sign  = "+" if total_pnl >= 0 else ""
    emoji = "🟢" if total_pnl >= 0 else "🔴"

    header = (
        f"📓 *Trade Journal*\n\n"
        f"{emoji} Total PnL: *{sign}{fmt_sol(total_pnl)}*\n"
        f"🎯 Winrate: *{winrate:.0f}%* ({wins}/{len(entries)})\n"
        f"🏆 Best exit: *{fmt_x(best_x)}*\n"
        f"━━━━━━━━━━━━━━\n\n"
    )

    lines = []
    for e in entries[:10]:
        s_emoji = "🟢" if e.pnl_sol >= 0 else "🔴"
        sign_e  = "+" if e.pnl_sol >= 0 else ""
        lines.append(
            f"{s_emoji} *${e.symbol}* {fmt_x(e.exit_x)}  "
            f"{sign_e}{fmt_sol(e.pnl_sol, 3)}  ({sign_e}{fmt_pct(e.pnl_pct)})"
        )

    text = header + "\n".join(lines)
    if len(entries) > 10:
        text += f"\n\n_...and {len(entries)-10} more_"

    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
