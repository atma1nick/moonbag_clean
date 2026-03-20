"""
Helius WebSocket — мгновенные алерты вместо polling каждые 90s.
Подключается к Helius Geyser Enhanced WebSocket.
При разрыве — авто-реконнект, fallback на wallet_loop polling.

Как работает:
  1. При старте подписываемся на все tracked кошельки
  2. Helius пушит транзакцию в реальном времени (< 1s)
  3. Парсим → вызываем ту же логику что и wallet_loop
  4. Каждые 5 минут проверяем новые кошельки → досписываемся
"""
import asyncio
import json
import logging
import os
from sqlalchemy import select
from database import async_session
from models import User, SmartWallet

log = logging.getLogger(__name__)

HELIUS_KEY    = os.getenv("HELIUS_KEY", "")
WS_URL        = f"wss://atlas-mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
SOL_MINT      = "So11111111111111111111111111111111111111112"
RECONNECT_SEC = 5
MAX_RECONNECTS= 999


class SubscriptionManager:
    """Хранит маппинг wallet → subscription_id."""
    def __init__(self):
        self.ws               = None
        self.subs: dict       = {}     # wallet → sub_id
        self.pending: dict    = {}     # req_id → wallet
        self._req_id          = 0

    def next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def subscribe(self, wallet: str):
        if not self.ws or wallet in self.subs:
            return
        req_id = self.next_id()
        self.pending[req_id] = wallet
        msg = {
            "jsonrpc": "2.0",
            "id":      req_id,
            "method":  "transactionSubscribe",
            "params": [
                {
                    "vote":           False,
                    "failed":         False,
                    "accountInclude": [wallet],
                },
                {
                    "commitment":                     "confirmed",
                    "encoding":                       "jsonParsed",
                    "transactionDetails":             "full",
                    "maxSupportedTransactionVersion": 0,
                }
            ]
        }
        try:
            await self.ws.send(json.dumps(msg))
        except Exception as e:
            log.warning(f"ws subscribe {wallet[:8]}: {e}")
            self.pending.pop(req_id, None)

    async def unsubscribe_all(self):
        self.subs.clear()
        self.pending.clear()


_mgr = SubscriptionManager()


async def _load_wallets() -> list[str]:
    """Все кошельки которые нужно отслеживать."""
    wallets = set()
    async with async_session() as s:
        # Личные кошельки (авто-трекинг)
        users = (await s.execute(
            select(User).where(User.mode == "wallet", User.wallet.isnot(None))
        )).scalars().all()
        for u in users:
            if u.wallet:
                wallets.add(u.wallet)

        # Смарт-кошельки
        sws = (await s.execute(select(SmartWallet))).scalars().all()
        for sw in sws:
            wallets.add(sw.address)

    return list(wallets)


