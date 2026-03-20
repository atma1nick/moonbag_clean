"""
Portfolio Snapshot — PnL картинка для шаринга.
"""
import io
import logging
from datetime import datetime
from sqlalchemy import select
from database import async_session
from models import Position, JournalEntry
from services.price import fetch_price, get_cached_sol_price
from utils import fmt_sol, fmt_usd, fmt_x, fmt_mcap, fmt_pct, calc_pnl

log = logging.getLogger(__name__)


async def generate_snapshot(user_id: int) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.error("Pillow not installed")
        return None

    # ── Данные ────────────────────────────────────────────────────────────────
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
    live_positions = []
    live_pnl = 0.0
    for pos in positions:
        data = await fetch_price(pos.contract)
        if data and pos.entry_price and data.get("price"):
            cur_x            = data["price"] / pos.entry_price
            pnl_sol, pnl_pct = calc_pnl(pos.sol_in, cur_x)
            live_pnl        += pnl_sol
            live_positions.append({
                "symbol": pos.symbol,
                "x":      round(cur_x,  2),
                "pnl":    round(pnl_sol, 3),
                "pct":    round(pnl_pct, 1),
            })

    # Журнал статистика
    closed_pnl   = sum(j.pnl_sol for j in journal)
    total_pnl    = closed_pnl + live_pnl
    total_trades = len(journal) + len(positions)
    wins         = sum(1 for j in journal if j.pnl_sol >= 0)
    wins        += sum(1 for p in live_positions if p["pnl"] >= 0)
    winrate      = (wins / total_trades * 100) if total_trades else 0
    best_x       = max(
        [j.exit_x for j in journal] + [p["x"] for p in live_positions],
        default=0
    )

    # ── Рисуем ───────────────────────────────────────────────────────────────
    W, H   = 800, 460
    BG     = (7,  10, 16)
    GREEN  = (0,  232, 122)
    RED    = (255, 53, 83)
    CYAN   = (0,  207, 255)
    WHITE  = (220, 235, 245)
    MUTED  = (62,  84, 112)
    DARK   = (17,  23, 32)
    PURPLE = (153, 69, 255)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Сетка
    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(15, 22, 35), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(15, 22, 35), width=1)

    # Верхняя полоса градиент
    for i in range(4):
        r = int(PURPLE[0] + (CYAN[0] - PURPLE[0]) * i / 3)
        g = int(PURPLE[1] + (CYAN[1] - PURPLE[1]) * i / 3)
        b = int(PURPLE[2] + (CYAN[2] - PURPLE[2]) * i / 3)
        draw.line([(0, i), (W, i)], fill=(r, g, b))

    # Шрифты
    try:
        font_xl   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 56)
        font_lg   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_md   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_sm   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     16)
        font_xs   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     13)
        font_mono = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 15)
    except Exception:
        font_xl = font_lg = font_md = font_sm = font_xs = font_mono = ImageFont.load_default()

    # Лого
    draw.text((28, 18), "🌙", font=font_lg, fill=WHITE)
    draw.text((60, 18), "MoonBag", font=font_lg, fill=WHITE)
    draw.text((60, 48), "Portfolio Snapshot", font=font_xs, fill=MUTED)

    # Дата
    date_str = datetime.utcnow().strftime("%d %b %Y  %H:%M UTC")
    bbox = draw.textbbox((0, 0), date_str, font=font_xs)
    draw.text((W - bbox[2] - 28, 28), date_str, font=font_xs, fill=MUTED)

    # Главный PnL
    pnl_color = GREEN if total_pnl >= 0 else RED
    sign      = "+" if total_pnl >= 0 else ""
    pnl_str   = f"{sign}{total_pnl:.3f} SOL"
    draw.text((28, 85), pnl_str, font=font_xl, fill=pnl_color)

    if sol_price:
        usd_str = f"≈ {sign}${abs(total_pnl * sol_price):,.0f}"
        draw.text((28, 148), usd_str, font=font_lg, fill=pnl_color)

    # Разделитель
    draw.rectangle([(28, 188), (W - 28, 189)], fill=DARK)

    # Стат карточки
    stats = [
        ("WINRATE", f"{winrate:.0f}%",   GREEN if winrate >= 50 else RED),
        ("TRADES",  str(total_trades),    CYAN),
        ("BEST X",  fmt_x(best_x),        GREEN if best_x > 1 else MUTED),
        ("OPEN",    str(len(positions)),   WHITE),
    ]
    card_w = (W - 56) // 4
    for i, (label, val, col) in enumerate(stats):
        x = 28 + i * card_w
        draw.rectangle([(x+2, 196), (x + card_w - 4, 256)], fill=DARK)
        draw.text((x + 10, 202), label, font=font_xs, fill=MUTED)
        draw.text((x + 10, 222), val,   font=font_md, fill=col)

    # Открытые позиции
    if live_positions:
        draw.text((28, 268), "OPEN POSITIONS", font=font_xs, fill=MUTED)
        y = 288
        for p in sorted(live_positions, key=lambda x: x["x"], reverse=True)[:5]:
            col   = GREEN if p["pnl"] >= 0 else RED
            sign_p = "+" if p["pnl"] >= 0 else ""
            row   = f"${p['symbol']:<10} {p['x']:.2f}x    {sign_p}{p['pnl']:.3f} SOL   ({sign_p}{p['pct']:.1f}%)"
            draw.text((28, y), row, font=font_mono, fill=col)
            y += 22
            if y > H - 50:
                break

    # Нижняя полоса и watermark
    draw.rectangle([(0, H - 3), (W, H)], fill=CYAN)
    draw.text((28, H - 22), "@MoonBagBot", font=font_xs, fill=MUTED)

    # Сохраняем
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()