"""
engines/trade_analytics.py
Central math module. Single source of truth for all P&L calculations.

Key formulas:
  shares    = cost_usdc / buy_price
  fee_usdc  = calc_fee(buy_price) * cost_usdc   (Polymarket canonical fee)
  pnl (win) = shares * 1.0 - cost_usdc - fee_usdc
  pnl (loss)= -(cost_usdc + fee_usdc)
  roi_pct   = pnl / cost_usdc * 100
  edge      = win_probability - buy_price
  ev        = p*(1/price-1) - (1-p) - calc_fee(price)
  kelly_f   = max(0, (p*b-q)/b) * kelly_fraction

Resolution via Gamma REST API (no CLOB, no token_id — works for ALL dry runs):
  GET https://gamma-api.polymarket.com/markets/{market_id}
  outcomePrices is a JSON STRING: '["1","0"]' = YES won, '["0","1"]' = NO won
  resolved field may be None/undefined for in-progress markets — use price levels.
"""
from __future__ import annotations
import json as _json
from typing import Optional
import requests, structlog

log = structlog.get_logger(component="trade_analytics")
GAMMA_API = "https://gamma-api.polymarket.com"


def calc_fee(p: float) -> float:
    """Polymarket canonical fee rate at price p. fee_usdc = calc_fee(p) * cost."""
    p = max(0.0, min(1.0, p))
    return 2.25 * (p * (1.0 - p)) ** 2


def calc_shares(budget_usdc: float, buy_price: float) -> int:
    """
    Integer shares: how many whole shares can we buy with budget_usdc?
    e.g. budget=$1.00, price=$0.30 -> 3 shares costing $0.90 (remainder unused)
    """
    if buy_price <= 0 or buy_price >= 1.0: return 0
    return int(budget_usdc / buy_price)


def calc_actual_cost(shares: int, buy_price: float) -> float:
    """Actual USDC spent = shares * buy_price (not the full budget)."""
    return round(shares * buy_price, 6)


def calc_fee_usdc(actual_cost_usdc: float, buy_price: float) -> float:
    """Fee on the actual amount spent."""
    return round(calc_fee(buy_price) * actual_cost_usdc, 6)


