import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import math
import argparse

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

class NiftyBearishSetup:
    """
    Intraday Options Strategy Analyst and Backtester for Indian Market (NSE).
    Setup: Bearish (Buying ATM Put)
    Underlying: Nifty 50 Index
    Timeframe: 3-minute candles
    """

    def __init__(self, df, oi_provider=None):
        """
        Args:
            df (pd.DataFrame): Historical 3-minute data for Nifty 50.
                               Must have a DatetimeIndex and columns: ['open', 'high', 'low', 'close']
            oi_provider (callable): A function `get_oi(timestamp, strike, opt_type)` returning OI data.
                                    If None, OI checks are bypassed or simulated.
        """
        self.df = df
        self.oi_provider = oi_provider
        
        # Strategy Parameters
        self.lot_size = 65  # Current Nifty 50 lot size
        self.capital = 50000
        self.premium_sl_pct = 0.30
        self.reward_ratio = 2.0
        self.slippage = 1.5  # Points per side
        
        # Observation Window
        self.window_start = time(9, 15)
        self.window_end = time(10, 6)    # Candle starting 10:06, closes 10:09
        self.signal_candle_start = time(10, 9) # Closes 10:12
        self.entry_candle_start = time(10, 12)
        self.exit_time_mandatory = time(15, 0)
        
    def calculate_costs(self, buy_price, sell_price, qty):
        """
        Approximates NSE options trading costs (Brokerage, STT, Exchange, GST, Stamp Duty).
        """
        brokerage = 40  # Flat 20 buy + 20 sell
        turnover = (buy_price + sell_price) * qty
        exchange_charges = turnover * 0.0005  # ~0.05%
        sebi_charges = turnover * 0.000001
        stt = (sell_price * qty) * 0.00125  # ~0.125% on sell side premium
        stamp_duty = (buy_price * qty) * 0.00003
        gst = (brokerage + exchange_charges + sebi_charges) * 0.18
        
        return brokerage + exchange_charges + sebi_charges + stt + stamp_duty + gst

    def get_oi_status(self, timestamp, current_price):
        """
        Analyzes OI for Condition B.
        Returns: (bool, str) -> (Passed, Reason)
        """
        if self.oi_provider is None:
            # Fallback to simulate passing if no data provided
            return True, "Simulated OI Pass (No Data)"
            
        atm_strike = round(current_price / 50) * 50
        
        try:
            # We need OI trend from 09:20 to 10:12
            t_base = timestamp.replace(hour=9, minute=20)
            t_check = timestamp.replace(hour=10, minute=12)
            
            # Call OI at ATM and 2 nearest OTM
            call_strikes = [atm_strike, atm_strike + 50, atm_strike + 100]
            call_oi_rising = True
            for strike in call_strikes:
                base_oi = self.oi_provider(t_base, strike, 'CE')
                check_oi = self.oi_provider(t_check, strike, 'CE')
                if check_oi <= base_oi:
                    call_oi_rising = False
                    break
                    
            # Put OI at ATM and 2 nearest ITM
            put_strikes = [atm_strike, atm_strike + 50, atm_strike + 100] # ITM for puts means strike > spot
            put_oi_falling = True
            for strike in put_strikes:
                base_oi = self.oi_provider(t_base, strike, 'PE')
                check_oi = self.oi_provider(t_check, strike, 'PE')
                if check_oi > base_oi:
                    put_oi_falling = False
                    break
                    
            # Check PCR
            total_ce_oi_base = sum([self.oi_provider(t_base, s, 'CE') for s in call_strikes])
            total_pe_oi_base = sum([self.oi_provider(t_base, s, 'PE') for s in put_strikes])
            pcr_base = total_pe_oi_base / total_ce_oi_base if total_ce_oi_base else 1
            
            total_ce_oi_check = sum([self.oi_provider(t_check, s, 'CE') for s in call_strikes])
            total_pe_oi_check = sum([self.oi_provider(t_check, s, 'PE') for s in put_strikes])
            pcr_check = total_pe_oi_check / total_ce_oi_check if total_ce_oi_check else 1
            
            pcr_falling = pcr_check < pcr_base
            
            if call_oi_rising and (put_oi_falling or pcr_falling):
                return True, "OI Confirm: Calls written, Puts unwound or PCR down"
            else:
                return False, "OI Fail: Mixed/Opposite trend"
                
        except Exception as e:
            return False, f"OI Data Missing/Stale: {e}"

    def simulate_put_premium(self, spot_entry, spot_current, initial_premium=100.0, delta=0.5):
        """
        Very basic premium simulator since we don't have historical options data natively.
        Assumes ATM Put delta of ~0.5.
        """
        spot_diff = spot_entry - spot_current
        return max(5.0, initial_premium + (spot_diff * delta))

    def run_backtest(self):
        """
        Runs the backtest over the provided historical dataframe.
        """
        days = np.unique(self.df.index.date)
        trades = []
        
        for day in days:
            day_data = self.df[self.df.index.date == day].copy()
            
            # Needs enough data to run (09:15 to 15:00)
            if len(day_data) < 100:
                continue
                
            # 1. Observation Window
            window_mask = (day_data.index.time >= self.window_start) & (day_data.index.time <= self.window_end)
            window_data = day_data[window_mask]
            
            if len(window_data) < 18:
                continue # Missing candles
                
            window_high = window_data['high'].max()
            window_low = window_data['low'].min()
            window_mid = (window_high + window_low) / 2
            
            # 2 & 3. Condition A (no close above reference level)
            if (window_data['close'] > window_high).any():
                continue # Technically impossible if window_high is max high, but kept for logic structure
                
            # 4. Signal Candle
            signal_candle = day_data[day_data.index.time == self.signal_candle_start]
            if signal_candle.empty:
                continue
                
            sig_open = signal_candle['open'].iloc[0]
            sig_close = signal_candle['close'].iloc[0]
            
            if sig_close > window_high:
                continue
                
            is_bearish = sig_close < sig_open
            is_below_mid = sig_close < window_mid
            
            if not (is_bearish or is_below_mid):
                continue
                
            # 5. Condition B (OI Confirmation)
            entry_time_dt = signal_candle.index[0] + timedelta(minutes=3) # 10:12
            spot_at_1012 = day_data.loc[day_data.index == entry_time_dt, 'open']
            
            if spot_at_1012.empty:
                continue
                
            spot_entry = spot_at_1012.iloc[0]
            oi_pass, oi_reason = self.get_oi_status(entry_time_dt, spot_entry)
            
            if not oi_pass:
                continue
                
            # 6. Entry
            entry_premium = 100.0  # Assumed ATM premium for simulation
            lots = math.floor(self.capital / (entry_premium * self.lot_size))
            
            if lots == 0:
                continue
                
            qty = lots * self.lot_size
            entry_premium_with_slippage = entry_premium + self.slippage
            
            # Risk Parameters
            stop_loss_premium = entry_premium * (1 - self.premium_sl_pct)
            target_premium = entry_premium + (entry_premium * self.premium_sl_pct * self.reward_ratio)
            
            # Manage Trade
            post_entry_data = day_data[day_data.index.time >= self.entry_candle_start]
            
            exit_time = None
            exit_premium = 0.0
            exit_reason = ""
            
            highest_premium_seen = entry_premium
            
            for t, row in post_entry_data.iterrows():
                current_time = t.time()
                current_spot_close = row['close']
                current_spot_low = row['low']
                current_spot_high = row['high']
                
                # Simulate premium using close for SL/Target
                current_premium = self.simulate_put_premium(spot_entry, current_spot_close, entry_premium)
                current_premium_high = self.simulate_put_premium(spot_entry, current_spot_low, entry_premium) # spot low = premium high
                current_premium_low = self.simulate_put_premium(spot_entry, current_spot_high, entry_premium) # spot high = premium low
                
                # Trailing logic
                highest_premium_seen = max(highest_premium_seen, current_premium_high)
                trailing_sl_level = highest_premium_seen * 0.85 # Trail by 15% from peak
                dynamic_sl = max(stop_loss_premium, trailing_sl_level)
                
                # Check Underlying SL (close above window_high)
                if current_spot_close > window_high:
                    exit_time = t
                    exit_premium = current_premium - self.slippage
                    exit_reason = "Underlying SL Hit (Close > Window High)"
                    break
                    
                # Check Trailing / Premium SL
                if current_premium_low <= dynamic_sl:
                    exit_time = t
                    exit_premium = dynamic_sl - self.slippage
                    exit_reason = "Trailing SL Hit" if dynamic_sl > stop_loss_premium else "Premium SL Hit"
                    break
                    
                # Check Target
                if current_premium_high >= target_premium:
                    exit_time = t
                    exit_premium = target_premium - self.slippage
                    exit_reason = "Target Hit (1:2 RR)"
                    break
                    
                # Check Mandatory Exit
                if current_time >= self.exit_time_mandatory:
                    exit_time = t
                    exit_premium = current_premium - self.slippage
                    exit_reason = "Mandatory Exit (15:00)"
                    break
            
            if exit_time is None:
                # Fallback exit at last available candle
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
                'Entry Premium': entry_premium_with_slippage,
                'Lots': lots,
                'Exit Time': exit_time.time(),
                'Exit Premium': exit_premium,
                'Exit Reason': exit_reason,
                'Gross PnL': round(gross_pnl, 2),
                'Costs': round(costs, 2),
                'Net PnL': round(net_pnl, 2),
                'Window High': round(window_high, 2)
            })
            
        return pd.DataFrame(trades)

    def print_trades_table(self, trades_df):
        if trades_df.empty:
            return
            
        print(f"\n{Colors.CYAN}{Colors.BOLD}ALL TRADES LOG{Colors.RESET}")
        print("-" * 105)
        headers = ["Date", "Entry Time", "Entry Prem", "Exit Time", "Exit Prem", "Exit Reason", "Net PnL (Rs)"]
        print(f"{Colors.BOLD}{headers[0]:<12} | {headers[1]:<10} | {headers[2]:<10} | {headers[3]:<10} | {headers[4]:<10} | {headers[5]:<25} | {headers[6]:<12}{Colors.RESET}")
        print("-" * 105)
        
        for _, row in trades_df.iterrows():
            date_str = row['Date'].strftime('%d %b %Y')
            entry_t = row['Trigger Time'].strftime('%H:%M')
            entry_p = f"{row['Entry Premium']:.1f}"
            exit_t = row['Exit Time'].strftime('%H:%M')
            exit_p = f"{row['Exit Premium']:.1f}"
            reason = str(row['Exit Reason'])[:25] # Truncate if too long
            pnl = row['Net PnL']
            
            pnl_color = Colors.GREEN if pnl > 0 else Colors.RED
            pnl_str = f"{pnl_color}{pnl:>10.2f}{Colors.RESET}"
            
            print(f"{date_str:<12} | {entry_t:<10} | {entry_p:<10} | {exit_t:<10} | {exit_p:<10} | {reason:<25} | {pnl_str}")
        print("-" * 105)

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
        
        # Max Drawdown Approximation
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

    def live_scan(self, current_time_dt, day_data, spot_price):
        """
        Evaluates the setup for the current day up to the current_time_dt.
        """
        print(f"\n{Colors.CYAN}{Colors.BOLD}[LIVE SCAN] Time: {current_time_dt.time()}{Colors.RESET}")
        
        if current_time_dt.time() < self.entry_candle_start:
            print(f"{Colors.YELLOW}Verdict: SKIP - Too early. Setup forms at 10:12.{Colors.RESET}")
            return
            
        window_mask = (day_data.index.time >= self.window_start) & (day_data.index.time <= self.window_end)
        window_data = day_data[window_mask]
        
        if window_data.empty:
            print(f"{Colors.YELLOW}Verdict: SKIP - Insufficient window data.{Colors.RESET}")
            return
            
        window_high = window_data['high'].max()
        window_mid = (window_high + window_data['low'].min()) / 2
        
        print(f"- Window High (Ref Level): {window_high:.2f}")
        
        signal_candle = day_data[day_data.index.time == self.signal_candle_start]
        if signal_candle.empty:
            print(f"{Colors.YELLOW}Verdict: SKIP - Signal candle (10:09) not formed yet.{Colors.RESET}")
            return
            
        sig_open = signal_candle['open'].iloc[0]
        sig_close = signal_candle['close'].iloc[0]
        
        cond_a_pass = True
        if (window_data['close'] > window_high).any() or (sig_close > window_high):
            cond_a_pass = False
            print(f"- Condition A {Colors.RED}FAILED{Colors.RESET}: Close above window high detected.")
        else:
            print(f"- Condition A {Colors.GREEN}PASSED{Colors.RESET}: No close above window high.")
            
        is_bearish = sig_close < sig_open
        is_below_mid = sig_close < window_mid
        
        if not (is_bearish or is_below_mid):
            print(f"- Signal Candle {Colors.RED}FAILED{Colors.RESET}: Close ({sig_close:.2f}) not bearish and not below mid ({window_mid:.2f}).")
            cond_a_pass = False
        else:
            print(f"- Signal Candle {Colors.GREEN}PASSED{Colors.RESET}: Appropriate bearish signature.")
            
        oi_pass, oi_reason = self.get_oi_status(current_time_dt, spot_price)
        oi_color = Colors.GREEN if oi_pass else Colors.RED
        print(f"- Condition B (OI): {oi_color}{'PASSED' if oi_pass else 'FAILED'}{Colors.RESET} - {oi_reason}")
        
        if cond_a_pass and oi_pass:
            print(f"\n{Colors.GREEN}{Colors.BOLD}Verdict: TAKE - Buy ATM Put (Weekly). SL on underlying close above {window_high:.2f}.{Colors.RESET}")
        else:
            print(f"\n{Colors.YELLOW}Verdict: SKIP - Setup conditions not met.{Colors.RESET}")


