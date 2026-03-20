from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database import async_session
from models import User
from handlers.base import get_user


async def show_settings(msg: Message, uid: int):
    user = await get_user(uid)
    if not user:
        return

    wallet_str = f"`{user.wallet[:16]}...`" if user.wallet else "_not connected_"
    currency   = user.currency  or "SOL"
    mode       = user.mode      or "manual"
    whale_min  = user.whale_min or 5.0
    lang       = user.lang      or "en"

    mode_str = "🤖 Auto (wallet)" if mode == "wallet" else "✋ Manual"
    cur_str  = "💵 USD" if currency == "USD" else "◎ SOL"

    text = (
        f"⚙️ *Settings*\n\n"
        f"👛 Wallet: {wallet_str}\n"
        f"💱 Display currency: *{cur_str}*\n"
        f"🐳 Whale alert: *≥{whale_min:.0f} SOL*\n"
        f"🤖 Tracking mode: *{mode_str}*\n"
        f"🌐 Language: *{'🇬🇧 EN' if lang == 'en' else '🇷🇺 RU'}*\n"
    )

    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 Set Wallet",      callback_data="cfg:wallet"),
             InlineKeyboardButton("💱 Currency",        callback_data="cfg:currency")],
            [InlineKeyboardButton("🐳 Whale Threshold", callback_data="cfg:whale"),
             InlineKeyboardButton("🤖 Track Mode",      callback_data="cfg:mode")],
            [InlineKeyboardButton("📋 Auto Exit Plan",  callback_data="cfg:autoplan"),
             InlineKeyboardButton("🌐 Language",        callback_data="cfg:lang")],
            [InlineKeyboardButton("◀️ Menu",            callback_data="do:menu")],
        ])
    )


async def settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":")
    action = parts[1]
    uid    = q.from_user.id
    user   = await get_user(uid)

    # ── Currency toggle ───────────────────────────────────────────────────
    if action == "currency":
        cur = user.currency if user else "SOL"
        new = "USD" if cur == "SOL" else "SOL"
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.currency = new
                await s.commit()
        icon = "💵 USD" if new == "USD" else "◎ SOL"
        await q.answer(f"Display currency → {icon}", show_alert=True)
        await show_settings(q.message, uid)

    # ── Language ──────────────────────────────────────────────────────────
    elif action == "lang":
        await q.edit_message_text(
            "🌐 *Choose language:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🇬🇧 English", callback_data="cfg:setlang:en"),
                 InlineKeyboardButton("🇷🇺 Русский", callback_data="cfg:setlang:ru")],
                [InlineKeyboardButton("◀️ Back",     callback_data="do:settings")],
            ])
        )

    elif action == "setlang":
        lang = parts[2]
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.lang = lang
                await s.commit()
        await q.answer("Language updated ✅", show_alert=True)
        await show_settings(q.message, uid)

    # ── Whale threshold ───────────────────────────────────────────────────
    elif action == "whale":
        ctx.user_data["cfg_whale"] = True
        await q.message.reply_text(
            "🐳 *Whale Alert Threshold*\n\n"
            "Alert when a wallet buys ≥ N SOL in a single tx.\n\n"
            "Send a number: `5`, `10`, `25`\n_(or /cancel)_",
            parse_mode="Markdown"
        )

    # ── Wallet ────────────────────────────────────────────────────────────
    elif action == "wallet":
        ctx.user_data["cfg_wallet"] = True
        cur = f"`{user.wallet[:16]}...`" if user and user.wallet else "_not set_"
        await q.message.reply_text(
            f"👛 *Connect Wallet*\n\n"
            f"Current: {cur}\n\n"
            f"Send your Solana wallet address.\n"
            f"Used for auto-tracking your on-chain buys.\n\n"
            f"_(or /cancel)_",
            parse_mode="Markdown"
        )

    # ── Track Mode ────────────────────────────────────────────────────────
    elif action == "mode":
        mode = user.mode if user else "manual"
        await q.edit_message_text(
            "🤖 *Tracking Mode*\n\n"
            "✋ *Manual* — you add positions yourself via ➕\n\n"
            "🤖 *Auto (wallet)* — bot watches your wallet on-chain.\n"
            "When you buy a token, position is created automatically\n"
            "with a default exit plan. Requires wallet to be connected.\n\n"
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
                [InlineKeyboardButton("◀️ Back", callback_data="do:settings")],
            ])
        )

    elif action == "autoplan":
        # Редирект на /autoplan
        await q.message.reply_text(
            "Use /autoplan to set your default exit plan.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Set Auto Plan", callback_data="ap:custom"),
                InlineKeyboardButton("◀️ Back", callback_data="do:settings"),
            ]])
        )

    elif action == "setmode":
        new_mode = parts[2]

        # Если Auto — нужен кошелёк
        user = await get_user(uid)
        if new_mode == "wallet" and (not user or not user.wallet):
            await q.answer(
                "⚠️ Connect your wallet first in Settings → Set Wallet",
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


async def settings_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    # ── Whale threshold ───────────────────────────────────────────────────
    if ctx.user_data.get("cfg_whale"):
        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Send a positive number like `5`\n_(or /cancel)_",
                parse_mode="Markdown"
            )
            return
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.whale_min = val
                await s.commit()
        ctx.user_data.pop("cfg_whale", None)
        await update.message.reply_text(
            f"✅ Whale threshold set: ≥{val:.0f} SOL",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
                InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
            ]])
        )
        # Автоматически показываем settings
        await show_settings(update.message, uid)

    # ── Wallet ────────────────────────────────────────────────────────────
    elif ctx.user_data.get("cfg_wallet"):
        address = text
        if len(address) < 32 or len(address) > 44 or " " in address:
            await update.message.reply_text(
                "❌ Invalid Solana address.\n_(or /cancel)_",
                parse_mode="Markdown"
            )
            return
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.wallet = address
                await s.commit()
        ctx.user_data.pop("cfg_wallet", None)
        await update.message.reply_text(
            f"✅ Wallet connected: `{address[:16]}...`\n\n"
            f"Now you can enable Auto tracking mode.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
                InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
            ]])
        )