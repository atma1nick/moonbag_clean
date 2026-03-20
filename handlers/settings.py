from telegram import Update, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database import async_session
from models import User
from handlers.base import get_user


async def show_settings(msg: Message, uid: int):
    user = await get_user(uid)
    if not user:
        return

    wallet_str = f"`{user.wallet[:16]}...`" if user.wallet else "_not set_"
    currency   = user.currency or "SOL"
    mode       = user.mode or "manual"
    whale_min  = user.whale_min or 5.0
    lang       = user.lang or "en"

    text = (
        f"⚙️ *Settings*\n\n"
        f"👛 Wallet: {wallet_str}\n"
        f"💱 Currency: *{currency}*\n"
        f"🐳 Whale threshold: *≥{whale_min:.0f} SOL*\n"
        f"🤖 Tracking mode: *{mode}*\n"
        f"🌐 Language: *{'🇬🇧 EN' if lang == 'en' else '🇷🇺 RU'}*\n"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Set Wallet",       callback_data="cfg:wallet"),
         InlineKeyboardButton("💱 Currency",         callback_data="cfg:currency")],
        [InlineKeyboardButton("🐳 Whale Threshold",  callback_data="cfg:whale"),
         InlineKeyboardButton("🤖 Track Mode",       callback_data="cfg:mode")],
        [InlineKeyboardButton("🌐 Language",         callback_data="cfg:lang")],
        [InlineKeyboardButton("◀️ Menu",             callback_data="do:menu")],
    ])
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
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
        await q.answer(f"Currency → {new}", show_alert=True)
        await show_settings(q.message, uid)

    elif action == "lang":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🇬🇧 English", callback_data="cfg:setlang:en"),
            InlineKeyboardButton("🇷🇺 Русский", callback_data="cfg:setlang:ru"),
        ], [InlineKeyboardButton("◀️ Back", callback_data="do:settings")]])
        await q.edit_message_text("🌐 Choose language:", reply_markup=kb)

    elif action.startswith("setlang"):
        lang = q.data.split(":")[2]
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.lang = lang
                await s.commit()
        await q.answer(f"Language set ✅", show_alert=True)
        await show_settings(q.message, uid)

    elif action == "whale":
        ctx.user_data["cfg_whale"] = True
        await q.message.reply_text(
            "🐳 *Whale Threshold*\n\n"
            "Alert when a wallet buys ≥ N SOL.\n\n"
            "Send a number: `5`, `10`, `20`\n_(or /cancel)_",
            parse_mode="Markdown"
        )

    elif action == "wallet":
        ctx.user_data["cfg_wallet"] = True
        await q.message.reply_text(
            "👛 *Set Your Wallet*\n\n"
            "Send your Solana wallet address.\n"
            "Used to auto-track your on-chain buys.\n\n"
            "_(or /cancel)_",
            parse_mode="Markdown"
        )

    elif action == "mode":
        mode = user.mode if user else "manual"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                ("✅ " if mode == "manual" else "") + "Manual",
                callback_data="cfg:setmode:manual"
            ),
             InlineKeyboardButton(
                ("✅ " if mode == "wallet" else "") + "Auto (wallet)",
                callback_data="cfg:setmode:wallet"
            )],
            [InlineKeyboardButton("◀️ Back", callback_data="do:settings")],
        ])
        await q.edit_message_text(
            "🤖 *Tracking Mode*\n\n"
            "*Manual* — add positions yourself\n"
            "*Auto (wallet)* — auto-track your on-chain buys",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif action == "setmode":
        new_mode = q.data.split(":")[2]
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.mode = new_mode
                await s.commit()
        await q.answer(f"Mode → {new_mode} ✅", show_alert=True)
        await show_settings(q.message, uid)


async def settings_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if ctx.user_data.get("cfg_whale"):
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a positive number like `5`", parse_mode="Markdown")
            return
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.whale_min = val
                await s.commit()
        ctx.user_data.pop("cfg_whale", None)
        await update.message.reply_text(
            f"✅ Whale threshold set: ≥{val:.0f} SOL",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
                InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
            ]])
        )

    elif ctx.user_data.get("cfg_wallet"):
        address = text
        if len(address) < 32 or len(address) > 44:
            await update.message.reply_text("❌ Invalid Solana address.")
            return
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.wallet = address
                await s.commit()
        ctx.user_data.pop("cfg_wallet", None)
        await update.message.reply_text(
            f"✅ Wallet saved: `{address[:16]}...`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Settings", callback_data="do:settings"),
                InlineKeyboardButton("◀️ Menu",     callback_data="do:menu"),
            ]])
        )
