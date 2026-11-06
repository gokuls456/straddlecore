import pandas as pd
import numpy as np
from datetime import datetime, time
import random
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

def load_data(filepath=None):
    if filepath:
        df = pd.read_csv(filepath)
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        return df
    else:
        try:
            print("Fetching the last 90 days of real 3-minute NIFTY candles from Dhan...")
            from datetime import timedelta
            from engine.dhan_client import nifty_candles
            today = datetime.now().date()
            df = nifty_candles((today - timedelta(days=90)).isoformat(), today.isoformat(), minutes=3)
            if not df.empty:
                print(f"Fetched {len(df)} real 3-min candles from Dhan.")
                return df
        except Exception as e:
            print(f"Could not fetch Dhan data ({e}). Falling back to mock data...")

        print("Generating 90 days of mock 3-minute Nifty 50 data...")
        dates = pd.date_range(start='2024-01-01', periods=90, freq='B')
        mock_data = []
        current_price = 22000
        for d in dates:
            current_price += random.randint(-150, 150) # Overnight gap
            for i in range(125):
                dt = d.replace(hour=9, minute=15) + pd.Timedelta(minutes=3*i)
                open_p = current_price
                high_p = open_p + random.uniform(0, 15)
                low_p = open_p - random.uniform(0, 15)
                close_p = open_p + random.uniform(-10, 10)
                current_price = close_p
                mock_data.append({'datetime': dt, 'open': open_p, 'high': high_p, 'low': low_p, 'close': close_p})
        
        df = pd.DataFrame(mock_data)
        df.set_index('datetime', inplace=True)
        return df

# ==========================================
# Core Strategy Logic
# ==========================================
def backtest_strategy(df, variant='C', stop_type='range', lot_size=65):
    """
    variant: 'A' (first-30-min follow only), 'B' (gap follow only), 'C' (both must agree)
    stop_type: 'range' (opposite end of first-30-min range), 'fixed' (0.35%)
    """
    trades = []
    
    # --- OPTIONS TRADING APPROXIMATION ---
    # Cost per round trip for 1 lot (25 qty) Options buying:
    # Brokerage: 40, STT/Charges: ~15, Slippage: ~20
    # Total roughly: 75 INR per trade per lot
    cost_per_trade = 75 
    option_delta = 0.5 # Rough ATM delta approximation for intraday
    
    days = df.groupby(df.index.date)
    prev_close = None
    
    for date, day_data in days:
        # Skip incomplete days (roughly need ~120 candles for a full day)
        if len(day_data) < 100:
            prev_close = day_data['close'].iloc[-1]
            continue
            
        if prev_close is None:
            prev_close = day_data['close'].iloc[-1]
            continue
            
        try:
            # First 30 minutes: 09:15 to 09:44 (closing at 09:45)
            first_30m = day_data.between_time('09:15', '09:44')
            if first_30m.empty:
                continue
                
            open_0915 = first_30m['open'].iloc[0]
            close_0945 = first_30m['close'].iloc[-1]
            high_30m = first_30m['high'].max()
            low_30m = first_30m['low'].min()
            
            # Entry candle (09:48 start)
            entry_candle = day_data.between_time('09:48', '09:48')
            if entry_candle.empty:
                continue
            entry_price = entry_candle['open'].iloc[0]
            
        except IndexError:
            prev_close = day_data['close'].iloc[-1]
            continue
            
        # Rules Calculation
        gap_pct = (open_0915 - prev_close) / prev_close * 100
        drive_pct = (close_0945 - open_0915) / open_0915 * 100
        
        # 0.10% filter
        gap_signal = 1 if gap_pct >= 0.10 else (-1 if gap_pct <= -0.10 else 0)
        drive_signal = 1 if drive_pct >= 0.10 else (-1 if drive_pct <= -0.10 else 0)
        
        # Confluence
        trade_dir = 0
        if variant == 'A':
            trade_dir = drive_signal
        elif variant == 'B':
            trade_dir = gap_signal
        elif variant == 'C':
            if gap_signal != 0 and gap_signal == drive_signal:
                trade_dir = gap_signal
                
        if trade_dir == 0:
            prev_close = day_data['close'].iloc[-1]
            continue
            
        # Determine Stop Loss
        if stop_type == 'range':
            sl_long = low_30m
            sl_short = high_30m
        else: # 'fixed' 0.35%
            sl_long = entry_price * (1 - 0.0035)
            sl_short = entry_price * (1 + 0.0035)
            
        # Simulate Trade from 09:48 to 15:12
        # Exit at 15:12 to avoid 15:15-15:24 flat candles if any
        trade_data = day_data.between_time('09:48', '15:12')
        exit_price, exit_time, exit_reason = None, None, ""
        
        for idx, row in trade_data.iterrows():
            if trade_dir == 1 and row['low'] <= sl_long:
                exit_price, exit_time, exit_reason = sl_long, idx, "SL"
                break
            elif trade_dir == -1 and row['high'] >= sl_short:
                exit_price, exit_time, exit_reason = sl_short, idx, "SL"
                break
                    
        # Time Exit
        if exit_price is None and not trade_data.empty:
            last_candle = trade_data.iloc[-1]
            exit_price, exit_time, exit_reason = last_candle['close'], last_candle.name, "Time"
            
        if exit_price is not None:
            index_points = (exit_price - entry_price) if trade_dir == 1 else (entry_price - exit_price)
            option_points = index_points * option_delta
            pnl_rupees = (option_points * lot_size) - cost_per_trade
            
            trades.append({
                'date': date,
                'direction': 'LONG (CALL)' if trade_dir == 1 else 'SHORT (PUT)',
                'entry_time': entry_candle.index[0],
                'entry_price': entry_price,
                'exit_time': exit_time,
                'exit_price': exit_price,
                'exit_reason': exit_reason,
                'index_points_gained': index_points,
                'option_points_gained': option_points,
                'pnl_rupees': pnl_rupees,
                'variant': variant
            })
            
        prev_close = day_data['close'].iloc[-1]
        
    return pd.DataFrame(trades)

