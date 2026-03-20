from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from database import async_session
from models import JournalEntry
from utils import fmt_sol, fmt_pct, fmt_x


async def show_journal(msg: Message, uid: int):
    async with async_session() as s:
        result = await s.execute(
            select(JournalEntry)
            .where(JournalEntry.user_id == uid)
            .order_by(JournalEntry.created_at.desc())
            .limit(40)
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

    # Разделяем manual и auto
    manual  = [e for e in entries if "Auto" not in (e.note or "") and "auto" not in (e.note or "")]
    auto    = [e for e in entries if "Auto" in (e.note or "") or "auto" in (e.note or "")]

    def stats_block(title: str, ents: list) -> str:
        if not ents:
            return ""
        total_in   = sum(e.sol_in  for e in ents)
        total_out  = sum(e.sol_out for e in ents)
        total_pnl  = total_out - total_in
        wins       = sum(1 for e in ents if e.pnl_sol >= 0)
        winrate    = (wins / len(ents) * 100) if ents else 0
        best_x     = max((e.exit_x for e in ents), default=0)
        sign       = "+" if total_pnl >= 0 else ""
        emoji      = "🟢" if total_pnl >= 0 else "🔴"

        lines = [f"*{title}* ({len(ents)} trades)"]
        lines.append(
            f"{emoji} PnL: *{sign}{fmt_sol(total_pnl, 3)}*  "
            f"WR: *{winrate:.0f}%*  Best: *{fmt_x(best_x)}*"
        )
        lines.append("━━━━━━━━")

        for e in ents[:8]:
            s_emoji = "🟢" if e.pnl_sol >= 0 else "🔴"
            sign_e  = "+" if e.pnl_sol >= 0 else ""
            note    = f" _{e.note}_" if e.note else ""
            lines.append(
                f"{s_emoji} *${e.symbol}* {fmt_x(e.exit_x)}  "
                f"{sign_e}{fmt_sol(e.pnl_sol, 3)}  "
                f"({sign_e}{fmt_pct(e.pnl_pct)})"
                f"{note}"
            )

        if len(ents) > 8:
            lines.append(f"_...and {len(ents)-8} more_")

        return "\n".join(lines)

    manual_block = stats_block("✋ Manual trades", manual)
    auto_block   = stats_block("🤖 Auto-tracked trades", auto)

    sections = [s for s in [manual_block, auto_block] if s]
    text = "📓 *Trade Journal*\n\n" + "\n\n".join(sections)

    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Snapshot", callback_data="snapshot:refresh"),
             InlineKeyboardButton("◀️ Menu",     callback_data="do:menu")],
        ])
    )