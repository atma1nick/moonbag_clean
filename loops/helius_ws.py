"""
Helius WebSocket — мгновенный трекинг личного кошелька юзера.
Только для personal wallet (mode=wallet).
Смарт-кошельки — через wallet_loop polling.

Ключевые улучшения парсинга:
- Проверяем что ПЕРВЫЙ signer == tracked wallet (не роутер Jupiter)
- Фильтруем dust transfers (< MIN_SOL_THRESHOLD)
- Дедупликация по sig
- Отличаем покупку от продажи по направлению токен-трансфера
"""
import asyncio
import json
import logging
import os
from sqlalchemy import select
from database import async_session
from models import User, Position, SeenTx, JournalEntry

log = logging.getLogger(__name__)

HELIUS_KEY        = os.getenv("HELIUS_KEY", "")
WS_URL            = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
SOL_MINT          = "So11111111111111111111111111111111111111112"
MIN_SOL_THRESHOLD = 0.001   # меньше этого — игнорируем (dust/fee)
RECONNECT_SEC     = 5
MAX_RECONNECTS    = 999

# Известные адреса роутеров — их транзакции не трекаем как "наши"
KNOWN_ROUTERS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",    # Jupiter v4
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MkiiL5SzsKVKAv",  # Raydium
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
}


class SubManager:
    def __init__(self):
        self.ws      = None
        self.subs    = {}   # wallet → sub_id
        self.pending = {}   # req_id → wallet
        self._rid    = 0

    def next_id(self):
        self._rid += 1
        return self._rid

    async def subscribe(self, wallet: str):
        if not self.ws or wallet in self.subs:
            return
        rid = self.next_id()
        self.pending[rid] = wallet
        try:
            await self.ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rid,
                "method":  "transactionSubscribe",
                "params":  [
                    {"vote": False, "failed": False, "accountInclude": [wallet]},
                    {"commitment": "confirmed", "encoding": "jsonParsed",
                     "transactionDetails": "full",
                     "maxSupportedTransactionVersion": 0}
                ]
            }))
        except Exception as e:
            log.warning(f"ws subscribe {wallet[:8]}: {e}")
            self.pending.pop(rid, None)

    async def clear(self):
        self.subs.clear()
        self.pending.clear()


_mgr = SubManager()


async def _get_personal_wallets() -> dict[str, int]:
    """wallet_address → user_id для всех юзеров с mode=wallet."""
    async with async_session() as s:
        users = (await s.execute(
            select(User).where(User.mode == "wallet", User.wallet.isnot(None))
        )).scalars().all()
    return {u.wallet: u.user_id for u in users if u.wallet}


async def _process_tx(tx_data: dict, wallet_map: dict[str, int], bot):
    try:
        sig = tx_data.get("signature", "")
        if not sig:
            return

        # Дедупликация
        async with async_session() as s:
            if await s.get(SeenTx, sig):
                return
            try:
                s.add(SeenTx(sig=sig))
                await s.commit()
            except Exception:
                pass

        # Получаем account keys
        msg      = tx_data.get("transaction", {}).get("message", {})
        acc_keys = msg.get("accountKeys", [])

        if not acc_keys:
            return

        # Первый signer = инициатор
        initiator = None
        for ak in acc_keys:
            key    = ak if isinstance(ak, str) else ak.get("pubkey", "")
            signer = True if isinstance(ak, str) else ak.get("signer", False)
            if key and signer:
                initiator = key
                break

        if not initiator:
            return

        # Проверяем что инициатор — наш tracked wallet, не роутер
        if initiator not in wallet_map:
            return
        if initiator in KNOWN_ROUTERS:
            return

        user_id = wallet_map[initiator]
        meta    = tx_data.get("meta", {})

        # Парсим токен-трансферы
        pre_tok  = {b["accountIndex"]: b for b in meta.get("preTokenBalances",  [])}
        post_tok = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])}

        # Ищем изменение баланса токена у нашего кошелька
        # Находим index нашего кошелька
        our_indices = set()
        for i, ak in enumerate(acc_keys):
            key = ak if isinstance(ak, str) else ak.get("pubkey", "")
            if key == initiator:
                our_indices.add(i)

        bought_mint = None
        sold_mint   = None
        sol_change  = 0.0

        # SOL изменение (preBalances - postBalances для нашего кошелька)
        pre_bals  = meta.get("preBalances",  [])
        post_bals = meta.get("postBalances", [])
        for idx in our_indices:
            if idx < len(pre_bals) and idx < len(post_bals):
                sol_change += (pre_bals[idx] - post_bals[idx]) / 1e9

        # Токен-изменения
        all_indices = set(list(pre_tok.keys()) + list(post_tok.keys()))
        for idx in all_indices:
            pre  = pre_tok.get(idx,  {})
            post = post_tok.get(idx, {})

            # Проверяем что этот токен-аккаунт принадлежит нашему кошельку
            owner = post.get("owner", pre.get("owner", ""))
            if owner != initiator:
                continue

            mint     = post.get("mint", pre.get("mint", ""))
            if not mint or mint == SOL_MINT:
                continue

            pre_amt  = int(pre.get("uiTokenAmount",  {}).get("amount", "0") or "0")
            post_amt = int(post.get("uiTokenAmount", {}).get("amount", "0") or "0")

            if post_amt > pre_amt:
                bought_mint = mint
            elif post_amt < pre_amt:
                sold_mint = mint

        # Игнорируем dust
        if abs(sol_change) < MIN_SOL_THRESHOLD:
            return

        # ── ПОКУПКА ──────────────────────────────────────────────────────────
        if bought_mint and sol_change > 0:
            sol_spent = sol_change
            await _handle_buy(bot, user_id, bought_mint, sol_spent, sig)

        # ── ПРОДАЖА ──────────────────────────────────────────────────────────
        elif sold_mint and sol_change < 0:
            sol_received = abs(sol_change)
            await _handle_sell(bot, user_id, sold_mint, sol_received)

    except Exception as e:
        log.error(f"ws _process_tx: {e}", exc_info=True)


