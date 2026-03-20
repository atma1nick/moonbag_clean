"""
Portfolio Snapshot — генерация PnL картинки для шаринга в Twitter/X.
Использует Pillow. Стиль: тёмный терминал, зелёные цифры.

Вызов: image_bytes = await generate_snapshot(user_id)
Затем: await bot.send_photo(chat_id, image_bytes)
"""
import io
import logging
from datetime import datetime
from sqlalchemy import select
from database import async_session
from models import Position, JournalEntry
from services.price import fetch_price, get_cached_sol_price
from utils import fmt_sol, fmt_usd, fmt_x, fmt_mcap, fmt_pct

log = logging.getLogger(__name__)


async def generate_snapshot(user_id: int) -> bytes | None:
    """Генерирует PNG картинку с PnL. Возвращает bytes или None."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.error("Pillow not installed. Run: pip install Pillow")
        return None

    # ── Собираем данные ───────────────────────────────────────────────────
    async with async_session() as s:
        positions = (await s.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.status  == "active"
            )
        )).scalars().all()

        journal = (await s.execute(
            select(JournalEntry).where(JournalEntry.user_id == user_id)
        )).scalars().all()

    sol_price = get_cached_sol_price() or 0

    # Live PnL по открытым позициям
    live_pnl = 0.0
    live_data = []
    for pos in positions:
        data = await fetch_price(pos.contract)
        if data and pos.entry_price:
            cur_x   = data["price"] / pos.entry_price
            pnl_sol = pos.sol_in * (cur_x - 1)
            live_pnl += pnl_sol
            live_data.append({
                "symbol": pos.symbol,
                "x":      cur_x,
                "pnl":    pnl_sol,
            })

    # Статистика из журнала
    total_trades = len(journal)
    wins         = sum(1 for j in journal if j.pnl_sol >= 0)
    total_pnl    = sum(j.pnl_sol for j in journal) + live_pnl
    winrate      = (wins / total_trades * 100) if total_trades else 0
    best_x       = max((j.exit_x for j in journal), default=0)

    # ── Рисуем картинку ───────────────────────────────────────────────────
    W, H    = 800, 480
    BG      = (7,  10, 16)
    GREEN   = (0,  232, 122)
    RED     = (255, 53, 83)
    CYAN    = (0,  207, 255)
    WHITE   = (220, 235, 245)
    MUTED   = (62,  84, 112)
    SURFACE = (17,  23, 32)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Фоновая сетка
    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(20, 28, 40), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(20, 28, 40), width=1)

    # Верхняя полоса
    draw.rectangle([(0, 0), (W, 4)], fill=GREEN)

    # Шрифты (системный fallback)
    try:
        font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_sm    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     18)
        font_tiny  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     14)
        font_mono  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 16)
    except Exception:
        font_big  = ImageFont.load_default()
        font_med  = font_sm = font_tiny = font_mono = font_big

    # Лого
    draw.text((32, 20), "🌙 MoonBag", font=font_med, fill=WHITE)
    draw.text((32, 52), "Portfolio Snapshot", font=font_sm, fill=MUTED)

    # Дата
    date_str = datetime.utcnow().strftime("%d %b %Y  %H:%M UTC")
    draw.text((W - 220, 30), date_str, font=font_tiny, fill=MUTED)

    # Главный PnL
    pnl_color = GREEN if total_pnl >= 0 else RED
    sign      = "+" if total_pnl >= 0 else ""
    pnl_sol_str = f"{sign}{total_pnl:.3f} SOL"
    pnl_usd_str = f"≈ {sign}${total_pnl * sol_price:,.0f}" if sol_price else ""

    draw.text((32, 100), pnl_sol_str, font=font_big, fill=pnl_color)
    draw.text((32, 158), pnl_usd_str, font=font_med, fill=pnl_color)

    # Разделитель
    draw.rectangle([(32, 200), (W - 32, 202)], fill=SURFACE)

    # Статы (три колонки)
    stats = [
        ("WINRATE",  f"{winrate:.0f}%",      GREEN if winrate >= 50 else RED),
        ("TRADES",   str(total_trades),       CYAN),
        ("BEST EXIT", fmt_x(best_x),          GREEN if best_x > 1 else MUTED),
    ]
    col_w = (W - 64) // 3
    for i, (label, value, color) in enumerate(stats):
        x = 32 + i * col_w
        draw.text((x, 215), label, font=font_tiny, fill=MUTED)
        draw.text((x, 235), value, font=font_med,  fill=color)

    # Открытые позиции
    draw.text((32, 290), "OPEN POSITIONS", font=font_tiny, fill=MUTED)
    y_pos = 312
    for item in sorted(live_data, key=lambda x: x["x"], reverse=True)[:4]:
        color  = GREEN if item["pnl"] >= 0 else RED
        sign_p = "+" if item["pnl"] >= 0 else ""
        txt    = f"${item['symbol']:<8}  {fmt_x(item['x']):<8}  {sign_p}{fmt_sol(item['pnl'], 3)}"
        draw.text((32, y_pos), txt, font=font_mono, fill=color)
        y_pos += 24
        if y_pos > H - 60:
            break

    # Watermark
    draw.text((32, H - 28), "t.me/MoonBagBot", font=font_tiny, fill=MUTED)
    draw.text((W - 180, H - 28), "moonbag.app", font=font_tiny, fill=MUTED)

    # Нижняя полоса
    draw.rectangle([(0, H - 4), (W, H)], fill=CYAN)

    # Конвертируем в bytes
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()