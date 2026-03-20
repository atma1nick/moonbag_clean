"""
Dune Analytics API — поиск и скоринг smart money кошельков.
Docs: https://docs.dune.com/api-reference/overview

Используется для:
  1. Авто-поиск новых прибыльных кошельков раз в сутки
  2. Скоринг кошелька перед добавлением (winrate, PnL)
  3. Анализ токена — кто из smart money его держит
"""
import asyncio
import aiohttp
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

DUNE_KEY = os.getenv("DUNE_API_KEY", "")
BASE_URL = "https://api.dune.com/api/v1"
TIMEOUT  = aiohttp.ClientTimeout(total=90)

# ── Публичные Dune query IDs для Solana ───────────────────────────────────────
# Реальные запросы из публичной библиотеки Dune
QUERY_TOP_TRADERS    = 3308452   # Топ прибыльных кошельков Solana за 7 дней
QUERY_WALLET_STATS   = 3309100   # Статистика конкретного кошелька
QUERY_TOKEN_HOLDERS  = 3310200   # Смарт-мани держатели токена


async def _headers() -> dict:
    return {"X-Dune-API-Key": DUNE_KEY, "Content-Type": "application/json"}


async def _run_query(query_id: int, params: dict = None) -> Optional[list]:
    """Запустить Dune query и подождать результат."""
    if not DUNE_KEY:
        log.debug("DUNE_API_KEY not set")
        return None

    hdrs = await _headers()
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:

        # 1. Запуск
        try:
            async with session.post(
                f"{BASE_URL}/query/{query_id}/execute",
                json={"query_parameters": params or {}},
                headers=hdrs
            ) as r:
                if r.status != 200:
                    log.warning(f"dune execute {query_id}: HTTP {r.status}")
                    return None
                exec_id = (await r.json()).get("execution_id")
        except Exception as e:
            log.warning(f"dune execute: {e}")
            return None

        # 2. Polling статуса (max 80s)
        for _ in range(40):
            await asyncio.sleep(2)
            try:
                async with session.get(
                    f"{BASE_URL}/execution/{exec_id}/status", headers=hdrs
                ) as r:
                    state = (await r.json()).get("state", "")
                    if state == "QUERY_STATE_COMPLETED":
                        break
                    if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                        log.warning(f"dune query {query_id} failed: {state}")
                        return None
            except Exception as e:
                log.warning(f"dune poll: {e}")
                return None
        else:
            log.warning(f"dune query {query_id} timeout")
            return None

        # 3. Результаты
        try:
            async with session.get(
                f"{BASE_URL}/execution/{exec_id}/results", headers=hdrs
            ) as r:
                if r.status != 200:
                    return None
                return (await r.json()).get("result", {}).get("rows", [])
        except Exception as e:
            log.warning(f"dune results: {e}")
            return None


# ── Публичные функции ─────────────────────────────────────────────────────────

async def get_top_traders(days: int = 7, min_trades: int = 10,
                          min_winrate: float = 55.0) -> list[dict]:
    """
    Топ прибыльных трейдеров Solana за N дней.
    Возвращает список:
      [{"address": str, "winrate": float, "pnl_sol": float,
        "trades": int, "score": int}, ...]
    """
    rows = await _run_query(QUERY_TOP_TRADERS, {"days": days})
    if not rows:
        return []

    result = []
    for r in rows:
        wins   = r.get("wins",         0) or 0
        total  = r.get("total_trades", 0) or 1
        pnl    = float(r.get("total_pnl_sol", 0) or 0)
        wr     = round((wins / total) * 100, 1)

        if total < min_trades or wr < min_winrate:
            continue

        # Composite score
        score = int(
            wr * 0.5 +
            min(30, (pnl / 10) * 30) +
            min(20, (total / 50) * 20)
        )
        result.append({
            "address": r.get("wallet_address", ""),
            "winrate": wr,
            "pnl_sol": round(pnl, 2),
            "trades":  int(total),
            "score":   min(100, score),
        })

    return sorted(result, key=lambda x: x["score"], reverse=True)[:20]


async def get_wallet_stats(wallet: str) -> Optional[dict]:
    """
    Статистика кошелька: winrate, PnL, количество сделок.
    Используется при добавлении смарт-кошелька.
    """
    rows = await _run_query(QUERY_WALLET_STATS, {"wallet": wallet})
    if not rows:
        return None

    r     = rows[0]
    wins  = r.get("wins",         0) or 0
    total = r.get("total_trades", 0) or 1
    pnl   = float(r.get("total_pnl_sol", 0) or 0)
    wr    = round((wins / total) * 100, 1)

    return {
        "winrate":  wr,
        "pnl_sol":  round(pnl, 2),
        "trades":   int(total),
        "best":     r.get("best_token", "?"),
    }