async def _handle_buy(bot, user_id: int, mint: str, sol_spent: float, sig: str):
    from services.price import fetch_price, get_cached_sol_price
    from utils import fmt_mcap, fmt_sol, fmt_usd, dexscreener
    import json as _json

    # Проверяем нет ли уже позиции
    async with async_session() as s:
        existing = (await s.execute(
            select(Position).where(
                Position.user_id  == user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    data = await fetch_price(mint)
    if not data:
        log.warning(f"ws buy: price not found for {mint[:8]}")
        return

    sol_price = get_cached_sol_price()
    usd_str   = f" (≈{fmt_usd(sol_spent * sol_price)})" if sol_price else ""

    if existing:
        # DCA — уже есть позиция
        await bot.send_message(
            user_id,
            f"➕ *DCA — ${data['symbol']}*\n\n"
            f"Added to existing position.\n"
            f"Amount: *{fmt_sol(sol_spent)}*{usd_str}\n"
            f"Current mcap: *{fmt_mcap(data['mcap'])}*\n\n"
            f"_Update position manually if needed._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 View position", callback_data="do:pos"),
            ]])
        )
        log.info(f"ws DCA: {data['symbol']} → user {user_id}")
        return

    # Новая позиция
    async with async_session() as s:
        user = await s.get(User, user_id)
    plan = _get_user_plan(user) if user else DEFAULT_PLAN

    async with async_session() as s:
        pos = Position(
            user_id     = user_id,
            contract    = mint,
            symbol      = data["symbol"],
            name        = data["name"],
            entry_price = data["price"],
            entry_mcap  = data["mcap"],
            sol_in      = sol_spent,
            exit_plan   = _json.dumps(plan),
            source      = "wallet",
            status      = "active",
        )
        s.add(pos)
        await s.commit()
        await s.refresh(pos)
        pos_id = pos.id

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton

    # Превью плана
    plan_preview = ""
    for l in plan:
        x = l.get("x", 0)
        pct = l.get("pct", 0)
        if x and sol_spent:
            val = sol_spent * (pct / 100) * x
            plan_preview += f"  • {l.get('label', f'{x}x')} → {pct}% ≈ {fmt_sol(val, 2)}\n"
        elif not x:
            plan_preview += f"  • 🌙 Hold {pct}%\n"

    await bot.send_message(
        user_id,
        f"🤖 *AUTO-TRACKED — ${data['symbol']}*\n\n"
        f"📛 {data['name']}\n"
        f"💰 {fmt_sol(sol_spent)}{usd_str}\n"
        f"📊 Entry mcap: *{fmt_mcap(data['mcap'])}*\n"
        f"💧 Liq: {fmt_mcap(data['liquidity'])}\n\n"
        f"📋 *Exit plan:*\n{plan_preview}\n"
        f"[DexScreener]({dexscreener(mint)})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✎ Edit Plan",  callback_data=f"editplan:{pos_id}"),
            InlineKeyboardButton("✅ Keep Plan", callback_data=f"keepplan:{pos_id}"),
        ]])
    )
    log.info(f"ws buy: {data['symbol']} {sol_spent:.3f} SOL → user {user_id}")


