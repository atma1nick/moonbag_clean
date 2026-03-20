from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select, func
from config import ADMIN_ID
from database import async_session
from models import User, Position


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    async with async_session() as s:
        users     = (await s.execute(select(func.count(User.user_id)))).scalar()
        positions = (await s.execute(select(func.count(Position.id))
                     .where(Position.status == "active"))).scalar()

    await update.message.reply_text(
        f"🔧 *Admin Panel*\n\n"
        f"Users: {users}\n"
        f"Active positions: {positions}\n\n"
        f"Commands:\n"
        f"/grant\\_pro USER\\_ID DAYS — grant Pro\n"
        f"/broadcast TEXT — send to all users",
        parse_mode="Markdown"
    )


async def cmd_grant_pro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /grant_pro USER_ID DAYS")
        return
    try:
        target_uid = int(args[0])
        days       = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid args.")
        return

    from datetime import datetime, timedelta
    async with async_session() as s:
        u = await s.get(User, target_uid)
        if not u:
            await update.message.reply_text("User not found.")
            return
        u.is_pro    = True
        u.pro_until = datetime.utcnow() + timedelta(days=days)
        await s.commit()

    await update.message.reply_text(f"✅ Pro granted to {target_uid} for {days} days.")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = " ".join(ctx.args) if ctx.args else None
    if not text:
        await update.message.reply_text("Usage: /broadcast YOUR MESSAGE")
        return

    async with async_session() as s:
        result = await s.execute(select(User))
        users  = result.scalars().all()

    sent = 0
    for u in users:
        try:
            await ctx.bot.send_message(u.user_id, f"📢 {text}")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ Sent to {sent}/{len(users)} users.")
