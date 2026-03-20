import asyncio
import logging
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)
from config import BOT_TOKEN
from database import init_db

from handlers.start        import cmd_start, lang_cb, menu_cb, cmd_menu
from handlers.positions    import (
    start_add_position,
    add_got_contract, add_got_entry, add_got_sol, add_got_plan, add_got_note,
    editplan_cb, editplan_got_text,
    setsl_cb, setsl_got_text,
    closepos_cb, close_got_pct,
    alert_done_cb, alert_skip_cb,
    cmd_cancel,
    ST_CONTRACT, ST_ENTRY, ST_SOL, ST_PLAN, ST_NOTE,
    ST_EDIT_PLAN, ST_SET_SL, ST_CLOSE_PCT,
)
from handlers.smartwallets import sw_cb, sw_add_start, sw_got_address, sw_cancel, ST_SW_ADD, cmd_wallets
from handlers.kols         import kol_cb, kol_add_start, kol_got_handle, kol_cancel, ST_KOL_ADD
from handlers.settings     import (settings_cb, show_settings,
                                   wallet_start, wallet_got_address,
                                   whale_start, whale_got_value,
                                   settings_cancel, ST_WALLET, ST_WHALE)
from handlers.admin        import cmd_admin, cmd_grant_pro, cmd_broadcast
from handlers.autoplan     import (cmd_autoplan, autoplan_cb,
                                   autoplan_got_text, ST_AUTOPLAN)
from handlers.snapshot     import cmd_snapshot, snapshot_cb

import loops.price_loop  as price_loop
import loops.wallet_loop as wallet_loop
import loops.helius_ws   as helius_ws
import loops.discovery_loop as discovery_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s"
)
log = logging.getLogger(__name__)


# ── keepplan ──────────────────────────────────────────────────────────────────

async def keepplan_cb(update, ctx):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    q = update.callback_query
    await q.answer("✅ Plan confirmed!")
    try:
        await q.edit_message_text(
            q.message.text + "\n\n✅ _Tracking with default plan._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Positions", callback_data="do:pos"),
                InlineKeyboardButton("◀️ Menu",      callback_data="do:menu"),
            ]])
        )
    except Exception:
        pass


# ── quickadd из bundle alert ──────────────────────────────────────────────────

async def quickadd_cb(update, ctx):
    q    = update.callback_query
    await q.answer()
    mint = q.data.split(":")[1]
    from services.price import fetch_price
    from utils import fmt_mcap
    data = await fetch_price(mint)
    if data:
        ctx.user_data.update({
            "add_ca":     mint,
            "add_name":   data["name"],
            "add_symbol": data["symbol"],
            "add_price":  data["price"],
            "add_mcap":   data["mcap"],
            "add_cur_price": data["price"],
            "add_cur_mcap":  data["mcap"],
        })
    await q.message.reply_text(
        f"➕ *Quick Add — ${data['symbol'] if data else mint[:8]}*\n\n"
        f"Entry: `now` (current price)\n\n"
        f"How much *SOL* did you put in?\n\n"
        f"_Example:_ `1.5`\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    # Ставим entry = current автоматически
    if data:
        ctx.user_data["add_price"] = data["price"]
        ctx.user_data["add_mcap"]  = data["mcap"]
    return ST_SOL


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update, ctx):
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    await update.message.reply_text(
        "🌙 *MoonBag Bot — Help*\n\n"
        "*Main commands:*\n"
        "  /start — main menu\n"
        "  /menu — main menu\n"
        "  /cancel — cancel current action\n\n"
        "*Adding a position:*\n"
        "  Tap ➕ → enter CA → set entry → set SOL → set exit plan\n\n"
        "*Alerts:*\n"
        "  Bot checks prices every 60s.\n"
        "  When take-profit hits → tap Done (logs sale) or Skip.\n\n"
        "*Smart Wallets:*\n"
        "  Add whale wallets → instant alert when they buy.\n"
        "  3+ wallets buy same token in 1h = 🚨 Bundle Signal.\n\n"
        "*Settings:*\n"
        "  Connect wallet → turn on Auto mode.\n"
        "  Currency toggle → show PnL in SOL or USD.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )



# ── pos_detail — полная карточка позиции ─────────────────────────────────────

async def pos_detail_cb(update, ctx):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])
    uid    = q.from_user.id
    from handlers.positions import _pos_card
    from handlers.base import get_user
    from models import Position
    from database import async_session
    user = await get_user(uid)
    currency = user.currency if user else "SOL"
    async with async_session() as s:
        pos = await s.get(Position, pos_id)
    if not pos or pos.user_id != uid:
        await q.answer("Position not found", show_alert=True)
        return
    card = await _pos_card(pos, currency)
    await q.message.reply_text(**card)


# ── Dune discovery callbacks ──────────────────────────────────────────────────

