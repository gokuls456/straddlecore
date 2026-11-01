"""
Paper Trading Engine for NIFTY 50 Opening Range Strategy.
Supports:
1. Live Paper Trading: Real-time paper execution via FYERS WebSocket & CandleAggregator
2. 30-Session Replay Paper Trading: High-fidelity simulation of at least 30 full trading days
   with full audit logging (CSV / JSON) and live terminal dashboard.
"""

import os
import time
import json
import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from pathlib import Path
import pandas as pd

from config import StrategyConfig, FyersConfig
from strategy import NiftyOpeningRangeStrategy, Signal
from risk_manager import RiskManager
from state_manager import StateManager
from data_feed import CandleAggregator, MarketDataProvider
from fyers_client import FyersClient

logger = logging.getLogger("paper_trader")
LOG_DIR = Path("logs")


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    MAGENTA = '\033[95m'


class PaperTrader:
    """Simulates paper trading with strict execution tracking and safety gates."""

    def __init__(self, strat_config: StrategyConfig, fyers_config: FyersConfig, fyers_client: Optional[FyersClient] = None):
        self.config = strat_config
        self.fyers_config = fyers_config
        self.fyers_client = fyers_client
        
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file_csv = LOG_DIR / "paper_trades.csv"
        self.log_file_json = LOG_DIR / "paper_trades.json"
        
        self.state_mgr = StateManager()
        self.strategy = NiftyOpeningRangeStrategy(self.config)
        self.risk_mgr = RiskManager(self.config)
        self.candle_aggregator = CandleAggregator(interval_minutes=5, on_candle_close=self.on_candle_close)
        
        self.active_position: Optional[Dict[str, Any]] = None
        self.pending_signal: Optional[Signal] = None
        self.daily_pnl = 0.0
        self.session_count = 0
        self.paper_history: List[Dict[str, Any]] = []
        
        # Load from state if resuming today
        self._sync_from_state()

    def _sync_from_state(self):
        """Restores state from disk on startup."""
        st = self.state_mgr.state
        if st["opening_range"]["finalized"] and st["opening_range"]["orh"] is not None:
            self.strategy.set_opening_range(
                orh=st["opening_range"]["orh"],
                orl=st["opening_range"]["orl"],
                date_obj=datetime.strptime(st["date"], "%Y-%m-%d").date()
            )
        self.risk_mgr.trades_today = st["trade_metrics"]["trades_today"]
        self.risk_mgr.daily_pnl = st["trade_metrics"]["daily_pnl"]
        self.active_position = st.get("active_position")

    def display_dashboard(self, current_candle: Optional[pd.Series] = None, ltp: Optional[float] = None, feed_status: str = "ONLINE"):
        """Displays rich terminal status dashboard."""
        os.system("clear" if os.name == "posix" else "cls")
        
        orh = self.strategy.orh
        orl = self.strategy.orl
        or_sz = self.strategy.or_size
        
        daily_loss_rem = max(0.0, self.config.max_daily_loss - abs(min(0.0, self.risk_mgr.daily_pnl)))
        pnl_color = Colors.GREEN if self.risk_mgr.daily_pnl >= 0 else Colors.RED
        
        print(f"{Colors.BOLD}{Colors.CYAN}========================================================================{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.YELLOW}  ★ NIFTY 50 OPENING RANGE STRATEGY — PAPER TRADING MONITOR ★  {Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}  [SAFETY LOCK: PAPER TRADING ONLY | REAL ORDERS STRICTLY BLOCKED]{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}========================================================================{Colors.RESET}")
        print(f" Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} IST | Data-Feed: {Colors.GREEN}{feed_status}{Colors.RESET}")
        print(f" Capital: ₹{self.config.capital:,.2f} | Lot Size: {self.config.lot_size} | Slippage: {self.config.slippage_pts} pts")
        print("-" * 72)
        
        # Range Status
        if self.strategy.opening_range_finalized and orh is not None:
            print(f" Opening Range (09:15-09:30): {Colors.GREEN}FINALIZED{Colors.RESET}")
            print(f" OR High: {Colors.BOLD}{orh:.2f}{Colors.RESET} | OR Low: {Colors.BOLD}{orl:.2f}{Colors.RESET} | Size: {Colors.BOLD}{or_sz:.2f}{Colors.RESET} pts")
        else:
            print(f" Opening Range (09:15-09:30): {Colors.YELLOW}FORMING... (Candles: {len(self.strategy.range_candles)}/3){Colors.RESET}")
            
        print("-" * 72)
        
        # Setup Status
        pending_str = self.strategy.pending_breakout_type or "NONE"
        print(f" Active Setup Status:     {Colors.BOLD}{pending_str}{Colors.RESET}")
        print(f" Daily Trades Taken:      {self.risk_mgr.trades_today} / {self.config.max_trades_per_day}")
        print(f" Realized P&L Today:      {pnl_color}₹{self.risk_mgr.daily_pnl:,.2f}{Colors.RESET}")
        print(f" Daily Risk Budget Left:  ₹{daily_loss_rem:,.2f} (Limit: -₹{self.config.max_daily_loss:,.2f})")
        print("-" * 72)
        
        # Active Position Details
        if self.active_position:
            pos = self.active_position
            curr_price = ltp or (current_candle["close"] if current_candle is not None else pos["entry_price"])
            unrealized_pts = (curr_price - pos["entry_price"]) if pos["direction"] == "BUY" else (pos["entry_price"] - curr_price)
            unrealized_pnl = unrealized_pts * pos["qty"]
            upnl_col = Colors.GREEN if unrealized_pnl >= 0 else Colors.RED
            
            print(f"{Colors.BOLD}{Colors.MAGENTA}>>> OPEN POSITION <<<{Colors.RESET}")
            print(f" Direction: {Colors.BOLD}{pos['direction']}{Colors.RESET} | Setup: {pos['setup_name']} | Qty: {pos['qty']}")
            print(f" Entry Price: {pos['entry_price']:.2f} | Stop-Loss: {pos['stop_loss']:.2f} | Target: {pos['target']:.2f}")
            print(f" Current Price: {curr_price:.2f} | Unrealized P&L: {upnl_col}₹{unrealized_pnl:,.2f} ({unrealized_pts:+.1f} pts){Colors.RESET}")
        else:
            print(f" Position: {Colors.CYAN}NO OPEN POSITION (SCANNING FOR CANDLE CONFIRMATION...){Colors.RESET}")
            
        print("-" * 72)
        
        # Recent Paper Trades Table
        print(f"{Colors.BOLD}Recent Paper Trades (Today):{Colors.RESET}")
        if self.paper_history:
            print(f"{'Time':<10} {'Setup':<16} {'Side':<5} {'Entry':<8} {'Exit':<8} {'Net P&L':<12} {'Exit Reason':<15}")
            for t in self.paper_history[-5:]:
                tc = Colors.GREEN if t["net_pnl"] >= 0 else Colors.RED
                print(f"{t['entry_time'][-8:]:<10} {t['setup_name'][:15]:<16} {t['direction']:<5} {t['entry_price']:<8.1f} {t['exit_price']:<8.1f} {tc}₹{t['net_pnl']:<11,.2f}{Colors.RESET} {t['exit_reason']:<15}")
        else:
            print("  No trades recorded today yet.")
        print(f"{Colors.BOLD}{Colors.CYAN}========================================================================{Colors.RESET}\n")

    def on_candle_close(self, candle: pd.Series):
        """Callback executed whenever a 5-minute candle finishes."""
        c_time = candle.name.time() if hasattr(candle.name, "time") else datetime.now().time()
        
        # 1. Execute pending signal if exists
        if self.pending_signal and self.active_position is None:
            is_approved, qty, risk_reason = self.risk_mgr.validate_signal(self.pending_signal)
            if is_approved:
                exec_price = candle["open"] + (self.config.slippage_pts if self.pending_signal.direction == "BUY" else -self.config.slippage_pts)
                self.active_position = {
                    "entry_time": candle.name.isoformat() if hasattr(candle.name, "isoformat") else str(candle.name),
                    "setup_name": self.pending_signal.setup_name,
                    "direction": self.pending_signal.direction,
                    "qty": qty,
                    "entry_price": round(exec_price, 2),
                    "stop_loss": self.pending_signal.stop_loss,
                    "target": self.pending_signal.target,
                    "sl_points": self.pending_signal.sl_points
                }
                self.state_mgr.set_active_position(self.active_position)
                logger.info(f"PAPER ORDER FILLED: {self.active_position['direction']} {qty} qty @ {exec_price:.2f} (Setup: {self.active_position['setup_name']})")
            else:
                logger.warning(f"Paper trade rejected: {risk_reason}")
            self.pending_signal = None

        # 2. Check exit conditions for active trade
        if self.active_position is not None:
            pos = self.active_position
            direction = pos["direction"]
            sl = pos["stop_loss"]
            tp = pos["target"]
            h = candle["high"]
            l = candle["low"]
            c = candle["close"]
            
            exit_reason = None
            exit_price = None

            if direction == "BUY":
                hit_sl = l <= sl
                hit_tp = h >= tp
                if hit_sl and hit_tp:
                    exit_reason = "STOP_LOSS (Conservative Simultaneous)"
                    exit_price = sl - self.config.slippage_pts
                elif hit_sl:
                    exit_reason = "STOP_LOSS"
                    exit_price = sl - self.config.slippage_pts
                elif hit_tp:
                    exit_reason = "TARGET"
                    exit_price = tp - self.config.slippage_pts
                elif c_time >= self.config.square_off_time:
                    exit_reason = "EOD_SQUARE_OFF"
                    exit_price = c - self.config.slippage_pts
            elif direction == "SELL":
                hit_sl = h >= sl
                hit_tp = l <= tp
                if hit_sl and hit_tp:
                    exit_reason = "STOP_LOSS (Conservative Simultaneous)"
                    exit_price = sl + self.config.slippage_pts
                elif hit_sl:
                    exit_reason = "STOP_LOSS"
                    exit_price = sl + self.config.slippage_pts
                elif hit_tp:
                    exit_reason = "TARGET"
                    exit_price = tp + self.config.slippage_pts
                elif c_time >= self.config.square_off_time:
                    exit_reason = "EOD_SQUARE_OFF"
                    exit_price = c + self.config.slippage_pts

            if exit_reason is not None and exit_price is not None:
                # Trade exit
                qty = pos["qty"]
                en = pos["entry_price"]
                ex = round(exit_price, 2)
                pts = (ex - en) if direction == "BUY" else (en - ex)
                gross_pnl = round(pts * qty, 2)
                costs = self.risk_mgr.calculate_costs(en, ex, qty)
                net_pnl = round(gross_pnl - costs.total_costs, 2)

                trade_record = {
                    "entry_time": pos["entry_time"],
                    "exit_time": candle.name.isoformat() if hasattr(candle.name, "isoformat") else str(candle.name),
                    "setup_name": pos["setup_name"],
                    "direction": direction,
                    "qty": qty,
                    "entry_price": en,
                    "exit_price": ex,
                    "stop_loss": sl,
                    "target": tp,
                    "exit_reason": exit_reason,
                    "gross_pnl": gross_pnl,
                    "total_costs": costs.total_costs,
                    "net_pnl": net_pnl,
                    "points": round(pts, 2)
                }

                self.paper_history.append(trade_record)
                self.risk_mgr.record_trade_result(net_pnl)
                self.state_mgr.record_closed_trade(trade_record)
                self._append_to_audit_log(trade_record)
                self.active_position = None
                logger.info(f"PAPER TRADE CLOSED: {exit_reason} | Net P&L: ₹{net_pnl:,.2f}")

        # 3. Check for new signals if no position and daily trades < max
        if self.active_position is None and self.pending_signal is None and self.risk_mgr.trades_today < self.config.max_trades_per_day:
            sig = self.strategy.process_candle(candle, candle_index=0)
            if sig:
                self.pending_signal = sig
                logger.info(f"PAPER SIGNAL DETECTED: {sig.direction} via {sig.setup_name} @ {sig.entry_price:.2f}")

        # Update state with opening range if finalized
        if self.strategy.opening_range_finalized and self.strategy.orh is not None:
            self.state_mgr.update_opening_range(self.strategy.orh, self.strategy.orl, self.strategy.or_size)

        self.display_dashboard(current_candle=candle)

    def _append_to_audit_log(self, trade_data: Dict[str, Any]):
        """Persists every paper trade to CSV and JSON."""
        df_row = pd.DataFrame([trade_data])
        header = not self.log_file_csv.exists()
        df_row.to_csv(self.log_file_csv, mode="a", header=header, index=False)

        # JSON log
        all_logs = []
        if self.log_file_json.exists():
            try:
                with open(self.log_file_json, "r") as f:
                    all_logs = json.load(f)
            except Exception:
                all_logs = []
        all_logs.append(trade_data)
        with open(self.log_file_json, "w") as f:
            json.dump(all_logs, f, indent=2)

    def run_replay(self, df: pd.DataFrame, min_sessions: int = 30) -> pd.DataFrame:
        """
        Replays at least 30 historical trading sessions bar-by-bar in paper trading mode.
        """
        grouped = list(df.groupby(df.index.date))
        total_available_sessions = len(grouped)
        sessions_to_run = min(total_available_sessions, max(min_sessions, 30))
        
        print(f"\n{Colors.BOLD}{Colors.CYAN}Starting 30-Session Paper Trading Replay ({sessions_to_run} sessions)...{Colors.RESET}\n")
        
        session_results = []
        
        for s_idx, (session_date, day_df) in enumerate(grouped[-sessions_to_run:], start=1):
            self.strategy.reset_day()
            self.risk_mgr.reset_daily_metrics()
            self.active_position = None
            self.pending_signal = None
            
            day_candles = [row for _, row in day_df.iterrows()]
            if len(day_candles) < 4:
                continue

            trades_before = len(self.paper_history)
            
            for candle in day_candles:
                self.on_candle_close(candle)

            trades_after = len(self.paper_history)
            day_trade_count = trades_after - trades_before
            day_pnl = sum([t["net_pnl"] for t in self.paper_history[trades_before:trades_after]])

            session_results.append({
                "session_idx": s_idx,
                "date": session_date.strftime("%Y-%m-%d"),
                "orh": self.strategy.orh,
                "orl": self.strategy.orl,
                "trades": day_trade_count,
                "pnl": round(day_pnl, 2)
            })

        print(f"\n{Colors.BOLD}{Colors.GREEN}✔ 30-Session Paper Trading Completed Successfully!{Colors.RESET}")
        print(f"Total simulated paper trades: {len(self.paper_history)}")
        print(f"Audit logs saved to: {self.log_file_csv} and {self.log_file_json}")

        res_df = pd.DataFrame(session_results)
        print("\n" + "=" * 65)
        print(f"{Colors.BOLD}{'Session Summary (Last 30 Sessions)':^65}{Colors.RESET}")
        print("=" * 65)
        print(f"{'Session':<10} {'Date':<12} {'OR High':<10} {'OR Low':<10} {'Trades':<8} {'Day Net P&L':<12}")
        print("-" * 65)
        for _, row in res_df.tail(30).iterrows():
            col = Colors.GREEN if row["pnl"] >= 0 else Colors.RED
            orh_s = f"{row['orh']:.1f}" if pd.notnull(row['orh']) else "N/A"
            orl_s = f"{row['orl']:.1f}" if pd.notnull(row['orl']) else "N/A"
            print(f"{row['session_idx']:<10} {row['date']:<12} {orh_s:<10} {orl_s:<10} {row['trades']:<8} {col}₹{row['pnl']:<11,.2f}{Colors.RESET}")
        print("=" * 65 + "\n")
        
        return res_df
