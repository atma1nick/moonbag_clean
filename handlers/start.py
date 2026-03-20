from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from handlers.base import ensure_user, get_user
from database import async_session
from models import User

MENU_TEXT = {
    "en": "🌙 *MoonBag Bot* — Main Menu",
    "ru": "🌙 *MoonBag Bot* — Главное меню",
}

def _menu_kb(lang: str) -> InlineKeyboardMarkup:
    if lang == "ru":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Позиции",         callback_data="do:pos"),
             InlineKeyboardButton("📓 Журнал",          callback_data="do:journal")],
            [InlineKeyboardButton("🧠 Смарт-кошельки", callback_data="do:smartwallets"),
             InlineKeyboardButton("🐦 KOL Монитор",    callback_data="do:kols")],
            [InlineKeyboardButton("➕ Добавить",        callback_data="do:add"),
             InlineKeyboardButton("⚙️ Настройки",       callback_data="do:settings")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Positions",    callback_data="do:pos"),
         InlineKeyboardButton("📓 Journal",          callback_data="do:journal")],
        [InlineKeyboardButton("🧠 Smart Wallets",   callback_data="do:smartwallets"),
         InlineKeyboardButton("🐦 KOL Monitor",     callback_data="do:kols")],
        [InlineKeyboardButton("➕ Add Position",    callback_data="do:add"),
         InlineKeyboardButton("⚙️ Settings",         callback_data="do:settings")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username
    user  = await ensure_user(uid, uname)

    if user.lang:
        await update.message.reply_text(
            MENU_TEXT[user.lang], parse_mode="Markdown",
            reply_markup=_menu_kb(user.lang)
        )
        return

    # Язык ещё не выбран
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
        InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
    ]])
    await update.message.reply_text(
        "🌙 *Welcome to MoonBag Bot!*\n\n"
        "Your trading assistant for Solana meme coins.\n"
        "• Track positions & take-profit alerts\n"
        "• Smart wallet monitoring\n"
        "• Bundle detector\n"
        "• Trade journal\n\n"
        "_Choose language / Выберите язык:_",
        parse_mode="Markdown", reply_markup=kb
    )


async def lang_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    lang = q.data.split(":")[1]
    uid  = q.from_user.id

    async with async_session() as s:
        user = await s.get(User, uid)
        if not user:
            user = User(user_id=uid, lang=lang)
            s.add(user)
        else:
            user.lang = lang
        await s.commit()

    # Убираем кнопки выбора языка
    try:
        lbl = "🇬🇧 English" if lang == "en" else "🇷🇺 Русский"
        await q.edit_message_text(f"✅ Language: {lbl}", parse_mode="Markdown")
    except Exception:
        pass

    # Показываем главное меню новым сообщением
    await q.message.chat.send_message(
        MENU_TEXT[lang], parse_mode="Markdown",
        reply_markup=_menu_kb(lang)
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = await get_user(uid)
    lang = user.lang if user else "en"
    await update.message.reply_text(
        MENU_TEXT[lang], parse_mode="Markdown",
        reply_markup=_menu_kb(lang)
    )


async def menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
    uid    = q.from_user.id
    user   = await get_user(uid)
    lang   = user.lang if user else "en"

    try:
        if action == "menu":
            await q.edit_message_text(
                MENU_TEXT[lang], parse_mode="Markdown",
                reply_markup=_menu_kb(lang)
            )
        elif action == "pos":
            from handlers.positions import show_positions
            await show_positions(q.message, uid)
        elif action == "journal":
            from handlers.journal import show_journal
            await show_journal(q.message, uid)
        elif action == "smartwallets":
            from handlers.smartwallets import show_smartwallets
            await show_smartwallets(q.message, uid)
        elif action == "kols":
            from handlers.kols import show_kols
            await show_kols(q.message, uid)
        elif action == "add":
            from handlers.positions import start_add_position
            await start_add_position(q, ctx)
        elif action == "settings":
            from handlers.settings import show_settings
            await show_settings(q.message, uid)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"menu_cb {action}: {e}")


def back_button(lang: str = "en") -> InlineKeyboardButton:
    lbl = "◀️ Back to menu" if lang == "en" else "◀️ Главное меню"
    return InlineKeyboardButton(lbl, callback_data="do:menu")
