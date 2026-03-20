import aiohttp
import logging

log = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=10)
SOL_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"
_sol_usd: float = 0.0  # кэш цены SOL


async def fetch_sol_price() -> float:
    """Получить текущую цену SOL в USD. Кэшируется глобально."""
    global _sol_usd
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(SOL_PRICE_URL) as r:
                if r.status == 200:
                    data = await r.json()
                    _sol_usd = data.get("solana", {}).get("usd", _sol_usd)
    except Exception as e:
        log.debug(f"sol price: {e}")
    return _sol_usd


def get_cached_sol_price() -> float:
    return _sol_usd


async def fetch_price(contract: str) -> dict | None:
    """
    Returns {price, mcap, name, symbol, volume24h, liquidity, price_change_5m, price_change_1h, price_change_24h}
    or None on failure.
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        # Берём пару с наибольшей ликвидностью
        pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)

        price       = float(pair.get("priceUsd") or 0)
        mcap        = float(pair.get("fdv") or pair.get("marketCap") or 0)
        liq         = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        vol24       = float(pair.get("volume", {}).get("h24", 0) or 0)
        pc          = pair.get("priceChange", {})
        name        = pair.get("baseToken", {}).get("name",   "Unknown")
        symbol      = pair.get("baseToken", {}).get("symbol", "???")

        if not price:
            return None

        return {
            "price":           price,
            "mcap":            mcap,
            "name":            name,
            "symbol":          symbol,
            "liquidity":       liq,
            "volume24h":       vol24,
            "price_change_5m": float(pc.get("m5",  0) or 0),
            "price_change_1h": float(pc.get("h1",  0) or 0),
            "price_change_24h":float(pc.get("h24", 0) or 0),
        }
    except Exception as e:
        log.warning(f"fetch_price {contract}: {e}")
        return None
