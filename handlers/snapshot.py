"""
/snapshot — отправить PnL картинку.
"""
import io
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes


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
    await update.message.reply_photo(
        photo=io.BytesIO(img_bytes),
        caption=(
            "🌙 *MoonBag Portfolio Snapshot*\n\n"
            "Share your gains 📈\n"
            "#MoonBag #Solana #memecoin"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh",  callback_data="snapshot:refresh"),
            InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
        ]])
    )


async def snapshot_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer("Generating...")
    uid = q.from_user.id

    from services.snapshot import generate_snapshot
    import io
    img_bytes = await generate_snapshot(uid)

    if img_bytes:
        await q.message.reply_photo(
            photo=io.BytesIO(img_bytes),
            caption="🌙 *MoonBag Portfolio Snapshot*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="snapshot:refresh"),
                InlineKeyboardButton("◀️ Menu",    callback_data="do:menu"),
            ]])
        )
