from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
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
            lbl = w.label or w.address[:16] + "..."
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


# ── sw:add через ConversationHandler ─────────────────────────────────────────

async def sw_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — callback кнопки ➕ Add Wallet."""
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "🧠 *Add Smart Wallet*\n\n"
        "Send the wallet address.\n"
        "Optionally add a label after a space:\n\n"
        "_Example:_\n`5fWkLJfoDsRAaXhPJcJY19qNtDDQ5h6q1 CryptoGod`\n\n"
        "_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_SW_ADD


async def sw_got_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получили адрес — сохраняем."""
    import logging
    logging.getLogger(__name__).info(f"sw_got_address called: {update.message.text[:20]}")
    parts   = update.message.text.strip().split(None, 1)
    address = parts[0]
    label   = parts[1].strip() if len(parts) > 1 else None
    uid     = update.effective_user.id

    if len(address) < 32 or len(address) > 44 or " " in address:
        await update.message.reply_text(
            "❌ Invalid address. Must be a valid Solana wallet address.\n\n"
            "Try again or /cancel",
            parse_mode="Markdown"
        )
        return ST_SW_ADD

    async with async_session() as s:
        # Проверяем дубликат
        existing = await s.execute(
            select(SmartWallet).where(
                SmartWallet.user_id == uid,
                SmartWallet.address == address,
            )
        )
        if existing.first():
            await update.message.reply_text(
                "⚠️ This wallet is already in your list.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🧠 Smart Wallets", callback_data="do:smartwallets"),
                    InlineKeyboardButton("◀️ Menu",          callback_data="do:menu"),
                ]])
            )
            return ConversationHandler.END

        s.add(SmartWallet(user_id=uid, address=address, label=label))
        await s.commit()

    display = label or (address[:8] + "..." + address[-4:])
    await update.message.reply_text(
        f"✅ *Wallet added:* `{display}`\n\n"
        f"I'll alert you when this wallet buys a token.\n"
        f"_Tracking starts within 90 seconds._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add another",   callback_data="sw:add"),
             InlineKeyboardButton("🧠 View list",     callback_data="do:smartwallets")],
            [InlineKeyboardButton("◀️ Menu",          callback_data="do:menu")],
        ])
    )
    return ConversationHandler.END


# ── Remove / list callbacks ───────────────────────────────────────────────────

async def sw_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id

    if action == "list_remove":
        async with async_session() as s:
            result  = await s.execute(
                select(SmartWallet).where(SmartWallet.user_id == uid)
            )
            wallets = result.scalars().all()

        if not wallets:
            await q.answer("No wallets to remove", show_alert=True)
            return

        buttons = [
            [InlineKeyboardButton(
                f"🗑 {w.label or w.address[:14]+'...'}",
                callback_data=f"sw:del:{w.id}"
            )]
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
                await q.answer(f"✅ Removed: {lbl}", show_alert=True)
        # Обновляем список
        await show_smartwallets(q.message, uid)


async def sw_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
    return ConversationHandler.END


async def cmd_wallets(update, ctx):
    """
    /wallets — показать статус всех smart wallets с winrate и активностью.
    """
    from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
    from services.wallet_discovery import score_tracked_wallets
    uid = update.effective_user.id

    msg = await update.message.reply_text("🔍 Analyzing wallets...")

    scores = await score_tracked_wallets(uid)

    if not scores:
        await msg.edit_text(
            "🧠 *Smart Wallets*\n\nNo wallets added yet.\n\nUse 🧠 Smart Wallets in the menu to add.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🧠 Smart Wallets", callback_data="do:smartwallets"),
            ]])
        )
        return

    lines = []
    for w in scores:
        addr  = w["address"]
        label = w.get("label") or f"{addr[:8]}..."
        wr    = w.get("winrate", 0)
        trades= w.get("trades", 0)
        status= w.get("status", "")

        status_icon = "✅" if status == "active" else ("💤" if status == "inactive" else "❓")
        wr_str = f"{wr:.0f}% WR" if wr > 0 else "no data"
        lines.append(f"{status_icon} *{label}* — {wr_str} · {trades} txs")

    text = (
        f"🧠 *Smart Wallets* ({len(scores)})\n\n"
        + "\n".join(lines)
        + "\n\n_✅ Active  💤 Silent 30d+_"
    )

    await msg.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Wallet", callback_data="sw:add"),
             InlineKeyboardButton("🗑 Remove",     callback_data="sw:list_remove")],
            [InlineKeyboardButton("◀️ Menu",       callback_data="do:menu")],
        ])
    )