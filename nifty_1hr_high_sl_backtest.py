import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import math
import argparse
import random

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

class NiftyDynamicSLSetup:
    """
    Intraday Options Strategy Analyst and Backtester for Indian Market (NSE).
    Setup: Bearish (Buying ATM Put)
    Underlying: Nifty 50 Index
    Timeframe: 3-minute candles
    Logic: Buy put at the next candle immediately after the 3rd bearish candle with body > 25 points forms.
    SL: Highest High of the preceding 1 hour.
    """

    def __init__(self, df):
        self.df = df
        
        # Strategy Parameters
        self.lot_size = 65
        self.capital = 50000
        self.reward_ratio = 2.0
        self.slippage = 1.5
        self.drop_points_threshold = 25
        self.min_drop_candles = 3
        
        # Observation Window
        self.window_start = time(9, 15)
        self.exit_time_mandatory = time(15, 0)
        
    def calculate_costs(self, buy_price, sell_price, qty):
        brokerage = 40
        turnover = (buy_price + sell_price) * qty
        exchange_charges = turnover * 0.0005
        sebi_charges = turnover * 0.000001
        stt = (sell_price * qty) * 0.00125
        stamp_duty = (buy_price * qty) * 0.00003
        gst = (brokerage + exchange_charges + sebi_charges) * 0.18
        return brokerage + exchange_charges + sebi_charges + stt + stamp_duty + gst

    def simulate_put_premium(self, spot_entry, spot_current, initial_premium=100.0, delta=0.5):
        spot_diff = spot_entry - spot_current
        return max(5.0, initial_premium + (spot_diff * delta))

    def run_backtest(self):
        days = np.unique(self.df.index.date)
        trades = []
        
        for day in days:
            day_data = self.df[self.df.index.date == day].copy()
            
            if len(day_data) < 50:
                continue
                
            # 1. Scan for drop candles (open - close > threshold)
            day_mask = (day_data.index.time >= self.window_start) & (day_data.index.time < self.exit_time_mandatory)
            scan_data = day_data[day_mask]
            
            drop_candles = scan_data[(scan_data['open'] - scan_data['close']) > self.drop_points_threshold]
            
            if len(drop_candles) < self.min_drop_candles:
                continue
                
            # 2. Get the time of the 3rd drop candle
            third_drop_time = drop_candles.index[self.min_drop_candles - 1]
            
            # 3. Entry is the next candle AFTER the 3rd drop candle
            post_drop_data = scan_data[scan_data.index > third_drop_time]
            if post_drop_data.empty:
                continue
                
            entry_candle = post_drop_data.iloc[[0]]
            entry_time_dt = entry_candle.index[0]
            spot_entry = entry_candle['open'].iloc[0]
            
            # --- NEW DYNAMIC STOP LOSS LOGIC ---
            one_hour_ago = entry_time_dt - pd.Timedelta(hours=1)
            pre_entry_data = day_data[(day_data.index >= one_hour_ago) & (day_data.index < entry_time_dt)]
            
            if pre_entry_data.empty:
                underlying_sl = spot_entry + 30 # Fallback
            else:
                underlying_sl = pre_entry_data['high'].max()
                
            underlying_sl = max(underlying_sl, spot_entry + 10) # Ensure SL is at least 10 points above entry
            
            underlying_risk = underlying_sl - spot_entry
            
            # --- PROTECTIVE RULE 1: Max Risk Skip (REMOVED) ---
            # We now allow all trades and rely strictly on Position Sizing (Rule 2) to handle risk.
            underlying_target = spot_entry - (self.reward_ratio * underlying_risk)
            # -----------------------------------
            
            # Entry Premium and Risk Parameters
            entry_premium = round(random.uniform(120, 135), 1)
            
            # --- PROTECTIVE RULE 2: True Position Sizing ---
            max_monetary_risk = self.capital * 0.05  # Risk 5% per trade max
            premium_risk_points = underlying_risk * 0.5
            monetary_risk_per_lot = premium_risk_points * self.lot_size
            
            lots_by_risk = math.floor(max_monetary_risk / monetary_risk_per_lot) if monetary_risk_per_lot > 0 else 1
            lots_by_capital = math.floor(self.capital / (entry_premium * self.lot_size))
            
            lots = min(lots_by_risk, lots_by_capital)
            
            if lots == 0:
                continue
                
            qty = lots * self.lot_size
            entry_premium_with_slippage = entry_premium + self.slippage
            
            # Manage Trade
            post_entry_data = day_data[day_data.index >= entry_time_dt]
            
            exit_time = None
            exit_premium = 0.0
            exit_reason = ""
            
            for t, row in post_entry_data.iterrows():
                current_time = t.time()
                current_spot_close = row['close']
                current_spot_low = row['low']
                current_spot_high = row['high']
                
                current_premium = self.simulate_put_premium(spot_entry, current_spot_close, entry_premium)
                
                # --- PROTECTIVE RULE 3: Time Stop ---
                time_in_trade = (t - entry_time_dt).total_seconds() / 60.0
                if time_in_trade >= 90:
                    # If spot hasn't even reached 1:1 RR, get out
                    if current_spot_low > (spot_entry - underlying_risk):
                        exit_time = t
                        exit_premium = current_premium - self.slippage
                        exit_reason = "Time Stop (90m Choppy)"
                        break
                        
                # Check Underlying SL (close above SL level)
                if current_spot_close > underlying_sl:
                    exit_time = t
                    exit_premium = current_premium - self.slippage
                    exit_reason = "1-Hr High SL Hit"
                    break
                    
                # Check Target (Spot hits target level)
                if current_spot_low <= underlying_target:
                    exit_time = t
                    exit_premium = self.simulate_put_premium(spot_entry, underlying_target, entry_premium) - self.slippage
                    exit_reason = f"Target Hit (1:{self.reward_ratio} RR)"
                    break
                    
                # Check Mandatory Exit
                if current_time >= self.exit_time_mandatory:
                    exit_time = t
                    exit_premium = current_premium - self.slippage
                    exit_reason = "Mandatory Exit (15:00)"
                    break
            
            if exit_time is None:
                exit_time = post_entry_data.index[-1]
                exit_premium = self.simulate_put_premium(spot_entry, post_entry_data.iloc[-1]['close'], entry_premium) - self.slippage
                exit_reason = "End of Data"
                
            gross_pnl = (exit_premium - entry_premium_with_slippage) * qty
            costs = self.calculate_costs(entry_premium_with_slippage, exit_premium, qty)
            net_pnl = gross_pnl - costs
            
            trades.append({
                'Date': day,
                'Trigger Time': entry_time_dt.time(),
                'Entry Spot': spot_entry,
                'SL Level': underlying_sl,
                'Target Level': underlying_target,
                'Entry Premium': entry_premium_with_slippage,
                'Lots': lots,
                'Exit Time': exit_time.time(),
                'Exit Premium': exit_premium,
                'Exit Reason': exit_reason,
                'Gross PnL': round(gross_pnl, 2),
                'Costs': round(costs, 2),
                'Net PnL': round(net_pnl, 2),
                'Drop Candles': len(drop_candles)
            })
            
        return pd.DataFrame(trades)

    def print_trades_table(self, trades_df):
        if trades_df.empty:
            return
            
        print(f"\n{Colors.CYAN}{Colors.BOLD}ALL TRADES LOG{Colors.RESET}")
        print("-" * 155)
        headers = ["Date", "Entry Time", "Spot Idx", "SL Level", "Target", "Entry Prem", "Lots", "Exit Time", "Exit Prem", "Exit Reason", "Net PnL (Rs)"]
        print(f"{Colors.BOLD}{headers[0]:<12} | {headers[1]:<10} | {headers[2]:<10} | {headers[3]:<10} | {headers[4]:<10} | {headers[5]:<10} | {headers[6]:<5} | {headers[7]:<10} | {headers[8]:<10} | {headers[9]:<25} | {headers[10]:<12}{Colors.RESET}")
        print("-" * 155)
        
        for _, row in trades_df.iterrows():
            date_str = row['Date'].strftime('%d %b %Y')
            entry_t = row['Trigger Time'].strftime('%H:%M')
            spot_val = f"{row['Entry Spot']:.1f}"
            sl_val = f"{row['SL Level']:.1f}"
            tgt_val = f"{row['Target Level']:.1f}"
            entry_p = f"{row['Entry Premium']:.1f}"
            lots_str = str(row['Lots'])
            exit_t = row['Exit Time'].strftime('%H:%M')
            exit_p = f"{row['Exit Premium']:.1f}"
            reason = str(row['Exit Reason'])[:25]
            pnl = row['Net PnL']
            
            pnl_color = Colors.GREEN if pnl > 0 else Colors.RED
            pnl_str = f"{pnl_color}{pnl:>10.2f}{Colors.RESET}"
            
            print(f"{date_str:<12} | {entry_t:<10} | {spot_val:<10} | {sl_val:<10} | {tgt_val:<10} | {entry_p:<10} | {lots_str:<5} | {exit_t:<10} | {exit_p:<10} | {reason:<25} | {pnl_str}")
        print("-" * 155)

    def print_summary(self, trades_df):
        if trades_df.empty:
            print("No trades triggered in the backtest period.")
            return
            
        wins = trades_df[trades_df['Net PnL'] > 0]
        losses = trades_df[trades_df['Net PnL'] <= 0]
        
        trade_count = len(trades_df)
        win_rate = len(wins) / trade_count * 100
        avg_win = wins['Net PnL'].mean() if not wins.empty else 0
        avg_loss = abs(losses['Net PnL'].mean()) if not losses.empty else 0
        
        expectancy = trades_df['Net PnL'].mean()
        profit_factor = wins['Net PnL'].sum() / abs(losses['Net PnL'].sum()) if not losses.empty else float('inf')
        
        trades_df['Cumulative'] = trades_df['Net PnL'].cumsum()
        trades_df['High_Water_Mark'] = trades_df['Cumulative'].cummax()
        trades_df['Drawdown'] = trades_df['High_Water_Mark'] - trades_df['Cumulative']
        max_dd = trades_df['Drawdown'].max()
        worst_day = trades_df['Net PnL'].min()
        
        print(f"\n{Colors.CYAN}{Colors.BOLD}" + "="*50)
        print("BACKTEST SUMMARY")
        print("="*50 + f"{Colors.RESET}")
        print(f"Total Trades:      {trade_count}")
        
        win_color = Colors.GREEN if win_rate >= 50 else Colors.RED
        print(f"Win Rate:          {win_color}{win_rate:.2f}%{Colors.RESET}")
        
        print(f"Average Win:       {Colors.GREEN}Rs {avg_win:.2f}{Colors.RESET}")
        print(f"Average Loss:      {Colors.RED}Rs {avg_loss:.2f}{Colors.RESET}")
        print(f"Avg Win / Avg Loss:{avg_win/avg_loss if avg_loss else 0:.2f}")
        
        exp_color = Colors.GREEN if expectancy > 0 else Colors.RED
        print(f"Expectancy:        {exp_color}Rs {expectancy:.2f} per trade{Colors.RESET}")
        
        pf_color = Colors.GREEN if profit_factor > 1 else Colors.RED
        print(f"Profit Factor:     {pf_color}{profit_factor:.2f}{Colors.RESET}")
        
        print(f"Max Drawdown:      {Colors.RED}Rs {max_dd:.2f}{Colors.RESET}")
        print(f"Worst Day:         {Colors.RED}Rs {worst_day:.2f}{Colors.RESET}")
        print(f"{Colors.CYAN}{Colors.BOLD}" + "="*50 + f"{Colors.RESET}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nifty Dynamic SL Setup Backtester")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)", default=(datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)", default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument("--real", action="store_true", help="Fetch real NIFTY 3-min candles from Dhan (needs Dhan keys + Data API plan)")
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    
    hist_data = pd.DataFrame()

    if args.real:
        print(f"\n{Colors.YELLOW}Fetching real 3-minute NIFTY candles from Dhan for {args.start} to {args.end}...{Colors.RESET}")
        try:
            from engine.dhan_client import nifty_candles

            hist_data = nifty_candles(args.start, args.end, minutes=3)
            if hist_data.empty:
                print(f"{Colors.RED}Dhan returned no candles for {args.start} to {args.end}. Falling back to dummy data...{Colors.RESET}")
            else:
                print(f"{Colors.GREEN}Fetched {len(hist_data)} real 3-min candles from Dhan.{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.RED}Error fetching Dhan data: {e}. Falling back to dummy data...{Colors.RESET}")
            hist_data = pd.DataFrame()

    if hist_data.empty:
        print(f"\n{Colors.YELLOW}Generating synthetic trend data from {args.start} to {args.end}...{Colors.RESET}")
        dates = pd.date_range(start=start_dt, end=end_dt, freq='B')
        dfs = []
        for d in dates:
            times = pd.date_range(start=f"{d.strftime('%Y-%m-%d')} 09:15", end=f"{d.strftime('%Y-%m-%d')} 15:30", freq='3min')
            np.random.seed(int(d.timestamp()))
            
            changes = np.random.normal(loc=0, scale=15, size=len(times))
            close = 24000 + np.cumsum(changes)
            open_p = close - changes
            high = np.maximum(open_p, close) + np.random.uniform(0, 5, len(times))
            low = np.minimum(open_p, close) - np.random.uniform(0, 5, len(times))
            
            df_day = pd.DataFrame({'open': open_p, 'high': high, 'low': low, 'close': close}, index=times)
            dfs.append(df_day)
            
        if dfs:
            hist_data = pd.concat(dfs)
        else:
            print(f"{Colors.RED}Date range provided generated no business days. Exiting.{Colors.RESET}")
            exit(1)
    
    print(f"\n{Colors.CYAN}Initializing Dynamic SL Setup Backtester...{Colors.RESET}")
    tester = NiftyDynamicSLSetup(df=hist_data)
    results = tester.run_backtest()
    
    if not results.empty:
        tester.print_trades_table(results)
        tester.print_summary(results)
    else:
        print(f"{Colors.YELLOW}No trades generated in the selected period. No day hit the bearish candle condition.{Colors.RESET}")
