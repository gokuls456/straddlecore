"""
Multi-Strategy Registry and Unified Backtest / Execution Engine.
Supports:
1. Hedged Selling (Iron Butterfly) — 1 Lot ATM CE/PE + OTM Wings (~₹1L margin)

"capital_per_lot" on each entry is the fallback capital for 1 lot (margin when selling, premium
when buying), taken from Dhan's margin calculator on 7 Nov 2026 (NIFTY ≈ 25,030). Backtest reports
use live values from engine/margin.py and only fall back to these when Dhan is unreachable.
2. First Candle Breakout (All Day, 1:2) — buys the side that breaks the first 5-min candle
3. Nifty Gap Drive Momentum — Morning gap continuation buyer (~₹30k capital)
4. 1 PM Volatility Drop — Post-lunch European open breakdown put buyer (~₹30k capital)
5. Option Buying (Long Straddle) — ATM CE + PE buyer with risk capped (~₹30k capital)
6. Classic Short Straddle — Naked ATM straddle seller (~₹2L margin per lot)
7. Trend-Hedged Short Straddle — ATM straddle seller + one OTM wing on the side the market opened
8. First Candle Breakout (1:2) — buys the side that breaks the first 5-min candle by 9:45
9. Gold Mini Trend Futures (backtest only) — buys/sells MCX Gold Mini by the opening direction
"""

from __future__ import annotations

import math
from datetime import datetime, date, time, timedelta
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd

from engine.straddle_engine import (
    NiftyStraddleEngine,
    bs_call_price,
    bs_put_price,
    get_atm_strike,
)

def calculate_nse_charges(buy_price: float, sell_price: float, qty: int, is_option: bool = True) -> float:
    """NSE Options charges: flat ₹40 brokerage, STT on sell side, exchange, GST, stamp duty."""
    brokerage = 40.0
    turnover = (buy_price + sell_price) * qty
    exchange_charges = turnover * 0.00053
    sebi_charges = turnover * 0.000001
    stt = (sell_price * qty) * 0.00125
    stamp_duty = (buy_price * qty) * 0.00003
    gst = (brokerage + exchange_charges + sebi_charges) * 0.18
    return round(brokerage + exchange_charges + sebi_charges + stt + stamp_duty + gst, 2)

def calculate_mcx_charges(buy_price: float, sell_price: float, qty_grams: float) -> float:
    """MCX futures charges per round trip: ₹20 brokerage per order, CTT 0.01% on the sell side,
    exchange 0.0021%, SEBI ₹10/crore, stamp duty 0.002% on the buy side, GST on fees.
    Prices are ₹ per 10 g, so turnover = price × grams / 10."""
    brokerage = 40.0
    buy_value = buy_price * qty_grams / 10.0
    sell_value = sell_price * qty_grams / 10.0
    turnover = buy_value + sell_value
    exchange_charges = turnover * 0.000021
    sebi_charges = turnover * 0.000001
    ctt = sell_value * 0.0001
    stamp_duty = buy_value * 0.00002
    gst = (brokerage + exchange_charges + sebi_charges) * 0.18
    return round(brokerage + exchange_charges + sebi_charges + ctt + stamp_duty + gst, 2)


