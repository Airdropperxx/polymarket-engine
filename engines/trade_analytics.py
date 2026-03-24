"""
engines/trade_analytics.py
==========================
Central math module. Single source of truth for all P&L calculations.

Key formulas:
  shares    = cost_usdc / buy_price
  fee_usdc  = calc_fee(buy_price) * cost_usdc   (Polymarket canonical fee)
  pnl (win) = shares * 1.0 - cost_usdc - fee_usdc
  pnl (loss)= -(cost_usdc + fee_usdc)
  roi_pct   = pnl / cost_usdc * 100
  edge      = win_probability - buy_price
  ev        = p*(1/price - 1) - (1-p) - calc_fee(price)
  kelly_f   = max(0, (p*b - q) / b) * kelly_fraction  where b = 1/price-1

Resolution: uses Gamma REST API (no CLOB, no token_id — works for ALL dry runs)
  GET https://gamma-api.polymarket.com/markets/{market_id}
  outcomePrices: ["1","0"] = YES won, ["0","1"] = NO won
"""
from __future__ import annotations
from typing import Optional
import requests, structlog

log = structlog.get_logger(component="trade_analytics")
GAMMA_API = "https://gamma-api.polymarket.com"


def calc_fee(p: float) -> float:
    """Polymarket canonical fee rate at price p. fee_usdc = calc_fee(p) * cost."""
    p = max(0.0, min(1.0, p))
    return 2.25 * (p * (1.0 - p)) ** 2


def calc_shares(cost_usdc: float, buy_price: float) -> float:
    if buy_price <= 0: return 0.0
    return round(cost_usdc / buy_price, 4)


def calc_fee_usdc(cost_usdc: float, buy_price: float) -> float:
    """Fee in USDC = calc_fee(buy_price) * cost_usdc."""
    return round(calc_fee(buy_price) * cost_usdc, 6)


def calc_pnl(side: str, shares: float, cost_usdc: float, fee_usdc: float,
             resolved_yes_price: float) -> tuple:
    """
    Returns (outcome, net_pnl_usdc).
    outcome: 'win' | 'loss' | 'open'  (open = not fully resolved yet)
    """
    side = (side or 'YES').upper().replace('BUY_', '')
    is_final = (resolved_yes_price >= 0.99 or resolved_yes_price <= 0.01)
    if not is_final:
        return ('open', 0.0)
    if side == 'YES':
        won = resolved_yes_price >= 0.99
    elif side == 'NO':
        won = resolved_yes_price <= 0.01
    else:
        won = resolved_yes_price >= 0.99
    if won:
        return ('win', round(shares * 1.0 - cost_usdc - fee_usdc, 6))
    return ('loss', round(-(cost_usdc + fee_usdc), 6))


def calc_roi(net_pnl: float, cost_usdc: float) -> float:
    if cost_usdc <= 0: return 0.0
    return round(net_pnl / cost_usdc * 100, 2)


def calc_edge(win_probability: float, buy_price: float) -> float:
    return round(win_probability - buy_price, 4)


def calc_expected_value(win_probability: float, buy_price: float) -> float:
    """EV per USDC invested, after fees. Positive = profitable edge."""
    if buy_price <= 0 or buy_price >= 1: return 0.0
    fee_rate = calc_fee(buy_price)
    gross_ev = win_probability * (1.0 / buy_price - 1.0) - (1 - win_probability)
    return round(gross_ev - fee_rate, 6)


def calc_kelly_fraction(win_probability: float, buy_price: float,
                        kelly_fraction: float = 0.25) -> float:
    if buy_price <= 0 or buy_price >= 1: return 0.0
    b = (1.0 / buy_price) - 1.0
    q = 1 - win_probability
    full_kelly = max(0.0, (win_probability * b - q) / b)
    return round(full_kelly * kelly_fraction, 4)


# ─── Gamma API resolution (works for dry + live) ──────────────────────────────

def fetch_market_resolution(market_id: str, timeout: int = 8) -> Optional[dict]:
    """
    Check if market is resolved via Gamma API. Only needs market_id.
    Returns {resolved, closed, yes_price, question} or None on error.
    """
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=timeout)
        if resp.status_code != 200:
            log.warning("gamma_fetch_failed", market_id=market_id, status=resp.status_code)
            return None
        data = resp.json()
        outcome_prices = data.get("outcomePrices", [])
        yes_price = -1.0
        if outcome_prices:
            try: yes_price = float(outcome_prices[0])
            except: yes_price = -1.0
        return {
            "resolved": bool(data.get("resolved", False)),
            "closed":   bool(data.get("closed", False)),
            "yes_price": yes_price,
            "question": data.get("question", ""),
            "end_date": data.get("endDate", ""),
        }
    except Exception as e:
        log.warning("gamma_fetch_error", market_id=market_id, error=str(e))
        return None


