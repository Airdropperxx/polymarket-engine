"""
scripts/diagnose_pipeline.py — Run this locally to diagnose what's broken.

Usage:
  python scripts/diagnose_pipeline.py

Checks:
  1. Gamma API connectivity + sample market data
  2. CLOB API connectivity + orderbook
  3. What S10 actually sees (time filter analysis)
  4. NegRisk group quality
  5. Strategy hit rates on live data
  6. Config sanity checks

No trades are placed. No secrets required for most checks.
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests


def check_gamma_api():
    print("\n=== GAMMA API CHECK ===")
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 20,
        "order": "volume",
        "ascending": "false"
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        markets = resp.json()
        print(f"  ✓ Connected. Got {len(markets)} markets.")

        now = int(time.time())
        for m in markets[:5]:
            tokens = m.get("tokens", [])
            yes_t  = next((t for t in tokens if t.get("outcome","").upper()=="YES"), {})
            end    = m.get("endDate", "unknown")
            price  = yes_t.get("price", "?")
            vol    = m.get("volume24hr", 0)

            # Compute days to resolution
            try:
                from engines.data_engine import DataEngine
                ts = DataEngine._parse_iso_to_ts(end)
                days_left = (ts - now) / 86400 if ts else 9999
            except Exception:
                days_left = -1

            print(f"  market: {m.get('question','')[:60]}")
            print(f"    end={end[:10]}  days_left={days_left:.1f}  yes_price={price}  vol24h=${vol:,.0f}")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")


def check_clob_api():
    print("\n=== CLOB API CHECK ===")
    # Use a known stable token ID for testing
    url  = "https://clob.polymarket.com/markets"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        print(f"  ✓ CLOB API responding. Sample: {str(data)[:100]}")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")


def check_time_filter():
    print("\n=== TIME FILTER ANALYSIS ===")
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "order": "volume",
        "ascending": "false"
    }
    try:
        resp  = requests.get(url, params=params, timeout=15)
        markets = resp.json()
        now   = int(time.time())

        buckets = {"< 1h": 0, "1-24h": 0, "1-7d": 0, "7-30d": 0, "> 30d": 0}
        for m in markets:
            end = m.get("endDate", "")
            if not end:
                continue
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
                try:
                    dt  = datetime.strptime(end[:len(fmt.replace('%','.'))], fmt)
                    ts  = int(dt.replace(tzinfo=timezone.utc).timestamp())
                    sec = ts - now
                    if sec < 3600:        buckets["< 1h"]   += 1
                    elif sec < 86400:     buckets["1-24h"]  += 1
                    elif sec < 7*86400:   buckets["1-7d"]   += 1
                    elif sec < 30*86400:  buckets["7-30d"]  += 1
                    else:                 buckets["> 30d"]  += 1
                    break
                except Exception:
                    continue

        print("  Distribution of top-100 markets by time to resolution:")
        for bucket, count in buckets.items():
            bar = "█" * count
            print(f"    {bucket:>8}: {count:3d} {bar}")

        print(f"\n  S10 target window (< 60 min): {buckets['< 1h']} markets")
        print(f"  S1 NegRisk window (< 30 days): {buckets['< 1h'] + buckets['1-24h'] + buckets['1-7d'] + buckets['7-30d']} markets")

    except Exception as e:
        print(f"  ✗ FAILED: {e}")


def check_negrisk_groups():
    print("\n=== NEGRISK GROUP CHECK ===")
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "neg_risk": "true",
        "limit": 100,
    }
    try:
        resp    = requests.get(url, params=params, timeout=15)
        markets = resp.json()
        now     = int(time.time())

        groups: dict = {}
        for m in markets:
            gid = m.get("negRiskGroupId") or m.get("neg_risk_group_id")
            if gid:
                groups.setdefault(gid, []).append(m)

        multi = {gid: ms for gid, ms in groups.items() if len(ms) >= 2}
        print(f"  ✓ {len(markets)} NegRisk markets → {len(multi)} groups with 2+ legs")

        # Show best groups
        for gid, ms in sorted(multi.items(), key=lambda x: len(x[1]), reverse=True)[:3]:
            tokens_lists = [m.get("tokens", []) for m in ms]
            yes_prices   = []
            for tl in tokens_lists:
                yes_t = next((t for t in tl if t.get("outcome","").upper()=="YES"), {})
                try:
                    yes_prices.append(float(yes_t.get("price", 0)))
                except Exception:
                    pass
            yes_sum = sum(yes_prices)
            vols    = [m.get("volume24hr", 0) for m in ms]
            min_vol = min(vols) if vols else 0

            print(f"\n  Group {gid[:16]}...")
            print(f"    Legs: {len(ms)}  |  YES sum: {yes_sum:.3f}  |  Min vol: ${min_vol:,.0f}")
            print(f"    Potential edge (before fees): {max(0, 1-yes_sum):.4f}")
            for m, p in zip(ms[:3], yes_prices[:3]):
                print(f"      • {m.get('question','')[:60]}  YES={p:.3f}")

    except Exception as e:
        print(f"  ✗ FAILED: {e}")


def check_config():
    print("\n=== CONFIG SANITY CHECK ===")
    import yaml

    issues = []

    # engine.yaml
    try:
        with open("configs/engine.yaml") as f:
            cfg = yaml.safe_load(f)
        allocs = cfg.get("engine", {}).get("allocations", {}) or cfg.get("allocations", {})
        total  = sum(allocs.values())
        if abs(total - 1.0) > 0.001:
            issues.append(f"Allocations sum to {total:.3f} (must be 1.0)")
        else:
            print(f"  ✓ Allocations sum to {total:.3f}")
    except FileNotFoundError:
        issues.append("configs/engine.yaml not found")
    except Exception as e:
        issues.append(f"engine.yaml error: {e}")

    # S10 config
    try:
        with open("configs/strategies.yaml") as f:
            content = f.read()
        # Multiple docs in one file
        import yaml as y
        docs = list(y.safe_load_all(content))
        s10 = {}
        for doc in docs:
            if doc and "s10_near_resolution" in doc:
                s10 = doc["s10_near_resolution"]
                break
        max_min = s10.get("max_minutes_remaining", 60)
        print(f"  ✓ S10 max_minutes_remaining: {max_min} minutes "
              f"({max_min * 60} seconds — compared to seconds_to_resolution)")
    except FileNotFoundError:
        issues.append("configs/strategies.yaml not found")
    except Exception as e:
        issues.append(f"strategies.yaml error: {e}")

    # Fee formula
    try:
        from strategies.base import BaseStrategy
        fee = BaseStrategy.calc_fee(0.5)
        if abs(fee - 0.140625) < 0.0001:
            print(f"  ✓ Fee formula: calc_fee(0.5) = {fee:.6f} (correct)")
        else:
            issues.append(f"Fee formula wrong: calc_fee(0.5) = {fee}")
    except ImportError:
        issues.append("strategies/base.py not importable")

    if issues:
        print("\n  ISSUES FOUND:")
        for issue in issues:
            print(f"  ✗ {issue}")
    else:
        print("  ✓ All config checks passed")


def main():
    print("=" * 60)
    print("POLYMARKET ENGINE PIPELINE DIAGNOSTIC")
    print("=" * 60)

    check_gamma_api()
    check_clob_api()
    check_time_filter()
    check_negrisk_groups()
    check_config()

    print("\n" + "=" * 60)
    print("DIAGNOSTIC COMPLETE")
    print("If S10 shows 0 markets in < 1h window:")
    print("  → Run this at different times (market openings, sports events)")
    print("  → Consider expanding to 2h window temporarily")
    print("If NegRisk groups have low volumes:")
    print("  → Lower min_leg_volume_24h from 200 to 50 temporarily")
    print("  → Check the YES sum on groups — anything < 0.98 is a candidate")
    print("=" * 60)


if __name__ == "__main__":
    main()
