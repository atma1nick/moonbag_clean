import aiohttp
import logging
import os

log = logging.getLogger(__name__)
HELIUS_KEY = os.getenv("HELIUS_KEY", "")
TIMEOUT    = aiohttp.ClientTimeout(total=12)


def _rpc_url() -> str:
    return f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"


async def fetch_wallet_txs(wallet: str, limit: int = 10) -> list:
    """Получить последние SWAP транзакции кошелька."""
    if not HELIUS_KEY:
        return []
    url = (f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
           f"?api-key={HELIUS_KEY}&limit={limit}&type=SWAP")
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return []
                return await r.json()
    except Exception as e:
        log.warning(f"helius wallet_txs {wallet[:8]}: {e}")
        return []


async def fetch_token_top_holders(contract: str) -> list:
    if not HELIUS_KEY:
        return []
    payload = {"jsonrpc": "2.0", "id": 1,
               "method": "getTokenLargestAccounts", "params": [contract]}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(_rpc_url(), json=payload) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        return data.get("result", {}).get("value", [])[:10]
    except Exception as e:
        log.warning(f"helius top_holders: {e}")
        return []


async def fetch_token_metadata(contract: str) -> dict | None:
    """Получить метаданные токена через Helius DAS API."""
    if not HELIUS_KEY:
        return None
    payload = {"jsonrpc": "2.0", "id": 1,
               "method": "getAsset", "params": {"id": contract}}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(_rpc_url(), json=payload) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        result = data.get("result", {})
        content = result.get("content", {})
        meta    = content.get("metadata", {})
        return {
            "name":   meta.get("name", "Unknown"),
            "symbol": meta.get("symbol", "???"),
            "image":  content.get("links", {}).get("image", ""),
        }
    except Exception as e:
        log.warning(f"helius metadata {contract[:8]}: {e}")
        return None