# Registry of all strategies with descriptions, parameters, and metadata
STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    "hedged_selling": {
        "id": "hedged_selling",
        "name": "Hedged Selling (Iron Butterfly)",
        "category": "Intraday Selling",
        "badge": "~₹1L margin / lot",
        "badge_color": "green",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 105000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "20% leg SL on ATM CE & PE; +300 OTM wings protect capital",
        "reentry_rule": "15-minute wait after SL hit, re-enters 1 lot straddle",
        "guard_rule": "±10% of required capital, max ±₹15,000",
        "description": "Sells 1 Lot ATM Call and 1 Lot ATM Put at 10:00 AM while buying +300 OTM protective wings. The wings cut margin from ~₹2.5L (naked straddle) to ~₹1L per lot, capturing intraday theta decay with capped risk.",
    },
    "nifty_10_12_put": {
        "id": "nifty_10_12_put",   # id kept so saved alert settings carry over
        "name": "First Candle Breakout (All Day, SL 15 / TP 15)",
        "category": "Opening Range Breakout",
        "badge": "1:2 Risk-Reward",
        "badge_color": "cyan",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 15000.0,
        "schedule": "From 9:20 AM until 2:30 PM IST (Mon–Fri)",
        "exit_time": "3:15 PM IST",
        "sl_rule": "15 points below the option's buy price",
        "reentry_rule": "One trade a day: the first side broken",
        "guard_rule": "Target 15 points above the buy price, else exit 3:15 PM",
        "description": "Marks the high and low of the first 5-minute candle (9:15–9:20). The first later candle that breaks it decides the trade: high broken → buy ATM Call; low broken → buy ATM Put. Stop-loss 15 points and target 15 points on the option premium (e.g. buy ₹150 → SL ₹135, target ₹165). No break by 2:30 PM → no trade.",
    },
    "nifty_gap_drive": {
        "id": "nifty_gap_drive",
        "name": "Nifty Gap Drive Momentum",
        "category": "Opening Momentum",
        "badge": "Morning Trend",
        "badge_color": "amber",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 15000.0,
        "schedule": "9:45 AM IST (Mon–Fri)",
        "exit_time": "3:15 PM IST",
        "sl_rule": "Opposite end of 30-minute opening range",
        "reentry_rule": "Trailing profit lock once +1.5x risk achieved",
        "guard_rule": "EOD hard exit at 3:15 PM",
        "description": "Exploits opening overnight gaps (>25 pts). If the first 30-minute range confirms gap direction, enters ATM directional option with range stop-loss and trailing profit lock.",
    },
    "nifty_orb_945": {
        "id": "nifty_orb_945",
        "name": "First Candle Breakout (1:2)",
        "category": "Opening Range Breakout",
        "badge": "1:2 Risk-Reward",
        "badge_color": "cyan",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 15000.0,
        "schedule": "9:20 – 9:45 AM IST (Mon–Fri)",
        "exit_time": "3:15 PM IST",
        "sl_rule": "Other end of the first 5-min candle (NIFTY spot)",
        "reentry_rule": "One trade a day: the first side broken by 9:45",
        "guard_rule": "Target = 2 × risk on NIFTY spot (1:2), else exit 3:15 PM",
        "description": "Marks the high and low of the first 5-minute candle (9:15–9:20). Whichever side NIFTY breaks first before 9:45 decides the trade: high broken → buy ATM Call, low broken → buy ATM Put. Stop-loss is the other end of that candle; target is twice that distance beyond the break (1:2). No break by 9:45 → no trade.",
    },
    "nifty_1pm_drop": {
        "id": "nifty_1pm_drop",
        "name": "1 PM Volatility Drop (European Open)",
        "category": "Volatility Breakdown",
        "badge": "Post-Lunch Alpha",
        "badge_color": "purple",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 15000.0,
        "schedule": "1:00 PM – 1:30 PM IST",
        "exit_time": "3:15 PM IST",
        "sl_rule": "30% premium SL with dynamic trailing",
        "reentry_rule": "Fires on 3 consecutive drop candles with body >25 pts",
        "guard_rule": "1:2 Risk-Reward target or 3:15 PM exit",
        "description": "Capitalizes on European market open volatility between 1:00 PM and 1:30 PM. Enters ATM Put after 3 consecutive bearish drop candles with tight trailing stop.",
    },
    "option_buying": {
        "id": "option_buying",
        "name": "Option Buying (Long Straddle)",
        "category": "Intraday Breakout",
        "badge": "Zero Margin Block",
        "badge_color": "cyan",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 24000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "Loss strictly capped at premium paid",
        "reentry_rule": "Captures violent single-day trend breakouts",
        "guard_rule": "Target +₹3,000 / Stop -₹3,000",
        "description": "Buys 1 Lot ATM CE + 1 Lot ATM PE at 10:00 AM. Zero margin required. Maximum downside is strictly capped at premium paid, profiting from strong trending sessions.",
    },
    "short_straddle": {
        "id": "short_straddle",
        "name": "Classic Short Straddle (Institutional)",
        "category": "Institutional Selling",
        "badge": "High Capital • ₹5L+",
        "badge_color": "orange",
        "default_capital": 500000.0,
        "recommended_lots": 10,
        "lot_size": 65,
        "capital_per_lot": 250000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "20% leg SL on both CE and PE",
        "reentry_rule": "15-minute re-entry of 1 lot after any SL hit",
        "guard_rule": "Portfolio Target +₹15,000 / Stop -₹15,000",
        "description": "Institutional naked short straddle selling 10 Lots (650 CE / 650 PE) at 10:00 AM with 20% leg stop-losses, 15m re-entry, and ±₹15,000 portfolio guard.",
    },
    "trend_hedged_straddle": {
        "id": "trend_hedged_straddle",
        "name": "Trend-Hedged Short Straddle",
        "category": "Intraday Selling",
        "badge": "One-Side Hedge",
        "badge_color": "purple",
        "default_capital": 150000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 255000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "20% leg SL on ATM CE & PE; one +300 OTM wing on the opening side",
        "reentry_rule": "15-minute wait after SL hit, re-enters 1 lot straddle",
        "guard_rule": "±10% portfolio guard up to ₹1L capital, else ±₹15,000",
        "description": "Sells 1 lot ATM Call + ATM Put at 10:00 AM like the Classic Short Straddle, and buys one OTM wing on the side the market opened: market opens up (above yesterday's close) → buys a +300 Call; opens down → buys a −300 Put. The other side stays unhedged, so margin is close to a naked straddle (verify in your broker).",
    },
    "gold_trend_futures": {
        "id": "gold_trend_futures",
        "name": "Gold Mini Trend Futures",
        "category": "Commodity Futures (backtest only)",
        "badge": "MCX Gold Mini",
        "badge_color": "orange",
        "default_capital": 300000.0,
        "recommended_lots": 1,
        "lot_size": 100,              # grams per Gold Mini lot; prices are ₹ per 10 g
        "capital_per_lot": 135000.0,  # rough MCX margin (~10–12% of ~₹12.6L contract value)
        "schedule": "10:00 AM IST (Mon–Fri, MCX)",
        "exit_time": "11:00 PM IST",
        "sl_rule": "0.5% stop-loss on the futures price",
        "reentry_rule": "No re-entry",
        "guard_rule": "1% target (1:2 risk-reward)",
        "description": "Trend-Hedged idea on Gold Mini futures: if gold opens at 9:00 AM above yesterday's close it BUYS 1 lot (100 g) at 10:00 AM, if it opens below it SELLS. 0.5% stop-loss, 1% target, exit at 11:00 PM. Real MCX GOLDM 5-minute candles from Dhan, front-month contract; history reaches back as far as Dhan's currently listed GOLDM contracts.",
    },
}