async def _handle_sell(bot, user_id: int, mint: str, sol_received: float):
    from services.price import fetch_price, get_cached_sol_price
    from utils import fmt_sol, fmt_x, fmt_usd

    async with async_session() as s:
        pos = (await s.execute(
            select(Position).where(
                Position.user_id  == user_id,
                Position.contract == mint,
                Position.status   == "active"
            )
        )).scalars().first()

    if not pos:
        return  # нет позиции — игнорируем

    data    = await fetch_price(mint)
    cur_x   = data["price"] / pos.entry_price if data and pos.entry_price else 0
    pnl_sol = sol_received - pos.sol_in
    sign    = "+" if pnl_sol >= 0 else ""
    emoji   = "🟢" if pnl_sol >= 0 else "🔴"

    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
    await bot.send_message(
        user_id,
        f"{emoji} *SELL DETECTED — ${pos.symbol}*\n\n"
        f"Received: *{fmt_sol(sol_received)}*\n"
        f"Exit: *{fmt_x(cur_x)}*\n"
        f"PnL: *{sign}{fmt_sol(pnl_sol)}*\n\n"
        f"_Tap Close to log to journal._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔒 Close position", callback_data=f"closepos:{pos.id}"),
        ]])
    )
    log.info(f"ws sell: {pos.symbol} → user {user_id}")


def _get_user_plan(user) -> list:
    DEFAULT = [
        {"x": 4, "pct": 50, "label": "4x"},
        {"x": 8, "pct": 30, "label": "8x"},
        {"x": 0, "pct": 20, "label": "moon"},
    ]
    if user and user.default_plan:
        try:
            return json.loads(user.default_plan)
        except Exception:
            pass
    return DEFAULT


DEFAULT_PLAN = [
    {"x": 4, "pct": 50, "label": "4x"},
    {"x": 8, "pct": 30, "label": "8x"},
    {"x": 0, "pct": 20, "label": "moon"},
]

from telegram import InlineKeyboardMarkup, InlineKeyboardButton


async def run(bot):
    if not HELIUS_KEY:
        log.warning("helius_ws: no HELIUS_KEY — disabled")
        return

    try:
        import websockets
    except ImportError:
        log.warning("helius_ws: websockets not installed")
        return

    log.info("helius_ws: starting ⚡")
    reconnects = 0

    while reconnects < MAX_RECONNECTS:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20, ping_timeout=30,
                close_timeout=10, max_size=10*1024*1024,
            ) as ws:
                _mgr.ws    = ws
                reconnects = 0
                log.info("helius_ws: connected ✅")

                # Подписываемся на личные кошельки
                wallet_map = await _get_personal_wallets()
                for w in wallet_map:
                    await _mgr.subscribe(w)
                log.info(f"helius_ws: watching {len(wallet_map)} personal wallets")

                # Ресинк каждые 5 минут
                async def resync():
                    while True:
                        await asyncio.sleep(300)
                        wmap = await _get_personal_wallets()
                        for w in wmap:
                            if w not in _mgr.subs:
                                await _mgr.subscribe(w)
                                log.info(f"helius_ws: subscribed new wallet {w[:8]}")
                asyncio.create_task(resync())

                async for raw in ws:
                    try:
                        msg    = json.loads(raw)
                        # Подтверждение подписки
                        if "result" in msg and "id" in msg:
                            rid = msg["id"]
                            if rid in _mgr.pending:
                                w = _mgr.pending.pop(rid)
                                _mgr.subs[w] = msg["result"]
                            continue

                        params = msg.get("params", {})
                        if not params:
                            continue
                        result = params.get("result", {})
                        value  = result.get("value", result) if isinstance(result, dict) else result

                        if isinstance(value, dict) and "transaction" in value:
                            # Получаем актуальный wallet_map для каждой транзакции
                            wmap = await _get_personal_wallets()
                            asyncio.create_task(_process_tx(value, wmap, bot))

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log.error(f"helius_ws msg: {e}")

        except Exception as e:
            reconnects += 1
            _mgr.ws = None
            await _mgr.clear()
            delay = min(RECONNECT_SEC * reconnects, 60)
            log.warning(f"helius_ws: disconnected ({e}), retry {reconnects} in {delay}s")
            await asyncio.sleep(delay)

    log.error("helius_ws: max reconnects reached")