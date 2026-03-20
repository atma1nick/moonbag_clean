from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from database import async_session
from models import User
from handlers.base import get_user

ST_WALLET = 300
ST_WHALE  = 301


async def show_settings(msg: Message, uid: int):
    user = await get_user(uid)
    if not user:
        return

    wallet_str = f"`{user.wallet[:16]}...`" if user.wallet else "_not connected_"
    currency   = user.currency  or "SOL"
    mode       = user.mode      or "manual"
    whale_min  = user.whale_min or 5.0
    lang       = user.lang      or "en"
    mode_str   = "🤖 Auto" if mode == "wallet" else "✋ Manual"
    cur_str    = "💵 USD" if currency == "USD" else "◎ SOL"

    text = (
        f"⚙️ *Settings*\n\n"
        f"👛 Wallet: {wallet_str}\n"
        f"💱 Currency: *{cur_str}*\n"
        f"🐳 Whale alert: *≥{whale_min:.0f} SOL*\n"
        f"🤖 Mode: *{mode_str}*\n"
        f"🌐 Language: *{'🇬🇧 EN' if lang == 'en' else '🇷🇺 RU'}*"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Wallet",          callback_data="cfg:wallet"),
         InlineKeyboardButton("💱 Currency",        callback_data="cfg:currency")],
        [InlineKeyboardButton("🐳 Whale threshold", callback_data="cfg:whale"),
         InlineKeyboardButton("🤖 Track mode",      callback_data="cfg:mode")],
        [InlineKeyboardButton("📋 Auto exit plan",  callback_data="cfg:autoplan"),
         InlineKeyboardButton("🌐 Language",        callback_data="cfg:lang")],
        [InlineKeyboardButton("◀️ Menu",            callback_data="do:menu")],
    ])
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ── Wallet ConversationHandler ─────────────────────────────────────────────────

async def wallet_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    user = await get_user(q.from_user.id)
    cur  = f"`{user.wallet[:16]}...`" if user and user.wallet else "_not set_"
    await q.message.reply_text(
        f"👛 *Connect Wallet*\n\n"
        f"Current: {cur}\n\n"
        f"Send your *Solana wallet address*\n"
        f"_(44 characters, starts with a letter)_\n\n"
        f"_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_WALLET


async def wallet_got_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    uid     = update.effective_user.id

    if len(address) < 32 or len(address) > 44 or " " in address:
        await update.message.reply_text(
            "❌ Invalid Solana wallet address.\n"
            "Must be 32–44 characters, no spaces.\n\nTry again or /cancel"
        )
        return ST_WALLET

    async with async_session() as s:
        u = await s.get(User, uid)
        if u:
            u.wallet = address
            await s.commit()

    await update.message.reply_text(
        f"✅ *Wallet connected!*\n\n`{address[:16]}...`\n\n"
        f"Now you can enable Auto tracking in Settings → Track mode.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
            InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Whale ConversationHandler ──────────────────────────────────────────────────

async def whale_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    user = await get_user(q.from_user.id)
    cur  = f"≥{user.whale_min:.0f} SOL" if user and user.whale_min else "≥5 SOL"
    await q.message.reply_text(
        f"🐳 *Whale Alert Threshold*\n\n"
        f"Current: *{cur}*\n\n"
        f"Alert when a wallet buys ≥ N SOL in one tx.\n\n"
        f"Send a number: `5`, `10`, `25`\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_WHALE


async def whale_got_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    try:
        val = float(text.replace(",", "."))
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Send a positive number like `5` or `10`\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_WHALE

    async with async_session() as s:
        u = await s.get(User, uid)
        if u:
            u.whale_min = val
            await s.commit()

    await update.message.reply_text(
        f"✅ Whale threshold: *≥{val:.0f} SOL*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
            InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Cancel ─────────────────────────────────────────────────────────────────────

async def settings_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
            InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Other callbacks ────────────────────────────────────────────────────────────

async def settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id
    user   = await get_user(uid)

    if action == "currency":
        cur = user.currency if user else "SOL"
        new = "USD" if cur == "SOL" else "SOL"
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.currency = new
                await s.commit()
        icon = "💵 USD" if new == "USD" else "◎ SOL"
        await q.answer(f"Currency → {icon}", show_alert=True)
        await show_settings(q.message, uid)

    elif action == "lang":
        await q.answer()
        await q.edit_message_text(
            "🌐 *Choose language:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇬🇧 English", callback_data="cfg:setlang:en"),
                 InlineKeyboardButton("🇷🇺 Русский", callback_data="cfg:setlang:ru")],
                [InlineKeyboardButton("◀️ Back",     callback_data="cfg:back")],
            ])
        )

    elif action == "setlang":
        lang = parts[2]
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.lang = lang
                await s.commit()
        await q.answer("✅ Language updated", show_alert=True)
        await show_settings(q.message, uid)

    elif action == "mode":
        await q.answer()
        mode = user.mode if user else "manual"
        await q.edit_message_text(
            "🤖 *Tracking Mode*\n\n"
            "✋ *Manual* — add positions yourself via ➕\n\n"
            "🤖 *Auto* — bot watches your wallet on-chain.\n"
            "When you buy a token → position created automatically.\n"
            "Requires wallet to be connected first.\n\n"
            f"Current: *{'Auto' if mode == 'wallet' else 'Manual'}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    ("✅ " if mode == "manual" else "") + "✋ Manual",
                    callback_data="cfg:setmode:manual"
                 ),
                 InlineKeyboardButton(
                    ("✅ " if mode == "wallet" else "") + "🤖 Auto",
                    callback_data="cfg:setmode:wallet"
                 )],
                [InlineKeyboardButton("◀️ Back", callback_data="cfg:back")],
            ])
        )

    elif action == "setmode":
        new_mode = parts[2]
        if new_mode == "wallet" and (not user or not user.wallet):
            await q.answer(
                "⚠️ Connect your wallet first (Settings → Wallet)",
                show_alert=True
            )
            return
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.mode = new_mode
                await s.commit()
        label = "🤖 Auto" if new_mode == "wallet" else "✋ Manual"
        await q.answer(f"Mode → {label} ✅", show_alert=True)
        await show_settings(q.message, uid)

    elif action == "autoplan":
        await q.answer()
        await q.message.reply_text(
            "📋 *Auto Exit Plan*\n\n"
            "Applied when bot auto-tracks your wallet buys.\n\n"
            "Use /autoplan to configure.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Open", callback_data="ap:menu")],
                [InlineKeyboardButton("◀️ Back", callback_data="cfg:back")],
            ])
        )

    elif action == "back":
        await q.answer()
        await show_settings(q.message, uid)

    # wallet и whale теперь через ConversationHandler — эти ветки не нужны
    # но оставим на случай если callback придёт мимо conversation
    elif action in ("wallet", "whale"):
        await q.answer()