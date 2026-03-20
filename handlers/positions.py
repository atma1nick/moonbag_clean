import json
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select

from database import async_session
from models import Position, JournalEntry, User
from services.price import fetch_price, get_cached_sol_price
from utils import (fmt_mcap, fmt_sol, fmt_pct, fmt_x, fmt_pnl,
                   parse_mcap, parse_exit_plan, exit_plan_text,
                   calc_pnl, dexscreener, solscan_token)
from handlers.base import get_user

log = logging.getLogger(__name__)

# ConversationHandler states
ST_CONTRACT  = 10
ST_SOL       = 11
ST_PLAN      = 12
ST_NOTE      = 13
ST_CLOSE_PRC = 20
ST_EDIT_PLAN = 30
ST_SET_SL    = 40


# ── Показать позиции ──────────────────────────────────────────────────────────

async def show_positions(msg: Message, uid: int):
    user = await get_user(uid)
    currency = user.currency if user else "SOL"

    async with async_session() as s:
        result = await s.execute(
            select(Position).where(
                Position.user_id == uid,
                Position.status  == "active"
            ).order_by(Position.created_at.desc())
        )
        positions = result.scalars().all()

    if not positions:
        await msg.reply_text(
            "📊 *No active positions*\n\nUse ➕ to add your first position.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add Position", callback_data="do:add"),
                InlineKeyboardButton("◀️ Menu",         callback_data="do:menu"),
            ]])
        )
        return

    for pos in positions:
        await msg.reply_text(
            **(await _pos_card(pos, currency)),
        )


async def _pos_card(pos: Position, currency: str = "SOL") -> dict:
    """Build position card text + keyboard."""
    data = await fetch_price(pos.contract)
    sol_price = get_cached_sol_price()

    cur_x    = 0.0
    pnl_sol  = 0.0
    pnl_pct  = 0.0
    mcap_str = "?"
    liq_str  = "?"
    vol_str  = "?"
    chg_1h   = 0.0
    chg_24h  = 0.0

    if data and data.get("price") and pos.entry_price:
        cur_x   = data["price"] / pos.entry_price
        pnl_sol, pnl_pct = calc_pnl(pos.sol_in, cur_x)
        mcap_str = fmt_mcap(data["mcap"])
        liq_str  = fmt_mcap(data["liquidity"])
        vol_str  = fmt_mcap(data["volume24h"])
        chg_1h   = data["price_change_1h"]
        chg_24h  = data["price_change_24h"]

    # PnL в нужной валюте
    pnl_usd = pnl_sol * sol_price if sol_price else None
    pnl_display = fmt_pnl(pnl_sol, pnl_usd, currency)

    # Прогресс по плану
    plan_lines = ""
    plan = json.loads(pos.exit_plan) if pos.exit_plan else []
    if plan:
        plan_lines = "\n\n📋 *Exit Plan:*\n" + exit_plan_text(plan)

    # Stop loss строка
    sl_line = ""
    if pos.stop_loss and pos.stop_loss > 0:
        sl_line = f"\n🛑 Stop Loss: {fmt_mcap(pos.stop_loss)}"

    # Знаки изменения цены
    def ch(v):
        return ("🟢 +" if v >= 0 else "🔴 ") + f"{v:.1f}%"

    text = (
        f"{'🟢' if cur_x >= 1 else '🔴'} *${pos.symbol}* — {pos.name}\n"
        f"`{pos.contract[:20]}...`\n\n"
        f"📈 *Current:* {fmt_x(cur_x)}  {pnl_display}  ({fmt_pct(pnl_pct)})\n"
        f"💰 *SOL in:* {fmt_sol(pos.sol_in)}\n"
        f"📊 *Mcap:* {mcap_str}  |  Liq: {liq_str}\n"
        f"🔄 *Vol 24h:* {vol_str}\n"
        f"⏱ *1h:* {ch(chg_1h)}  *24h:* {ch(chg_24h)}"
        f"{sl_line}"
        f"{plan_lines}"
    )

    if pos.note:
        text += f"\n\n💭 _{pos.note}_"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✎ Edit Take-Profits", callback_data=f"editplan:{pos.id}"),
         InlineKeyboardButton("🛑 Stop Loss",         callback_data=f"setsl:{pos.id}")],
        [InlineKeyboardButton("📈 Chart",             url=dexscreener(pos.contract)),
         InlineKeyboardButton("🔒 Close",             callback_data=f"closepos:{pos.id}")],
        [InlineKeyboardButton("◀️ Menu",              callback_data="do:menu")],
    ])

    return {"text": text, "parse_mode": "Markdown", "reply_markup": kb,
            "disable_web_page_preview": True}


