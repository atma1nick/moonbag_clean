import json
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select

from database import async_session
from models import Position, JournalEntry
from services.price import fetch_price, get_cached_sol_price
from utils import (fmt_mcap, fmt_sol, fmt_pct, fmt_x, fmt_pnl,
                   parse_mcap, parse_exit_plan, exit_plan_text,
                   calc_pnl, dexscreener)
from handlers.base import get_user

log = logging.getLogger(__name__)

# ConversationHandler states
ST_CONTRACT  = 10
ST_ENTRY     = 11   # точка входа (цена или mcap)
ST_SOL       = 12
ST_PLAN      = 13
ST_NOTE      = 14
ST_CLOSE_PCT = 20   # % позиции для продажи
ST_EDIT_PLAN = 30
ST_SET_SL    = 40


# ── Позиции ───────────────────────────────────────────────────────────────────

async def show_positions(msg: Message, uid: int):
    user     = await get_user(uid)
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
        card = await _pos_card(pos, currency)
        await msg.reply_text(**card)


async def _pos_card(pos: Position, currency: str = "SOL") -> dict:
    data      = await fetch_price(pos.contract)
    sol_price = get_cached_sol_price()

    cur_x = pnl_sol = pnl_pct = chg_1h = chg_24h = 0.0
    mcap_str = liq_str = vol_str = "?"
    cur_price = 0.0

    if data and data.get("price") and pos.entry_price:
        cur_price        = data["price"]
        cur_x            = cur_price / pos.entry_price
        pnl_sol, pnl_pct = calc_pnl(pos.sol_in, cur_x)
        mcap_str         = fmt_mcap(data["mcap"])
        liq_str          = fmt_mcap(data["liquidity"])
        vol_str          = fmt_mcap(data["volume24h"])
        chg_1h           = data["price_change_1h"]
        chg_24h          = data["price_change_24h"]

    pnl_usd     = pnl_sol * sol_price if sol_price else None
    pnl_display = fmt_pnl(pnl_sol, pnl_usd, currency)

    def ch(v):
        return ("🟢 +" if v >= 0 else "🔴 ") + f"{v:.1f}%"

    # Exit plan с фактической стоимостью на каждом уровне
    plan      = json.loads(pos.exit_plan) if pos.exit_plan else []
    plan_lines = ""
    if plan:
        lines = []
        for l in plan:
            x_target = l.get("x", 0)
            pct      = l.get("pct", 0)
            done     = l.get("done", False)
            skipped  = l.get("skipped", False)
            lbl      = l.get("label", f"{x_target}x") if x_target else "🌙 Moon"

            # Фактическая стоимость = SOL_in * x_target * pct%
            if x_target and pos.sol_in:
                sol_at_target = pos.sol_in * (pct / 100) * x_target
                val_str = f"≈{fmt_sol(sol_at_target, 2)}"
            else:
                val_str = f"{pct}% held"

            if done:
                status = "✅"
            elif skipped:
                status = "⏭"
            else:
                # прогресс к уровню
                progress = min(100, int((cur_x / x_target) * 100)) if x_target and cur_x else 0
                bar      = "█" * (progress // 20) + "░" * (5 - progress // 20)
                status   = f"[{bar}] {progress}%"

            lines.append(f"  {status} *{lbl}* — sell {pct}% → {val_str}")

        plan_lines = "\n\n📋 *Exit Plan:*\n" + "\n".join(lines)

    sl_text = f"\n🛑 SL: {fmt_mcap(pos.stop_loss)}" if pos.stop_loss else ""

    text = (
        f"{'🟢' if cur_x >= 1 else '🔴'} *${pos.symbol}* — {pos.name}\n"
        f"`{pos.contract[:20]}...`\n\n"
        f"📈 *Current:* {fmt_x(cur_x)}  {pnl_display}  ({fmt_pct(pnl_pct)})\n"
        f"💰 *SOL in:* {fmt_sol(pos.sol_in)}\n"
        f"📊 *Mcap:* {mcap_str}  |  Liq: {liq_str}\n"
        f"🔄 *Vol 24h:* {vol_str}\n"
        f"⏱ *1h:* {ch(chg_1h)}  *24h:* {ch(chg_24h)}"
        f"{sl_text}"
        f"{plan_lines}"
    )
    if pos.note:
        text += f"\n\n💭 _{pos.note}_"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✎ Edit Take-Profits", callback_data=f"editplan:{pos.id}"),
         InlineKeyboardButton("🛑 Stop Loss",         callback_data=f"setsl:{pos.id}")],
        [InlineKeyboardButton("📈 Chart",             url=dexscreener(pos.contract)),
         InlineKeyboardButton("💸 Sell %",            callback_data=f"closepos:{pos.id}")],
        [InlineKeyboardButton("◀️ Menu",              callback_data="do:menu")],
    ])

    return {"text": text, "parse_mode": "Markdown",
            "reply_markup": kb, "disable_web_page_preview": True}


