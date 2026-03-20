"""
/rugcheck CA — проверить токен прямо в боте.
Также автоматически вызывается при добавлении позиции.
"""
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes


async def cmd_rugcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "🔍 *RugCheck*\n\n"
            "Usage: `/rugcheck CONTRACT_ADDRESS`\n\n"
            "_Example:_\n"
            "`/rugcheck DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`",
            parse_mode="Markdown"
        )
        return

    contract = args[0].strip()
    if len(contract) < 32 or len(contract) > 44:
        await update.message.reply_text("❌ Invalid contract address.")
        return

    await _run_rugcheck(update.message, contract)


async def run_rugcheck_inline(message, contract: str, symbol: str = ""):
    """Вызывается из positions.py при добавлении позиции."""
    await _run_rugcheck(message, contract, symbol, inline=True)


async def _run_rugcheck(message, contract: str, symbol: str = "", inline: bool = False):
    msg = await message.reply_text("🔍 Running security check...")

    from services.rugcheck import check_token, format_rugcheck
    result = await check_token(contract)

    if not result:
        await msg.edit_text(
            "⚠️ RugCheck unavailable for this token.\n"
            "_Token may be too new or not indexed yet._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🌐 Check manually", url=f"https://rugcheck.xyz/tokens/{contract}"),
                InlineKeyboardButton("◀️ Menu",           callback_data="do:menu"),
            ]])
        )
        return

    text = format_rugcheck(result, symbol)
    score = result["score"]

    # Кнопки зависят от контекста
    buttons = [[
        InlineKeyboardButton("🌐 Full report", url=result["link"]),
    ]]

    if not inline:
        # Standalone команда — предлагаем добавить позицию
        buttons.append([
            InlineKeyboardButton("➕ Add Position", callback_data=f"addpos:{contract}"),
            InlineKeyboardButton("◀️ Menu",         callback_data="do:menu"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu"),
        ])

    await msg.edit_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons)
    )