# ── Добавить позицию ──────────────────────────────────────────────────────────

async def start_add_position(q, ctx: ContextTypes.DEFAULT_TYPE):
    await q.message.reply_text(
        "➕ *Add Position*\n\n"
        "Send the *contract address* (CA) of the token:\n\n"
        "_Example:_ `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`\n\n"
        "_(or /cancel to go back)_",
        parse_mode="Markdown"
    )
    return ST_CONTRACT


async def add_got_contract(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ca = update.message.text.strip()
    # Basic validation
    if len(ca) < 32 or len(ca) > 44 or " " in ca:
        await update.message.reply_text(
            "❌ Invalid contract address. Please send a valid Solana CA.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_CONTRACT

    msg = await update.message.reply_text("🔍 Looking up token...")
    data = await fetch_price(ca)

    if not data:
        await msg.edit_text(
            "❌ Token not found on DexScreener.\n"
            "Make sure the CA is correct and the token has liquidity.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_CONTRACT

    ctx.user_data["add_ca"]     = ca
    ctx.user_data["add_name"]   = data["name"]
    ctx.user_data["add_symbol"] = data["symbol"]
    ctx.user_data["add_price"]  = data["price"]
    ctx.user_data["add_mcap"]   = data["mcap"]

    await msg.edit_text(
        f"✅ Found: *{data['name']}* (${data['symbol']})\n"
        f"📊 Mcap: {fmt_mcap(data['mcap'])}\n\n"
        f"💰 How much *SOL* did you put in?\n\n"
        f"_Example:_ `1.5`\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_SOL


async def add_got_sol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        sol = float(update.message.text.strip().replace(",", "."))
        if sol <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid number. Example: `1.5`\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_SOL

    ctx.user_data["add_sol"] = sol

    await update.message.reply_text(
        f"📋 *Set your exit plan* (take-profits)\n\n"
        f"Format: `4x 50%, 8x 30%, moon 20%`\n\n"
        f"Or send `auto` to use the default plan:\n"
        f"  • 4x → sell 50%\n"
        f"  • 8x → sell 30%\n"
        f"  • rest → 🌙 moonbag\n\n"
        f"_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_PLAN


async def add_got_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() in ("auto", "default", "-"):
        plan = [{"x": 4, "pct": 50, "label": "4x"},
                {"x": 8, "pct": 30, "label": "8x"},
                {"x": 0, "pct": 20, "label": "moon"}]
    else:
        plan = parse_exit_plan(text)
        if not plan:
            await update.message.reply_text(
                "❌ Wrong format. Try: `4x 50%, 8x 30%, moon 20%`\n_(or /cancel)_",
                parse_mode="Markdown"
            )
            return ST_PLAN

    ctx.user_data["add_plan"] = plan

    await update.message.reply_text(
        "💭 Add a note? (optional, for your journal)\n\n"
        "_Example:_ `KOL signal, low mcap, high risk`\n\n"
        "Or send `-` to skip.",
        parse_mode="Markdown"
    )
    return ST_NOTE


async def add_got_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    note = None if text == "-" else text

    uid  = update.effective_user.id
    d    = ctx.user_data
    plan = d.get("add_plan", [])

    async with async_session() as s:
        pos = Position(
            user_id     = uid,
            contract    = d["add_ca"],
            symbol      = d["add_symbol"],
            name        = d["add_name"],
            entry_price = d["add_price"],
            entry_mcap  = d["add_mcap"],
            sol_in      = d["add_sol"],
            exit_plan   = json.dumps(plan),
            source      = "manual",
            status      = "active",
            note        = note,
        )
        s.add(pos)
        await s.commit()
        await s.refresh(pos)

    ctx.user_data.clear()

    await update.message.reply_text(
        f"✅ *Position added!*\n\n"
        f"*{d['add_name']}* (${d['add_symbol']})\n"
        f"💰 {fmt_sol(d['add_sol'])} in\n"
        f"📊 Entry mcap: {fmt_mcap(d['add_mcap'])}\n\n"
        f"*Exit plan:*\n{exit_plan_text(plan)}\n\n"
        f"🎯 I'll alert you when targets are hit!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 View Positions", callback_data="do:pos"),
            InlineKeyboardButton("◀️ Menu",           callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Edit Take-Profits ─────────────────────────────────────────────────────────

async def editplan_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])
    ctx.user_data["edit_pos_id"] = pos_id

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        await q.answer("Position not found", show_alert=True)
        return ConversationHandler.END

    current = exit_plan_text(json.loads(pos.exit_plan)) if pos.exit_plan else "_none_"

    await q.message.reply_text(
        f"✎ *Edit Take-Profits — ${pos.symbol}*\n\n"
        f"*Current plan:*\n{current}\n\n"
        f"Send new plan:\n`4x 50%, 10x 30%, moon 20%`\n\n"
        f"_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_EDIT_PLAN


async def editplan_got_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    plan = parse_exit_plan(update.message.text.strip())
    if not plan:
        await update.message.reply_text(
            "❌ Wrong format. Example: `4x 50%, 10x 30%, moon 20%`",
            parse_mode="Markdown"
        )
        return ST_EDIT_PLAN

    pos_id = ctx.user_data.get("edit_pos_id")
    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        if pos:
            # Сброс done-флагов при редактировании
            pos.exit_plan = json.dumps([{**l, "done": False} for l in plan])
            await s.commit()

    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ *Take-profits updated!*\n\n{exit_plan_text(plan)}\n\n"
        f"_Fired levels reset — tracking from scratch._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Positions", callback_data="do:pos"),
            InlineKeyboardButton("◀️ Menu",      callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Set Stop Loss ─────────────────────────────────────────────────────────────

async def setsl_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])
    ctx.user_data["sl_pos_id"] = pos_id

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        await q.answer("Position not found", show_alert=True)
        return ConversationHandler.END

    current = fmt_mcap(pos.stop_loss) if pos.stop_loss else "not set"
    await q.message.reply_text(
        f"🛑 *Set Stop Loss — ${pos.symbol}*\n\n"
        f"Current: {current}\n\n"
        f"Send mcap level to alert:\n"
        f"  `200k`, `500k`, `1m`\n\n"
        f"Or `off` to remove.\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_SET_SL


async def setsl_got_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip().lower()
    pos_id = ctx.user_data.get("sl_pos_id")

    if text == "off":
        sl_val = 0.0
        reply  = "✅ Stop loss removed."
    else:
        sl_val = parse_mcap(text)
        if not sl_val:
            await update.message.reply_text(
                "❌ Wrong format. Examples: `200k`, `1.5m`\nOr `off` to remove.",
                parse_mode="Markdown"
            )
            return ST_SET_SL
        reply = f"🛑 *Stop loss set at {fmt_mcap(sl_val)}*\nI'll alert you if mcap drops below this."

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        if pos:
            pos.stop_loss = sl_val
            await s.commit()

    ctx.user_data.clear()
    await update.message.reply_text(
        reply, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Positions", callback_data="do:pos"),
            InlineKeyboardButton("◀️ Menu",      callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Alert Done / Skip ─────────────────────────────────────────────────────────

async def alert_done_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь нажал Done на алерте тейк-профита.
    Вычитаем % из позиции, записываем в журнал частично.
    callback_data = "done:pos_id:x_level"
    """
    q       = update.callback_query
    await q.answer()
    parts   = q.data.split(":")
    pos_id  = int(parts[1])
    x_level = float(parts[2])

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        if not pos or pos.user_id != q.from_user.id:
            return

        plan  = json.loads(pos.exit_plan) if pos.exit_plan else []
        level = next((l for l in plan if abs(l.get("x", 0) - x_level) < 0.01), None)

        if not level or level.get("done"):
            await q.answer("Already marked as done.", show_alert=True)
            return

        pct      = level["pct"]
        sol_sold = pos.sol_in * (pct / 100)
        sol_recv = sol_sold * x_level   # сколько SOL получили обратно

        # Обновить позицию
        level["done"]  = True
        pos.exit_plan  = json.dumps(plan)
        pos.sol_in     = max(0.0, pos.sol_in - sol_sold)
        pos.sol_out    = (pos.sol_out or 0) + sol_recv

        # Запись в журнал как частичная продажа
        je = JournalEntry(
            user_id     = pos.user_id,
            position_id = pos.id,
            contract    = pos.contract,
            symbol      = pos.symbol,
            sol_in      = sol_sold,
            sol_out     = sol_recv,
            pnl_sol     = sol_recv - sol_sold,
            pnl_pct     = (x_level - 1) * 100,
            exit_x      = x_level,
            note        = f"Take-profit at {x_level}x",
        )
        s.add(je)
        await s.commit()

    suffix = (
        f"\n\n✅ *Done!* Sold {pct:.0f}% at {x_level}x\n"
        f"≈ {fmt_sol(sol_recv)} received\n"
        f"PnL on this tranche: +{fmt_sol(sol_recv - sol_sold)}\n"
        f"Remaining in position: {fmt_sol(pos.sol_in)}"
    )
    try:
        await q.edit_message_text(
            q.message.text + suffix,
            parse_mode="Markdown",
            reply_markup=None
        )
    except Exception:
        pass


async def alert_skip_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Пропустить уровень — не продаём, но помечаем чтобы не алертить повторно."""
    q      = update.callback_query
    await q.answer("Level skipped")
    parts  = q.data.split(":")
    pos_id = int(parts[1])
    x_level= float(parts[2])

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        if not pos or pos.user_id != q.from_user.id:
            return
        plan  = json.loads(pos.exit_plan) if pos.exit_plan else []
        for l in plan:
            if abs(l.get("x", 0) - x_level) < 0.01:
                l["skipped"] = True
        pos.exit_plan = json.dumps(plan)
        await s.commit()

    try:
        await q.edit_message_text(
            q.message.text + "\n\n⏭ _Skipped. Next target is still active._",
            parse_mode="Markdown",
            reply_markup=None
        )
    except Exception:
        pass


# ── Close Position ────────────────────────────────────────────────────────────

async def closepos_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])
    ctx.user_data["close_pos_id"] = pos_id

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        return ConversationHandler.END

    data = await fetch_price(pos.contract)
    cur_str = f" (current: {fmt_x(data['price']/pos.entry_price)})" if data and pos.entry_price else ""

    await q.message.reply_text(
        f"🔒 *Close ${pos.symbol}*{cur_str}\n\n"
        f"What was your final *exit multiple*?\n\n"
        f"Example: `3.5` (means 3.5x from entry)\n"
        f"Or `now` to use current price.\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_CLOSE_PRC


async def close_got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip().lower()
    pos_id = ctx.user_data.get("close_pos_id")
    uid    = update.effective_user.id

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos:
        await update.message.reply_text("Position not found.")
        return ConversationHandler.END

    if text == "now":
        data = await fetch_price(pos.contract)
        if data and pos.entry_price:
            exit_x = data["price"] / pos.entry_price
        else:
            await update.message.reply_text("Can't fetch current price. Enter manually.")
            return ST_CLOSE_PRC
    else:
        try:
            exit_x = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Enter a number like `3.5` or `now`.", parse_mode="Markdown")
            return ST_CLOSE_PRC

    sol_out = pos.sol_in * exit_x
    pnl_sol = sol_out - pos.sol_in
    pnl_pct = (exit_x - 1) * 100

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        pos.status    = "closed"
        pos.exit_price= pos.entry_price * exit_x if pos.entry_price else 0
        pos.sol_out   = sol_out
        pos.closed_at = datetime.utcnow()

        # Итоговая запись в журнал
        je = JournalEntry(
            user_id     = uid,
            position_id = pos_id,
            contract    = pos.contract,
            symbol      = pos.symbol,
            sol_in      = pos.sol_in,
            sol_out     = sol_out,
            pnl_sol     = pnl_sol,
            pnl_pct     = pnl_pct,
            exit_x      = exit_x,
            note        = pos.note,
        )
        s.add(je)
        await s.commit()

    ctx.user_data.clear()
    sign  = "+" if pnl_sol >= 0 else ""
    emoji = "🟢" if pnl_sol >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} *Position Closed — ${pos.symbol}*\n\n"
        f"Exit: {fmt_x(exit_x)}\n"
        f"PnL: {sign}{fmt_sol(pnl_sol)} ({sign}{pnl_pct:.1f}%)\n"
        f"SOL out: {fmt_sol(sol_out)}\n\n"
        f"_Saved to journal._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📓 Journal", callback_data="do:journal"),
            InlineKeyboardButton("◀️ Menu",    callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Cancel ────────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Menu", callback_data="do:menu")
        ]])
    )
    return ConversationHandler.END
