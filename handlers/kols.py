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
    action = q.data.split(":")[1]
    uid    = q.from_user.id

    if action == "add":
        ctx.user_data["kol_adding"] = True
        await q.message.reply_text(
            "🐦 *Add KOL*\n\nSend their Twitter handle (without @):\n\n"
            "_Example:_ `ansemtrades`\n\n_(or /cancel)_",
            parse_mode="Markdown"
        )

    elif action == "remove":
        async with async_session() as s:
            result = await s.execute(select(KOL).where(KOL.user_id == uid))
            kols   = result.scalars().all()
        if not kols:
            await q.answer("Nothing to remove", show_alert=True)
            return
        buttons = [[InlineKeyboardButton(f"🗑 @{k.handle}", callback_data=f"kol:del:{k.id}")]
                   for k in kols]
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="do:kols")])
        await q.edit_message_text(
            "🗑 *Remove KOL:*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif action == "del":
        kol_id = int(q.data.split(":")[2])
        async with async_session() as s:
            k = await s.get(KOL, kol_id)
            if k and k.user_id == uid:
                await s.delete(k)
                await s.commit()
                await q.answer(f"Removed @{k.handle}")
        await show_kols(q.message, uid)


async def kol_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("kol_adding"):
        return
    handle = update.message.text.strip().lstrip("@").lower()
    uid    = update.effective_user.id

    if not handle or " " in handle:
        await update.message.reply_text("❌ Invalid handle. Send without @, no spaces.")
        return

    async with async_session() as s:
        s.add(KOL(user_id=uid, handle=handle))
        await s.commit()

    ctx.user_data.pop("kol_adding", None)
    await update.message.reply_text(
        f"✅ Added: @{handle}\nI'll alert you when they mention a Solana token.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🐦 KOL Monitor", callback_data="do:kols"),
            InlineKeyboardButton("◀️ Menu",        callback_data="do:menu"),
        ]])
    )
