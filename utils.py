import re
import json


# ── Форматтеры ────────────────────────────────────────────────────────────────

def fmt_mcap(v: float | None) -> str:
    if not v:
        return "?"
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def fmt_sol(v: float | None, decimals: int = 3) -> str:
    if v is None:
        return "?"
    return f"{v:.{decimals}f} SOL"


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "?"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def fmt_x(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:.2f}x"


def fmt_pnl(sol: float, usd: float | None = None, currency: str = "SOL") -> str:
    sign = "+" if sol >= 0 else ""
    emoji = "🟢" if sol >= 0 else "🔴"
    if currency == "USD" and usd is not None:
        return f"{emoji} {sign}{fmt_usd(usd)}"
    return f"{emoji} {sign}{fmt_sol(sol)}"


def solscan_tx(sig: str) -> str:
    return f"https://solscan.io/tx/{sig}"


def solscan_token(contract: str) -> str:
    return f"https://solscan.io/token/{contract}"


def dexscreener(contract: str) -> str:
    return f"https://dexscreener.com/solana/{contract}"


# ── Парсеры ───────────────────────────────────────────────────────────────────

def parse_mcap(text: str) -> float | None:
    """'200k' → 200000, '1.5m' → 1500000, '2b' → 2000000000"""
    text = text.strip().lower().replace(",", "").replace("$", "")
    m = re.match(r"^([\d.]+)\s*([kmb]?)$", text)
    if not m:
        return None
    n, suffix = float(m.group(1)), m.group(2)
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
    return n * mult


def parse_exit_plan(text: str) -> list | None:
    """
    '4x 50%, 8x 30%, moon 20%' → [{"x":4,"pct":50},{"x":8,"pct":30},{"x":0,"pct":20}]
    """
    levels = []
    total_pct = 0
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            continue
        # moon / moonbag
        if "moon" in part:
            m = re.search(r"(\d+)\s*%", part)
            pct = int(m.group(1)) if m else (100 - total_pct)
            levels.append({"x": 0, "pct": pct, "label": "moon"})
            total_pct += pct
            continue
        # Nx Y%
        m = re.match(r"([\d.]+)\s*x.*?(\d+)\s*%", part)
        if m:
            levels.append({"x": float(m.group(1)), "pct": int(m.group(2)), "label": f"{m.group(1)}x"})
            total_pct += int(m.group(2))
    if not levels:
        return None
    return levels


def exit_plan_text(levels: list) -> str:
    lines = []
    for l in levels:
        lbl = l.get("label", f"{l['x']}x") if l.get("x") else "🌙 Moonbag"
        done = " ✅" if l.get("done") else ""
        lines.append(f"  • {lbl} → sell {l['pct']}%{done}")
    return "\n".join(lines)


def calc_pnl(sol_in: float, current_x: float) -> tuple[float, float]:
    """Returns (pnl_sol, pnl_pct)"""
    if not sol_in or not current_x:
        return 0.0, 0.0
    current_value = sol_in * current_x
    pnl_sol = current_value - sol_in
    pnl_pct = ((current_value / sol_in) - 1) * 100
    return pnl_sol, pnl_pct
