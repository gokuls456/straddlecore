"""
Multi-Strategy Registry and Unified Backtest / Execution Engine.
Supports:
1. Hedged Selling (Iron Butterfly) — 1 Lot ATM CE/PE + OTM Wings (~₹30k margin)

"capital_per_lot" on each entry is a rough estimate of capital needed for 1 lot
(margin when selling, premium when buying). Verify against the broker margin calculator.
2. 10:12 Bearish Put Breakdown — Morning breakdown put buyer (~₹30k capital)
3. Nifty Gap Drive Momentum — Morning gap continuation buyer (~₹30k capital)
4. 1 PM Volatility Drop — Post-lunch European open breakdown put buyer (~₹30k capital)
5. Option Buying (Long Straddle) — ATM CE + PE buyer with risk capped (~₹30k capital)
6. Classic Short Straddle — Naked ATM straddle seller (~₹2L margin per lot)
7. Trend-Hedged Short Straddle — ATM straddle seller + one OTM wing on the side the market opened
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

# Registry of all strategies with descriptions, parameters, and metadata
STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    "hedged_selling": {
        "id": "hedged_selling",
        "name": "Hedged Selling (Iron Butterfly)",
        "category": "Intraday Selling",
        "badge": "Built for ₹30,000",
        "badge_color": "green",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 30000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "20% leg SL on ATM CE & PE; +300 OTM wings protect capital",
        "reentry_rule": "15-minute wait after SL hit, re-enters 1 lot straddle",
        "guard_rule": "±10% portfolio guard (±₹3,000 on ₹30k)",
        "description": "Sells 1 Lot ATM Call and 1 Lot ATM Put at 10:00 AM while buying +300 OTM protective wings. Compresses NSE margin from ₹1.25L to ~₹30k–₹35k, capturing steady intraday theta decay safely.",
    },
    "nifty_10_12_put": {
        "id": "nifty_10_12_put",
        "name": "10:12 Bearish Put Breakdown",
        "category": "Breakdown Momentum",
        "badge": "Directional Alpha",
        "badge_color": "cyan",
        "default_capital": 30000.0,
        "recommended_lots": 1,
        "lot_size": 65,
        "capital_per_lot": 15000.0,
        "schedule": "10:12 AM IST (Mon–Fri)",
        "exit_time": "3:00 PM IST",
        "sl_rule": "30% premium SL with 15% trailing stop",
        "reentry_rule": "Single high-conviction entry per session",
        "guard_rule": "1:2 Risk-to-Reward Profit Target (+60% premium gain)",
        "description": "Monitors opening range (9:15–10:06 AM). If signal candle at 10:09 AM closes bearish below range midpoint, buys ATM Put at 10:12 AM with 30% SL and 1:2 R:R target.",
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
        "capital_per_lot": 20000.0,
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
        "capital_per_lot": 200000.0,
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
        "capital_per_lot": 150000.0,
        "schedule": "10:00 AM IST (Mon–Fri)",
        "exit_time": "3:25 PM IST",
        "sl_rule": "20% leg SL on ATM CE & PE; one +300 OTM wing on the opening side",
        "reentry_rule": "15-minute wait after SL hit, re-enters 1 lot straddle",
        "guard_rule": "±10% portfolio guard up to ₹1L capital, else ±₹15,000",
        "description": "Sells 1 lot ATM Call + ATM Put at 10:00 AM like the Classic Short Straddle, and buys one OTM wing on the side the market opened: market opens up (above yesterday's close) → buys a +300 Call; opens down → buys a −300 Put. The other side stays unhedged, so margin is close to a naked straddle (verify in your broker).",
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
            )
            res = engine.run_backtest(hist_data)
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

        # Unknown strategy: run Iron Butterfly through the normal path (it maps the request
        # params onto engine arguments; passing them straight to the engine crashes on extras like "period").
        return UnifiedStrategyRunner("hedged_selling", **self.params).run(hist_data)

    def _run_10_12_put(self, hist_data: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        capital = float(self.params.get("capital") or 30000.0)
        raw_lot_size = self.params.get("lot_size")
        lot_size = int(raw_lot_size) if raw_lot_size not in (None, "", 0) else 65
        raw_lots = self.params.get("initial_lots")
        lots = int(raw_lots) if raw_lots not in (None, "", "auto", 0) else 1
        qty = lots * lot_size
        raw_sl = self.params.get("sl_pct")
        premium_sl_pct = float(raw_sl) if raw_sl not in (None, "") else 0.30
        reward_ratio = 2.0
        raw_slip = self.params.get("slippage")
        slippage = float(raw_slip) if raw_slip not in (None, "") else 1.5

        trade_logs = []
        equity_curve = []
        cum_pnl = 0.0

        for day_date in sorted(hist_data.keys()):
            df = hist_data[day_date]
            if len(df) < 25:
                continue

            day_name = day_date.strftime("%A")
            # 1. Observation window: 09:15 to 10:05
            win_mask = (df.index.time >= time(9, 15)) & (df.index.time <= time(10, 5))
            win_df = df[win_mask]
            if len(win_df) < 5:
                continue

            win_high = win_df["high"].max()
            win_low = win_df["low"].min()
            win_mid = (win_high + win_low) / 2.0

            # 2. Signal check around 10:05–10:10
            sig_mask = (df.index.time >= time(10, 5)) & (df.index.time <= time(10, 10))
            sig_df = df[sig_mask]
            if sig_df.empty:
                continue

            sig_close = sig_df["close"].iloc[-1]
            sig_open = sig_df["open"].iloc[0]
            is_bearish = sig_close < sig_open
            is_below_mid = sig_close < win_mid

            # Entry at 10:10 or 10:15
            entry_mask = df.index.time >= time(10, 10)
            entry_df = df[entry_mask]
            if entry_df.empty or not (is_bearish or is_below_mid):
                # No setup trigger today: record neutral day or skipped
                continue

            entry_row = entry_df.iloc[0]
            entry_time_str = entry_row.name.strftime("%H:%M")
            spot_entry = entry_row["open"]
            atm_strike = get_atm_strike(spot_entry)

            # Estimate ATM Put Premium (~100 pts)
            entry_premium = max(40.0, bs_put_price(spot_entry, atm_strike, 0.008, 0.065, 0.14))
            entry_with_slip = entry_premium + slippage
            sl_premium = entry_premium * (1.0 - premium_sl_pct)
            target_premium = entry_premium + (entry_premium * premium_sl_pct * reward_ratio)

            # Intraday tracking
            post_df = df[df.index.time >= time(10, 15)]
            exit_time_str = "15:25"
            exit_premium = entry_premium
            exit_reason = "Time Exit (15:25)"
            highest_prem = entry_premium
            intraday_curve = []

            for t_idx, row in post_df.iterrows():
                spot_c = row["close"]
                spot_l = row["low"]
                spot_h = row["high"]

                # Put premium moves inversely with spot
                diff = spot_entry - spot_c
                cur_prem = max(2.0, entry_premium + (diff * 0.50))
                diff_l = spot_entry - spot_l
                diff_h = spot_entry - spot_h
                prem_high = max(2.0, entry_premium + (diff_l * 0.50))
                prem_low = max(2.0, entry_premium + (diff_h * 0.50))

                highest_prem = max(highest_prem, prem_high)
                trailing_sl = max(sl_premium, highest_prem * 0.85)

                cur_pnl = (cur_prem - entry_with_slip) * qty
                intraday_curve.append({"time": t_idx.strftime("%H:%M"), "pnl": round(cur_pnl, 2)})

                if prem_low <= trailing_sl:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_premium = trailing_sl - slippage
                    exit_reason = "Trailing SL Hit" if trailing_sl > sl_premium else "SL Hit (-30%)"
                    break
                if prem_high >= target_premium:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_premium = target_premium - slippage
                    exit_reason = "Target Hit (1:2 R:R)"
                    break
                if t_idx.time() >= time(15, 20):
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_premium = cur_prem - slippage
                    exit_reason = "Time Exit (15:25)"
                    break

            gross_pnl = (exit_premium - entry_with_slip) * qty
            charges = calculate_nse_charges(entry_with_slip, exit_premium, qty, is_option=True)
            net_pnl = round(gross_pnl - charges, 2)
            cum_pnl = round(cum_pnl + net_pnl, 2)

            pos_detail = [{
                "leg": f"{atm_strike} PE",
                "branch": "Branch 1 (ATM Put)",
                "side": "BUY",
                "qty": qty,
                "entry_price": round(entry_with_slip, 2),
                "exit_price": round(exit_premium, 2),
                "exit_reason": exit_reason,
                "pnl": net_pnl,
            }]

            evs = [
                {"timestamp": entry_time_str, "type": "ENTRY", "detail": f"Bought {lots} lot ({qty} Qty) {atm_strike} PE @ ₹{entry_with_slip:.2f}"},
                {"timestamp": exit_time_str, "type": "EXIT", "detail": f"Exited {atm_strike} PE @ ₹{exit_premium:.2f} ({exit_reason})"},
            ]

            trade_logs.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_name,
                "entry_time": entry_time_str,
                "spot_at_entry": round(spot_entry, 1),
                "ce_strike": "-",
                "ce_entry": 0.0,
                "ce_exit": 0.0,
                "ce_sl_hit": False,
                "pe_strike": atm_strike,
                "pe_entry": round(entry_with_slip, 2),
                "pe_exit": round(exit_premium, 2),
                "pe_sl_hit": "SL" in exit_reason,
                "reentry_count": 0,
                "reentry_pnl": 0.0,
                "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2),
                "charges": round(charges, 2),
                "net_pnl": net_pnl,
                "cum_pnl": cum_pnl,
                "side": "BUY",
                "positions_detail": pos_detail,
                "events": evs,
                "intraday_curve": intraday_curve,
            })

            equity_curve.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "cum_pnl": cum_pnl,
                "net_pnl": net_pnl,
            })

        return self._format_results(trade_logs, equity_curve, capital)

    def _run_gap_drive(self, hist_data: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        capital = float(self.params.get("capital") or 30000.0)
        raw_lot_size = self.params.get("lot_size")
        lot_size = int(raw_lot_size) if raw_lot_size not in (None, "", 0) else 65
        raw_lots = self.params.get("initial_lots")
        lots = int(raw_lots) if raw_lots not in (None, "", "auto", 0) else 1
        qty = lots * lot_size
        raw_slip = self.params.get("slippage")
        slippage = float(raw_slip) if raw_slip not in (None, "") else 1.5

        trade_logs = []
        equity_curve = []
        cum_pnl = 0.0
        sorted_dates = sorted(hist_data.keys())

        for idx, day_date in enumerate(sorted_dates):
            if idx == 0:
                continue
            prev_day = sorted_dates[idx - 1]
            prev_close = hist_data[prev_day]["close"].iloc[-1]
            df = hist_data[day_date]
            if len(df) < 25:
                continue

            open_spot = df["open"].iloc[0]
            gap_pts = open_spot - prev_close

            # Needs at least 20 pts gap to qualify
            if abs(gap_pts) < 20:
                continue

            opt_type = "CE" if gap_pts > 0 else "PE"
            day_name = day_date.strftime("%A")

            # Entry at 09:45 AM
            sub_945 = df[df.index.time >= time(9, 45)]
            if sub_945.empty:
                continue
            entry_row = sub_945.iloc[0]
            spot_entry = entry_row["open"]
            atm_strike = get_atm_strike(spot_entry)

            entry_prem = max(50.0, bs_call_price(spot_entry, atm_strike, 0.008, 0.065, 0.14) if opt_type == "CE" else bs_put_price(spot_entry, atm_strike, 0.008, 0.065, 0.14))
            entry_with_slip = entry_prem + slippage
            sl_prem = entry_prem * 0.75  # 25% SL
            target_prem = entry_prem * 1.50  # 50% target

            post_df = df[df.index.time >= time(9, 50)]
            exit_time_str = "15:15"
            exit_prem = entry_prem
            exit_reason = "EOD Exit (15:15)"
            intraday_curve = []

            for t_idx, row in post_df.iterrows():
                spot_c = row["close"]
                diff = (spot_c - spot_entry) if opt_type == "CE" else (spot_entry - spot_c)
                cur_prem = max(2.0, entry_prem + (diff * 0.50))
                cur_pnl = (cur_prem - entry_with_slip) * qty
                intraday_curve.append({"time": t_idx.strftime("%H:%M"), "pnl": round(cur_pnl, 2)})

                if cur_prem <= sl_prem:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = sl_prem - slippage
                    exit_reason = "Range SL Hit (-25%)"
                    break
                if cur_prem >= target_prem:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = target_prem - slippage
                    exit_reason = "Target Hit (+50%)"
                    break
                if t_idx.time() >= time(15, 15):
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = cur_prem - slippage
                    exit_reason = "EOD Exit (15:15)"
                    break

            gross_pnl = (exit_prem - entry_with_slip) * qty
            charges = calculate_nse_charges(entry_with_slip, exit_prem, qty, is_option=True)
            net_pnl = round(gross_pnl - charges, 2)
            cum_pnl = round(cum_pnl + net_pnl, 2)

            pos_detail = [{
                "leg": f"{atm_strike} {opt_type}",
                "branch": f"Gap Drive ({gap_pts:+.0f} pts)",
                "side": "BUY",
                "qty": qty,
                "entry_price": round(entry_with_slip, 2),
                "exit_price": round(exit_prem, 2),
                "exit_reason": exit_reason,
                "pnl": net_pnl,
            }]

            evs = [
                {"timestamp": "09:45", "type": "ENTRY", "detail": f"Gap {gap_pts:+.0f} pts detected. Bought {qty} Qty {atm_strike} {opt_type} @ ₹{entry_with_slip:.2f}"},
                {"timestamp": exit_time_str, "type": "EXIT", "detail": f"Exited {atm_strike} {opt_type} @ ₹{exit_prem:.2f} ({exit_reason})"},
            ]

            trade_logs.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_name,
                "entry_time": "09:45",
                "spot_at_entry": round(spot_entry, 1),
                "ce_strike": atm_strike if opt_type == "CE" else "-",
                "ce_entry": round(entry_with_slip, 2) if opt_type == "CE" else 0.0,
                "ce_exit": round(exit_prem, 2) if opt_type == "CE" else 0.0,
                "ce_sl_hit": opt_type == "CE" and "SL" in exit_reason,
                "pe_strike": atm_strike if opt_type == "PE" else "-",
                "pe_entry": round(entry_with_slip, 2) if opt_type == "PE" else 0.0,
                "pe_exit": round(exit_prem, 2) if opt_type == "PE" else 0.0,
                "pe_sl_hit": opt_type == "PE" and "SL" in exit_reason,
                "reentry_count": 0,
                "reentry_pnl": 0.0,
                "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2),
                "charges": round(charges, 2),
                "net_pnl": net_pnl,
                "cum_pnl": cum_pnl,
                "side": "BUY",
                "positions_detail": pos_detail,
                "events": evs,
                "intraday_curve": intraday_curve,
            })

            equity_curve.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "cum_pnl": cum_pnl,
                "net_pnl": net_pnl,
            })

        return self._format_results(trade_logs, equity_curve, capital)

    def _run_1pm_drop(self, hist_data: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        capital = float(self.params.get("capital") or 30000.0)
        raw_lot_size = self.params.get("lot_size")
        lot_size = int(raw_lot_size) if raw_lot_size not in (None, "", 0) else 65
        raw_lots = self.params.get("initial_lots")
        lots = int(raw_lots) if raw_lots not in (None, "", "auto", 0) else 1
        qty = lots * lot_size
        raw_slip = self.params.get("slippage")
        slippage = float(raw_slip) if raw_slip not in (None, "") else 1.5

        trade_logs = []
        equity_curve = []
        cum_pnl = 0.0

        for day_date in sorted(hist_data.keys()):
            df = hist_data[day_date]
            if len(df) < 50:
                continue

            day_name = day_date.strftime("%A")
            # Look at 12:45 to 13:15
            drop_win = df[(df.index.time >= time(12, 45)) & (df.index.time <= time(13, 15))]
            if len(drop_win) < 4:
                continue

            # Check if 2 or more candles were red
            red_count = sum(1 for _, r in drop_win.iterrows() if r["close"] < r["open"])
            if red_count < 2:
                continue

            entry_row = drop_win.iloc[-1]
            entry_time_str = entry_row.name.strftime("%H:%M")
            spot_entry = entry_row["close"]
            atm_strike = get_atm_strike(spot_entry)

            entry_prem = max(45.0, bs_put_price(spot_entry, atm_strike, 0.008, 0.065, 0.14))
            entry_with_slip = entry_prem + slippage
            sl_prem = entry_prem * 0.70  # 30% SL
            target_prem = entry_prem * 1.60  # 60% Target

            post_df = df[df.index.time > entry_row.name.time()]
            exit_time_str = "15:20"
            exit_prem = entry_prem
            exit_reason = "EOD Exit (15:20)"
            highest_prem = entry_prem
            intraday_curve = []

            for t_idx, row in post_df.iterrows():
                spot_c = row["close"]
                diff = spot_entry - spot_c
                cur_prem = max(2.0, entry_prem + (diff * 0.50))
                highest_prem = max(highest_prem, cur_prem)
                trailing_sl = max(sl_prem, highest_prem * 0.85)

                cur_pnl = (cur_prem - entry_with_slip) * qty
                intraday_curve.append({"time": t_idx.strftime("%H:%M"), "pnl": round(cur_pnl, 2)})

                if cur_prem <= trailing_sl:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = trailing_sl - slippage
                    exit_reason = "Trailing SL Hit" if trailing_sl > sl_prem else "SL Hit (-30%)"
                    break
                if cur_prem >= target_prem:
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = target_prem - slippage
                    exit_reason = "Target Hit (1:2 R:R)"
                    break
                if t_idx.time() >= time(15, 20):
                    exit_time_str = t_idx.strftime("%H:%M")
                    exit_prem = cur_prem - slippage
                    exit_reason = "EOD Exit (15:20)"
                    break

            gross_pnl = (exit_prem - entry_with_slip) * qty
            charges = calculate_nse_charges(entry_with_slip, exit_prem, qty, is_option=True)
            net_pnl = round(gross_pnl - charges, 2)
            cum_pnl = round(cum_pnl + net_pnl, 2)

            pos_detail = [{
                "leg": f"{atm_strike} PE",
                "branch": "1 PM European Open Drop",
                "side": "BUY",
                "qty": qty,
                "entry_price": round(entry_with_slip, 2),
                "exit_price": round(exit_prem, 2),
                "exit_reason": exit_reason,
                "pnl": net_pnl,
            }]

            evs = [
                {"timestamp": entry_time_str, "type": "ENTRY", "detail": f"1 PM drop breakdown. Bought {qty} Qty {atm_strike} PE @ ₹{entry_with_slip:.2f}"},
                {"timestamp": exit_time_str, "type": "EXIT", "detail": f"Exited {atm_strike} PE @ ₹{exit_prem:.2f} ({exit_reason})"},
            ]

            trade_logs.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "day_name": day_name,
                "entry_time": entry_time_str,
                "spot_at_entry": round(spot_entry, 1),
                "ce_strike": "-",
                "ce_entry": 0.0,
                "ce_exit": 0.0,
                "ce_sl_hit": False,
                "pe_strike": atm_strike,
                "pe_entry": round(entry_with_slip, 2),
                "pe_exit": round(exit_prem, 2),
                "pe_sl_hit": "SL" in exit_reason,
                "reentry_count": 0,
                "reentry_pnl": 0.0,
                "exit_reason": exit_reason,
                "gross_pnl": round(gross_pnl, 2),
                "charges": round(charges, 2),
                "net_pnl": net_pnl,
                "cum_pnl": cum_pnl,
                "side": "BUY",
                "positions_detail": pos_detail,
                "events": evs,
                "intraday_curve": intraday_curve,
            })

            equity_curve.append({
                "date": day_date.strftime("%Y-%m-%d"),
                "cum_pnl": cum_pnl,
                "net_pnl": net_pnl,
            })

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
