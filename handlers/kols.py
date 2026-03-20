from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select
from database import async_session
from models import KOL

ST_KOL_ADD = 201


async def show_kols(msg: Message, uid: int):
    async with async_session() as s:
        result = await s.execute(select(KOL).where(KOL.user_id == uid))
        kols   = result.scalars().all()

    if not kols:
        text = (
            "🐦 *KOL Monitor*\n\n"
            "Track crypto influencers on Twitter/X.\n"
            "When they mention a Solana CA — you get an alert.\n\n"
            "_No KOLs added yet._"
        )
    else:
        lines = [f"• @{k.handle}" for k in kols]
        text  = f"🐦 *KOL Monitor* ({len(kols)})\n\n" + "\n".join(lines)

    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add KOL",  callback_data="kol:add"),
             InlineKeyboardButton("🗑 Remove",   callback_data="kol:remove")],
            [InlineKeyboardButton("◀️ Menu",     callback_data="do:menu")],
        ])
    )


# ── kol:add через ConversationHandler ────────────────────────────────────────

async def kol_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — callback кнопки ➕ Add KOL."""
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🐦 *Add KOL*\n\n"
        "Send their Twitter/X handle (without @):\n\n"
        "_Example:_ `ansemtrades`\n\n"
        "_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_KOL_ADD


async def kol_got_handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили хендл — сохраняем."""
    handle = update.message.text.strip().lstrip("@").lower()
    uid    = update.effective_user.id

    if not handle or " " in handle or len(handle) > 50:
        await update.message.reply_text(
            "❌ Invalid handle. Send without @, no spaces.\n\nTry again or /cancel",
            parse_mode="Markdown"
        )
        return ST_KOL_ADD

    async with async_session() as s:
        # Проверяем дубликат
        existing = await s.execute(
            select(KOL).where(KOL.user_id == uid, KOL.handle == handle)
        )
        if existing.first():
            await update.message.reply_text(
                f"⚠️ @{handle} is already in your list.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🐦 KOL Monitor", callback_data="do:kols"),
                    InlineKeyboardButton("◀️ Menu",        callback_data="do:menu"),
                ]])
            )
            return ConversationHandler.END

        s.add(KOL(user_id=uid, handle=handle))
        await s.commit()

    await update.message.reply_text(
        f"✅ *Added:* @{handle}\n\n"
        f"I'll alert you when they mention a Solana contract.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add another",  callback_data="kol:add"),
             InlineKeyboardButton("🐦 View list",    callback_data="do:kols")],
            [InlineKeyboardButton("◀️ Menu",         callback_data="do:menu")],
        ])
    )
    return ConversationHandler.END


# ── Remove callbacks ──────────────────────────────────────────────────────────

async def kol_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id

    if action == "remove":
        async with async_session() as s:
            result = await s.execute(select(KOL).where(KOL.user_id == uid))
            kols   = result.scalars().all()

        if not kols:
            await q.answer("Nothing to remove", show_alert=True)
            return

        buttons = [
            [InlineKeyboardButton(f"🗑 @{k.handle}", callback_data=f"kol:del:{k.id}")]
            for k in kols
        ]
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="do:kols")])
        await q.edit_message_text(
            "🗑 *Select KOL to remove:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif action == "del":
        kol_id = int(parts[2])
        async with async_session() as s:
            k = await s.get(KOL, kol_id)
            if k and k.user_id == uid:
                handle = k.handle
                await s.delete(k)
                await s.commit()
                await q.answer(f"✅ Removed @{handle}", show_alert=True)
        await show_kols(q.message, uid)


async def kol_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
    return ConversationHandler.END