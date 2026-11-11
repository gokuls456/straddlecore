"""
NIFTY 50 Options Strategy Engine (Multi-Mode)
Supports:
1. Option Buying (Long Straddle) - Optimized for ₹40,000 Capital (Zero Margin Block, Sized by Capital)
2. Hedged Option Selling (Iron Butterfly) - Optimized for ₹40,000 Margin (OTM Protection Wings + Short ATM Straddle)
3. Classic Short-Premium ATM Straddle - Institutional 10-Lot Option Selling (Requires ₹10L+ Capital)

Architecture:
- Schedule: Monday to Friday at 10:00 AM IST
- Branch 1: CE Leg Entry with 20% SL, auto square-off all positions, 15m wait -> re-enter straddle
- Branch 2: PE Leg Entry with 20% SL, auto square-off all positions, 15m wait -> re-enter straddle
- Branch 3: Portfolio P&L guard (checks every 10s / bar), if open positions > 0 and Total P&L reaches target or stop
- Branch 4: Time exit at 3:25 PM (15:25 IST) -> mandatory square-off all
- Black-Scholes option pricing with intraday time decay, Greeks, and realistic NSE charges.
"""

import math
import numpy as np
import pandas as pd
from datetime import datetime, time, timedelta, date
from typing import Dict, List, Optional, Tuple, Any

from engine.expiry import nifty_weekly_expiry, trading_days_until

# Standard Black-Scholes pricing
def bs_call_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 1e-6:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    c = S * 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0))) - K * math.exp(-r * T) * 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
    return max(0.05, c)

def bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 1e-6:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    p = K * math.exp(-r * T) * 0.5 * (1.0 - math.erf(d2 / math.sqrt(2.0))) - S * 0.5 * (1.0 - math.erf(d1 / math.sqrt(2.0)))
    return max(0.05, p)

def get_atm_strike(spot: float, step: int = 50) -> int:
    return int(round(spot / step) * step)

class StrategyPosition:
    def __init__(self, leg_id: str, symbol: str, opt_type: str, strike: int, qty: int, 
                 entry_price: float, entry_time: datetime, sl_price: Optional[float] = None, 
                 is_reentry: bool = False, branch_id: str = "", side: str = "SELL"):
        self.leg_id = leg_id
        self.symbol = symbol
        self.opt_type = opt_type # 'CE' or 'PE'
        self.strike = strike
        self.qty = qty
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.sl_price = sl_price
        self.is_reentry = is_reentry
        self.branch_id = branch_id
        self.side = side.upper() # 'BUY' or 'SELL'
        
        self.is_open = True
        self.exit_price: Optional[float] = None
        self.exit_time: Optional[datetime] = None
        self.exit_reason: str = ""
        self.pnl: float = 0.0
        self.current_price: float = entry_price
        self.worst: float = entry_price   # worst price in the latest candle (real-candle stop-losses)

    def update_price(self, price: float):
        self.current_price = price
        if self.is_open:
            if self.side == "BUY":
                # Long position: profit when price rises
                self.pnl = (price - self.entry_price) * self.qty
            else:
                # Short position: profit when price falls
                self.pnl = (self.entry_price - price) * self.qty

    def close(self, price: float, exit_time: datetime, reason: str):
        if not self.is_open:
            return
        self.is_open = False
        self.exit_price = price
        self.exit_time = exit_time
        self.exit_reason = reason
        self.current_price = price
        if self.side == "BUY":
            self.pnl = (price - self.entry_price) * self.qty
        else:
            self.pnl = (self.entry_price - price) * self.qty

class DaySimulationResult:
    def __init__(self, date: datetime.date):
        self.date = date
        self.positions: List[StrategyPosition] = []
        self.events: List[Dict[str, Any]] = []
        self.status = "COMPLETED"
        self.stop_reason = ""
        self.gross_pnl = 0.0
        self.charges = 0.0
        self.net_pnl = 0.0
        self.ce_sl_hit = False
        self.pe_sl_hit = False
        self.reentry_executed = False
        self.guard_hit = False
        self.time_exit_hit = False
        self.max_profit = 0.0
        self.max_drawdown = 0.0
        self.intraday_pnl_curve: List[Dict[str, Any]] = []