# ── Добавление позиции ────────────────────────────────────────────────────────

async def start_add_position(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "➕ *Add Position — Step 1/5*\n\n"
        "Send the token *contract address* (CA):\n\n"
        "_Example:_\n`DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`\n\n"
        "_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_CONTRACT


async def add_got_contract(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ca = update.message.text.strip()
    if len(ca) < 32 or len(ca) > 44 or " " in ca:
        await update.message.reply_text(
            "❌ Invalid CA. Send a valid Solana contract address.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_CONTRACT

    msg  = await update.message.reply_text("🔍 Looking up token...")
    data = await fetch_price(ca)

    if not data:
        await msg.edit_text(
            "❌ Token not found on DexScreener.\n"
            "Check the CA and make sure it has liquidity.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_CONTRACT

    ctx.user_data.update({
        "add_ca":        ca,
        "add_name":      data["name"],
        "add_symbol":    data["symbol"],
        "add_cur_price": data["price"],
        "add_cur_mcap":  data["mcap"],
    })

    await msg.edit_text(
        f"✅ *{data['name']}* (${data['symbol']})\n"
        f"📊 Current mcap: {fmt_mcap(data['mcap'])}\n\n"
        f"*Step 2/5* — What was your *entry price / mcap*?\n\n"
        f"Options:\n"
        f"  • Send `now` — use current price as entry\n"
        f"  • Send mcap: `500k`, `1.2m`\n"
        f"  • Send price in USD: `0.0000045`\n\n"
        f"_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_ENTRY


async def add_got_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text.strip().lower()
    cur_price = ctx.user_data.get("add_cur_price", 0)
    cur_mcap  = ctx.user_data.get("add_cur_mcap",  0)

    if text == "now":
        entry_price = cur_price
        entry_mcap  = cur_mcap
        note_entry  = "entry = current price"
    else:
        # Попробуем как mcap (500k, 1m...)
        mcap_val = parse_mcap(text)
        if mcap_val and cur_price and cur_mcap:
            # Масштабируем цену пропорционально mcap
            entry_price = cur_price * (mcap_val / cur_mcap) if cur_mcap else cur_price
            entry_mcap  = mcap_val
            note_entry  = f"entry mcap {fmt_mcap(mcap_val)}"
        else:
            # Попробуем как прямую цену
            try:
                entry_price = float(text.replace(",", "."))
                entry_mcap  = cur_mcap * (entry_price / cur_price) if cur_price else cur_mcap
                note_entry  = f"entry price ${entry_price}"
            except ValueError:
                await update.message.reply_text(
                    "❌ Can't parse that. Try `now`, `500k`, or `0.0000045`\n_(or /cancel)_",
                    parse_mode="Markdown"
                )
                return ST_ENTRY

    ctx.user_data["add_price"] = entry_price
    ctx.user_data["add_mcap"]  = entry_mcap

    cur_x = cur_price / entry_price if entry_price else 1.0
    x_str = f" (currently {fmt_x(cur_x)})" if abs(cur_x - 1.0) > 0.01 else " (at entry)"

    await update.message.reply_text(
        f"✅ Entry set: {fmt_mcap(entry_mcap)}{x_str}\n\n"
        f"*Step 3/5* — How much *SOL* did you put in?\n\n"
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
        f"*Step 4/5* — Set your *exit plan*\n\n"
        f"Format: `4x 50%, 8x 30%, moon 20%`\n\n"
        f"Or send `auto` for default:\n"
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
        f"*Step 5/5* — Add a note? (optional)\n\n"
        f"_Example:_ `KOL signal, high risk`\n\n"
        f"Send `-` to skip.\n_(or /cancel)_",
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

    ctx.user_data.clear()

    # Показываем итог и кнопки навигации
    await update.message.reply_text(
        f"✅ *Position added!*\n\n"
        f"*{d['add_name']}* (${d['add_symbol']})\n"
        f"💰 {fmt_sol(d['add_sol'])} in  |  Entry: {fmt_mcap(d['add_mcap'])}\n\n"
        f"*Exit plan:*\n{exit_plan_text(plan)}\n\n"
        f"🎯 I'll alert you when targets are hit!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 My Positions", callback_data="do:pos"),
            InlineKeyboardButton("◀️ Menu",         callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Edit Take-Profits ─────────────────────────────────────────────────────────

async def editplan_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        await q.answer("Position not found", show_alert=True)
        return ConversationHandler.END

    ctx.user_data["edit_pos_id"] = pos_id
    current = exit_plan_text(json.loads(pos.exit_plan)) if pos.exit_plan else "_none_"

    await q.message.reply_text(
        f"✎ *Edit Take-Profits — ${pos.symbol}*\n\n"
        f"*Current:*\n{current}\n\n"
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
            pos.exit_plan = json.dumps([{**l, "done": False} for l in plan])
            await s.commit()

    ctx.user_data.clear()
    await update.message.reply_text(
        f"✅ *Take-profits updated!*\n\n{exit_plan_text(plan)}\n\n"
        f"_Fired levels reset._",
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

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        await q.answer("Position not found", show_alert=True)
        return ConversationHandler.END

    ctx.user_data["sl_pos_id"] = pos_id
    current = fmt_mcap(pos.stop_loss) if pos.stop_loss else "not set"

    await q.message.reply_text(
        f"🛑 *Stop Loss — ${pos.symbol}*\n\n"
        f"Current: {current}\n\n"
        f"Send mcap level for alert:\n"
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
                "❌ Wrong format. Examples: `200k`, `1.5m`\nOr `off`.",
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


# ── Продать % позиции ─────────────────────────────────────────────────────────

async def closepos_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    pos_id = int(q.data.split(":")[1])

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos or pos.user_id != q.from_user.id:
        return ConversationHandler.END

    ctx.user_data["close_pos_id"] = pos_id

    data  = await fetch_price(pos.contract)
    cur_x = (data["price"] / pos.entry_price) if data and pos.entry_price else 0
    x_str = f" (now {fmt_x(cur_x)})" if cur_x else ""

    await q.message.reply_text(
        f"💸 *Sell — ${pos.symbol}*{x_str}\n\n"
        f"SOL currently in position: *{fmt_sol(pos.sol_in)}*\n\n"
        f"How much do you want to sell?\n\n"
        f"  `25` — sell 25% of position\n"
        f"  `50` — sell 50%\n"
        f"  `100` — close fully\n\n"
        f"Send a number from 1 to 100.\n_(or /cancel)_",
        parse_mode="Markdown"
    )
    return ST_CLOSE_PCT


async def close_got_pct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text   = update.message.text.strip().replace("%", "")
    pos_id = ctx.user_data.get("close_pos_id")
    uid    = update.effective_user.id

    try:
        pct = float(text.replace(",", "."))
        if not (1 <= pct <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a number from 1 to 100.\n_(or /cancel)_",
            parse_mode="Markdown"
        )
        return ST_CLOSE_PCT

    async with async_session() as s:
        pos = await s.get(Position, pos_id)

    if not pos:
        await update.message.reply_text("Position not found.")
        return ConversationHandler.END

    data  = await fetch_price(pos.contract)
    cur_x = (data["price"] / pos.entry_price) if data and pos.entry_price else 1.0

    sol_selling = pos.sol_in * (pct / 100)
    sol_recv    = sol_selling * cur_x
    pnl_sol     = sol_recv - sol_selling
    pnl_pct     = (cur_x - 1) * 100
    remaining   = pos.sol_in - sol_selling

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        pos.sol_in  = max(0.0, remaining)
        pos.sol_out = (pos.sol_out or 0.0) + sol_recv

        if pct >= 100 or pos.sol_in < 0.001:
            pos.status    = "closed"
            pos.closed_at = datetime.utcnow()
            status_msg    = "_Position fully closed._"
        else:
            status_msg = f"_Remaining in position: {fmt_sol(pos.sol_in)}_"

        s.add(JournalEntry(
            user_id     = uid,
            position_id = pos_id,
            contract    = pos.contract,
            symbol      = pos.symbol,
            sol_in      = sol_selling,
            sol_out     = sol_recv,
            pnl_sol     = pnl_sol,
            pnl_pct     = pnl_pct,
            exit_x      = cur_x,
            note        = f"Manual sell {pct:.0f}%",
        ))
        await s.commit()

    ctx.user_data.clear()
    sign  = "+" if pnl_sol >= 0 else ""
    emoji = "🟢" if pnl_sol >= 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} *Sold {pct:.0f}% of ${pos.symbol}*\n\n"
        f"At: {fmt_x(cur_x)}\n"
        f"SOL sold: {fmt_sol(sol_selling)}\n"
        f"SOL received: ≈{fmt_sol(sol_recv)}\n"
        f"PnL: {sign}{fmt_sol(pnl_sol)} ({sign}{pnl_pct:.1f}%)\n\n"
        f"{status_msg}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Positions", callback_data="do:pos"),
            InlineKeyboardButton("📓 Journal",   callback_data="do:journal"),
            InlineKeyboardButton("◀️ Menu",      callback_data="do:menu"),
        ]])
    )
    return ConversationHandler.END


# ── Alert Done / Skip ─────────────────────────────────────────────────────────

async def alert_done_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
            await q.answer("Already done.", show_alert=True)
            return

        pct      = level["pct"]
        sol_sold = pos.sol_in * (pct / 100)
        sol_recv = sol_sold * x_level

        level["done"] = True
        pos.exit_plan = json.dumps(plan)
        pos.sol_in    = max(0.0, pos.sol_in - sol_sold)
        pos.sol_out   = (pos.sol_out or 0.0) + sol_recv

        s.add(JournalEntry(
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
        ))
        await s.commit()

    try:
        await q.edit_message_text(
            q.message.text +
            f"\n\n✅ *Done!* Sold {pct:.0f}% at {x_level}x\n"
            f"≈ {fmt_sol(sol_recv)} received\n"
            f"PnL this tranche: +{fmt_sol(sol_recv - sol_sold)}\n"
            f"Remaining: {fmt_sol(pos.sol_in)}",
            parse_mode="Markdown", reply_markup=None
        )
    except Exception:
        pass


async def alert_skip_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer("Skipped")
    parts   = q.data.split(":")
    pos_id  = int(parts[1])
    x_level = float(parts[2])

    async with async_session() as s:
        pos = await s.get(Position, pos_id)
        if not pos or pos.user_id != q.from_user.id:
            return
        plan = json.loads(pos.exit_plan) if pos.exit_plan else []
        for l in plan:
            if abs(l.get("x", 0) - x_level) < 0.01:
                l["skipped"] = True
        pos.exit_plan = json.dumps(plan)
        await s.commit()

    try:
        await q.edit_message_text(
            q.message.text + "\n\n⏭ _Skipped._",
            parse_mode="Markdown", reply_markup=None
        )
    except Exception:
        pass


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
