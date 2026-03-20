"""
Smart Wallet Auto-Discovery.

Источники (в порядке приоритета):
1. GMGN leaderboard — топ прибыльных трейдеров Solana за 7 дней
2. Внутренняя аналитика — winrate из наших smart_wallet_txs
3. Очистка неактивных — кошельки без активности 30+ дней

Запускается раз в 24 часа через dune_loop.py (переименуем в discovery_loop.py)
"""
import aiohttp
import logging
from datetime import datetime, timedelta
from sqlalchemy import select, func
from database import async_session
from models import SmartWallet, SmartWalletTx

log = logging.getLogger(__name__)

GMGN_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ── GMGN Leaderboard ──────────────────────────────────────────────────────────

async def fetch_gmgn_top_wallets(period: str = "7d", limit: int = 20) -> list[dict]:
    """
    Получить топ прибыльных кошельков с GMGN.
    period: "1d" | "7d" | "30d"
    Возвращает: [{"address": str, "pnl": float, "winrate": float, "trades": int}, ...]
    """
    url = (f"https://gmgn.ai/defi/quotation/v1/rank/sol/wallets/{period}"
           f"?orderby=pnl&direction=desc&limit={limit}")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept":     "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=GMGN_TIMEOUT) as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    log.warning(f"gmgn leaderboard: HTTP {r.status}")
                    return []
                data = await r.json()

        wallets = data.get("data", {}).get("rank", []) or []
        result  = []
        for w in wallets:
            addr = w.get("wallet_address", "")
            if not addr or len(addr) < 32:
                continue
            pnl     = float(w.get("realized_profit", 0) or 0)
            winrate = float(w.get("winrate", 0) or 0) * 100
            trades  = int(w.get("buy_30d", 0) or 0)

            if pnl > 0 and winrate > 50 and trades >= 5:
                result.append({
                    "address": addr,
                    "pnl_sol": round(pnl, 2),
                    "winrate": round(winrate, 1),
                    "trades":  trades,
                    "source":  "gmgn",
                })

        log.info(f"gmgn: fetched {len(result)} qualifying wallets")
        return result

    except Exception as e:
        log.warning(f"gmgn fetch: {e}")
        return []


# ── Внутренняя аналитика ──────────────────────────────────────────────────────

async def score_tracked_wallets(user_id: int) -> list[dict]:
    """
    Считаем winrate наших tracked кошельков по собственным данным.
    Кошелёк "успешный" если токены которые он купил выросли.
    Возвращаем отсортированный список с актуальным winrate.
    """
    from models import Position
    from services.price import fetch_price

    async with async_session() as s:
        wallets = (await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user_id)
        )).scalars().all()

    result = []
    for sw in wallets:
        # Все покупки этого кошелька
        async with async_session() as s:
            txs = (await s.execute(
                select(SmartWalletTx).where(
                    SmartWalletTx.user_id == user_id,
                    SmartWalletTx.address == sw.address,
                    SmartWalletTx.action  == "buy",
                )
            )).scalars().all()

        if len(txs) < 3:
            result.append({
                "address":    sw.address,
                "label":      sw.label,
                "winrate":    0,
                "trades":     len(txs),
                "last_active": sw.added_at,
                "status":     "insufficient_data",
            })
            continue

        # Считаем winrate — проверяем текущую цену токенов
        wins    = 0
        checked = 0
        last_tx = max(t.seen_at for t in txs)

        # Берём последние 10 уникальных токенов
        seen_contracts = set()
        for tx in sorted(txs, key=lambda x: x.seen_at, reverse=True):
            if tx.contract in seen_contracts:
                continue
            seen_contracts.add(tx.contract)
            if len(seen_contracts) > 10:
                break

            # Есть ли у нас позиция по этому токену? — значит знаем цену входа
            async with async_session() as s:
                pos = (await s.execute(
                    select(Position).where(
                        Position.user_id == user_id,
                        Position.contract == tx.contract,
                    )
                )).scalars().first()

            if pos and pos.entry_price:
                data = await fetch_price(tx.contract)
                if data and data.get("price"):
                    cur_x = data["price"] / pos.entry_price
                    if cur_x > 1.2:  # вырос на 20%+ = win
                        wins += 1
                    checked += 1

        winrate = (wins / checked * 100) if checked > 0 else 0

        result.append({
            "address":     sw.address,
            "label":       sw.label,
            "winrate":     round(winrate, 1),
            "trades":      len(txs),
            "last_active": last_tx,
            "status":      "active" if last_tx > datetime.utcnow() - timedelta(days=14) else "inactive",
        })

    return sorted(result, key=lambda x: x["winrate"], reverse=True)


# ── Поиск новых кошельков для конкретного пользователя ───────────────────────

async def discover_new_wallets(user_id: int) -> list[dict]:
    """
    Находит новые кошельки которых нет в списке пользователя.
    Комбинирует GMGN + фильтр по уже добавленным.
    """
    # Уже добавленные
    async with async_session() as s:
        existing = (await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user_id)
        )).scalars().all()
    known = {w.address for w in existing}

    # Топ с GMGN
    top = await fetch_gmgn_top_wallets(period="7d", limit=30)

    # Фильтруем новые
    new_wallets = [w for w in top if w["address"] not in known][:5]
    return new_wallets


# ── Очистка неактивных ────────────────────────────────────────────────────────

async def get_inactive_wallets(user_id: int, days: int = 30) -> list[dict]:
    """
    Кошельки без активности N дней — кандидаты на удаление.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with async_session() as s:
        wallets = (await s.execute(
            select(SmartWallet).where(SmartWallet.user_id == user_id)
        )).scalars().all()

    inactive = []
    for sw in wallets:
        async with async_session() as s:
            last_tx = (await s.execute(
                select(func.max(SmartWalletTx.seen_at)).where(
                    SmartWalletTx.user_id == user_id,
                    SmartWalletTx.address == sw.address,
                )
            )).scalar()

        # Нет транзакций вообще или последняя была давно
        if not last_tx or last_tx < cutoff:
            inactive.append({
                "id":          sw.id,
                "address":     sw.address,
                "label":       sw.label or sw.address[:12] + "...",
                "last_active": last_tx,
                "days_silent": (datetime.utcnow() - last_tx).days if last_tx else None,
            })

    return inactive