class NiftyStraddleEngine:
    def __init__(self, 
                 strategy_mode: str = "option_buying",  # "option_buying", "hedged_selling", "short_straddle", "trend_hedged_straddle"
                 capital: float = 40000.0,
                 initial_lots: Optional[int] = None,
                 reentry_lots: Optional[int] = None,
                 lot_size: int = 65,
                 sl_pct: float = 0.20,
                 reentry_wait_minutes: int = 15,
                 pl_target: Optional[float] = None,
                 pl_stop: Optional[float] = None,
                 slippage_pct: float = 0.0,
                 r: float = 0.065,
                 default_iv: float = 0.135,
                 execution_mode: str = "fyers_calibrated",
                 hedge_distance_pts: int = 300,
                 reentry_enabled: bool = True,
                 reentry_hedged: bool = False,
                 reentry_sl_pct: Optional[float] = None,
                 max_reentries: Optional[int] = None,
                 real_prices: bool = False):
        self.strategy_mode = strategy_mode.lower()
        self.capital = float(capital)
        self.initial_lots = initial_lots
        self.reentry_lots = reentry_lots
        self.lot_size = lot_size
        self.sl_pct = sl_pct
        self.reentry_wait_minutes = reentry_wait_minutes
        self.slippage_pct = slippage_pct
        self.r = r
        self.default_iv = default_iv
        self.execution_mode = execution_mode  # "fyers_calibrated" or "raw_spot_wick"
        self.hedge_distance_pts = hedge_distance_pts
        self.prev_close: Optional[float] = None
        # Re-entry risk controls (defaults keep the original naked, no-SL re-entry).
        self.reentry_enabled = reentry_enabled
        self.reentry_hedged = reentry_hedged      # hedged_selling: buy wings with the re-entry too
        self.reentry_sl_pct = reentry_sl_pct      # stop-loss % on each re-entry leg; closes that re-entry set
        self.max_reentries = max_reentries        # cap re-entry sets per day (None = one per SL trigger)
        # Real Dhan option candles (engine/option_history.py) instead of Black-Scholes. Days without
        # real candles are skipped, stop-losses trigger on the real candle high/low and fill at the SL.
        self.real_prices = real_prices
        self.book = None
        self.skipped_days: List[str] = []
        
        # P&L Guard: scale proportionally with capital if account is small (< 1 Lakh)
        if pl_target is not None and pl_target > 0 and (capital >= 100000 or pl_target <= capital * 0.25):
            self.pl_target = float(pl_target)
        else:
            # Scaled 10% daily target for small capital accounts
            self.pl_target = round(self.capital * 0.10, 0)
            
        if pl_stop is not None and pl_stop < 0 and (capital >= 100000 or abs(pl_stop) <= capital * 0.25):
            self.pl_stop = float(pl_stop)
        else:
            # Scaled 10% daily stop loss for small capital accounts
            self.pl_stop = round(-self.capital * 0.10, 0)
            
        self.trading_days = None  # set by run_backtest so expiry can account for holidays
        self.entry_time_start = time(10, 0)
        self.time_exit = time(15, 25)

    def calculate_charges(self, buy_val: float, sell_val: float, orders_count: int) -> float:
        """NSE Options Charges: Brokerage + STT + Turnover Charges + GST + Stamp Duty"""
        brokerage = orders_count * 20.0
        turnover = buy_val + sell_val
        exchange_charges = turnover * 0.00053
        sebi_charges = turnover * 0.000001
        stt = sell_val * 0.0010  # ~0.1% on sell side option premium
        stamp_duty = buy_val * 0.00003
        gst = (brokerage + exchange_charges + sebi_charges) * 0.18
        return brokerage + exchange_charges + sebi_charges + stt + stamp_duty + gst

    def get_time_to_expiry(self, current_dt: datetime) -> float:
        """Year fraction T to the current NIFTY weekly expiry (15:30 IST).

        Uses the real expiry calendar: Thursday until Aug 2025, Tuesday from Sep 2025,
        moved earlier when the expiry day is a holiday (see engine/expiry.py).
        """
        expiry = nifty_weekly_expiry(current_dt.date(), self.trading_days)
        if self.execution_mode == "fyers_calibrated":
            days_ahead = trading_days_until(current_dt.date(), expiry, self.trading_days)
            cur_mins = current_dt.hour * 60 + current_dt.minute
            today_frac = max(0.04, (15 * 60 + 30 - cur_mins) / 375.0)
            total_trading_days = today_frac + days_ahead
            return max(0.0005, total_trading_days / 252.0)
        else:
            expiry_dt = datetime.combine(expiry, time(15, 30))
            diff_seconds = max(10, (expiry_dt - current_dt).total_seconds())
            return diff_seconds / (365.0 * 86400.0)

    def price(self, spot: float, strike: int, opt: str, current_dt: datetime, iv: float, field: str = "close") -> float:
        """One leg's price: the real Dhan candle when real_prices is on, else Black-Scholes."""
        if self.real_prices:
            return self.book.price(strike, opt, current_dt, field)
        ce, pe = self.get_option_prices(spot, strike, current_dt, iv)
        return ce if opt == "CE" else pe

    def get_option_prices(self, spot: float, strike: int, current_dt: datetime, iv: float) -> Tuple[float, float]:
        T = self.get_time_to_expiry(current_dt)
        ce = bs_call_price(spot, strike, T, self.r, iv)
        pe = bs_put_price(spot, strike, T, self.r, iv)
        return ce, pe

    def simulate_day(self, day_df: pd.DataFrame, daily_iv: Optional[float] = None) -> DaySimulationResult:
        """
        Simulates 1 trading day with full parallel branch logic.
        """
        if day_df.empty:
            return DaySimulationResult(datetime.now().date())
            
        day_date = day_df.index[0].date()
        result = DaySimulationResult(day_date)
        iv = daily_iv if daily_iv is not None else self.default_iv
        
        automation_active = True
        positions: List[StrategyPosition] = []
        pending_reentries: List[Dict[str, Any]] = []
        
        has_entered = False
        ce_initial_pos: Optional[StrategyPosition] = None
        pe_initial_pos: Optional[StrategyPosition] = None
        
        eff_reentry_lots = 1

        def compute_total_pnl() -> float:
            return sum(p.pnl for p in positions)

        def apply_slippage(price: float, is_buy: bool) -> float:
            if self.slippage_pct <= 0:
                return price
            slip = price * (self.slippage_pct / 100.0)
            return (price + slip) if is_buy else max(0.05, price - slip)

        def square_off_all(current_spot: float, current_dt: datetime, reason: str, branch_id: Optional[str] = None,
                           fills: Optional[Dict[int, float]] = None):
            for pos in positions:
                if pos.is_open and (branch_id is None or pos.branch_id == branch_id):
                    market_p = (fills or {}).get(id(pos))
                    if market_p is None:
                        market_p = self.price(current_spot, pos.strike, pos.opt_type, current_dt, iv)
                    # Closing BUY position means selling to market (is_buy=False)
                    # Closing SELL position means buying back from market (is_buy=True)
                    exit_p = apply_slippage(market_p, is_buy=(pos.side == "SELL"))
                    pos.close(exit_p, current_dt, reason)
                    result.events.append({
                        'timestamp': current_dt.strftime('%H:%M:%S'),
                        'type': 'SQUARE_OFF',
                        'leg': f"{pos.side} {pos.strike} {pos.opt_type} ({pos.branch_id})",
                        'price': round(exit_p, 2),
                        'reason': reason
                    })

        for current_dt, bar in day_df.iterrows():
            if not automation_active:
                break
                
            cur_time = current_dt.time()
            if cur_time < time(9, 15) or cur_time > time(15, 30):
                continue

            open_p, high_p, low_p, close_p = bar['open'], bar['high'], bar['low'], bar['close']

            # Branch 4: Time Exit (3:25 PM)
            if cur_time >= self.time_exit:
                automation_active = False
                result.time_exit_hit = True
                result.stop_reason = "Time Exit at 3:25 PM"
                square_off_all(close_p, current_dt, "Time Exit (15:25)")
                result.events.append({
                    'timestamp': current_dt.strftime('%H:%M:%S'),
                    'type': 'STOP_AUTOMATION',
                    'reason': 'Branch 4: 3:25 PM Time Exit triggered'
                })
                break

            # Initial Entry at 10:00 AM
            if not has_entered and cur_time >= self.entry_time_start:
                has_entered = True
                atm_strike = get_atm_strike(open_p)
                ce_prem = self.price(open_p, atm_strike, "CE", current_dt, iv, "open")
                pe_prem = self.price(open_p, atm_strike, "PE", current_dt, iv, "open")

                if self.strategy_mode == "option_buying":
                    # Option Buying (Long Straddle): Buy ATM CE & Buy ATM PE
                    ce_entry = apply_slippage(ce_prem, is_buy=True)
                    pe_entry = apply_slippage(pe_prem, is_buy=True)
                    cost_per_lot = (ce_entry + pe_entry) * self.lot_size

                    # Size lots to fit capital (allocating ~70% of capital for initial entry, leaving buffer for re-entry)
                    if self.initial_lots and self.initial_lots > 0:
                        eff_lots = self.initial_lots
                    else:
                        eff_lots = max(1, int((self.capital * 0.70) / max(1.0, cost_per_lot)))

                    eff_reentry_lots = self.reentry_lots if self.reentry_lots else max(1, int(eff_lots * 0.25))
                    qty_initial = eff_lots * self.lot_size

                    # 20% stop loss below buy price
                    ce_sl = ce_entry * (1.0 - self.sl_pct)
                    pe_sl = pe_entry * (1.0 - self.sl_pct)

                    ce_initial_pos = StrategyPosition(
                        leg_id="CE_MAIN", symbol=f"NIFTY_{atm_strike}_CE", opt_type="CE",
                        strike=atm_strike, qty=qty_initial, entry_price=ce_entry,
                        entry_time=current_dt, sl_price=ce_sl, is_reentry=False, branch_id="Branch 1", side="BUY"
                    )
                    pe_initial_pos = StrategyPosition(
                        leg_id="PE_MAIN", symbol=f"NIFTY_{atm_strike}_PE", opt_type="PE",
                        strike=atm_strike, qty=qty_initial, entry_price=pe_entry,
                        entry_time=current_dt, sl_price=pe_sl, is_reentry=False, branch_id="Branch 2", side="BUY"
                    )
                    positions.extend([ce_initial_pos, pe_initial_pos])

                    invested = cost_per_lot * eff_lots
                    result.events.append({
                        'timestamp': current_dt.strftime('%H:%M:%S'),
                        'type': 'ENTRY_INITIAL',
                        'detail': f"Bought {eff_lots} lots ATM {atm_strike} Straddle (₹{invested:,.0f} invested from ₹{self.capital:,.0f} capital). CE @ ₹{ce_entry:.2f} (SL ₹{ce_sl:.2f}), PE @ ₹{pe_entry:.2f} (SL ₹{pe_sl:.2f})"
                    })

                elif self.strategy_mode == "hedged_selling":
                    # Hedged Option Selling (Iron Butterfly): Buy OTM wings + Sell ATM straddle
                    eff_lots = self.initial_lots if self.initial_lots else (1 if self.capital < 100000 else max(1, int(self.capital / 50000)))
                    eff_reentry_lots = self.reentry_lots if self.reentry_lots else 1
                    qty_initial = eff_lots * self.lot_size

                    otm_ce_strike = atm_strike + self.hedge_distance_pts
                    otm_pe_strike = atm_strike - self.hedge_distance_pts
                    otm_ce_p = self.price(open_p, otm_ce_strike, "CE", current_dt, iv, "open")
                    otm_pe_p = self.price(open_p, otm_pe_strike, "PE", current_dt, iv, "open")
                    otm_ce_entry = apply_slippage(otm_ce_p, is_buy=True)
                    otm_pe_entry = apply_slippage(otm_pe_p, is_buy=True)

                    otm_ce_pos = StrategyPosition(
                        leg_id="CE_WING", symbol=f"NIFTY_{otm_ce_strike}_CE", opt_type="CE",
                        strike=otm_ce_strike, qty=qty_initial, entry_price=otm_ce_entry,
                        entry_time=current_dt, sl_price=None, is_reentry=False, branch_id="Hedge Wing CE", side="BUY"
                    )
                    otm_pe_pos = StrategyPosition(
                        leg_id="PE_WING", symbol=f"NIFTY_{otm_pe_strike}_PE", opt_type="PE",
                        strike=otm_pe_strike, qty=qty_initial, entry_price=otm_pe_entry,
                        entry_time=current_dt, sl_price=None, is_reentry=False, branch_id="Hedge Wing PE", side="BUY"
                    )

                    ce_entry = apply_slippage(ce_prem, is_buy=False)
                    pe_entry = apply_slippage(pe_prem, is_buy=False)
                    ce_sl = ce_entry * (1.0 + self.sl_pct)
                    pe_sl = pe_entry * (1.0 + self.sl_pct)

                    ce_initial_pos = StrategyPosition(
                        leg_id="CE_MAIN", symbol=f"NIFTY_{atm_strike}_CE", opt_type="CE",
                        strike=atm_strike, qty=qty_initial, entry_price=ce_entry,
                        entry_time=current_dt, sl_price=ce_sl, is_reentry=False, branch_id="Branch 1", side="SELL"
                    )
                    pe_initial_pos = StrategyPosition(
                        leg_id="PE_MAIN", symbol=f"NIFTY_{atm_strike}_PE", opt_type="PE",
                        strike=atm_strike, qty=qty_initial, entry_price=pe_entry,
                        entry_time=current_dt, sl_price=pe_sl, is_reentry=False, branch_id="Branch 2", side="SELL"
                    )
                    positions.extend([otm_ce_pos, otm_pe_pos, ce_initial_pos, pe_initial_pos])

                    result.events.append({
                        'timestamp': current_dt.strftime('%H:%M:%S'),
                        'type': 'ENTRY_INITIAL',
                        'detail': f"Iron Butterfly (Hedged): Bought wings ({otm_ce_strike} CE @ ₹{otm_ce_entry:.2f} / {otm_pe_strike} PE @ ₹{otm_pe_entry:.2f}) & Sold {eff_lots} lot ATM {atm_strike} Straddle. CE @ ₹{ce_entry:.2f}, PE @ ₹{pe_entry:.2f}"
                    })

                elif self.strategy_mode == "trend_hedged_straddle":
                    # Short ATM straddle + one OTM wing on the side the market opened
                    # (open above yesterday's close → buy a CE wing, else a PE wing).
                    eff_lots = self.initial_lots if self.initial_lots else 1
                    eff_reentry_lots = self.reentry_lots if self.reentry_lots else 1
                    qty_initial = eff_lots * self.lot_size

                    day_open = float(day_df.iloc[0]['open'])
                    opened_up = self.prev_close is None or day_open >= self.prev_close
                    wing_type = "CE" if opened_up else "PE"
                    wing_strike = atm_strike + self.hedge_distance_pts if opened_up else atm_strike - self.hedge_distance_pts
                    wing_entry = apply_slippage(self.price(open_p, wing_strike, wing_type, current_dt, iv, "open"), is_buy=True)
                    wing_pos = StrategyPosition(
                        leg_id=f"{wing_type}_WING", symbol=f"NIFTY_{wing_strike}_{wing_type}", opt_type=wing_type,
                        strike=wing_strike, qty=qty_initial, entry_price=wing_entry,
                        entry_time=current_dt, sl_price=None, is_reentry=False, branch_id=f"Hedge Wing {wing_type}", side="BUY"
                    )

                    ce_entry = apply_slippage(ce_prem, is_buy=False)
                    pe_entry = apply_slippage(pe_prem, is_buy=False)
                    ce_sl = ce_entry * (1.0 + self.sl_pct)
                    pe_sl = pe_entry * (1.0 + self.sl_pct)
                    ce_initial_pos = StrategyPosition(
                        leg_id="CE_MAIN", symbol=f"NIFTY_{atm_strike}_CE", opt_type="CE",
                        strike=atm_strike, qty=qty_initial, entry_price=ce_entry,
                        entry_time=current_dt, sl_price=ce_sl, is_reentry=False, branch_id="Branch 1", side="SELL"
                    )
                    pe_initial_pos = StrategyPosition(
                        leg_id="PE_MAIN", symbol=f"NIFTY_{atm_strike}_PE", opt_type="PE",
                        strike=atm_strike, qty=qty_initial, entry_price=pe_entry,
                        entry_time=current_dt, sl_price=pe_sl, is_reentry=False, branch_id="Branch 2", side="SELL"
                    )
                    positions.extend([wing_pos, ce_initial_pos, pe_initial_pos])

                    gap = f"{day_open - self.prev_close:+.0f} pts vs prev close" if self.prev_close else "no prev close"
                    result.events.append({
                        'timestamp': current_dt.strftime('%H:%M:%S'),
                        'type': 'ENTRY_INITIAL',
                        'detail': f"Trend-Hedged Straddle: market opened {'up' if opened_up else 'down'} ({gap}). Bought {wing_strike} {wing_type} wing @ ₹{wing_entry:.2f} & Sold {eff_lots} lot ATM {atm_strike} Straddle. CE @ ₹{ce_entry:.2f}, PE @ ₹{pe_entry:.2f}"
                    })

                else:
                    # Classic Naked Short Straddle
                    eff_lots = self.initial_lots if self.initial_lots else 10
                    eff_reentry_lots = self.reentry_lots if self.reentry_lots else 1
                    qty_initial = eff_lots * self.lot_size

                    ce_entry = apply_slippage(ce_prem, is_buy=False)
                    pe_entry = apply_slippage(pe_prem, is_buy=False)
                    ce_sl = ce_entry * (1.0 + self.sl_pct)
                    pe_sl = pe_entry * (1.0 + self.sl_pct)

                    ce_initial_pos = StrategyPosition(
                        leg_id="CE_MAIN", symbol=f"NIFTY_{atm_strike}_CE", opt_type="CE",
                        strike=atm_strike, qty=qty_initial, entry_price=ce_entry,
                        entry_time=current_dt, sl_price=ce_sl, is_reentry=False, branch_id="Branch 1", side="SELL"
                    )
                    pe_initial_pos = StrategyPosition(
                        leg_id="PE_MAIN", symbol=f"NIFTY_{atm_strike}_PE", opt_type="PE",
                        strike=atm_strike, qty=qty_initial, entry_price=pe_entry,
                        entry_time=current_dt, sl_price=pe_sl, is_reentry=False, branch_id="Branch 2", side="SELL"
                    )
                    positions.extend([ce_initial_pos, pe_initial_pos])

                    result.events.append({
                        'timestamp': current_dt.strftime('%H:%M:%S'),
                        'type': 'ENTRY_INITIAL',
                        'detail': f"Sold {eff_lots} lots ATM {atm_strike} Straddle. CE @ ₹{ce_entry:.2f} (SL ₹{ce_sl:.2f}), PE @ ₹{pe_entry:.2f} (SL ₹{pe_sl:.2f})"
                    })

            # Check Pending Re-entries whose 15 minutes wait has elapsed
            if pending_reentries and automation_active:
                remaining_pending = []
                for re_item in pending_reentries:
                    if current_dt >= re_item['trigger_time']:
                        reentries_done = len({p.branch_id for p in positions if p.is_reentry and p.leg_id.startswith(("CE_RE", "PE_RE"))})
                        if self.max_reentries is not None and reentries_done >= self.max_reentries:
                            continue
                        new_atm = get_atm_strike(open_p)
                        ce_re_p = self.price(open_p, new_atm, "CE", current_dt, iv, "open")
                        pe_re_p = self.price(open_p, new_atm, "PE", current_dt, iv, "open")
                        is_buy_mode = (self.strategy_mode == "option_buying")
                        ce_re_entry = apply_slippage(ce_re_p, is_buy=is_buy_mode)
                        pe_re_entry = apply_slippage(pe_re_p, is_buy=is_buy_mode)
                        re_qty = eff_reentry_lots * self.lot_size
                        side_label = "BUY" if is_buy_mode else "SELL"
                        re_branch = f"{re_item['trigger_branch']} Re-entry"
                        if self.reentry_sl_pct:
                            mult = (1.0 - self.reentry_sl_pct) if is_buy_mode else (1.0 + self.reentry_sl_pct)
                            ce_re_sl, pe_re_sl = ce_re_entry * mult, pe_re_entry * mult
                        else:
                            ce_re_sl = pe_re_sl = None
                        if self.reentry_hedged and self.strategy_mode == "hedged_selling":
                            w_ce_strike, w_pe_strike = new_atm + self.hedge_distance_pts, new_atm - self.hedge_distance_pts
                            w_ce_p = self.price(open_p, w_ce_strike, "CE", current_dt, iv, "open")
                            w_pe_p = self.price(open_p, w_pe_strike, "PE", current_dt, iv, "open")
                            positions.extend([
                                StrategyPosition(leg_id=f"CE_REWING_{len(positions)}", symbol=f"NIFTY_{w_ce_strike}_CE", opt_type="CE",
                                                 strike=w_ce_strike, qty=re_qty, entry_price=apply_slippage(w_ce_p, is_buy=True),
                                                 entry_time=current_dt, sl_price=None, is_reentry=True, branch_id=re_branch, side="BUY"),
                                StrategyPosition(leg_id=f"PE_REWING_{len(positions)+1}", symbol=f"NIFTY_{w_pe_strike}_PE", opt_type="PE",
                                                 strike=w_pe_strike, qty=re_qty, entry_price=apply_slippage(w_pe_p, is_buy=True),
                                                 entry_time=current_dt, sl_price=None, is_reentry=True, branch_id=re_branch, side="BUY"),
                            ])

                        re_ce_pos = StrategyPosition(
                            leg_id=f"CE_RE_{len(positions)}", symbol=f"NIFTY_{new_atm}_CE", opt_type="CE",
                            strike=new_atm, qty=re_qty, entry_price=ce_re_entry,
                            entry_time=current_dt, sl_price=ce_re_sl, is_reentry=True, 
                            branch_id=re_branch, side=side_label
                        )
                        re_pe_pos = StrategyPosition(
                            leg_id=f"PE_RE_{len(positions)+1}", symbol=f"NIFTY_{new_atm}_PE", opt_type="PE",
                            strike=new_atm, qty=re_qty, entry_price=pe_re_entry,
                            entry_time=current_dt, sl_price=pe_re_sl, is_reentry=True, 
                            branch_id=re_branch, side=side_label
                        )
                        positions.extend([re_ce_pos, re_pe_pos])
                        result.reentry_executed = True

                        result.events.append({
                            'timestamp': current_dt.strftime('%H:%M:%S'),
                            'type': 'RE_ENTRY',
                            'detail': f"Re-entered {eff_reentry_lots} lot ATM {new_atm} Straddle ({side_label}) after 15m wait ({re_item['trigger_branch']}). CE @ ₹{ce_re_entry:.2f}, PE @ ₹{pe_re_entry:.2f}"
                        })
                    else:
                        remaining_pending.append(re_item)
                pending_reentries = remaining_pending

            # Evaluate Price Movements within this candle/interval
            if self.execution_mode == "fyers_calibrated":
                path_spots = [close_p]
            else:
                path_spots = [open_p, high_p, low_p, close_p] if open_p > close_p else [open_p, low_p, high_p, close_p]

            for spot_step in path_spots:
                if not automation_active:
                    break

                for pos in positions:
                    if pos.is_open:
                        pos.update_price(self.price(spot_step, pos.strike, pos.opt_type, current_dt, iv))
                        # Real candles: a stop-loss triggers on the candle's worst price for the leg.
                        pos.worst = pos.current_price
                        if self.real_prices:
                            b = self.book.bar(pos.strike, pos.opt_type, current_dt)
                            if b is not None:
                                pos.worst = float(b["high"]) if pos.side == "SELL" else float(b["low"])

                def sl_fill(pos) -> float:
                    """Real candles: fill at the SL, or at the candle open if it gapped past it."""
                    if not self.real_prices:
                        return pos.current_price
                    b = self.book.bar(pos.strike, pos.opt_type, current_dt)
                    o = float(b["open"]) if b is not None else pos.sl_price
                    return max(o, pos.sl_price) if pos.side == "SELL" else min(o, pos.sl_price)

                ce_sl_triggered = False
                pe_sl_triggered = False

                def sl_hit(pos) -> bool:
                    if not (pos and pos.is_open and pos.sl_price):
                        return False
                    return pos.worst <= pos.sl_price if pos.side == "BUY" else pos.worst >= pos.sl_price

                ce_sl_triggered = sl_hit(ce_initial_pos)
                pe_sl_triggered = sl_hit(pe_initial_pos)

                if ce_sl_triggered or pe_sl_triggered:
                    if ce_sl_triggered:
                        result.ce_sl_hit = True
                        result.events.append({
                            'timestamp': current_dt.strftime('%H:%M:%S'),
                            'type': 'SL_TRIGGERED',
                            'detail': f"Branch 1 CE SL Hit: Premium ₹{ce_initial_pos.worst:.2f} vs SL ₹{ce_initial_pos.sl_price:.2f}"
                        })
                        if self.reentry_enabled:
                            pending_reentries.append({
                                'trigger_time': current_dt + timedelta(minutes=self.reentry_wait_minutes),
                                'trigger_branch': 'Branch 1 CE SL'
                            })

                    if pe_sl_triggered:
                        result.pe_sl_hit = True
                        result.events.append({
                            'timestamp': current_dt.strftime('%H:%M:%S'),
                            'type': 'SL_TRIGGERED',
                            'detail': f"Branch 2 PE SL Hit: Premium ₹{pe_initial_pos.worst:.2f} vs SL ₹{pe_initial_pos.sl_price:.2f}"
                        })
                        if self.reentry_enabled:
                            pending_reentries.append({
                                'trigger_time': current_dt + timedelta(minutes=self.reentry_wait_minutes),
                                'trigger_branch': 'Branch 2 PE SL'
                            })

                    square_off_reason = "Branch 1 & 2 SL Hit" if (ce_sl_triggered and pe_sl_triggered) else ("Branch 1 CE SL Hit" if ce_sl_triggered else "Branch 2 PE SL Hit")
                    fills = {id(p): sl_fill(p) for p, hit in ((ce_initial_pos, ce_sl_triggered), (pe_initial_pos, pe_sl_triggered)) if hit}
                    square_off_all(spot_step, current_dt, square_off_reason, fills=fills)

                # Re-entry legs with their own stop-loss: close only that re-entry set
                if self.reentry_sl_pct:
                    for pos in positions:
                        if not (pos.is_open and pos.is_reentry and pos.sl_price):
                            continue
                        if sl_hit(pos):
                            result.events.append({
                                'timestamp': current_dt.strftime('%H:%M:%S'),
                                'type': 'SL_TRIGGERED',
                                'detail': f"Re-entry {pos.opt_type} SL Hit: Premium ₹{pos.worst:.2f} vs SL ₹{pos.sl_price:.2f}"
                            })
                            square_off_all(spot_step, current_dt, "Re-entry SL Hit", branch_id=pos.branch_id,
                                           fills={id(pos): sl_fill(pos)})

                # Branch 3: P&L guard
                open_pos_count = sum(1 for p in positions if p.is_open)
                total_pnl = compute_total_pnl()

                if open_pos_count > 0:
                    if total_pnl >= self.pl_target:
                        automation_active = False
                        result.guard_hit = True
                        result.stop_reason = f"P&L Guard Profit Target Hit (+₹{total_pnl:,.2f} >= +₹{self.pl_target:,.2f})"
                        result.events.append({
                            'timestamp': current_dt.strftime('%H:%M:%S'),
                            'type': 'GUARD_HIT',
                            'detail': result.stop_reason
                        })
                        square_off_all(spot_step, current_dt, f"P&L Target Hit (+₹{self.pl_target:,.0f})")
                        break
                    elif total_pnl <= self.pl_stop:
                        automation_active = False
                        result.guard_hit = True
                        result.stop_reason = f"P&L Guard Max Loss Hit (₹{total_pnl:,.2f} <= ₹{self.pl_stop:,.2f})"
                        result.events.append({
                            'timestamp': current_dt.strftime('%H:%M:%S'),
                            'type': 'GUARD_HIT',
                            'detail': result.stop_reason
                        })
                        square_off_all(spot_step, current_dt, f"P&L Stop Hit (₹{self.pl_stop:,.0f})")
                        break

            cur_pnl = compute_total_pnl()
            result.max_profit = max(result.max_profit, cur_pnl)
            result.max_drawdown = min(result.max_drawdown, cur_pnl)
            result.intraday_pnl_curve.append({
                'time': current_dt.strftime('%H:%M'),
                'spot': round(close_p, 2),
                'pnl': round(cur_pnl, 2),
                'open_positions': sum(1 for p in positions if p.is_open)
            })

        if any(p.is_open for p in positions):
            last_dt = day_df.index[-1]
            last_spot = day_df.iloc[-1]['close']
            square_off_all(last_spot, last_dt, "End of Day Market Close")

        total_buy_val = 0.0
        total_sell_val = 0.0
        gross_pnl = 0.0
        order_count = 0

        for pos in positions:
            gross_pnl += pos.pnl
            if pos.side == "BUY":
                entry_v = pos.entry_price * pos.qty
                exit_v = (pos.exit_price or pos.entry_price) * pos.qty
                total_buy_val += entry_v
                total_sell_val += exit_v
            else:
                sell_v = pos.entry_price * pos.qty
                buy_v = (pos.exit_price or pos.entry_price) * pos.qty
                total_sell_val += sell_v
                total_buy_val += buy_v
            order_count += 2

        charges = self.calculate_charges(total_buy_val, total_sell_val, order_count) if order_count > 0 else 0.0
        net_pnl = gross_pnl - charges

        result.positions = positions
        result.gross_pnl = gross_pnl
        result.charges = charges
        result.net_pnl = net_pnl
        return result

    def run_backtest(self, historical_data_by_day: Dict[date, pd.DataFrame]) -> Dict[str, Any]:
        daily_results: List[DaySimulationResult] = []
        trade_logs: List[Dict[str, Any]] = []
        cumulative_pnl = 0.0
        equity_curve: List[Dict[str, Any]] = []
        
        sorted_dates = sorted(historical_data_by_day.keys())
        # Dates with data double as the holiday calendar for expiry dates.
        self.trading_days = set(sorted_dates)
        self.prev_close = None  # trend_hedged_straddle compares today's open with this
        for d in sorted_dates:
            day_df = historical_data_by_day[d]
            if len(day_df) < 10:
                continue
            if self.real_prices:
                from engine.option_history import NoRealPrice, load_day
                self.book = load_day(d, day_df)
                if self.book is None:
                    self.skipped_days.append(f"{d}: no Dhan option candles")
                    self.prev_close = float(day_df.iloc[-1]['close'])
                    continue
                try:
                    res = self.simulate_day(day_df)
                except NoRealPrice as exc:
                    self.skipped_days.append(str(exc))
                    self.prev_close = float(day_df.iloc[-1]['close'])
                    continue
            else:
                res = self.simulate_day(day_df)
            self.prev_close = float(day_df.iloc[-1]['close'])
            daily_results.append(res)
            cumulative_pnl += res.net_pnl
            
            main_ce = next((p for p in res.positions if p.leg_id == "CE_MAIN"), None)
            main_pe = next((p for p in res.positions if p.leg_id == "PE_MAIN"), None)
            re_positions = [p for p in res.positions if p.is_reentry]
            
            exit_reason_summary = res.stop_reason or ("3:25 PM Time Exit" if res.time_exit_hit else ("P&L Guard Hit" if res.guard_hit else "Normal Exit"))
            
            side_text = "BUY" if self.strategy_mode == "option_buying" else "SELL"
            
            trade_logs.append({
                'date': d.strftime('%Y-%m-%d'),
                'day_name': d.strftime('%a'),
                'entry_time': "10:00 AM",
                'spot_at_entry': round(day_df.iloc[0]['open'], 1),
                'side': side_text,
                'ce_strike': main_ce.strike if main_ce else "-",
                'pe_strike': main_pe.strike if main_pe else "-",
                'ce_entry': round(main_ce.entry_price, 2) if main_ce else 0.0,
                'ce_exit': round(main_ce.exit_price or 0.0, 2) if main_ce else 0.0,
                'ce_sl_hit': res.ce_sl_hit,
                'pe_entry': round(main_pe.entry_price, 2) if main_pe else 0.0,
                'pe_exit': round(main_pe.exit_price or 0.0, 2) if main_pe else 0.0,
                'pe_sl_hit': res.pe_sl_hit,
                'reentry_count': sum(1 for p in re_positions if p.leg_id.startswith('CE_RE_')),
                'reentry_pnl': round(sum(p.pnl for p in re_positions), 2),
                'exit_reason': exit_reason_summary,
                'gross_pnl': round(res.gross_pnl, 2),
                'charges': round(res.charges, 2),
                'net_pnl': round(res.net_pnl, 2),
                'cum_pnl': round(cumulative_pnl, 2),
                'max_intraday_profit': round(res.max_profit, 2),
                'max_intraday_dd': round(res.max_drawdown, 2),
                'events_count': len(res.events),
                'positions_detail': [
                    {
                        'leg': p.symbol,
                        'type': p.opt_type,
                        'side': p.side,
                        'qty': p.qty,
                        'branch': p.branch_id,
                        'entry_price': round(p.entry_price, 2),
                        'exit_price': round(p.exit_price or 0.0, 2),
                        'entry_time': p.entry_time.strftime('%H:%M') if p.entry_time else '',
                        'exit_time': p.exit_time.strftime('%H:%M') if getattr(p, 'exit_time', None) else '',
                        'pnl': round(p.pnl, 2),
                        'exit_reason': p.exit_reason
                    }
                    for p in res.positions
                ],
                'events': res.events,
                'intraday_curve': res.intraday_pnl_curve
            })
            equity_curve.append({
                'date': d.strftime('%Y-%m-%d'),
                'daily_pnl': round(res.net_pnl, 2),
                'cumulative_pnl': round(cumulative_pnl, 2)
            })

        total_days = len(daily_results)
        if total_days == 0:
            return {
                'strategy_mode': self.strategy_mode,
                'capital': self.capital,
                'total_days': 0, 'win_days': 0, 'loss_days': 0, 'win_rate': 0.0,
                'total_net_pnl': 0.0, 'total_gross_pnl': 0.0, 'total_charges': 0.0,
                'trade_logs': [], 'equity_curve': []
            }

        daily_pnls = [r.net_pnl for r in daily_results]
        wins = [p for p in daily_pnls if p > 0]
        losses = [p for p in daily_pnls if p <= 0]
        
        win_rate = (len(wins) / total_days) * 100.0 if total_days > 0 else 0.0
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(abs(np.mean(losses))) if losses else 0.0
        profit_factor = float(sum(wins) / abs(sum(losses))) if (losses and sum(losses) != 0) else 999.0

        cum_series = np.cumsum(daily_pnls)
        high_water_mark = np.maximum.accumulate(cum_series)
        drawdowns = high_water_mark - cum_series
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        std_pnl = float(np.std(daily_pnls)) if len(daily_pnls) > 1 else 1.0
        sharpe_ratio = float((np.mean(daily_pnls) / std_pnl * np.sqrt(252))) if std_pnl > 0 else 0.0

        downside_pnls = [p for p in daily_pnls if p < 0]
        downside_std = float(np.std(downside_pnls)) if len(downside_pnls) > 1 else 1.0
        sortino_ratio = float((np.mean(daily_pnls) / downside_std * np.sqrt(252))) if downside_std > 0 else 0.0

        ce_sl_count = sum(1 for r in daily_results if r.ce_sl_hit)
        pe_sl_count = sum(1 for r in daily_results if r.pe_sl_hit)
        both_sl_count = sum(1 for r in daily_results if r.ce_sl_hit and r.pe_sl_hit)
        reentry_days_count = sum(1 for r in daily_results if r.reentry_executed)
        guard_hit_count = sum(1 for r in daily_results if r.guard_hit)
        guard_target_count = sum(1 for r in daily_results if r.guard_hit and r.net_pnl > 0)
        guard_stop_count = sum(1 for r in daily_results if r.guard_hit and r.net_pnl <= 0)
        time_exit_count = sum(1 for r in daily_results if r.time_exit_hit)

        monthly_dict: Dict[int, Dict[int, float]] = {}
        for r in daily_results:
            yr = r.date.year
            mo = r.date.month
            if yr not in monthly_dict:
                monthly_dict[yr] = {m: 0.0 for m in range(1, 13)}
            monthly_dict[yr][mo] += r.net_pnl

        monthly_matrix = []
        for yr in sorted(monthly_dict.keys()):
            row = {'year': yr, 'total': round(sum(monthly_dict[yr].values()), 2)}
            for m in range(1, 13):
                row[f'm_{m}'] = round(monthly_dict[yr][m], 2)
            monthly_matrix.append(row)

        fy_dict: Dict[str, Dict[str, Any]] = {}
        total_order_trades = 0
        for r in daily_results:
            yr = r.date.year
            mo = r.date.month
            fy_label = f"FY{yr-2000}-{yr-1999}" if mo >= 4 else f"FY{yr-2001}-{yr-2000}"
            if fy_label not in fy_dict:
                fy_dict[fy_label] = {'pnl': 0.0, 'traded_days': 0, 'profit_days': 0, 'loss_days': 0}
            fy_dict[fy_label]['traded_days'] += 1
            fy_dict[fy_label]['pnl'] += r.net_pnl
            if r.net_pnl > 0:
                fy_dict[fy_label]['profit_days'] += 1
            else:
                fy_dict[fy_label]['loss_days'] += 1
            total_order_trades += len(r.positions) * 2

        fiscal_year_breakdown = [
            {
                'fy': fy,
                'pnl': round(data['pnl'], 2),
                'traded_days': data['traded_days'],
                'profit_days': data['profit_days'],
                'loss_days': data['loss_days'],
                'win_rate': round((data['profit_days'] / data['traded_days']) * 100.0, 1) if data['traded_days'] > 0 else 0.0
            }
            for fy, data in sorted(fy_dict.items(), reverse=True)
        ]

        total_net = sum(daily_pnls)
        roi_pct = round((total_net / self.capital) * 100.0, 2) if self.capital > 0 else 0.0
        max_dd_pct = round((max_dd / self.capital) * 100.0, 2) if self.capital > 0 else 0.0

        return {
            'strategy_mode': self.strategy_mode,
            'capital': self.capital,
            'roi_pct': roi_pct,
            'execution_mode': self.execution_mode,
            'total_days': total_days,
            'total_trades': total_order_trades,
            'win_days': len(wins),
            'loss_days': len(losses),
            'win_rate': round(win_rate, 2),
            'total_net_pnl': round(total_net, 2),
            'total_gross_pnl': round(sum(r.gross_pnl for r in daily_results), 2),
            'total_charges': round(sum(r.charges for r in daily_results), 2),
            'avg_daily_pnl': round(float(np.mean(daily_pnls)), 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'max_drawdown': round(max_dd, 2),
            'max_drawdown_pct': max_dd_pct,
            'sharpe_ratio': round(sharpe_ratio, 2),
            'sortino_ratio': round(sortino_ratio, 2),
            'best_day': round(float(max(daily_pnls)), 2) if daily_pnls else 0.0,
            'worst_day': round(float(min(daily_pnls)), 2) if daily_pnls else 0.0,
            'branch_breakdown': {
                'ce_sl_count': ce_sl_count,
                'pe_sl_count': pe_sl_count,
                'both_sl_count': both_sl_count,
                'reentry_days_count': reentry_days_count,
                'guard_target_count': guard_target_count,
                'guard_stop_count': guard_stop_count,
                'time_exit_count': time_exit_count
            },
            'fiscal_year_breakdown': fiscal_year_breakdown,
            'monthly_matrix': monthly_matrix,
            'trade_logs': trade_logs,
            'equity_curve': equity_curve
        }
