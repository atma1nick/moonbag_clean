from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from sqlalchemy import select
from database import async_session
from models import KOL


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


async def kol_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id

    if action == "add":
        ctx.user_data["kol_adding"] = True
        await q.message.reply_text(
            "🐦 *Add KOL*\n\n"
            "Send their Twitter handle (without @):\n\n"
            "_Example:_ `ansemtrades`\n\n"
            "_(or /cancel)_",
            parse_mode="Markdown"
        )

    elif action == "remove":
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
                await q.answer(f"Removed @{handle}")
        await show_kols(q.message, uid)


async def kol_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("kol_adding"):
        return

    handle = update.message.text.strip().lstrip("@").lower()
    uid    = update.effective_user.id

    if not handle or " " in handle or len(handle) > 50:
        await update.message.reply_text(
            "❌ Invalid handle. Send without @, no spaces.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return

    async with async_session() as s:
        s.add(KOL(user_id=uid, handle=handle))
        await s.commit()

    ctx.user_data.pop("kol_adding", None)

    await update.message.reply_text(
        f"✅ *KOL added:* @{handle}\n\n"
        f"I'll alert you when they mention a Solana token.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🐦 KOL Monitor", callback_data="do:kols"),
             InlineKeyboardButton("◀️ Menu",        callback_data="do:menu")],
        ])
    )
    # Показываем список КОЛов сразу после добавления
    await show_kols(update.message, update.effective_user.id)