async def _process_tx(tx_data: dict, bot):
    """Парсим транзакцию и вызываем логику wallet_loop."""
    try:
        # Определяем адрес кошелька из account keys
        msg_accounts = (tx_data.get("transaction", {})
                               .get("message", {})
                               .get("accountKeys", []))
        if not msg_accounts:
            return

        # Первый signer = инициатор транзакции
        wallet = None
        for ak in msg_accounts:
            key    = ak if isinstance(ak, str) else ak.get("pubkey", "")
            signer = True if isinstance(ak, str) else ak.get("signer", False)
            if key and signer:
                wallet = key
                break

        if not wallet:
            return

        # Проверяем: это наш tracked кошелёк?
        async with async_session() as s:
            user_result = (await s.execute(
                select(User).where(User.wallet == wallet, User.mode == "wallet")
            )).scalars().first()

            sw_result = (await s.execute(
                select(SmartWallet).where(SmartWallet.address == wallet)
            )).scalars().all()

        if not user_result and not sw_result:
            return

        # Передаём в wallet_loop для обработки
        # Формируем упрощённый tx объект совместимый с парсерами
        meta = tx_data.get("meta", {})
        simplified_tx = {
            "signature":      tx_data.get("signature", ""),
            "tokenTransfers": _extract_token_transfers(tx_data),
            "nativeTransfers": _extract_native_transfers(tx_data, meta),
        }

        from loops.wallet_loop import (
            _tx_seen, _mark_seen, _bought_mint, _sold_mint,
            _sol_spent, _sol_received,
        )

        sig = simplified_tx["signature"]
        if not sig or await _tx_seen(sig):
            return
        await _mark_seen(sig)

        # Личный кошелёк
        if user_result:
            from loops.wallet_loop import _process_personal_wallet
            # Делаем временный объект user с одной транзакцией
            # (переиспользуем логику напрямую)
            mint    = _bought_mint(simplified_tx, wallet)
            is_sell = False
            if not mint:
                mint    = _sold_mint(simplified_tx, wallet)
                is_sell = True

            if mint:
                from services.price import fetch_price, get_cached_sol_price
                data = await fetch_price(mint)
                if data:
                    sol = (_sol_spent(simplified_tx, wallet) if not is_sell
                           else _sol_received(simplified_tx, wallet))
                    sol_price = get_cached_sol_price()
                    currency  = user_result.currency or "SOL"

                    if not is_sell:
                        from sqlalchemy import select as sel
                        from models import Position
                        async with async_session() as s:
                            existing = (await s.execute(
                                sel(Position).where(
                                    Position.user_id  == user_result.user_id,
                                    Position.contract == mint,
                                    Position.status   == "active"
                                )
                            )).scalars().first()

                        if existing:
                            from loops.wallet_loop import _send_dca_alert
                            await _send_dca_alert(bot, user_result, mint, data,
                                                  sol, currency, sol_price)
                        else:
                            from loops.wallet_loop import _get_user_plan
                            import json as _json
                            from models import Position as Pos
                            plan = _get_user_plan(user_result)
                            async with async_session() as s:
                                pos = Pos(
                                    user_id     = user_result.user_id,
                                    contract    = mint,
                                    symbol      = data["symbol"],
                                    name        = data["name"],
                                    entry_price = data["price"],
                                    entry_mcap  = data["mcap"],
                                    sol_in      = sol or 0.5,
                                    exit_plan   = _json.dumps(plan),
                                    source      = "wallet",
                                    status      = "active",
                                )
                                s.add(pos)
                                await s.commit()
                                await s.refresh(pos)
                                pos_id = pos.id

                            from loops.wallet_loop import _send_buy_alert
                            await _send_buy_alert(bot, user_result, pos_id, mint,
                                                  data, sol, plan, currency, sol_price)
                    else:
                        from loops.wallet_loop import _handle_wallet_sell
                        await _handle_wallet_sell(bot, user_result, mint, data,
                                                  sol, currency, sol_price)

        # Смарт-кошельки
        for sw in sw_result:
            mint = _bought_mint(simplified_tx, wallet)
            if not mint:
                continue
            sol = _sol_spent(simplified_tx, wallet)

            from models import SmartWalletTx
            async with async_session() as s:
                try:
                    s.add(SmartWalletTx(
                        user_id    = sw.user_id,
                        address    = sw.address,
                        contract   = mint,
                        label      = sw.label,
                        action     = "buy",
                        sol_amount = sol,
                        tx_sig     = sig,
                    ))
                    await s.commit()
                except Exception:
                    pass

            # Bundle check запускаем асинхронно
            asyncio.create_task(_bundle_check(bot, sw.user_id, mint))

        log.debug(f"ws tx processed: {wallet[:8]} sig={sig[:12]}")

    except Exception as e:
        log.error(f"ws _process_tx: {e}", exc_info=True)


async def _bundle_check(bot, user_id: int, mint: str):
    """Быстрый bundle check после каждой смарт-покупки."""
    from datetime import datetime, timedelta
    from loops.wallet_loop import _was_alerted, _mark_alerted
    from services.price import fetch_price
    from utils import fmt_mcap, dexscreener
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    from loops.wallet_loop import BUNDLE_THRESHOLD, BUNDLE_WINDOW

    cutoff = datetime.utcnow() - timedelta(minutes=BUNDLE_WINDOW)
    async with async_session() as s:
        from models import SmartWalletTx
        result = await s.execute(
            select(SmartWalletTx).where(
                SmartWalletTx.user_id  == user_id,
                SmartWalletTx.contract == mint,
                SmartWalletTx.action   == "buy",
                SmartWalletTx.seen_at  >= cutoff,
            )
        )
        recent = result.scalars().all()

    unique = list({r.address: r for r in recent}.values())
    if len(unique) < BUNDLE_THRESHOLD:
        return

    key = f"bundle:{user_id}:{mint}"
    if await _was_alerted(key):
        return
    await _mark_alerted(user_id, key)

    data   = await fetch_price(mint)
    mcap   = fmt_mcap(data["mcap"]) if data else "?"
    symbol = data["symbol"] if data else "???"
    name   = data["name"] if data else mint[:8]
    liq    = fmt_mcap(data["liquidity"]) if data else "?"

    lines = "\n".join(
        f"  🧠 {w.label or w.address[:8]+'...'} — {w.sol_amount:.2f} SOL"
        for w in unique[:5]
    )
    try:
        await bot.send_message(
            user_id,
            f"🚨 *BUNDLE — {name}* (${symbol})\n\n"
            f"*{len(unique)} wallets* in {BUNDLE_WINDOW}min!\n\n"
            f"📊 Mcap: {mcap}  |  Liq: {liq}\n"
            f"`{mint}`\n\n{lines}\n\n"
            f"[Chart]({dexscreener(mint)})",
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add Position", callback_data=f"quickadd:{mint}"),
                InlineKeyboardButton("📈 Chart",        url=dexscreener(mint)),
            ]])
        )
    except Exception as e:
        log.error(f"bundle send: {e}")