def list_strategies() -> List[Dict[str, Any]]:
    return list(STRATEGY_REGISTRY.values())


def get_strategy(strategy_id: str) -> Dict[str, Any]:
    return STRATEGY_REGISTRY.get(strategy_id) or STRATEGY_REGISTRY["hedged_selling"]


class UnifiedStrategyRunner:
    """
    Executes backtests across all strategy types and produces standardized output
    matching the dashboard UI, equity curves, heatmaps, and trade logs.
    """

    def __init__(self, strategy_id: str = "hedged_selling", **params):
        self.strategy_id = strategy_id
        self.params = params
        self.meta = get_strategy(strategy_id)

    def run(self, hist_data: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        # If strategy is one of the 3 straddle modes, use NiftyStraddleEngine directly
        if self.strategy_id in ("hedged_selling", "option_buying", "short_straddle", "trend_hedged_straddle"):
            engine = NiftyStraddleEngine(
                strategy_mode=self.strategy_id,
                capital=float(self.params.get("capital", self.meta["default_capital"])),
                initial_lots=int(self.params.get("initial_lots", 1)) if self.params.get("initial_lots") not in (None, 0, "auto") else (10 if self.strategy_id == "short_straddle" else 1),
                reentry_lots=int(self.params.get("reentry_lots", 1)),
                lot_size=int(self.params.get("lot_size", 65)),
                sl_pct=float(self.params.get("sl_pct", 0.20)),
                reentry_wait_minutes=int(self.params.get("reentry_wait_minutes", 15)),
                pl_target=float(self.params["pl_target"]) if self.params.get("pl_target") else None,
                pl_stop=float(self.params["pl_stop"]) if self.params.get("pl_stop") else None,
                slippage_pct=float(self.params.get("slippage", 0.0)),
                execution_mode=self.params.get("execution_mode", "fyers_calibrated"),
                reentry_enabled=bool(self.params.get("reentry_enabled", True)),
                reentry_hedged=bool(self.params.get("reentry_hedged", False)),
                reentry_sl_pct=float(self.params["reentry_sl_pct"]) if self.params.get("reentry_sl_pct") else None,
                max_reentries=int(self.params["max_reentries"]) if self.params.get("max_reentries") not in (None, "") else None,
                real_prices=True,
            )
            res = engine.run_backtest(hist_data)
            res["skipped_days"] = engine.skipped_days
            res["strategy_id"] = self.strategy_id
            res["strategy_name"] = self.meta["name"]
            res["strategy_meta"] = self.meta
            return res

        # Otherwise execute the specialized directional / momentum strategies
        if self.strategy_id == "nifty_10_12_put":
            return self._run_10_12_put(hist_data)
        elif self.strategy_id == "nifty_gap_drive":
            return self._run_gap_drive(hist_data)
        elif self.strategy_id == "nifty_1pm_drop":
            return self._run_1pm_drop(hist_data)
        elif self.strategy_id == "nifty_orb_945":
            return self._run_orb_945(hist_data)
        elif self.strategy_id == "gold_trend_futures":
            return self._run_gold_trend(hist_data)

        # Unknown strategy: run Iron Butterfly through the normal path (it maps the request
        # params onto engine arguments; passing them straight to the engine crashes on extras like "period").
        return UnifiedStrategyRunner("hedged_selling", **self.params).run(hist_data)

    # ---------- single-option buyers, on real Dhan option candles ----------

    def _buyer_params(self):
        raw_lot_size = self.params.get("lot_size")
        lot_size = int(raw_lot_size) if raw_lot_size not in (None, "", 0) else 65
        raw_lots = self.params.get("initial_lots")
        lots = int(raw_lots) if raw_lots not in (None, "", "auto", 0) else 1
        raw_slip = self.params.get("slippage")
        slippage = float(raw_slip) if raw_slip not in (None, "") else 1.0
        return lots, lots * lot_size, slippage

    def _run_buyer(self, hist_data: Dict[date, pd.DataFrame], decide, name: str) -> Dict[str, Any]:
        """Shared loop. decide(day, df, prev_close) returns None (no trade) or a setup dict:
          opt, entry_at (datetime: the order goes in at that candle's open), why, branch,
          exit_at (time), and either premium rules (sl_pct, tgt_pct, trail) or spot levels
          (sl_spot, tgt_spot) for strategies whose stop/target are on NIFTY.
        Every price is a real Dhan option candle; a day without one is skipped, not modelled.
        Stop-loss / target trigger on the candle low / high and fill at the level (or the candle
        open if it gapped past it); the time exit fills at that candle's open."""
        from engine.option_history import NoRealPrice, load_day

        capital = float(self.params.get("capital") or 30000.0)
        lots, qty, slippage = self._buyer_params()
        trade_logs, equity_curve, skipped = [], [], []
        cum_pnl = 0.0
        days = sorted(hist_data.keys())

        for idx, day_date in enumerate(days):
            df = hist_data[day_date]
            prev_close = float(hist_data[days[idx - 1]]["close"].iloc[-1]) if idx else None
            if len(df) < 25:
                continue
            setup = decide(day_date, df, prev_close)
            if not setup:
                continue
            book = load_day(day_date, df)
            if book is None:
                skipped.append(f"{day_date}: no Dhan option candles")
                continue
            opt, entry_at, exit_at = setup["opt"], setup["entry_at"], setup["exit_at"]
            spot_rows = df[df.index >= entry_at]
            if spot_rows.empty:
                continue
            spot_entry = float(spot_rows["open"].iloc[0])
            strike = get_atm_strike(spot_entry)
            try:
                c = book.candles(strike, opt)
                entry = book.price(strike, opt, entry_at, "open")
            except NoRealPrice as exc:
                skipped.append(str(exc))
                continue
            entry_time = c.index[c.index >= entry_at][0]
            entry_with_slip = entry + slippage

            by_spot = "sl_spot" in setup
            by_pts = "sl_pts" in setup
            sl = None if by_spot else entry - setup["sl_pts"] if by_pts else entry * (1 - setup["sl_pct"])
            target = None if by_spot else entry + setup["tgt_pts"] if by_pts else entry * (1 + setup["tgt_pct"])
            trail = setup.get("trail")
            highest = entry
            exit_px, exit_reason, exit_time, exit_spot = None, f"Time Exit ({exit_at:%H:%M})", None, None
            curve = []
            for t, bar in c[c.index >= entry_time].iterrows():
                if by_spot:
                    if t in df.index and t > entry_time:
                        s = df.loc[t]
                        up = opt == "CE"
                        hit_sl = s["low"] <= setup["sl_spot"] if up else s["high"] >= setup["sl_spot"]
                        hit_tgt = s["high"] >= setup["tgt_spot"] if up else s["low"] <= setup["tgt_spot"]
                        if hit_sl or hit_tgt:   # both in one candle → assume the stop came first
                            # The option's price at the moment NIFTY touched the level isn't in a 5-min
                            # candle; the candle's close is used (real, but it can be past the level).
                            exit_px = float(bar["close"])
                            exit_reason = "SL Hit (other end of first candle)" if hit_sl else "Target Hit (1:2 R:R)"
                            exit_spot = setup["sl_spot"] if hit_sl else setup["tgt_spot"]
                else:
                    stop = max(sl, highest * (1 - trail)) if trail else sl
                    if bar["low"] <= stop:
                        exit_px = min(float(bar["open"]), stop) if t > entry_time else stop
                        exit_reason = ("Trailing SL Hit" if stop > sl else
                                       f"SL Hit (-{setup['sl_pts']:g} pts)" if by_pts else f"SL Hit (-{setup['sl_pct']:.0%})")
                    elif bar["high"] >= target:
                        exit_px = max(float(bar["open"]), target) if t > entry_time else target
                        exit_reason = f"Target Hit (+{setup['tgt_pts']:g} pts)" if by_pts else f"Target Hit (+{setup['tgt_pct']:.0%})"
                    highest = max(highest, float(bar["high"]))
                if exit_px is None and t.time() >= exit_at:
                    exit_px = float(bar["open"])
                if exit_px is not None:
                    exit_time = t
                    curve.append({"time": t.strftime("%H:%M"), "pnl": round((exit_px - slippage - entry_with_slip) * qty, 2)})
                    break
                curve.append({"time": t.strftime("%H:%M"), "pnl": round((float(bar["close"]) - entry_with_slip) * qty, 2)})
            if exit_px is None:
                exit_px, exit_time, exit_reason = float(c["close"].iloc[-1]), c.index[-1], "Last candle of the day"

            exit_with_slip = max(0.05, exit_px - slippage)
            gross_pnl = (exit_with_slip - entry_with_slip) * qty
            charges = calculate_nse_charges(entry_with_slip, exit_with_slip, qty, is_option=True)
            net_pnl = round(gross_pnl - charges, 2)
            cum_pnl = round(cum_pnl + net_pnl, 2)
            is_ce = opt == "CE"
            et, xt = entry_time.strftime("%H:%M"), exit_time.strftime("%H:%M")
            levels = (f"SL NIFTY {setup['sl_spot']:,.2f} | Target NIFTY {setup['tgt_spot']:,.2f}" if by_spot
                      else f"SL ₹{sl:.2f}{f' (trails {trail:.0%})' if trail else ''} | Target ₹{target:.2f}")
            trade_logs.append({
                "date": day_date.strftime("%Y-%m-%d"), "day_name": day_date.strftime("%A"),
                "entry_time": et, "spot_at_entry": round(spot_entry, 1),
                "ce_strike": strike if is_ce else "-", "ce_entry": round(entry_with_slip, 2) if is_ce else 0.0,
                "ce_exit": round(exit_with_slip, 2) if is_ce else 0.0, "ce_sl_hit": is_ce and "SL" in exit_reason,
                "pe_strike": strike if not is_ce else "-", "pe_entry": round(entry_with_slip, 2) if not is_ce else 0.0,
                "pe_exit": round(exit_with_slip, 2) if not is_ce else 0.0, "pe_sl_hit": (not is_ce) and "SL" in exit_reason,
                "reentry_count": 0, "reentry_pnl": 0.0, "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2), "charges": round(charges, 2), "net_pnl": net_pnl, "cum_pnl": cum_pnl,
                "side": "BUY",
                "positions_detail": [{"leg": f"{strike} {opt}", "type": opt, "branch": setup["branch"], "side": "BUY", "qty": qty,
                                      "entry_time": et, "exit_time": xt,
                                      "entry_price": round(entry_with_slip, 2), "exit_price": round(exit_with_slip, 2),
                                      "exit_reason": exit_reason, "pnl": net_pnl}],
                "events": setup.get("events", []) + [
                    {"timestamp": et, "type": "ENTRY", "detail": f"{setup['why']}. Bought {lots} lot ({qty} Qty) {strike} {opt} @ ₹{entry_with_slip:.2f} (Dhan price) | {levels}"},
                    {"timestamp": xt, "type": "EXIT", "detail": f"Exited {strike} {opt} @ ₹{exit_with_slip:.2f}"
                                                              f"{f' (NIFTY {exit_spot:,.2f})' if exit_spot else ''} ({exit_reason})"},
                ],
                "intraday_curve": curve,
            })
            equity_curve.append({"date": day_date.strftime("%Y-%m-%d"), "cum_pnl": cum_pnl, "net_pnl": net_pnl})

        out = self._format_results(trade_logs, equity_curve, capital)
        out["skipped_days"] = skipped
        return out

    @staticmethod
    def _at(day_date: date, hh: int, mm: int) -> datetime:
        return datetime.combine(day_date, time(hh, mm))

    def _run_10_12_put(self, hist_data):
        """First 5-min candle breakout, any candle up to 2:30 PM. High broken → buy ATM CE, low broken →
        buy ATM PE. Stop-loss 15 points / target 15 points on the option premium; exit 15:15."""
        return self._run_buyer(hist_data, self._first_candle_decide(time(14, 30), points=(15.0, 15.0)), "First Candle Breakout")

    def _run_gap_drive(self, hist_data):
        """Gap of at least ±20 pts at the open → buy ATM CE (up) / PE (down) at 9:45.
        25% SL, +50% target, exit 15:15."""
        def decide(d, df, prev):
            if prev is None:
                return None
            gap = float(df["open"].iloc[0]) - prev
            if abs(gap) < 20:
                return None
            return {"opt": "CE" if gap > 0 else "PE", "entry_at": self._at(d, 9, 45), "exit_at": time(15, 15),
                    "why": f"Gap {gap:+.0f} pts", "branch": f"Gap Drive ({gap:+.0f} pts)",
                    "sl_pct": 0.25, "tgt_pct": 0.50, "trail": None}
        return self._run_buyer(hist_data, decide, "Gap Drive")

    def _run_1pm_drop(self, hist_data):
        """2+ red candles 12:45–1:15 PM → buy ATM PE when the 1:15 candle closes (1:20 open).
        30% SL trailing 15%, +60% target, exit 15:15."""
        def decide(d, df, _prev):
            win = df[(df.index.time >= time(12, 45)) & (df.index.time <= time(13, 15))]
            if len(win) < 4:
                return None
            red = int((win["close"] < win["open"]).sum())
            if red < 2:
                return None
            return {"opt": "PE", "entry_at": self._at(d, 13, 20), "exit_at": time(15, 15),
                    "why": f"{red} of {len(win)} candles red 12:45–1:15 PM", "branch": "1 PM Drop (ATM Put)",
                    "sl_pct": 0.30, "tgt_pct": 0.60, "trail": 0.15}
        return self._run_buyer(hist_data, decide, "1 PM Drop")

    def _run_orb_945(self, hist_data):
        """Same breakout, but only if the first candle is broken before 9:45."""
        return self._run_buyer(hist_data, self._first_candle_decide(time(9, 45)), "First Candle Breakout 9:45")

    def _first_candle_decide(self, last_break: time, points=None):
        """First 5-min candle (9:15) sets the range. The first candle from 9:20 that breaks it (starting
        before last_break) decides: high → buy ATM CE, low → buy ATM PE, entered when that candle closes.
        SL = other end of the first candle, target = break level ± 2 × range (1:2), both on NIFTY spot;
        exit 15:15. A candle breaking both sides follows its close."""
        def decide(d, df, _prev):
            first = df[df.index.time == time(9, 15)]
            if first.empty:
                return None
            hi, lo = float(first["high"].iloc[0]), float(first["low"].iloc[0])
            rng = hi - lo
            if rng <= 0:
                return None
            for t, row in df[(df.index.time >= time(9, 20)) & (df.index.time < last_break)].iterrows():
                up, down = row["high"] > hi, row["low"] < lo
                side = ("CE" if row["close"] >= row["open"] else "PE") if (up and down) else "CE" if up else "PE" if down else None
                if side:
                    lvl = hi if side == "CE" else lo
                    word = "high" if side == "CE" else "low"
                    return {"opt": side, "entry_at": t + timedelta(minutes=5), "exit_at": time(15, 15),
                            "why": f"First candle {word} {lvl:,.2f} broken at {t:%H:%M}",
                            "branch": f"First candle {word} broken",
                            **({"sl_pts": points[0], "tgt_pts": points[1], "trail": None} if points else {
                                "sl_spot": lo if side == "CE" else hi,
                                "tgt_spot": lvl + 2 * rng if side == "CE" else lvl - 2 * rng}),
                            "events": [{"timestamp": "09:20", "type": "SIGNAL",
                                        "detail": f"First candle: high {hi:,.2f} • low {lo:,.2f} (range {rng:.1f} pts)"}]}
            return None
        return decide

    def _run_gold_trend(self, hist_data: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        """Gold Mini futures: direction from the 9:00 open vs yesterday's close, entry at 10:00,
        0.5% SL / 1% target on the futures price, time exit at 23:00. 1 lot = 100 g, and
        prices are ₹ per 10 g, so P&L = price move × 10 per lot."""
        capital = float(self.params.get("capital") or 300000.0)
        raw_lots = self.params.get("initial_lots")
        lots = int(raw_lots) if raw_lots not in (None, "", "auto", 0) else 1
        grams = lots * 100
        mult = grams / 10.0
        sl_pct, target_pct = 0.005, 0.010
        raw_slip = self.params.get("slippage")
        slippage = float(raw_slip) if raw_slip not in (None, "") else 0.0  # ₹ per 10 g, each side

        trade_logs, equity_curve = [], []
        cum_pnl = 0.0
        sorted_dates = sorted(hist_data.keys())

        for idx, day_date in enumerate(sorted_dates):
            if idx == 0:
                continue
            prev_close = float(hist_data[sorted_dates[idx - 1]]["close"].iloc[-1])
            df = hist_data[day_date]
            entry_df = df[df.index.time >= time(10, 0)]
            if entry_df.empty:
                continue

            day_open = float(df["open"].iloc[0])
            side = "BUY" if day_open >= prev_close else "SELL"
            sign = 1 if side == "BUY" else -1
            entry_row = entry_df.iloc[0]
            entry_time_str = entry_row.name.strftime("%H:%M")
            entry = float(entry_row["open"]) + sign * slippage
            sl = entry * (1 - sign * sl_pct)
            target = entry * (1 + sign * target_pct)

            exit_price, exit_time_str, exit_reason = None, "23:00", "Time Exit (23:00)"
            intraday_curve = []
            for t_idx, row in entry_df.iterrows():
                lo, hi, close = float(row["low"]), float(row["high"]), float(row["close"])
                hit_sl = lo <= sl if side == "BUY" else hi >= sl
                hit_tgt = hi >= target if side == "BUY" else lo <= target
                if hit_sl:  # if both touch in one candle, assume the stop came first
                    exit_price, exit_reason = sl, "SL Hit (-0.5%)"
                elif hit_tgt:
                    exit_price, exit_reason = target, "Target Hit (+1%)"
                elif t_idx.time() >= time(23, 0) or t_idx == entry_df.index[-1]:
                    exit_price = close
                    exit_reason = "Time Exit (23:00)" if t_idx.time() >= time(23, 0) else "Session End"
                cur = exit_price if exit_price is not None else close
                intraday_curve.append({"time": t_idx.strftime("%H:%M"), "spot": round(close, 0),
                                       "pnl": round((cur - entry) * sign * mult, 2)})
                if exit_price is not None:
                    exit_time_str = t_idx.strftime("%H:%M")
                    break

            exit_price -= sign * slippage
            gross_pnl = (exit_price - entry) * sign * mult
            buy_p, sell_p = (entry, exit_price) if side == "BUY" else (exit_price, entry)
            charges = calculate_mcx_charges(buy_p, sell_p, grams)
            net_pnl = round(gross_pnl - charges, 2)
            cum_pnl = round(cum_pnl + net_pnl, 2)
            gap = day_open - prev_close

            trade_logs.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_date.strftime("%A"),
                "entry_time": entry_time_str,
                "spot_at_entry": round(entry, 0),
                "ce_strike": "-", "ce_entry": 0.0, "ce_exit": 0.0, "ce_sl_hit": False,
                "pe_strike": "-", "pe_entry": 0.0, "pe_exit": 0.0, "pe_sl_hit": False,
                "reentry_count": 0,
                "reentry_pnl": 0.0,
                "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2),
                "charges": round(charges, 2),
                "net_pnl": net_pnl,
                "cum_pnl": cum_pnl,
                "side": side,
                "positions_detail": [{
                    "leg": "GOLDM FUT", "type": "FUT", "branch": f"Opened {'up' if gap >= 0 else 'down'} {gap:+.0f}",
                    "side": side, "qty": grams, "entry_price": round(entry, 0), "exit_price": round(exit_price, 0),
                    "exit_reason": exit_reason, "pnl": net_pnl,
                }],
                "events": [
                    {"timestamp": "09:00", "type": "SIGNAL", "detail": f"Gold opened ₹{day_open:,.0f} vs yesterday ₹{prev_close:,.0f} ({gap:+,.0f}) → {side}"},
                    {"timestamp": entry_time_str, "type": "ENTRY", "detail": f"{side} {lots} lot Gold Mini ({grams} g) @ ₹{entry:,.0f} | SL ₹{sl:,.0f} | Target ₹{target:,.0f}"},
                    {"timestamp": exit_time_str, "type": "EXIT", "detail": f"Exited @ ₹{exit_price:,.0f} ({exit_reason})"},
                ],
                "intraday_curve": intraday_curve,
            })
            equity_curve.append({"date": day_date.strftime("%Y-%m-%d"), "cum_pnl": cum_pnl, "net_pnl": net_pnl})

        return self._format_results(trade_logs, equity_curve, capital)

    def _format_results(self, trade_logs: List[Dict[str, Any]], equity_curve: List[Dict[str, Any]], capital: float) -> Dict[str, Any]:
        if not trade_logs:
            return {
                "total_days": 0, "win_days": 0, "loss_days": 0, "win_rate": 0.0,
                "total_net_pnl": 0.0, "total_gross_pnl": 0.0, "total_charges": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
                "sharpe_ratio": 0.0, "sortino_ratio": 0.0, "avg_daily_pnl": 0.0,
                "trade_logs": [], "equity_curve": [], "monthly_pnl": {},
                "weekday_pnl": {}, "pnl_distribution": {"bins": [], "counts": []},
                "strategy_id": self.strategy_id,
                "strategy_name": self.meta["name"],
                "strategy_meta": self.meta,
            }

        net_pnls = [t["net_pnl"] for t in trade_logs]
        win_days = sum(1 for p in net_pnls if p > 0)
        loss_days = sum(1 for p in net_pnls if p <= 0)
        total_net = sum(net_pnls)
        total_gross = sum(t["gross_pnl"] for t in trade_logs)
        total_charges = sum(t["charges"] for t in trade_logs)
        win_rate = round((win_days / len(net_pnls)) * 100.0, 1) if net_pnls else 0.0

        gross_win = sum(p for p in net_pnls if p > 0)
        gross_loss = abs(sum(p for p in net_pnls if p < 0))
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0

        # Drawdown calculation
        peak = 0.0
        max_dd = 0.0
        for pt in equity_curve:
            peak = max(peak, pt["cum_pnl"])
            dd = peak - pt["cum_pnl"]
            max_dd = max(max_dd, dd)
            pt["drawdown"] = round(dd, 2)

        max_dd_pct = round((max_dd / capital) * 100.0, 2) if capital else 0.0

        # Sharpe / Sortino
        arr = np.array(net_pnls)
        std = np.std(arr) if len(arr) > 1 else 0.0
        downside_std = np.std(arr[arr < 0]) if len(arr[arr < 0]) > 1 else 0.0
        mean_pnl = np.mean(arr) if len(arr) else 0.0
        sharpe = round((mean_pnl / std) * math.sqrt(252), 2) if std > 0 else 0.0
        sortino = round((mean_pnl / downside_std) * math.sqrt(252), 2) if downside_std > 0 else 0.0

        # Monthly breakdown
        monthly_pnl: Dict[str, Dict[str, float]] = {}
        for t in trade_logs:
            d = datetime.strptime(t["date"], "%Y-%m-%d")
            yr = str(d.year)
            mon = d.strftime("%b")
            monthly_pnl.setdefault(yr, {})
            monthly_pnl[yr][mon] = round(monthly_pnl[yr].get(mon, 0.0) + t["net_pnl"], 2)

        # Weekday breakdown
        weekday_pnl: Dict[str, float] = {w: 0.0 for w in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]}
        for t in trade_logs:
            if t["day_name"] in weekday_pnl:
                weekday_pnl[t["day_name"]] = round(weekday_pnl[t["day_name"]] + t["net_pnl"], 2)

        # Histogram distribution
        counts, bin_edges = np.histogram(arr, bins=12)
        bins_labels = [f"₹{int(bin_edges[i])}" for i in range(len(counts))]
        pnl_distribution = {
            "bins": bins_labels,
            "counts": [int(c) for c in counts],
        }

        return {
            "total_days": len(trade_logs),
            "win_days": win_days,
            "loss_days": loss_days,
            "win_rate": win_rate,
            "total_net_pnl": round(total_net, 2),
            "total_gross_pnl": round(total_gross, 2),
            "total_charges": round(total_charges, 2),
            "profit_factor": profit_factor,
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": max_dd_pct,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "avg_daily_pnl": round(mean_pnl, 2),
            "avg_win": round(gross_win / win_days, 2) if win_days else 0.0,
            "avg_loss": round(gross_loss / loss_days, 2) if loss_days else 0.0,
            "best_day": round(float(np.max(arr)), 2) if len(arr) else 0.0,
            "worst_day": round(float(np.min(arr)), 2) if len(arr) else 0.0,
            "trade_logs": trade_logs,
            "equity_curve": equity_curve,
            "monthly_pnl": monthly_pnl,
            "weekday_pnl": weekday_pnl,
            "pnl_distribution": pnl_distribution,
            "strategy_id": self.strategy_id,
            "strategy_name": self.meta["name"],
            "strategy_meta": self.meta,
            "branch_stats": {
                "ce_sl_hit": sum(1 for t in trade_logs if t.get("ce_sl_hit")),
                "pe_sl_hit": sum(1 for t in trade_logs if t.get("pe_sl_hit")),
                "both_sl_hit": sum(1 for t in trade_logs if t.get("ce_sl_hit") and t.get("pe_sl_hit")),
                "reentry_days": sum(1 for t in trade_logs if t.get("reentry_count", 0) > 0),
                "guard_target_hits": sum(1 for t in trade_logs if "Target" in t.get("exit_reason", "")),
                "guard_stop_hits": sum(1 for t in trade_logs if "Stop" in t.get("exit_reason", "") or "SL" in t.get("exit_reason", "")),
            },
        }
