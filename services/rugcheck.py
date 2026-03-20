"""
RugCheck.xyz API — проверка токена на риски.
Публичный API, не требует ключа.
Docs: https://api.rugcheck.xyz/swagger/index.html

Что проверяем:
  - Mint authority (команда может допечатать токены)
  - Freeze authority (команда может заморозить твой аккаунт)
  - LP locked (ликвидность заблокирована или нет)
  - Top holders concentration (концентрация у топ-холдеров)
  - Insider wallets (инсайдеры держат большой % supply)
  - Known scam patterns
"""
import aiohttp
import logging

log = logging.getLogger(__name__)
BASE_URL = "https://api.rugcheck.xyz/v1"
TIMEOUT  = aiohttp.ClientTimeout(total=15)


async def check_token(contract: str) -> dict | None:
    """
    Возвращает dict с результатами проверки или None при ошибке.
    {
      "score":        int,        # 0=risky, 100=safe (RugCheck score)
      "rating":       str,        # "Good" | "Warning" | "Danger"
      "risks":        list[str],  # список найденных рисков
      "mint_auth":    bool,       # True = опасно (можно допечатать)
      "freeze_auth":  bool,       # True = опасно (можно заморозить)
      "lp_locked":    bool,       # True = хорошо
      "lp_locked_pct":float,      # % заблокированной ликвидности
      "top10_pct":    float,      # % supply у топ-10 холдеров
      "insider_pct":  float,      # % у инсайдеров
      "link":         str,        # ссылка на rugcheck
    }
    """
    url = f"{BASE_URL}/tokens/{contract}/report/summary"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status == 404:
                    log.debug(f"rugcheck 404: {contract[:8]}")
                    return None
                if r.status != 200:
                    log.warning(f"rugcheck {r.status}: {contract[:8]}")
                    return None
                data = await r.json()
    except Exception as e:
        log.warning(f"rugcheck fetch: {e}")
        return None

    return _parse(data, contract)


async def check_token_full(contract: str) -> dict | None:
    """Полный отчёт — медленнее но больше деталей."""
    url = f"{BASE_URL}/tokens/{contract}/report"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
    except Exception as e:
        log.warning(f"rugcheck full: {e}")
        return None

    return _parse(data, contract)


def _parse(data: dict, contract: str) -> dict:
    score  = data.get("score", 0)
    risks  = data.get("risks", []) or []
    risks  = [r if isinstance(r, str) else r.get("description", str(r)) for r in risks]

    # Парсим токеномику
    token_meta = data.get("token", {}) or {}
    mint_auth  = bool(token_meta.get("mintAuthority"))
    freeze_auth= bool(token_meta.get("freezeAuthority"))

    # LP
    markets     = data.get("markets", []) or []
    lp_locked   = False
    lp_locked_pct = 0.0
    for m in markets:
        lp = m.get("lp", {}) or {}
        if lp.get("lpLockedPct", 0) > 0:
            lp_locked     = True
            lp_locked_pct = max(lp_locked_pct, float(lp.get("lpLockedPct", 0)))

    # Топ холдеры
    top_holders = data.get("topHolders", []) or []
    top10_pct   = sum(float(h.get("pct", 0)) for h in top_holders[:10])

    # Инсайдеры
    insider_pct = float(data.get("insiderNetworkPercentage", 0) or 0)

    # Рейтинг
    if score >= 80:
        rating = "Good ✅"
    elif score >= 50:
        rating = "Warning ⚠️"
    else:
        rating = "Danger 🚨"

    # Добавляем авто-риски если API не вернул
    if mint_auth and "Mint authority not revoked" not in " ".join(risks):
        risks.insert(0, "Mint authority not revoked — team can print tokens")
    if freeze_auth and "Freeze authority" not in " ".join(risks):
        risks.insert(0, "Freeze authority active — team can freeze your account")
    if not lp_locked and "LP not locked" not in " ".join(risks):
        risks.append("LP not locked — liquidity can be removed anytime")
    if top10_pct > 50:
        risks.append(f"Top 10 holders own {top10_pct:.0f}% of supply")
    if insider_pct > 20:
        risks.append(f"Insider wallets hold {insider_pct:.0f}% of supply")

    return {
        "score":        score,
        "rating":       rating,
        "risks":        risks[:8],   # максимум 8 рисков в сообщении
        "mint_auth":    mint_auth,
        "freeze_auth":  freeze_auth,
        "lp_locked":    lp_locked,
        "lp_locked_pct":lp_locked_pct,
        "top10_pct":    top10_pct,
        "insider_pct":  insider_pct,
        "link":         f"https://rugcheck.xyz/tokens/{contract}",
    }


def format_rugcheck(result: dict, symbol: str = "") -> str:
    """Форматирует результат для отправки в Telegram."""
    sym    = f" — ${symbol}" if symbol else ""
    score  = result["score"]
    rating = result["rating"]
    risks  = result["risks"]

    # Прогресс-бар безопасности
    bars   = int(score / 10)
    bar    = "█" * bars + "░" * (10 - bars)
    color  = "🟢" if score >= 80 else ("🟡" if score >= 50 else "🔴")

    lines = [
        f"🔍 *RugCheck{sym}*",
        f"{color} Score: *{score}/100* [{bar}]",
        f"Rating: *{rating}*",
        "",
    ]

    # Ключевые метрики
    lines.append("*Security checks:*")
    lines.append(f"{'❌' if result['mint_auth']   else '✅'} Mint authority {'NOT revoked' if result['mint_auth'] else 'revoked'}")
    lines.append(f"{'❌' if result['freeze_auth'] else '✅'} Freeze authority {'active' if result['freeze_auth'] else 'disabled'}")

    if result["lp_locked"]:
        lines.append(f"✅ LP locked ({result['lp_locked_pct']:.0f}%)")
    else:
        lines.append("❌ LP not locked")

    if result["top10_pct"] > 0:
        icon = "⚠️" if result["top10_pct"] > 50 else "✅"
        lines.append(f"{icon} Top 10 holders: {result['top10_pct']:.0f}% supply")

    if result["insider_pct"] > 0:
        icon = "⚠️" if result["insider_pct"] > 20 else "ℹ️"
        lines.append(f"{icon} Insider wallets: {result['insider_pct']:.0f}%")

    # Риски
    if risks:
        lines.append("")
        lines.append("*⚠️ Risks found:*")
        for r in risks:
            lines.append(f"  • {r}")

    lines.append(f"\n[Full report]({result['link']})")
    return "\n".join(lines)