# ==========================================
# Metrics & Baselines
# ==========================================
def calculate_metrics(trades_df, title="Strategy Results"):
    if trades_df.empty:
        console.print(f"[bold red]No trades executed for {title}.[/bold red]")
        return None
        
    trades_count = len(trades_df)
    winning_trades = trades_df[trades_df['pnl_rupees'] > 0]
    losing_trades = trades_df[trades_df['pnl_rupees'] <= 0]
    
    win_rate = len(winning_trades) / trades_count * 100
    avg_win = winning_trades['pnl_rupees'].mean() if not winning_trades.empty else 0
    avg_loss = abs(losing_trades['pnl_rupees'].mean()) if not losing_trades.empty else 0
    
    profit_factor = avg_win / avg_loss if avg_loss != 0 else float('inf')
    expectancy = trades_df['pnl_rupees'].mean()
    
    # Drawdown Calculation
    trades_df['cumulative_pnl'] = trades_df['pnl_rupees'].cumsum()
    trades_df['peak'] = trades_df['cumulative_pnl'].cummax()
    trades_df['drawdown'] = trades_df['peak'] - trades_df['cumulative_pnl']
    max_dd = trades_df['drawdown'].max()
    
    worst_day = trades_df['pnl_rupees'].min()
    
    # Longest losing streak
    streaks, current_streak = [], 0
    for pnl in trades_df['pnl_rupees']:
        if pnl < 0: current_streak += 1
        else: streaks.append(current_streak); current_streak = 0
    streaks.append(current_streak)
    longest_losing_streak = max(streaks) if streaks else 0

    table = Table(title=title, show_header=True, header_style="bold magenta", title_style="bold cyan", border_style="cyan")
    table.add_column("Metric", style="white")
    table.add_column("Value", justify="right")
    
    table.add_row("Total Trades", str(trades_count))
    table.add_row("Win Rate", f"[bold {'green' if win_rate > 50 else 'yellow'}]{win_rate:.2f}%[/]")
    table.add_row("Expectancy", f"[bold {'green' if expectancy > 0 else 'red'}]₹{expectancy:.2f}[/]")
    table.add_row("Profit Factor", f"[bold {'green' if profit_factor > 1 else 'red'}]{profit_factor:.2f}[/]")
    table.add_row("Average Win | Loss", f"[green]₹{avg_win:.2f}[/] | [red]₹{avg_loss:.2f}[/]")
    table.add_row("Max Drawdown", f"[bold red]₹{max_dd:.2f}[/]")
    table.add_row("Worst Single Trade", f"[red]₹{worst_day:.2f}[/]")
    table.add_row("Longest Losing Streak", str(longest_losing_streak))
    
    total_net = trades_df['pnl_rupees'].sum()
    table.add_row("Total Net Profit", f"[bold {'green' if total_net > 0 else 'red'}]₹{total_net:.2f}[/]")

    console.print(table)
    console.print()
    return trades_df