def _extract_token_transfers(tx_data: dict) -> list:
    meta = tx_data.get("meta", {})
    transfers = []
    pre  = {b["accountIndex"]: b for b in meta.get("preTokenBalances",  [])}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])}

    # Смотрим все аккаунты в message
    account_keys = (tx_data.get("transaction", {})
                           .get("message", {})
                           .get("accountKeys", []))

    for idx, post_bal in post.items():
        mint      = post_bal.get("mint", "")
        pre_bal   = pre.get(idx, {})
        pre_amt   = int(pre_bal.get("uiTokenAmount", {}).get("amount", "0") or "0")
        post_amt  = int(post_bal.get("uiTokenAmount", {}).get("amount", "0") or "0")
        owner     = post_bal.get("owner", "")

        if not mint or mint == SOL_MINT:
            continue

        if post_amt > pre_amt:
            transfers.append({
                "mint":            mint,
                "toUserAccount":   owner,
                "fromUserAccount": "",
                "amount":          post_amt - pre_amt,
            })
        elif post_amt < pre_amt:
            transfers.append({
                "mint":             mint,
                "fromUserAccount":  owner,
                "toUserAccount":    "",
                "amount":           pre_amt - post_amt,
            })

    return transfers


def _extract_native_transfers(tx_data: dict, meta: dict) -> list:
    pre_bals  = meta.get("preBalances",  [])
    post_bals = meta.get("postBalances", [])
    account_keys = (tx_data.get("transaction", {})
                           .get("message", {})
                           .get("accountKeys", []))
    transfers = []
    for i, (pre, post) in enumerate(zip(pre_bals, post_bals)):
        if i >= len(account_keys):
            break
        ak   = account_keys[i]
        addr = ak if isinstance(ak, str) else ak.get("pubkey", "")
        diff = pre - post
        if diff > 0:
            transfers.append({"fromUserAccount": addr, "toUserAccount": "", "amount": diff})
        elif diff < 0:
            transfers.append({"fromUserAccount": "", "toUserAccount": addr, "amount": -diff})
    return transfers


# ── Main WebSocket loop ───────────────────────────────────────────────────────

async def run(bot):
    if not HELIUS_KEY:
        log.warning("helius_ws: HELIUS_KEY not set — WebSocket disabled, using polling fallback")
        return

    try:
        import websockets
    except ImportError:
        log.warning("helius_ws: websockets not installed")
        return

    log.info("helius_ws: starting real-time listener ⚡")
    reconnects = 0

    while reconnects < MAX_RECONNECTS:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval = 20,
                ping_timeout  = 30,
                close_timeout = 10,
                max_size      = 10 * 1024 * 1024,
            ) as ws:
                _mgr.ws = ws
                reconnects = 0
                log.info("helius_ws: connected ✅")

                # Подписываемся на все кошельки
                wallets = await _load_wallets()
                for w in wallets:
                    await _mgr.subscribe(w)
                log.info(f"helius_ws: subscribed to {len(wallets)} wallets")

                # Задача пересинхронизации — подписываемся на новые кошельки
                async def resync():
                    while True:
                        await asyncio.sleep(300)  # каждые 5 минут
                        try:
                            current = await _load_wallets()
                            new     = [w for w in current if w not in _mgr.subs]
                            for w in new:
                                await _mgr.subscribe(w)
                            if new:
                                log.info(f"helius_ws: subscribed {len(new)} new wallets")
                        except Exception as e:
                            log.warning(f"helius_ws resync: {e}")

                asyncio.create_task(resync())

                # Основной receive loop
                async for raw in ws:
                    try:
                        msg = json.loads(raw)

                        # Подтверждение подписки
                        if "result" in msg and "id" in msg:
                            req_id = msg["id"]
                            if req_id in _mgr.pending:
                                wallet = _mgr.pending.pop(req_id)
                                _mgr.subs[wallet] = msg["result"]
                            continue

                        # Транзакция
                        params = msg.get("params", {})
                        if not params:
                            continue

                        result = params.get("result", {})
                        value  = result.get("value", result) if isinstance(result, dict) else result

                        if isinstance(value, dict) and "transaction" in value:
                            asyncio.create_task(_process_tx(value, bot))

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"helius_ws msg: {e}")

        except Exception as e:
            reconnects += 1
            _mgr.ws = None
            await _mgr.unsubscribe_all()
            delay = min(RECONNECT_SEC * reconnects, 60)
            log.warning(f"helius_ws: disconnected ({e}), retry {reconnects} in {delay}s")
            await asyncio.sleep(delay)

    log.error("helius_ws: max reconnects reached")