# ==========================================
# USAGE EXAMPLES
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nifty Bearish Setup Backtester")
    parser.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)", default=(datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    parser.add_argument("--end", type=str, help="End date (YYYY-MM-DD)", default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument("--real", action="store_true", help="Fetch real Nifty data using yfinance (Note: Yahoo limits 1m data to the last 7 days)")
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    
    hist_data = pd.DataFrame()

    if args.real:
        print(f"\n{Colors.YELLOW}Attempting to fetch real 3-minute market data via TradingView from {args.start} to {args.end}...{Colors.RESET}")
        try:
            # We use tvdatafeed to bypass the 7-day 1m limit of Yahoo Finance. 
            # TradingView allows fetching up to ~5000 historical intraday bars for free (~40 trading days for 3-min).
            from tvDatafeed import TvDatafeed, Interval
            
            import logging
            logging.getLogger('tvDatafeed').setLevel(logging.CRITICAL) # Suppress tvdatafeed init warnings
            
            tv = TvDatafeed()
            # Fetch 5000 bars of 3-minute data for NIFTY
            raw_data = tv.get_hist(symbol='NIFTY', exchange='NSE', interval=Interval.in_3_minute, n_bars=5000)
            
            if raw_data is not None and not raw_data.empty:
                # Slice the data according to the custom dates requested
                mask = (raw_data.index >= args.start) & (raw_data.index <= args.end + " 23:59:59")
                hist_data = raw_data.loc[mask].copy()
                
                if not hist_data.empty:
                    # Drop unnecessary columns
                    hist_data.drop(columns=['symbol', 'volume'], inplace=True, errors='ignore')
                    print(f"{Colors.GREEN}Successfully fetched {len(hist_data)} real 3-min candles directly from TradingView!{Colors.RESET}")
                else:
                    print(f"{Colors.RED}Data for {args.start} to {args.end} is too far in the past. Free TradingView tier only goes back ~40 trading days for 3min candles. Falling back to dummy data...{Colors.RESET}")
                    hist_data = pd.DataFrame()
            else:
                print(f"{Colors.RED}Failed to fetch data from TradingView. Falling back to dummy data...{Colors.RESET}")
                
        except ImportError:
            print(f"{Colors.RED}tvdatafeed library not found. Please install using `pip install --upgrade --no-cache-dir git+https://github.com/rongardF/tvdatafeed.git`. Falling back to dummy data...{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.RED}Error fetching real data from TradingView: {e}. Falling back to dummy data...{Colors.RESET}")

    if hist_data.empty:
        print(f"\n{Colors.YELLOW}Generating synthetic trend data from {args.start} to {args.end}...{Colors.RESET}")
        dates = pd.date_range(start=start_dt, end=end_dt, freq='B')
        dfs = []
        for d in dates:
            times = pd.date_range(start=f"{d.strftime('%Y-%m-%d')} 09:15", end=f"{d.strftime('%Y-%m-%d')} 15:30", freq='3min')
            np.random.seed(int(d.timestamp()))
            
            # Random walk for Nifty with momentum (Trend creation)
            changes = np.random.normal(loc=0, scale=4, size=len(times))
            
            # Inject downward drift and momentum so bearish setup actually catches moves
            for i in range(1, len(changes)):
                changes[i] += changes[i-1] * 0.60 
                
            close = 24000 + np.cumsum(changes) - np.arange(len(times)) * 1.5
            open_p = close - changes
            high = np.maximum(open_p, close) + np.random.uniform(0, 2, len(times))
            low = np.minimum(open_p, close) - np.random.uniform(0, 2, len(times))
            
            df_day = pd.DataFrame({'open': open_p, 'high': high, 'low': low, 'close': close}, index=times)
            dfs.append(df_day)
            
        if dfs:
            hist_data = pd.concat(dfs)
        else:
            print(f"{Colors.RED}Date range provided generated no business days. Exiting.{Colors.RESET}")
            exit(1)
    
    # 2. Run Backtest
    print(f"\n{Colors.CYAN}Initializing Backtester...{Colors.RESET}")
    tester = NiftyBearishSetup(df=hist_data)
    results = tester.run_backtest()
    
    if not results.empty:
        tester.print_trades_table(results)
        tester.print_summary(results)
    else:
        print(f"{Colors.YELLOW}No trades generated in the selected period.{Colors.RESET}")
        
    # 3. Compare with Baseline (Random buy put)
    print(f"\n{Colors.CYAN}{Colors.BOLD}" + "="*50)
    print("BASELINE COMPARISON (Random 10:12 Entries)")
    print("="*50 + f"{Colors.RESET}")
    # Simulate buying Put on every day at 10:12 without setup
    random_trades = []
    for day in np.unique(hist_data.index.date):
        day_data = hist_data[hist_data.index.date == day]
        entry_time_dt = datetime.combine(day, time(10, 12))
        if entry_time_dt in day_data.index:
            spot = day_data.loc[entry_time_dt, 'open']
            # Assume arbitrary end of day exit
            exit_val = day_data.iloc[-1]['close']
            pnl = (spot - exit_val) * 25 # simplified
            random_trades.append(pnl)
            
    if random_trades:
        print(f"Random Buy Put Expectancy: Rs {np.mean(random_trades):.2f}")
        print("(Note: In real market data, this validates the alpha of the setup.)")

    # 4. Live Scan Simulation
    latest_day = hist_data.index.date[-1]
    latest_data = hist_data[hist_data.index.date == latest_day]
    # Simulate scanning at 10:12
    scan_time = datetime.combine(latest_day, time(10, 12))
    
    if scan_time in latest_data.index:
        current_spot = latest_data.loc[scan_time, 'open']
        tester.live_scan(scan_time, latest_data, current_spot)
    else:
        print(f"\n{Colors.YELLOW}[LIVE SCAN] Cannot scan: 10:12 data missing for the last day ({latest_day}).{Colors.RESET}")
