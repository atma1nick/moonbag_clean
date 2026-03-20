"""
/autoplan — настройка дефолтного плана выхода для автотрекинга.
Когда бот автоматически создаёт позицию (wallet mode),
он использует этот план вместо захардкоженного 4x/8x/moon.
"""
import json
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from database import async_session
from models import User
from utils import parse_exit_plan, exit_plan_text
from handlers.base import get_user

ST_AUTOPLAN = 60

DEFAULT_PLAN = [
    {"x": 4,  "pct": 50, "label": "4x"},
    {"x": 8,  "pct": 30, "label": "8x"},
    {"x": 0,  "pct": 20, "label": "moon"},
]

PRESETS = {
    "conservative": [
        {"x": 3,  "pct": 40, "label": "3x"},
        {"x": 6,  "pct": 40, "label": "6x"},
        {"x": 0,  "pct": 20, "label": "moon"},
    ],
    "aggressive": [
        {"x": 5,  "pct": 30, "label": "5x"},
        {"x": 15, "pct": 40, "label": "15x"},
        {"x": 0,  "pct": 30, "label": "moon"},
    ],
    "moonbag": [
        {"x": 3,  "pct": 50, "label": "3x"},
        {"x": 0,  "pct": 50, "label": "moon"},
    ],
}


async def cmd_autoplan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = await get_user(uid)

    if user and user.default_plan:
        try:
            plan = json.loads(user.default_plan)
            current = exit_plan_text(plan)
        except Exception:
            current = "_default (4x 50%, 8x 30%, moon 20%)_"
    else:
        current = "_default (4x 50%, 8x 30%, moon 20%)_"

    await update.message.reply_text(
        f"🤖 *Auto-Track Exit Plan*\n\n"
        f"This plan is applied automatically when the bot\n"
        f"detects a new buy from your wallet.\n\n"
        f"*Current plan:*\n{current}\n\n"
        f"Choose a preset or send a custom plan:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Conservative (3x/6x)", callback_data="ap:conservative"),
             InlineKeyboardButton("🚀 Aggressive (5x/15x)",  callback_data="ap:aggressive")],
            [InlineKeyboardButton("🌙 Moonbag (3x/moon)",    callback_data="ap:moonbag"),
             InlineKeyboardButton("✎ Custom plan",           callback_data="ap:custom")],
            [InlineKeyboardButton("🔄 Reset to default",     callback_data="ap:reset")],
            [InlineKeyboardButton("◀️ Menu",                 callback_data="do:menu")],
        ])
    )


async def autoplan_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    action = q.data.split(":")[1]
    uid    = q.from_user.id

    if action == "menu":
        await cmd_autoplan.__wrapped__(update, ctx) if hasattr(cmd_autoplan, '__wrapped__') else None
        # Показываем меню autoplan заново
        from handlers.base import get_user
        import json
        user = await get_user(uid)
        if user and user.default_plan:
            try:
                plan = json.loads(user.default_plan)
                from utils import exit_plan_text
                current = exit_plan_text(plan)
            except Exception:
                current = "_default (4x 50%, 8x 30%, moon 20%)_"
        else:
            current = "_default (4x 50%, 8x 30%, moon 20%)_"
        await q.message.reply_text(
            f"📋 *Auto-Track Exit Plan*\n\n"
            f"*Current:*\n{current}\n\n"
            f"Choose preset or send custom:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Conservative", callback_data="ap:conservative"),
                 InlineKeyboardButton("🚀 Aggressive",   callback_data="ap:aggressive")],
                [InlineKeyboardButton("🌙 Moonbag",      callback_data="ap:moonbag"),
                 InlineKeyboardButton("✎ Custom",        callback_data="ap:custom")],
                [InlineKeyboardButton("🔄 Reset default",callback_data="ap:reset")],
                [InlineKeyboardButton("◀️ Back",         callback_data="do:settings")],
            ])
        )
        return

    if action == "custom":
        await q.message.reply_text(
            "✎ *Custom Auto Plan*\n\n"
            "Send your exit plan:\n"
            "`4x 50%, 10x 30%, moon 20%`\n\n"
            "_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_AUTOPLAN

    elif action == "reset":
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.default_plan = None
                await s.commit()
        await q.edit_message_text(
            "✅ *Auto plan reset to default:*\n\n"
            "  • 4x → sell 50%\n"
            "  • 8x → sell 30%\n"
            "  • 🌙 hold 20%",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
            ]])
        )
        return ConversationHandler.END

    elif action in PRESETS:
        plan = PRESETS[action]
        async with async_session() as s:
            u = await s.get(User, uid)
            if u:
                u.default_plan = json.dumps(plan)
                await s.commit()
        await q.edit_message_text(
            f"✅ *Auto plan set:*\n\n{exit_plan_text(plan)}\n\n"
            f"_Applied to all future auto-tracked buys._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
            ]])
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def autoplan_got_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    plan = parse_exit_plan(update.message.text.strip())
    if not plan:
        await update.message.reply_text(
            "❌ Wrong format. Example: `4x 50%, 10x 30%, moon 20%`\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_AUTOPLAN

    uid = update.effective_user.id
    async with async_session() as s:
        u = await s.get(User, uid)
        if u:
            u.default_plan = json.dumps(plan)
            await s.commit()

    await update.message.reply_text(
        f"✅ *Custom auto plan saved:*\n\n{exit_plan_text(plan)}\n\n"
        f"_Applied to all future auto-tracked buys._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
    return ConversationHandler.END
