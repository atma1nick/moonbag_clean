"""
/snapshot — PnL картинка с разделением manual/auto позиций.
"""
import io
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("🔄 Refresh", callback_data="snapshot:refresh"),
    InlineKeyboardButton("📊 Positions", callback_data="do:pos"),
    InlineKeyboardButton("◀️ Menu",    callback_data="do:menu"),
]])


async def cmd_snapshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("📸 Generating snapshot...")

    from services.snapshot import generate_snapshot
    img_bytes = await generate_snapshot(uid)

    if not img_bytes:
        await msg.edit_text(
            "❌ Could not generate snapshot.\n"
            "Make sure you have positions or closed trades.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
            ]])
        )
        return

    await msg.delete()
    await ctx.bot.send_photo(
        chat_id    = update.effective_chat.id,
        photo      = io.BytesIO(img_bytes),
        caption    = (
            "🌙 *MoonBag Portfolio Snapshot*\n\n"
            "Share your gains 📈\n"
            "#MoonBag #Solana #memecoin"
        ),
        parse_mode = "Markdown",
        reply_markup = KB,
    )


async def snapshot_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer("Generating...")
    uid = q.from_user.id

    from services.snapshot import generate_snapshot
    img_bytes = await generate_snapshot(uid)

    if not img_bytes:
        await q.message.reply_text(
            "❌ No data for snapshot yet.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
            ]])
        )
        return

    # Отправляем через bot.send_photo — это единственный способ
    # добавить рабочие кнопки к фото через callback
    await ctx.bot.send_photo(
        chat_id      = q.message.chat_id,
        photo        = io.BytesIO(img_bytes),
        caption      = "🌙 *MoonBag Portfolio Snapshot*",
        parse_mode   = "Markdown",
        reply_markup = KB,
    )