def calc_pnl(side: str, shares: float, cost_usdc: float, fee_usdc: float,
             resolved_yes_price: float) -> tuple:
    """
    Returns (outcome, net_pnl_usdc).
    outcome: 'win' | 'loss' | 'open'
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


# ─── Gamma API resolution ─────────────────────────────────────────────────────

def fetch_market_resolution(market_id: str, timeout: int = 8,
                              trade_notes: str = "") -> Optional[dict]:
    """
    Fetch market resolution state from Gamma API.
    Works for ALL trades (dry+live) — decoupled from order engine.

    Handles two market_id formats automatically:
      - Numeric (e.g. "1670725") → GET /markets/{id}   [S10/S8 trades]
      - Hex conditionId (e.g. "0x0b41...") → GET /markets?conditionId={id} [S1 NegRisk trades]

    For NegRisk group trades (hex group_id), we check ALL constituent markets.
    The group is "resolved" when any leg has yes_price >= 0.99 (that outcome won).

    Critical notes:
    - outcomePrices is a JSON STRING like '["0.97","0.03"]' — must json.loads it
    - resolved field is None/undefined for in-progress markets (not False)
    - A market is considered resolved when: price >= 0.99 OR price <= 0.01
    """
    try:
        is_hex = str(market_id).startswith("0x") or len(str(market_id)) > 20

        if is_hex:
            # NegRisk group ID — the hex group_id is not directly queryable on Gamma.
            # Strategy: extract individual leg market IDs from trade notes (stored at
            # execution time), then check each leg's resolution status.
            # Leg IDs are stored as "leg_ids=1234,5678,9012" in the notes field.

            # Step 1: try to get leg IDs from notes
            leg_ids = []
            if trade_notes and "leg_ids=" in trade_notes:
                try:
                    leg_part = trade_notes.split("leg_ids=")[1].split(" ")[0]
                    leg_ids = [lid.strip() for lid in leg_part.split(",") if lid.strip()]
                except Exception:
                    pass

            if not leg_ids:
                # No leg IDs in notes — this is an old S1 trade from before the fix.
                # We can't resolve it via the group ID. Log at debug level (not warning)
                # to avoid spamming logs. These trades will remain open until manually cleared.
                log.debug("hex_market_id_no_leg_ids",
                          market_id=market_id[:20],
                          hint="old S1 trade without leg_ids in notes — cannot resolve via Gamma")
                return None

            # Step 2: check each leg using its numeric market ID
            best_yes  = -1.0
            any_resolved = False
            end_date  = ""
            question  = ""

            for leg_id in leg_ids[:20]:  # cap at 20 to avoid excessive API calls
                try:
                    leg_resp = requests.get(f"{GAMMA_API}/markets/{leg_id}", timeout=timeout)
                    if leg_resp.status_code != 200:
                        continue
                    leg_data = leg_resp.json()
                    op_raw = leg_data.get("outcomePrices") or "[]"
                    try:
                        prices = _json.loads(op_raw) if isinstance(op_raw, str) else list(op_raw)
                    except Exception:
                        prices = []
                    if prices and len(prices) >= 1:
                        try:
                            yp = float(prices[0])
                            best_yes = max(best_yes, yp)
                        except Exception:
                            pass
                    if leg_data.get("resolved"):
                        any_resolved = True
                    if not end_date:
                        end_date = leg_data.get("endDate", "")
                    if not question:
                        question = leg_data.get("question", "")
                except Exception:
                    continue

            if best_yes < 0:
                return None  # couldn't fetch any legs

            log.debug("negrisk_legs_checked",
                      market_id=market_id[:20], legs=len(leg_ids),
                      best_yes=best_yes, resolved=any_resolved)

            return {
                "resolved":  any_resolved or best_yes >= 0.99,
                "closed":    best_yes >= 0.99,
                "yes_price": best_yes,
                "question":  question,
                "end_date":  end_date,
                "is_negrisk": True,
                "leg_count": len(leg_ids),
            }

        else:
            # Numeric market ID — direct lookup
            resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=timeout)
            if resp.status_code != 200:
                log.warning("gamma_fetch_failed", market_id=market_id,
                            status=resp.status_code)
                return None
            data = resp.json()

            op_raw = data.get("outcomePrices") or "[]"
            try:
                prices = _json.loads(op_raw) if isinstance(op_raw, str) else list(op_raw)
            except Exception:
                prices = []

            yes_price = -1.0
            if prices and len(prices) >= 1:
                try: yes_price = float(prices[0])
                except: yes_price = -1.0

            resolved = bool(data.get("resolved") or False)
            closed   = bool(data.get("closed")   or False)

            return {
                "resolved":  resolved,
                "closed":    closed,
                "yes_price": yes_price,
                "question":  data.get("question", ""),
                "end_date":  data.get("endDate", ""),
                "is_negrisk": False,
            }

    except Exception as e:
        log.warning("gamma_fetch_error", market_id=market_id, error=str(e))
        return None


def is_market_resolved(ms: dict, entry_time_iso: str = "") -> bool:
    """
    True if market is definitively resolved.
    Handles Gamma quirk: resolved=None for in-progress markets.
    We treat a market as resolved when price is at 0 or 1.

    BUG-4 FIX: NegRisk LOSS detection.
    For NegRisk BUY_ALL_YES, a LOSS means no leg ever won.
    best_yes stays at ~0.5 indefinitely — never hits 0.99 or 0.01.
    We detect expiry by: end_date is in the past AND (resolved flag OR closed flag).
    Also: if entry_time is provided and end_date is > 7 days past entry, force expire.
    """
    from datetime import datetime, timezone
    if not ms: return False
    yp = ms.get("yes_price", -1.0)
    # Price at boundary = resolved WIN or resolved LOSS
    if yp >= 0.99 or yp <= 0.01:
        return True
    # Explicit resolved flag (set after official resolution)
    if ms.get("resolved", False):
        return True
    # BUG-4 FIX: time-based expiry for NegRisk and standard markets
    # If end_date is past AND market is closed, treat as resolved (LOSS path in calc_pnl)
    end_date_str = ms.get("end_date", "")
    if end_date_str:
        try:
            # Gamma end_date format: "2024-12-31T00:00:00Z" or "2024-12-31"
            ed_str = end_date_str.replace("Z", "+00:00")
            if "T" not in ed_str:
                ed_str = ed_str + "T00:00:00+00:00"
            end_dt = datetime.fromisoformat(ed_str)
            now    = datetime.now(timezone.utc)
            if end_dt < now and ms.get("closed", False):
                return True
            # Hard expiry: end_date more than 3 days ago = definitely expired
            from datetime import timedelta
            if end_dt < (now - timedelta(days=3)):
                return True
        except Exception:
            pass
    return False


# ─── Portfolio analytics ─────────────────────────────────────────────────────

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
    """Human-readable trade line for review engine prompts."""
    pnl=float(t.get("pnl_usdc",0) or 0); cost=float(t.get("cost_usdc",0) or 0)
    shares=float(t.get("shares",0) or 0); price=float(t.get("price",0) or 0)
    fee=float(t.get("fee_usdc",0) or 0); roi=calc_roi(pnl,cost)
    notes=t.get("notes","") or ""
    ep=" ".join(p for p in notes.split() if any(k in p for k in ("edge=","score=","p=","ev=")))
    return (
        "  [{}] {}  Q:\"{}\""
        "  {}@{:.3f} x{:.4f}  Cost:{:.2f} Fee:{:.4f} PnL:{:+.4f} ROI:{:+.1f}%"
        + ("  "+ep if ep else "")
    ).format(
        t.get("strategy","?"), str(t.get("outcome") or "open").upper(),
        str(t.get("market_question",""))[:50],
        t.get("side","?"), price, shares, cost, fee, pnl, roi
    )