def is_market_resolved(ms: dict) -> bool:
    if not ms: return False
    if ms.get("resolved", False): return True
    if ms.get("closed", False):
        yp = ms.get("yes_price", -1.0)
        if yp >= 0.99 or yp <= 0.01: return True
    return False


# ─── Portfolio analytics ───────────────────────────────────────────────────────

def compute_portfolio_stats(trades: list) -> dict:
    """Full portfolio analytics from list of trade dicts."""
    resolved = [t for t in trades if t.get("status") == "resolved"]
    open_pos  = [t for t in trades if t.get("status") == "open"]
    wins      = [t for t in resolved if t.get("outcome") == "win"]
    losses    = [t for t in resolved if t.get("outcome") == "loss"]

    total_invested = sum(float(t.get("cost_usdc", 0) or 0) for t in resolved)
    total_pnl      = sum(float(t.get("pnl_usdc",  0) or 0) for t in resolved)
    win_rate       = (len(wins) / len(resolved) * 100) if resolved else 0.0

    by_strategy = {}
    for t in resolved:
        s = t.get("strategy", "unknown")
        if s not in by_strategy:
            by_strategy[s] = {"wins":0,"losses":0,"total_pnl":0.0,"total_invested":0.0,"trades":0}
        by_strategy[s]["trades"]         += 1
        by_strategy[s]["total_pnl"]      += float(t.get("pnl_usdc",  0) or 0)
        by_strategy[s]["total_invested"] += float(t.get("cost_usdc", 0) or 0)
        if t.get("outcome") == "win": by_strategy[s]["wins"] += 1
        else:                          by_strategy[s]["losses"] += 1

    for s, v in by_strategy.items():
        inv = v["total_invested"]; n = v["trades"]
        v["win_rate_pct"] = round(v["wins"]/n*100,1) if n else 0
        v["roi_pct"]      = round(v["total_pnl"]/inv*100,2) if inv>0 else 0.0
        v["avg_pnl"]      = round(v["total_pnl"]/n,6) if n else 0

    pnl_vals = [float(t.get("pnl_usdc",0) or 0) for t in resolved]
    open_exp  = sum(float(t.get("cost_usdc",0) or 0) for t in open_pos)

    return {
        "total_trades":    len(trades),
        "resolved_trades": len(resolved),
        "open_trades":     len(open_pos),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate_pct":    round(win_rate,1),
        "total_invested":  round(total_invested,2),
        "total_pnl":       round(total_pnl,6),
        "total_roi_pct":   round(total_pnl/total_invested*100,2) if total_invested>0 else 0.0,
        "avg_pnl":         round(total_pnl/len(resolved),6) if resolved else 0.0,
        "best_trade_pnl":  round(max(pnl_vals),6) if pnl_vals else 0.0,
        "worst_trade_pnl": round(min(pnl_vals),6) if pnl_vals else 0.0,
        "open_exposure":   round(open_exp,2),
        "by_strategy":     by_strategy,
    }


def format_trade_summary(t: dict) -> str:
    pnl=float(t.get("pnl_usdc",0) or 0); cost=float(t.get("cost_usdc",0) or 0)
    shares=float(t.get("shares",0) or 0); price=float(t.get("price",0) or 0)
    fee=float(t.get("fee_usdc",0) or 0); roi=calc_roi(pnl,cost)
    notes=t.get("notes","") or ""
    ep=" ".join(p for p in notes.split() if any(k in p for k in ("edge=","score=","p=","ev=")))
    return (f"  [{t.get('strategy','?')}] {str(t.get('outcome') or 'open').upper()}"
            f"  Q:\"{str(t.get('market_question',''))[:50]}\""
            f"  {t.get('side','?')}@{price:.3f} x{shares:.4f}"
            f"  Cost:{cost:.2f} Fee:{fee:.4f} PnL:{pnl:+.4f} ROI:{roi:+.1f}%"
            + (f"  {ep}" if ep else ""))
