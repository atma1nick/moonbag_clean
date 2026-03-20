from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from sqlalchemy import select
from database import async_session
from models import SmartWallet

ST_SW_ADD = 200


async def show_smartwallets(msg: Message, uid: int):
    async with async_session() as s:
        result  = await s.execute(select(SmartWallet).where(SmartWallet.user_id == uid))
        wallets = result.scalars().all()

    if not wallets:
        text = (
            "🧠 *Smart Wallets*\n\n"
            "Track whale and smart money wallets.\n"
            "When they buy — you get an instant alert.\n\n"
            "_No wallets added yet._"
        )
    else:
        lines = []
        for w in wallets:
            wr  = f" ({w.winrate:.0f}% WR)" if w.winrate else ""
            lbl = w.label or w.address[:12] + "..."
            lines.append(f"• `{lbl}`{wr}")
        text = f"🧠 *Smart Wallets* ({len(wallets)})\n\n" + "\n".join(lines)

    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Wallet",  callback_data="sw:add"),
             InlineKeyboardButton("🗑 Remove",      callback_data="sw:list_remove")],
            [InlineKeyboardButton("◀️ Menu",        callback_data="do:menu")],
        ])
    )


async def sw_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id

    if action == "add":
        ctx.user_data["sw_adding"] = True
        await q.message.reply_text(
            "🧠 *Add Smart Wallet*\n\n"
            "Send the wallet address.\n"
            "Optionally add a label after a space:\n\n"
            "_Example:_\n`5fWkLJf... CryptoGod`\n\n"
            "_(or /cancel)_",
            parse_mode="Markdown"
        )

    elif action == "list_remove":
        async with async_session() as s:
            result  = await s.execute(select(SmartWallet).where(SmartWallet.user_id == uid))
            wallets = result.scalars().all()

        if not wallets:
            await q.answer("No wallets to remove", show_alert=True)
            return

        buttons = [
            [InlineKeyboardButton(f"🗑 {w.label or w.address[:14]+'...'}", callback_data=f"sw:del:{w.id}")]
            for w in wallets
        ]
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="do:smartwallets")])
        await q.edit_message_text(
            "🗑 *Select wallet to remove:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif action == "del":
        sw_id = int(parts[2])
        async with async_session() as s:
            w = await s.get(SmartWallet, sw_id)
            if w and w.user_id == uid:
                lbl = w.label or w.address[:14] + "..."
                await s.delete(w)
                await s.commit()
                await q.answer(f"Removed: {lbl}")
        await show_smartwallets(q.message, uid)


async def sw_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("sw_adding"):
        return

    parts   = update.message.text.strip().split(None, 1)
    address = parts[0]
    label   = parts[1] if len(parts) > 1 else None
    uid     = update.effective_user.id

    if len(address) < 32 or len(address) > 44:
        await update.message.reply_text(
            "❌ Invalid address. Send a valid Solana wallet address.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return

    async with async_session() as s:
        s.add(SmartWallet(user_id=uid, address=address, label=label))
        await s.commit()

    ctx.user_data.pop("sw_adding", None)
    display = label or address[:16] + "..."

    await update.message.reply_text(
        f"✅ *Wallet added:* `{display}`\n\n"
        f"I'll alert you when this wallet buys a token.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧠 Smart Wallets", callback_data="do:smartwallets"),
             InlineKeyboardButton("◀️ Menu",          callback_data="do:menu")],
        ])
    )