def run_baselines(df, strategy_trades, lot_size=65):
    """
    1. Always Long Baseline
    2. Random Direction Baseline (Monte Carlo)
    """
    if strategy_trades.empty:
        console.print("[yellow]Need strategy trades to align baseline entries/exits.[/yellow]")
        return
        
    always_long_trades = []
    # 1. Always Long (Buy Call) using exact same entry/exit times
    for _, trade in strategy_trades.iterrows():
        try:
            entry = df.loc[trade['entry_time']]['open']
            exit_price = df.loc[trade['exit_time']]['close']
            index_points = exit_price - entry
            option_points = index_points * 0.5
            pnl_rupees = (option_points * lot_size) - 75
            always_long_trades.append({'pnl_rupees': pnl_rupees})
        except KeyError:
            continue
    al_df = pd.DataFrame(always_long_trades)
    al_net = al_df['pnl_rupees'].sum()
    
    # 2. Random Direction
    num_runs = 1000
    random_pnls = []
    
    for _ in range(num_runs):
        run_pnl = 0
        for _, trade in strategy_trades.iterrows():
            try:
                entry = df.loc[trade['entry_time']]['open']
                exit_price = df.loc[trade['exit_time']]['close']
                direction = random.choice([1, -1])
                index_points = (exit_price - entry) if direction == 1 else (entry - exit_price)
                option_points = index_points * 0.5
                run_pnl += (option_points * lot_size) - 75
            except KeyError:
                continue
        random_pnls.append(run_pnl)
        
    strategy_net = strategy_trades['pnl_rupees'].sum()
    percentile = (sum(1 for p in random_pnls if p < strategy_net) / num_runs) * 100
    mean_rnd = np.mean(random_pnls)
    
    panel_content = (
        f"Always-Long Net Profit: [bold {'green' if al_net > 0 else 'red'}]₹{al_net:.2f}[/] (from {len(al_df)} trades)\n"
        f"Random Baseline Mean PnL: [bold {'green' if mean_rnd > 0 else 'red'}]₹{mean_rnd:.2f}[/]\n\n"
        f"Edge Validation: [bold cyan]Strategy beats {percentile:.1f}% of random runs.[/]"
    )
    console.print(Panel(panel_content, title="[bold yellow]Baselines Comparison[/]", border_style="yellow"))

# ==========================================
# Main Execution
# ==========================================
if __name__ == "__main__":
    # Load your historical CSV here or use the mock generator
    # df = load_data('nifty_3min_data.csv')
    df = load_data() 
    
    # Compare Variants
    console.print(Panel("[bold cyan]Evaluating Variant A: First-30-Min Follow Only[/bold cyan]"))
    trades_A = backtest_strategy(df, variant='A', stop_type='range')
    calculate_metrics(trades_A, "Variant A")
    
    console.print(Panel("[bold cyan]Evaluating Variant B: Gap Follow Only[/bold cyan]"))
    trades_B = backtest_strategy(df, variant='B', stop_type='range')
    calculate_metrics(trades_B, "Variant B")
    
    console.print(Panel("[bold cyan]Evaluating Variant C: Confluence (Both Must Agree) [Main Strategy][/bold cyan]"))
    trades_C = backtest_strategy(df, variant='C', stop_type='range')
    calculate_metrics(trades_C, "Variant C (Range Stop)")
    
    console.print(Panel("[bold cyan]Evaluating Variant C with Fixed 0.35% Stop[/bold cyan]"))
    trades_C_fixed = backtest_strategy(df, variant='C', stop_type='fixed')
    calculate_metrics(trades_C_fixed, "Variant C (Fixed 0.35% Stop)")
    
    # Run Baselines on the Main Strategy
    if trades_C is not None and not trades_C.empty:
        run_baselines(df, trades_C)
        
    capital_text = (
        "You asked how to trade this with ₹20,000 capital. Here is the reality:\n\n"
        "1. [bold]Nifty 50 Futures:[/bold] Margin required is over [bold]₹1,00,000[/bold]. You [bold red]cannot[/bold red] trade futures with ₹20,000.\n"
        "2. [bold]Nifty 50 Options:[/bold] You can buy an ATM or slightly OTM Call/Put option at 09:48. However:\n"
        "   - Options have [italic]Theta decay[/italic] (time value loss) and [italic]Delta[/italic] (non-linear movement).\n"
        "   - A 10-point index move won't give exactly 10 points in options.\n"
        "   - [bold red]Warning:[/bold red] A few losses in a row can easily wipe out 50%+ of your ₹20K capital.\n"
        "3. [bold]NiftyBees (ETF):[/bold] You can buy ₹20,000 worth of ETFs. But a 0.5% move in Nifty only yields roughly ₹100 profit, which doesn't cover brokerage/slippage well.\n\n"
        "[bold cyan]Recommendation:[/bold cyan] Save up more capital for futures, or adapt this script to test actual Option Premium data to see if the Greek decay destroys the edge."
    )
    console.print(Panel(capital_text, title="[bold magenta]Capital Allocation Insight (₹20,000)[/]", border_style="magenta"))
