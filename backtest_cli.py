#!/usr/bin/env python3
"""
CLI Backtester for NIFTY 50 Short-Premium ATM Straddle Strategy
Usage:
    python backtest_cli.py --period "1 year" --capital 100000 --slippage 0.0
    python backtest_cli.py --period "6 months" --exclude-last-30
"""

import argparse
import sys
from datetime import datetime
from engine.data_feed import NiftyDataFeed
from engine.straddle_engine import NiftyStraddleEngine

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

def main():
    parser = argparse.ArgumentParser(description="NIFTY 50 Short Straddle with Re-entry & P&L Guard Backtester")
    parser.add_argument("--period", type=str, default="1 year", help="Backtest period: '1 month', '3 months', '6 months', '1 year', '2 years'")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial Capital in INR (default: 1,00,000)")
    parser.add_argument("--slippage", type=float, default=0.0, help="Slippage percentage (default: 0.0%%)")
    parser.add_argument("--initial-lots", type=int, default=10, help="Initial straddle lots (default: 10)")
    parser.add_argument("--reentry-lots", type=int, default=1, help="Re-entry straddle lots (default: 1)")
    parser.add_argument("--lot-size", type=int, default=65, help="NIFTY lot size (default: 65)")
    parser.add_argument("--sl-pct", type=float, default=0.20, help="Leg stop loss percentage (default: 0.20 for 20%%)")
    parser.add_argument("--pl-target", type=float, default=15000.0, help="Portfolio P&L guard target (default: +15,000)")
    parser.add_argument("--pl-stop", type=float, default=-15000.0, help="Portfolio P&L guard stop-loss (default: -15,000)")
    parser.add_argument("--exclude-last-30", action="store_true", help="Exclude the last 30 days of data")
    
    args = parser.parse_args()

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.YELLOW}   NIFTY 50 SHORT-PREMIUM ATM STRADDLE BACKTEST ENGINE{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*80}{Colors.RESET}")
    print(f" Period: {args.period.upper()} | Exclude Last 30d: {args.exclude_last_30}")
    print(f" Capital: ₹{args.capital:,.2f} | Slippage: {args.slippage}% | Lot Size: {args.lot_size}")
    print(f" Initial Lots: {args.initial_lots} | Re-entry Lots: {args.reentry_lots} | Leg SL: {args.sl_pct*100:.0f}%")
    print(f" P&L Guard Limits: [ +₹{args.pl_target:,.0f} / ₹{args.pl_stop:,.0f} ] | Time Exit: 15:25 IST")
    print(f"{Colors.CYAN}{'-'*80}{Colors.RESET}")

    feed = NiftyDataFeed()
    print(f"{Colors.YELLOW}Fetching historical data for {args.period}...{Colors.RESET}")
    data = feed.get_historical_intraday_data(period=args.period, exclude_last_30=args.exclude_last_30)
    
    if not data:
        print(f"{Colors.RED}No historical data retrieved. Exiting.{Colors.RESET}")
        sys.exit(1)

    print(f"{Colors.GREEN}Successfully loaded {len(data)} trading sessions.{Colors.RESET}")
    print(f"{Colors.YELLOW}Executing 4-branch multi-leg simulation...{Colors.RESET}\n")

    engine = NiftyStraddleEngine(
        initial_lots=args.initial_lots,
        reentry_lots=args.reentry_lots,
        lot_size=args.lot_size,
        sl_pct=args.sl_pct,
        pl_target=args.pl_target,
        pl_stop=args.pl_stop,
        slippage_pct=args.slippage
    )

    results = engine.run_backtest(data)

    # Print summary table
    print(f"{Colors.BOLD}{Colors.CYAN}=========================== PERFORMANCE SUMMARY ==========================={Colors.RESET}")
    print(f" Total Sessions:      {results['total_days']}")
    win_col = Colors.GREEN if results['win_rate'] >= 50 else Colors.RED
    print(f" Win Rate:            {win_col}{results['win_rate']}%{Colors.RESET} ({results['win_days']} W / {results['loss_days']} L)")
    pnl_col = Colors.GREEN if results['total_net_pnl'] >= 0 else Colors.RED
    print(f" Total Net P&L:       {pnl_col}₹{results['total_net_pnl']:,.2f}{Colors.RESET}")
    print(f" Total Gross P&L:     ₹{results['total_gross_pnl']:,.2f}")
    print(f" Total Charges:       ₹{results['total_charges']:,.2f}")
    print(f" Profit Factor:       {results['profit_factor']:.2f}")
    print(f" Max Drawdown:        {Colors.RED}₹{results['max_drawdown']:,.2f}{Colors.RESET}")
    print(f" Sharpe Ratio:        {results['sharpe_ratio']:.2f}")
    print(f" Sortino Ratio:       {results['sortino_ratio']:.2f}")
    print(f" Avg Daily P&L:       ₹{results['avg_daily_pnl']:,.2f}")
    print(f" Avg Win / Avg Loss:  ₹{results['avg_win']:,.2f} / ₹{results['avg_loss']:,.2f}")
    print(f" Best Day / Worst:    {Colors.GREEN}₹{results['best_day']:,.2f}{Colors.RESET} / {Colors.RED}₹{results['worst_day']:,.2f}{Colors.RESET}")
    print(f"{Colors.CYAN}{'='*75}{Colors.RESET}")

    # Print Branch Trigger Analytics
    bb = results['branch_breakdown']
    print(f"\n{Colors.BOLD}{Colors.YELLOW}=========================== BRANCH TRIGGER ANALYTICS ======================={Colors.RESET}")
    print(f" Branch 1 (CE SL Hit):       {bb['ce_sl_count']} times ({(bb['ce_sl_count']/results['total_days'])*100:.1f}%)")
    print(f" Branch 2 (PE SL Hit):       {bb['pe_sl_count']} times ({(bb['pe_sl_count']/results['total_days'])*100:.1f}%)")
    print(f" Both SLs Hit (Dual Re-entry): {bb['both_sl_count']} times")
    print(f" 15m Re-entry Executions:    {bb['reentry_days_count']} days")
    print(f" Branch 3 (Guard Target Hit): {bb['guard_target_count']} days")
    print(f" Branch 3 (Guard Stop Hit):   {bb['guard_stop_count']} days")
    print(f" Branch 4 (3:25 PM Exit):    {bb['time_exit_count']} days")
    print(f"{Colors.YELLOW}{'='*75}{Colors.RESET}\n")

if __name__ == "__main__":
    main()
