#!/usr/bin/env python3
"""
Polymarket Candle Momentum Trader (Direct Clob Edition)
Bypasses Simmer SDK - Uses Direct Polymarket Gamma API & PyClob Client

Trade 5-minute crypto fast markets using 1-minute candle body analysis
and volume surge detection from Binance.
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, timezone

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    print("ERROR: py-clob-client not installed. Run: pip install py-clob-client")
    sys.exit(1)

# ---- Constants ----
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137 # Polygon Mainnet

ASSET_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "DOGE": "DOGEUSDT",
    "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
}

# For searching Polymarket Gamma API
ASSET_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
    "XRP": "XRP", "BNB": "BNB", "DOGE": "Dogecoin"
}

# ---- Config ----
DEFAULTS = {
    "body_threshold": 0.60,
    "vol_threshold": 1.5,
    "max_position": 5.0,
    "asset": "BTC",
    "assets": ["BTC", "ETH", "SOL", "XRP", "BNB"],
    "window": "5m",
    "min_time_remaining": 60,
    "lookback_candles": 3,
    "entry_threshold": 0.05,
}

def load_config():
    """Load config from env vars and defaults."""
    cfg = dict(DEFAULTS)
    env_map = {
        "CM_BODY_THRESHOLD": ("body_threshold", float),
        "CM_VOL_THRESHOLD": ("vol_threshold", float),
        "CM_MAX_POSITION": ("max_position", float),
        "CM_MIN_TIME": ("min_time_remaining", int),
        "CM_LOOKBACK": ("lookback_candles", int),
    }
    for env_key, (cfg_key, cast) in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            cfg[cfg_key] = cast(val)
    return cfg

_client = None
def get_client():
    """Create PyClob Client from environment private key."""
    global _client
    if _client is None:
        pk = os.environ.get("WALLET_PRIVATE_KEY")
        if not pk:
            print("ERROR: WALLET_PRIVATE_KEY not set in environment!")
            sys.exit(1)
        
        # Initialize direct connection to Polymarket
        _client = ClobClient(CLOB_HOST, key=pk, chain_id=CHAIN_ID)
        try:
            _client.set_api_creds(_client.create_or_derive_api_creds())
        except Exception as e:
            print(f"ERROR: Failed to derive API Creds from private key: {e}")
            sys.exit(1)
    return _client

# ---- Signal Logic (Unchanged from Original) ----
def fetch_binance_candles(symbol, interval="1m", limit=10):
    try:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        candles = []
        for k in raw:
            candles.append({
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]), "close_time": int(k[6]),
            })
        return candles
    except Exception as e:
        print(f"  ERROR fetching Binance data: {e}")
        return None

def get_candle_signal(cfg):
    asset = cfg["asset"].upper()
    symbol = ASSET_SYMBOLS.get(asset)
    if not symbol:
        return None, 0, 0, "unknown asset"

    lookback = cfg["lookback_candles"]
    candles = fetch_binance_candles(symbol, "1m", limit=lookback + 2)
    if not candles or len(candles) < lookback + 1:
        return None, 0, 0, "insufficient candle data"

    last = candles[-2]
    prev_candles = candles[-(lookback + 2):-2]
    candle_range = last["high"] - last["low"]
    
    if candle_range <= 0: return None, 0, 0, "zero range candle"

    body = abs(last["close"] - last["open"])
    body_ratio = body / candle_range
    is_bullish = last["close"] > last["open"]

    if prev_candles:
        avg_vol = sum(c["volume"] for c in prev_candles) / len(prev_candles)
        vol_surge = last["volume"] / avg_vol if avg_vol > 0 else 0
    else:
        vol_surge = 1.0

    first_price = candles[0]["open"]
    last_price = last["close"]
    momentum_pct = (last_price - first_price) / first_price if first_price > 0 else 0
    momentum_up = momentum_pct > 0

    body_ok = body_ratio >= cfg["body_threshold"]
    vol_ok = vol_surge >= cfg["vol_threshold"]
    direction_aligned = (is_bullish and momentum_up) or (not is_bullish and not momentum_up)

    if not body_ok: return None, body_ratio, vol_surge, f"body_ratio {body_ratio:.2f} < {cfg['body_threshold']}"
    if not vol_ok: return None, body_ratio, vol_surge, f"vol_surge {vol_surge:.1f}x < {cfg['vol_threshold']}x"
    if not direction_aligned: return None, body_ratio, vol_surge, "direction mismatch"

    direction = "UP" if momentum_up else "DOWN"
    reasoning = f"Candle body={body_ratio:.0%}, vol={vol_surge:.1f}x, {asset} {direction}"
    return direction, body_ratio, vol_surge, reasoning

# ---- Market Discovery (Re-written for Direct Gamma API) ----
def find_fast_markets(cfg):
    """Find active fast markets directly via Polymarket Gamma API."""
    try:
        asset_name = ASSET_NAMES.get(cfg["asset"], cfg["asset"])
        resp = requests.get(
            GAMMA_API_URL,
            params={"limit": 50, "active": "true", "closed": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json()
        
        valid_markets = []
        for ev in events:
            title = ev.get("title", "")
            # Filter for specific crypto and "Up or Down" fast markets
            if asset_name.lower() in title.lower() and "up or down" in title.lower():
                markets = ev.get("markets", [])
                if markets:
                    m = markets[0]
                    # Try to extract current price from tokens array
                    yes_price = 0.50
                    tokens = m.get("tokens", [])
                    if len(tokens) >= 2:
                        yes_price = tokens[0].get("price", 0.50)

                    valid_markets.append({
                        "id": m.get("conditionId"),
                        "question": m.get("question"),
                        "is_live_now": m.get("active"),
                        "resolves_at": m.get("endDate"),
                        "current_probability": yes_price,
                        "clobTokenIds": m.get("clobTokenIds", [])
                    })
        return valid_markets
    except Exception as e:
        print(f"  ERROR discovering markets via Gamma API: {e}")
        return []

def select_best_market(markets, cfg):
    min_time = cfg["min_time_remaining"]
    viable = []
    for m in markets:
        if not m.get("is_live_now", False): continue
        resolves_at = m.get("resolves_at", "")
        if resolves_at:
            try:
                res_dt = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                remaining = (res_dt - now).total_seconds()
                if remaining < min_time: continue
                m["_remaining"] = remaining
                viable.append(m)
            except Exception:
                continue
    if not viable: return None
    viable.sort(key=lambda x: x.get("_remaining", 0), reverse=True)
    return viable[0]

# ---- Main Execution ----
def run_cycle(cfg, live=False, quiet=False):
    if not quiet:
        mode = "LIVE" if live else "DRY RUN (PAPER)"
        print(f"\n  🔥 Polymarket Direct Candle Momentum Trader")
        print("=" * 50)
        print(f"  [{mode}]")
        print(f"  Assets Scanning: {cfg.get('assets', [cfg['asset']])}")
        print()

    assets = cfg.get("assets", [cfg["asset"]])
    best_signal = None

    for asset in assets:
        asset_cfg = dict(cfg)
        asset_cfg["asset"] = asset

        markets = find_fast_markets(asset_cfg)
        if not markets: continue

        market = select_best_market(markets, asset_cfg)
        if not market: continue

        direction, body_ratio, vol_surge, reasoning = get_candle_signal(asset_cfg)
        if direction is None: continue

        score = body_ratio * vol_surge
        if best_signal is None or score > best_signal[0]:
            best_signal = (score, asset, direction, body_ratio, vol_surge, reasoning, market)
            if not quiet:
                print(f"  🎯 {asset}: SIGNAL body={body_ratio:.0%} vol={vol_surge:.1f}x dir={direction} score={score:.2f}")

    if best_signal is None:
        if not quiet: print("  💤 No tradeable signal across all assets.")
        return {"action": "skip"}

    score, asset, direction, body_ratio, vol_surge, reasoning, market = best_signal
    yes_price = market.get("current_probability", 0.5)
    side = "yes" if direction == "UP" else "no"
    amount = cfg["max_position"]
    
    # 0 for YES, 1 for NO
    side_idx = 0 if side == "yes" else 1
    token_ids = market.get("clobTokenIds", [])

    if not quiet:
        print(f"\n  🎯 Best target locked: {asset} {direction} (score={score:.2f})")
        print(f"  Market: {market.get('question', market['id'])[:60]}")
        print(f"  YES Price: ${yes_price:.3f}")

    if not live:
        if not quiet:
            print(f"\n  ✅ [PAPER] Would buy ${amount:.2f} {side.upper()} shares (No real money spent)")
        return {"action": "dry_run"}

    # ---- REAL LIVE TRADE EXECUTION ----
    if len(token_ids) < 2:
        print("  ERROR: Cannot fetch token IDs for this market.")
        return {"action": "error"}

    client = get_client()
    target_token = token_ids[side_idx]
    
    # We use Market Price simulation for execution, fetching orderbook could be added later
    order_args = OrderArgs(
        price=1.0, # Will execute at best available market price up to $1.0
        size=amount,
        side=BUY,
        token_id=target_token,
        order_type=OrderType.FOK, # Fill or Kill
    )
    
    try:
        if not quiet: print(f"  🚀 Sending real order to Polymarket CLOB...")
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order)
        
        if resp and resp.get("success"):
            print(f"\n  💰 TRADED SUCCESSFULLY: Bought ${amount:.2f} of {side.upper()}")
        else:
            print(f"\n  ❌ Trade failed or rejected by CLOB: {resp}")
        return {"action": "traded"}
    except Exception as e:
        if not quiet: print(f"\n  ❌ Trade execution error: {e}")
        return {"action": "error"}

def main():
    parser = argparse.ArgumentParser(description="Polymarket Candle Momentum Trader")
    parser.add_argument("--live", action="store_true", help="Enable real trading")