async def dune_add_cb(update, ctx):
    """Юзер нажал ➕ добавить кошелёк из Dune discovery."""
    q       = update.callback_query
    await q.answer()
    address = q.data.split(":")[1]
    uid     = q.from_user.id

    from models import SmartWallet
    async with __import__("database").async_session() as s:
        try:
            s.add(SmartWallet(user_id=uid, address=address, label="🔍 Dune"))
            await s.commit()
            await q.answer(f"✅ Added: {address[:8]}...", show_alert=True)
        except Exception:
            await q.answer("Already in your list", show_alert=True)

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def dune_skip_cb(update, ctx):
    q = update.callback_query
    await q.answer("Skipped")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def disc_keep_cb(update, ctx):
    q = update.callback_query
    await q.answer("Keeping all wallets")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ── Universal text router ─────────────────────────────────────────────────────

async def universal_text(update, ctx):
    pass  # all inputs handled by ConversationHandlers


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Add position (5 steps: contract → entry → sol → plan → note)
    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_add_position, pattern="^do:add$"),
        ],
        states={
            ST_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_contract)],
            ST_ENTRY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_entry)],
            ST_SOL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_sol)],
            ST_PLAN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_plan)],
            ST_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_note)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    # quickadd из bundle alert (начинается с ST_SOL)
    quickadd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(quickadd_cb, pattern=r"^quickadd:")],
        states={
            ST_SOL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_sol)],
            ST_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_plan)],
            ST_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_note)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    edit_plan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(editplan_cb, pattern=r"^editplan:\d+$")],
        states={ST_EDIT_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, editplan_got_text)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    set_sl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(setsl_cb, pattern=r"^setsl:\d+$")],
        states={ST_SET_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setsl_got_text)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    close_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(closepos_cb, pattern=r"^closepos:\d+$")],
        states={ST_CLOSE_PCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_got_pct)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    sw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sw_add_start, pattern=r"^sw:add$")],
        states={ST_SW_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, sw_got_address)]},
        fallbacks=[CommandHandler("cancel", sw_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    kol_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(kol_add_start, pattern=r"^kol:add$")],
        states={ST_KOL_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, kol_got_handle)]},
        fallbacks=[CommandHandler("cancel", kol_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    wallet_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(wallet_start, pattern=r"^cfg:wallet$")],
        states={ST_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_got_address)]},
        fallbacks=[CommandHandler("cancel", settings_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    whale_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(whale_start, pattern=r"^cfg:whale$")],
        states={ST_WHALE: [MessageHandler(filters.TEXT & ~filters.COMMAND, whale_got_value)]},
        fallbacks=[CommandHandler("cancel", settings_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    autoplan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(autoplan_cb, pattern=r"^ap:")],
        states={ST_AUTOPLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, autoplan_got_text)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=300,
    )

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("autoplan",  cmd_autoplan))
    app.add_handler(CommandHandler("snapshot",  cmd_snapshot))
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("grant_pro", cmd_grant_pro))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Conversations — BEFORE generic callbacks
    app.add_handler(wallet_conv)
    app.add_handler(whale_conv)
    app.add_handler(sw_conv)
    app.add_handler(kol_conv)
    app.add_handler(autoplan_conv)
    app.add_handler(add_conv)
    app.add_handler(quickadd_conv)
    app.add_handler(edit_plan_conv)
    app.add_handler(set_sl_conv)
    app.add_handler(close_conv)

    # Callbacks
    app.add_handler(CallbackQueryHandler(lang_cb,       pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(menu_cb,       pattern=r"^do:"))
    app.add_handler(CallbackQueryHandler(keepplan_cb,   pattern=r"^keepplan:"))
    app.add_handler(CallbackQueryHandler(alert_done_cb, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(alert_skip_cb, pattern=r"^skip:"))
    app.add_handler(CallbackQueryHandler(sw_cb,         pattern=r"^sw:"))
    app.add_handler(CallbackQueryHandler(kol_cb,        pattern=r"^kol:"))
    app.add_handler(CallbackQueryHandler(settings_cb,   pattern=r"^cfg:"))
    app.add_handler(CallbackQueryHandler(snapshot_cb,   pattern=r"^snapshot:"))
    app.add_handler(CallbackQueryHandler(pos_detail_cb, pattern=r"^pos_detail:"))
    app.add_handler(CallbackQueryHandler(dune_add_cb,   pattern=r"^dune_add:"))
    app.add_handler(CallbackQueryHandler(dune_skip_cb,  pattern=r"^dune_skip:"))
    app.add_handler(CallbackQueryHandler(disc_keep_cb,  pattern=r"^disc:"))

    # Free text fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, universal_text))

    async def on_startup(app):
        await init_db()
        log.info("✅ DB initialized")
        asyncio.create_task(price_loop.run(app.bot))
        asyncio.create_task(wallet_loop.run(app.bot))
        asyncio.create_task(helius_ws.run(app.bot))
        asyncio.create_task(discovery_loop.run(app.bot))
        log.info("✅ Loops started")

    app.post_init = on_startup
    log.info("🌙 MoonBag Bot v7 starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()