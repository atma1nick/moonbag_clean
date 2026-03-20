"""
MoonBag Bot v7 — clean rewrite
Entry point: polling + loops
"""
import asyncio
import logging
import json
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)
from config import BOT_TOKEN
from database import init_db

# Handlers
from handlers.start       import cmd_start, lang_cb, menu_cb, cmd_menu
from handlers.positions   import (
    show_positions, start_add_position,
    add_got_contract, add_got_sol, add_got_plan, add_got_note,
    editplan_cb, editplan_got_text,
    setsl_cb, setsl_got_text,
    closepos_cb, close_got_price,
    alert_done_cb, alert_skip_cb,
    cmd_cancel,
    ST_CONTRACT, ST_SOL, ST_PLAN, ST_NOTE,
    ST_EDIT_PLAN, ST_SET_SL, ST_CLOSE_PRC,
)
from handlers.journal     import show_journal
from handlers.smartwallets import sw_cb, sw_input, ST_SW_ADD
from handlers.kols        import kol_cb, kol_input
from handlers.settings    import settings_cb, settings_input
from handlers.admin       import cmd_admin, cmd_grant_pro, cmd_broadcast

# Loops
import loops.price_loop   as price_loop
import loops.wallet_loop  as wallet_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s"
)
log = logging.getLogger(__name__)


# ── keepplan callback ─────────────────────────────────────────────────────────

async def keepplan_cb(update, ctx):
    q = update.callback_query
    await q.answer("✅ Plan confirmed!")
    try:
        await q.edit_message_text(
            q.message.text + "\n\n✅ _Tracking with default plan._",
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ── quickadd callback — быстро добавить позицию из bundle alert ──────────────

async def quickadd_cb(update, ctx):
    q    = update.callback_query
    await q.answer()
    mint = q.data.split(":")[1]
    uid  = q.from_user.id

    ctx.user_data["add_ca_preset"] = mint
    await q.message.reply_text(
        f"💰 How much SOL did you put in `{mint[:16]}...`?\n\n"
        f"Send a number: `1.5`\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    # Redirect to add flow from SOL step
    ctx.user_data["add_ca"]     = mint
    ctx.user_data["quick_add"]  = True
    from services.price import fetch_price
    data = await fetch_price(mint)
    if data:
        ctx.user_data["add_name"]   = data["name"]
        ctx.user_data["add_symbol"] = data["symbol"]
        ctx.user_data["add_price"]  = data["price"]
        ctx.user_data["add_mcap"]   = data["mcap"]


# ── Universal text router ─────────────────────────────────────────────────────

async def universal_text(update, ctx):
    """Routes free text to whichever handler is expecting input."""
    if ctx.user_data.get("sw_adding"):
        return await sw_input(update, ctx)
    if ctx.user_data.get("kol_adding"):
        return await kol_input(update, ctx)
    if ctx.user_data.get("cfg_whale") or ctx.user_data.get("cfg_wallet"):
        return await settings_input(update, ctx)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Add position conversation ──────────────────────────────────────────
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_position, pattern="^do:add$")],
        states={
            ST_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_contract)],
            ST_SOL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_sol)],
            ST_PLAN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_plan)],
            ST_NOTE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_note)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Edit take-profits conversation ────────────────────────────────────
    edit_plan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(editplan_cb, pattern=r"^editplan:\d+$")],
        states={
            ST_EDIT_PLAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, editplan_got_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Set stop loss conversation ────────────────────────────────────────
    set_sl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(setsl_cb, pattern=r"^setsl:\d+$")],
        states={
            ST_SET_SL: [MessageHandler(filters.TEXT & ~filters.COMMAND, setsl_got_text)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Close position conversation ───────────────────────────────────────
    close_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(closepos_cb, pattern=r"^closepos:\d+$")],
        states={
            ST_CLOSE_PRC: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_got_price)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Commands ───────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CommandHandler("grant_pro",  cmd_grant_pro))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("cancel",     cmd_cancel))

    # ── Conversations (MUST be before generic CallbackQueryHandlers) ───────
    app.add_handler(add_conv)
    app.add_handler(edit_plan_conv)
    app.add_handler(set_sl_conv)
    app.add_handler(close_conv)

    # ── Callbacks ──────────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(lang_cb,      pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(menu_cb,      pattern=r"^do:"))
    app.add_handler(CallbackQueryHandler(keepplan_cb,  pattern=r"^keepplan:"))
    app.add_handler(CallbackQueryHandler(quickadd_cb,  pattern=r"^quickadd:"))
    app.add_handler(CallbackQueryHandler(alert_done_cb,pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(alert_skip_cb,pattern=r"^skip:"))
    app.add_handler(CallbackQueryHandler(sw_cb,        pattern=r"^sw:"))
    app.add_handler(CallbackQueryHandler(kol_cb,       pattern=r"^kol:"))
    app.add_handler(CallbackQueryHandler(settings_cb,  pattern=r"^cfg:"))

    # ── Universal text (for free-form inputs outside conversations) ────────
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, universal_text
    ))

    # ── Startup ────────────────────────────────────────────────────────────
    async def on_startup(app):
        await init_db()
        log.info("✅ DB initialized")
        asyncio.create_task(price_loop.run(app.bot))
        asyncio.create_task(wallet_loop.run(app.bot))
        log.info("✅ All loops started")

    app.post_init = on_startup

    log.info("🌙 MoonBag Bot v7 starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
