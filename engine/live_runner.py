"""
Live / paper algo runner for the NIFTY 50 4-branch straddle.

Demo account: simulated fills against live-ish prices, ₹30,000 virtual cash.
Original account: Groww market orders when strategy triggers fire.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from engine.data_feed import NiftyDataFeed
from engine.groww_broker import GrowwBroker, next_weekly_expiry
from engine.dhan_broker import DhanBroker
from engine import telegram_notifier as tg
from engine.expiry import trading_days_until
from engine.strategy_registry import UnifiedStrategyRunner, get_strategy
from engine.straddle_engine import (
    NiftyStraddleEngine,
    get_atm_strike,
    bs_call_price,
    bs_put_price,
)

logger = logging.getLogger("LiveAlgoRunner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache", "live_algo_state.json")
BROKER_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache", "broker_config.json")
DEMO_CAPITAL = 30000.0


def _now() -> datetime:
    return datetime.now()


class LiveAlgoRunner:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(LiveAlgoRunner, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.is_running = False
        self.is_armed = False
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.strategy_mode = "hedged_selling"
        self.capital = DEMO_CAPITAL
        self.initial_lots = 1
        self.lot_size = 65
        self.reentry_lots = 1
        self.sl_pct = 0.20
        self.reentry_wait_minutes = 15
        self.reentry_sl_pct = 0.40   # stop-loss on re-entry legs (None = no SL, the old behaviour)
        self.in_reentry = False
        self.pl_target = 3000.0
        self.pl_stop = -3000.0
        self.hedge_distance_pts = 300

        self.broker_name = "groww"
        self.account_type = "demo"
        self.execution_mode = "paper"
        self.broker: Optional[Any] = None  # GrowwBroker or DhanBroker
        self.live_balance = DEMO_CAPITAL

        self.active_positions: List[Dict[str, Any]] = []
        self.daily_events: List[Dict[str, Any]] = []
        self.daily_pnl = 0.0
        self.gross_pnl = 0.0
        self.charges = 0.0
        self.status = "IDLE"
        self.status_message = "Algo is idle. Choose Demo or Original, then Start Algo."
        self.has_entered = False
        self.session_date = ""
        self.pending_reentry_at: Optional[datetime] = None
        self.halted_for_day = False
        self.spot = 0.0
        self.expiry = next_weekly_expiry()
        self.demo_settled_session = ""
        self._entry_aborted = False
        self._entry_error = ""
        self._entry_plan: List[Dict[str, Any]] = []
        self._entry_sl_pct: Optional[float] = None
        self.manual_trade: Optional[Dict[str, Any]] = None

        self._load_state()

    def _load_broker_config(self) -> Dict[str, Any]:
        if os.path.exists(BROKER_CONFIG_FILE):
            try:
                with open(BROKER_CONFIG_FILE, "r") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Failed to load broker config: %s", exc)
        return {}

    def _settle_demo_pnl(self):
        """Credit/debit the session's realised P&L to the demo virtual cash (once per session)."""
        if self.account_type != "demo" or self.demo_settled_session == self.session_date:
            return
        self.demo_settled_session = self.session_date
        cfg = self._load_broker_config()
        new_bal = round(float(cfg.get("demo_balance", DEMO_CAPITAL)) + self.daily_pnl, 2)
        cfg["demo_balance"] = new_bal
        try:
            with open(BROKER_CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as exc:
            logger.error("Failed to update demo balance: %s", exc)
            return
        self.live_balance = new_bal
        self._event("DEMO_SETTLED", f"Paper P&L ₹{self.daily_pnl:,.2f} settled • Demo cash now ₹{new_bal:,.2f}")

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.get_status(), f, indent=2)
        except Exception as exc:
            logger.error("Failed to save algo state: %s", exc)

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    st = json.load(f)
                    self.status = st.get("status", "IDLE")
                    self.status_message = st.get("status_message", "")
                    self.daily_pnl = st.get("daily_pnl", 0.0)
                    self.capital = st.get("capital", DEMO_CAPITAL)
                    self.strategy_mode = st.get("strategy_mode", "hedged_selling")
                    self.account_type = st.get("account_type", "demo")
                    self.manual_trade = st.get("manual_trade")
            except Exception:
                pass

    def _event(self, event_type: str, detail: str, extra: Optional[Dict[str, Any]] = None):
        item = {
            "timestamp": _now().strftime("%H:%M:%S"),
            "type": event_type,
            "detail": detail,
        }
        if extra:
            item.update(extra)
        self.daily_events.append(item)
        logger.info("%s | %s", event_type, detail)
        self._telegram_alert(event_type, detail)

    # Events worth a phone alert, with their headline. Everything else stays in the in-page log only.
    TELEGRAM_EVENTS = {
        "ALGO_STARTED": "▶️ Algo started",
        "ALGO_STOPPED": "⏹ Algo stopped",
        "ENTRY": "✅ Entered",
        "SL_TRIGGERED": "🛑 Stop-loss hit",
        "REENTRY_SL": "🛑 Re-entry stop-loss hit",
        "GUARD_TARGET": "🎯 Profit guard hit",
        "GUARD_STOP": "🧯 Loss guard hit",
        "TIME_EXIT": "🕞 3:25 PM exit",
        "MANUAL_EXIT": "✋ Manual square-off",
        "ORDER_REJECTED": "❌ Order rejected",
        "MANUAL_TRADE": None,  # formatted separately: full "place this by hand" table
        "ENGINE_ERROR": "⚠️ Engine error",
        "BROKER_ERROR": "⚠️ Broker error",
        "BROKER_WARNING": "⚠️ Broker warning",
    }

    def _telegram_alert(self, event_type: str, detail: str):
        if event_type not in self.TELEGRAM_EVENTS:
            return
        cfg = self._load_broker_config().get("telegram") or {}
        if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
            return
        if event_type == "MANUAL_TRADE" and self.manual_trade:
            text = tg.format_manual_trade(self.manual_trade)
        else:
            mode = "DEMO" if self.account_type == "demo" else f"LIVE • {self.broker_name.upper()}"
            lines = [f"<b>{self.TELEGRAM_EVENTS[event_type]}</b>  <i>[{mode}]</i>", tg.esc(detail)]
            if event_type == "ENTRY":
                for p in self._open_positions():
                    sl = f" • SL ₹{p['sl_price']:,.2f}" if p.get("sl_price") else ""
                    lines.append(f"{'🟢' if p['side'] == 'BUY' else '🔴'} {p['side']} <code>{tg.esc(p['trading_symbol'])}</code> × {p['qty']} @ ₹{p['entry_price']:,.2f}{sl}")
            if event_type in ("SL_TRIGGERED", "REENTRY_SL", "GUARD_TARGET", "GUARD_STOP", "TIME_EXIT", "MANUAL_EXIT", "ALGO_STOPPED") and self.has_entered:
                sign = "−" if self.daily_pnl < 0 else "+"
                lines.append(f"Day P&L: <b>{sign}₹{abs(self.daily_pnl):,.2f}</b>")
            lines.append(f"<i>{_now().strftime('%d %b %H:%M:%S')}</i>")
            text = "\n".join(lines)
        # Recurring errors would otherwise repeat every 8-second tick.
        dedupe = f"{event_type}:{detail}" if event_type in ("ENGINE_ERROR", "BROKER_ERROR", "BROKER_WARNING") else None
        tg.send_async(cfg["bot_token"], str(cfg["chat_id"]), text, dedupe_key=dedupe)

    def _scale_guards(self):
        if self.capital <= 100000:
            self.pl_target = round(self.capital * 0.10)
            self.pl_stop = -round(self.capital * 0.10)
        else:
            self.pl_target = 15000.0
            self.pl_stop = -15000.0

    def _init_broker(self, cfg: Dict[str, Any]) -> Optional[Any]:
        """Order broker for the Original account. Dhan places orders; Groww (if keys exist) supplies quotes."""
        groww_cfg = cfg.get("groww", {})
        groww = None
        if groww_cfg.get("api_key"):
            groww = GrowwBroker(
                api_key=groww_cfg.get("api_key") or "",
                api_secret=groww_cfg.get("api_secret") or groww_cfg.get("auth_token") or "",
                access_token=groww_cfg.get("access_token") or "",
            )
        if self.broker_name == "dhan":
            dhan_cfg = cfg.get("dhan", {})
            if groww and not groww.connect()[0]:
                groww = None
            if not groww:
                self._event("BROKER_WARNING", "No Groww connection for quotes: Dhan orders will use estimated prices.")
            broker = DhanBroker(dhan_cfg.get("client_id") or "", dhan_cfg.get("access_token") or "", data_broker=groww)
        else:
            broker = groww or GrowwBroker()
        ok, msg = broker.connect()
        if not ok:
            self._event("BROKER_ERROR", msg)
            return None
        funds = broker.get_balance()
        if funds.get("success"):
            self.live_balance = funds.get("balance", 0.0)
            # Never let the algo think it has more than the account holds.
            if self.account_type == "original" and self.live_balance > 0:
                self.capital = min(self.capital, self.live_balance)
            self._event("BALANCE", f"{broker.name} available funds ₹{self.live_balance:,.2f}")
        return broker

    def start(self, config_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.is_running:
            return {"success": True, "message": "Algo is already running.", "state": self.get_status()}

        broker_cfg = self._load_broker_config()
        self.broker_name = broker_cfg.get("broker", "groww")
        self.account_type = broker_cfg.get("account_type", "demo")
        if config_override and config_override.get("account_type"):
            self.account_type = config_override["account_type"]

        if self.account_type == "original":
            self.execution_mode = "live"
        else:
            self.execution_mode = "paper"
            self.capital = float(broker_cfg.get("demo_balance", DEMO_CAPITAL))
            self.live_balance = self.capital

        if config_override:
            if config_override.get("capital"):
                self.capital = float(config_override.get("capital", self.capital))
            self.strategy_mode = config_override.get("strategy_mode", self.strategy_mode)
            self.lot_size = int(config_override.get("lot_size", self.lot_size))
            self.initial_lots = int(config_override.get("initial_lots", 1))

        self._scale_guards()
        self.broker = None
        if self.account_type == "original":
            self.broker = self._init_broker(broker_cfg)
            if not self.broker:
                return {
                    "success": False,
                    "error": f"Original account needs working {self.broker_name.title()} credentials. Save and test them first.",
                    "state": self.get_status(),
                }

        self.has_entered = False
        self.in_reentry = False
        self.halted_for_day = False
        self.pending_reentry_at = None
        self.active_positions = []
        # Fresh session: drop P&L left over from a Test Trigger or an earlier day.
        self.manual_trade = None
        self.daily_pnl = 0.0
        self.gross_pnl = 0.0
        self.charges = 0.0
        self.session_date = _now().strftime("%Y-%m-%d")
        self.expiry = next_weekly_expiry()

        self.is_running = True
        self.is_armed = True
        self.stop_event.clear()

        now = _now()
        market_open_today = now.replace(hour=10, minute=0, second=0, microsecond=0)
        market_close_today = now.replace(hour=15, minute=25, second=0, microsecond=0)
        account_label = "DEMO" if self.account_type == "demo" else f"ORIGINAL / {self.broker_name.upper()} LIVE"

        if now.weekday() < 5 and market_open_today <= now <= market_close_today:
            self.status = "RUNNING"
            self.status_message = (
                f"Live session on {account_label} • {self.strategy_mode.replace('_', ' ').title()} "
                f"• ₹{self.capital:,.0f} • Target +₹{self.pl_target:,.0f} / Stop ₹{self.pl_stop:,.0f}"
            )
        else:
            self.status = "ARMED"
            if now.weekday() < 5 and now < market_open_today:
                when = "today 10:00 AM IST"
            else:
                days_ahead = 1
                if now.weekday() == 4:
                    days_ahead = 3
                elif now.weekday() == 5:
                    days_ahead = 2
                when = f"{(now + timedelta(days=days_ahead)).strftime('%A, %d %b')} 10:00 AM IST"
            self.status_message = f"Armed on {account_label}. Waiting for {when}."

        self._event(
            "ALGO_STARTED",
            f"Algo started in {account_label} mode with ₹{self.capital:,.0f} capital "
            f"({self.strategy_mode.replace('_', ' ')} • {self.initial_lots} lot).",
        )
        self._save_state()

        self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.worker_thread.start()
        return {"success": True, "message": "Algo started.", "state": self.get_status()}

    def stop(self) -> Dict[str, Any]:
        self.is_running = False
        self.is_armed = False
        self.stop_event.set()
        if any(p.get("is_open") for p in self.active_positions):
            self._square_off_all("Stopped by user")
        if self.has_entered:
            self._settle_demo_pnl()
        self.status = "STOPPED"
        self.status_message = "Algo stopped."
        self._event("ALGO_STOPPED", "Automation stopped.")
        self._save_state()
        return {"success": True, "message": "Algo stopped.", "state": self.get_status()}

    def square_off_all_now(self, reason: str = "Manual Square Off") -> Dict[str, Any]:
        count = len(self._open_positions())
        if count > 0:
            self._square_off_all(reason)
            self._event("MANUAL_EXIT", f"Emergency square-off executed for {count} open positions.")
        self.status_message = f"Squared off {count} open positions ({reason})."
        self._save_state()
        return {"success": True, "message": self.status_message, "closed_count": count, "state": self.get_status()}

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running,
            "is_armed": self.is_armed,
            "status": self.status,
            "status_message": self.status_message,
            "broker": self.broker_name.upper(),
            "account_type": self.account_type,
            "execution_mode": self.execution_mode.upper(),
            "capital": self.capital,
            "live_balance": round(self.live_balance, 2),
            "strategy_mode": self.strategy_mode,
            "lot_size": self.lot_size,
            "initial_lots": self.initial_lots,
            "daily_pnl": round(self.daily_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "charges": round(self.charges, 2),
            "spot": self.spot,
            "expiry": self.expiry,
            "has_entered": self.has_entered,
            "active_positions": self.active_positions,
            "open_positions": [p for p in self.active_positions if p.get("is_open")],
            "events": self.daily_events[-40:],
            "next_schedule": "Mon-Fri 10:00 AM IST",
            "pl_target": self.pl_target,
            "pl_stop": self.pl_stop,
            "manual_trade": self.manual_trade,
            "last_heartbeat": _now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def execute_instant_paper_cycle(self, strategy_mode: Optional[str] = None) -> Dict[str, Any]:
        data_feed = NiftyDataFeed()
        recent_sessions = data_feed.get_historical_intraday_data(period="1 month", exclude_last_30=False)
        if not recent_sessions:
            return {"success": False, "error": "No recent market data available to run paper test."}

        strat = strategy_mode or self.strategy_mode
        self.strategy_mode = strat

        runner = UnifiedStrategyRunner(
            strategy_id=strat,
            capital=self.capital,
            initial_lots=self.initial_lots,
            reentry_lots=self.reentry_lots,
            lot_size=self.lot_size,
            sl_pct=self.sl_pct,
            reentry_sl_pct=self.reentry_sl_pct,
            reentry_wait_minutes=self.reentry_wait_minutes,
            pl_target=self.pl_target,
            pl_stop=self.pl_stop,
            execution_mode="fyers_calibrated",
        )
        sim_res = runner.run(recent_sessions)
        trades = sim_res.get("trade_logs", [])
        if not trades:
            return {"success": False, "error": "No trade signals generated for this period."}

        last_trade = trades[-1]
        self.daily_pnl = last_trade.get("net_pnl", 0.0)
        self.gross_pnl = last_trade.get("gross_pnl", 0.0)
        self.charges = last_trade.get("charges", 0.0)
        self.daily_events = last_trade.get("events", [])
        
        pos_list = []
        for p in last_trade.get("positions_detail", []):
            pos_list.append({
                "leg": p.get("leg", "NIFTY Option"),
                "branch": p.get("branch", ""),
                "side": p.get("side", "BUY"),
                "qty": p.get("qty", self.lot_size),
                "entry_price": round(p.get("entry_price", 0.0), 2),
                "current_price": round(p.get("exit_price", 0.0), 2),
                "exit_price": round(p.get("exit_price", 0.0), 2),
                "pnl": round(p.get("pnl", 0.0), 2),
                "exit_reason": p.get("exit_reason", ""),
                "order_id": f"PAPER-SIM-{int(time.time()*1000)%1000000}",
                "is_open": False,
            })
        self.active_positions = pos_list
        self.status = "COMPLETED_CYCLE"
        meta = get_strategy(self.strategy_mode)
        strat_name = meta.get("name", self.strategy_mode)
        self.status_message = (
            f"Paper Cycle ({last_trade['date']}): {strat_name} • Net P&L "
            f"{'+' if self.daily_pnl >= 0 else ''}₹{self.daily_pnl:,.2f} ({last_trade.get('exit_reason', 'Closed')})"
        )
        self._save_state()
        return {
            "success": True,
            "session_date": str(last_trade["date"]),
            "strategy": strat_name,
            "net_pnl": self.daily_pnl,
            "gross_pnl": self.gross_pnl,
            "charges": self.charges,
            "stop_reason": last_trade.get("exit_reason", ""),
            "positions": pos_list,
            "events": self.daily_events,
            "state": self.get_status(),
        }

    def _open_positions(self) -> List[Dict[str, Any]]:
        return [p for p in self.active_positions if p.get("is_open")]

    def _mark_pnl(self):
        total = 0.0
        for pos in self.active_positions:
            price = pos.get("current_price") or pos.get("entry_price") or 0
            entry = pos.get("entry_price") or 0
            qty = pos.get("qty") or 0
            if pos.get("side") == "BUY":
                pnl = (price - entry) * qty
            else:
                pnl = (entry - price) * qty
            pos["pnl"] = round(pnl, 2)
            if not pos.get("is_open") and pos.get("exit_price"):
                if pos.get("side") == "BUY":
                    pnl = (pos["exit_price"] - entry) * qty
                else:
                    pnl = (entry - pos["exit_price"]) * qty
                pos["pnl"] = round(pnl, 2)
            total += pos["pnl"]
        self.daily_pnl = round(total, 2)
        self.gross_pnl = self.daily_pnl

    def _refresh_market(self):
        """NIFTY spot and India VIX from Yahoo Finance when no broker supplies them (demo mode).
        Cached for 30 s; keeps the last good values on failure."""
        if time.time() - getattr(self, "_market_at", 0) < 30:
            return
        self._market_at = time.time()
        try:
            from engine.signal_scanner import fetch_market_data, now_ist

            candles, prev_close, vix = fetch_market_data(now_ist())
            spot = float(candles["close"].iloc[-1]) if not candles.empty else prev_close
            if spot > 0:
                self.spot = spot
            if vix > 0:
                self.iv = vix / 100.0
        except Exception as exc:
            logger.warning("Market data fetch failed: %s", exc)

    def _estimate_premium(self, strike: int, opt_type: str) -> float:
        if self.broker:
            leg = self.broker.get_option_leg(strike, opt_type, self.expiry)
            if leg.get("ltp"):
                return float(leg["ltp"])
            live = self.broker.get_ltp(leg.get("trading_symbol") or "")
            if live:
                return live
        now = _now()
        expiry = datetime.strptime(self.expiry, "%Y-%m-%d").date()
        today_frac = max(0.04, (15 * 60 + 30 - (now.hour * 60 + now.minute)) / 375.0)
        t = max(0.0008, (today_frac + trading_days_until(now.date(), expiry)) / 252.0)
        if opt_type == "CE":
            return max(0.5, bs_call_price(self.spot or 25000, strike, t, 0.065, getattr(self, "iv", 0.14)))
        return max(0.5, bs_put_price(self.spot or 25000, strike, t, 0.065, getattr(self, "iv", 0.14)))

    def _place_leg(self, strike: int, opt_type: str, side: str, qty: int, branch: str, sl: Optional[float] = None) -> Dict[str, Any]:
        trading_symbol = f"NIFTY{self.expiry[2:4]}{datetime.strptime(self.expiry, '%Y-%m-%d').strftime('%b').upper()}{strike}{opt_type}"
        fill_price = self._estimate_premium(strike, opt_type)
        order_id = "PAPER"
        if self.broker:
            leg = self.broker.get_option_leg(strike, opt_type, self.expiry)
            trading_symbol = leg.get("trading_symbol") or trading_symbol
            if leg.get("ltp"):
                fill_price = float(leg["ltp"])
        # Every intended leg goes into the plan, so a failed entry can be placed by hand.
        sl_pct = self._entry_sl_pct if sl else None
        self._entry_plan.append({
            "trading_symbol": trading_symbol,
            "side": side,
            "qty": qty,
            "lots": qty // self.lot_size if self.lot_size else qty,
            "ref_price": round(fill_price, 2),
            "sl_pct": sl_pct,
            "sl_price": (round(fill_price * (1 + sl_pct), 2) if side == "SELL" else round(fill_price * (1 - sl_pct), 2)) if sl_pct else None,
            "branch": branch,
        })
        if self._entry_aborted:
            return {}
        if self.execution_mode == "live" and self.broker:
            result = self.broker.place_market_order(trading_symbol, qty, side, strike=strike, opt_type=opt_type, expiry=self.expiry)
            if not result.get("success"):
                self._event("ORDER_REJECTED", f"{side} {trading_symbol} failed: {result.get('error')}")
                # Stop the rest of the structure: e.g. never sell naked legs if a hedge buy failed.
                self._entry_aborted = True
                self._entry_error = (result.get("error") or "Order rejected").rstrip(". ")
                return {}
            order_id = result.get("order_id") or "LIVE"
            self._event("ORDER_PLACED", f"{side} {qty} {trading_symbol} @ MKT (id {order_id})")
        else:
            self._event("PAPER_FILL", f"{side} {qty} {trading_symbol} @ ₹{fill_price:.2f}")

        pos = {
            "leg": f"{strike} {opt_type}",
            "trading_symbol": trading_symbol,
            "opt_type": opt_type,
            "strike": strike,
            "side": side,
            "qty": qty,
            "entry_price": round(fill_price, 2),
            "current_price": round(fill_price, 2),
            "exit_price": None,
            "sl_price": round(sl, 2) if sl else None,
            "pnl": 0.0,
            "is_open": True,
            "branch": branch,
            "order_id": order_id,
            "exit_reason": "",
        }
        self.active_positions.append(pos)
        return pos

    def _enter_structure(self, reason: str, lots: Optional[int] = None, with_sl: bool = True,
                         sl_pct: Optional[float] = None) -> bool:
        """Place the strategy's legs. Returns False (and halts for the day) if any leg was rejected."""
        lots = lots or self.initial_lots
        sl_pct = self.sl_pct if sl_pct is None else sl_pct
        qty = lots * self.lot_size
        if self.spot <= 0:
            if self.broker:
                self.spot = self.broker.get_nifty_spot()
            else:
                self._refresh_market()
            if self.spot <= 0:
                self.spot = 25000.0
        atm = get_atm_strike(self.spot)
        self._entry_aborted = False
        self._entry_error = ""
        self._entry_plan = []
        self._entry_sl_pct = sl_pct
        legs_before = len(self.active_positions)
        if self.strategy_mode == "nifty_10_12_put":
            pe_px = self._estimate_premium(atm, "PE")
            sl_pe = pe_px * (1.0 - sl_pct) if with_sl else None
            self._place_leg(atm, "PE", "BUY", qty, "10:12 Bearish Put", sl_pe)
            summary = f"{reason}: 10:12 Strategy Bought ATM Put {atm} PE ({lots} lot • {qty} Qty)."
        elif self.strategy_mode == "nifty_gap_drive":
            side_type = "CE" if (self.spot % 100) >= 50 else "PE"
            px = self._estimate_premium(atm, side_type)
            sl_px = px * (1.0 - sl_pct) if with_sl else None
            self._place_leg(atm, side_type, "BUY", qty, f"Gap Drive {side_type}", sl_px)
            summary = f"{reason}: Gap Drive Bought ATM {atm} {side_type} ({lots} lot • {qty} Qty)."
        elif self.strategy_mode == "nifty_1pm_drop":
            pe_px = self._estimate_premium(atm, "PE")
            sl_pe = pe_px * (1.0 - sl_pct) if with_sl else None
            self._place_leg(atm, "PE", "BUY", qty, "1 PM Drop Put", sl_pe)
            summary = f"{reason}: 1 PM Drop Bought ATM {atm} PE ({lots} lot • {qty} Qty)."
        elif self.strategy_mode == "option_buying":
            ce = self._place_leg(atm, "CE", "BUY", qty, "Branch 1 (CE)", None)
            pe = self._place_leg(atm, "PE", "BUY", qty, "Branch 2 (PE)", None)
            summary = f"{reason}: Bought ATM {atm} straddle ({lots} lot)."
        elif self.strategy_mode == "hedged_selling":
            wing_ce = atm + self.hedge_distance_pts
            wing_pe = atm - self.hedge_distance_pts
            self._place_leg(wing_ce, "CE", "BUY", qty, "Hedge CE (+300)", None)
            self._place_leg(wing_pe, "PE", "BUY", qty, "Hedge PE (-300)", None)
            ce_px = self._estimate_premium(atm, "CE")
            pe_px = self._estimate_premium(atm, "PE")
            sl_ce = ce_px * (1 + sl_pct) if with_sl else None
            sl_pe = pe_px * (1 + sl_pct) if with_sl else None
            self._place_leg(atm, "CE", "SELL", qty, "Branch 1 (ATM CE)", sl_ce)
            self._place_leg(atm, "PE", "SELL", qty, "Branch 2 (ATM PE)", sl_pe)
            summary = f"{reason}: Iron Butterfly ATM {atm} with ±{self.hedge_distance_pts} wings ({lots} lot)."
        else:
            ce_px = self._estimate_premium(atm, "CE")
            pe_px = self._estimate_premium(atm, "PE")
            sl_ce = ce_px * (1 + sl_pct) if with_sl else None
            sl_pe = pe_px * (1 + sl_pct) if with_sl else None
            self._place_leg(atm, "CE", "SELL", qty, "Branch 1", sl_ce)
            self._place_leg(atm, "PE", "SELL", qty, "Branch 2", sl_pe)
            summary = f"{reason}: Short ATM {atm} straddle ({lots} lot)."
        self.has_entered = True

        if self._entry_aborted:
            self.pending_reentry_at = None
            self.halted_for_day = True
            filled = self.active_positions[legs_before:]
            if filled:
                self._square_off_all("Partial entry rollback")
            stuck = [p for p in filled if p.get("is_open")]
            if stuck:
                self.status = "ERROR"
                self.status_message = (
                    f"Entry failed ({self._entry_error}) and {len(stuck)} filled leg(s) could not be closed: "
                    f"{', '.join(p['trading_symbol'] for p in stuck)}. Close them manually in Groww now."
                )
            else:
                self.status = "ENTRY_FAILED"
                self.status_message = (
                    f"Entry failed: {self._entry_error}. "
                    + (f"Rolled back {len(filled)} filled leg(s). " if filled else "No position was opened. ")
                    + "Halted for the day."
                )
            self.manual_trade = {
                "id": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "reason": self._entry_error,
                "strategy_mode": self.strategy_mode,
                "legs": self._entry_plan,
                "stuck_legs": [p["trading_symbol"] for p in stuck],
                "pl_target": self.pl_target,
                "pl_stop": self.pl_stop,
                "exit_time": "3:25 PM IST",
            }
            self._event("ENTRY_FAILED", self.status_message)
            self._event("MANUAL_TRADE", "Place these by hand: " + "; ".join(
                f"{l['side']} {l['qty']} {l['trading_symbol']} ~₹{l['ref_price']}"
                + (f" (SL ₹{l['sl_price']})" if l["sl_price"] else "") for l in self._entry_plan))
            self._mark_pnl()
            return False

        self._event("ENTRY", summary)
        self._mark_pnl()
        return True


    def _close_position(self, pos: Dict[str, Any], reason: str):
        if not pos.get("is_open"):
            return
        price = pos.get("current_price") or pos.get("entry_price") or 0
        close_side = "SELL" if pos.get("side") == "BUY" else "BUY"
        if self.execution_mode == "live" and self.broker and pos.get("trading_symbol"):
            result = self.broker.place_market_order(pos["trading_symbol"], pos["qty"], close_side,
                                                    strike=pos.get("strike"), opt_type=pos.get("opt_type"), expiry=self.expiry)
            if not result.get("success"):
                self._event("ORDER_REJECTED", f"Square-off {pos['trading_symbol']} failed: {result.get('error')}")
                return
            self._event("ORDER_PLACED", f"SQUARE OFF {close_side} {pos['qty']} {pos['trading_symbol']}")
        pos["is_open"] = False
        pos["exit_price"] = round(price, 2)
        pos["exit_reason"] = reason
        self._event("SQUARE_OFF", f"{pos['side']} {pos['leg']} @ ₹{price:.2f} • {reason}")

    def _square_off_all(self, reason: str):
        for pos in list(self._open_positions()):
            self._refresh_price(pos)
            self._close_position(pos, reason)
        self._mark_pnl()

    def _refresh_price(self, pos: Dict[str, Any]):
        price = 0.0
        if self.broker and pos.get("trading_symbol"):
            price = self.broker.get_ltp(pos["trading_symbol"])
        if price <= 0:
            price = self._estimate_premium(int(pos.get("strike") or 0), pos.get("opt_type") or "CE")
        pos["current_price"] = round(price, 2)

    def _tick(self):
        now = _now()
        is_weekday = now.weekday() < 5
        market_open = now.replace(hour=10, minute=0, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=25, second=0, microsecond=0)

        if not is_weekday or now < market_open:
            if self.is_armed and self.status != "ARMED" and not self.halted_for_day:
                self.status = "ARMED"
                self.status_message = "Armed. Waiting for next 10:00 AM IST session."
                self._save_state()
            return

        if now >= market_close:
            if self._open_positions():
                self._square_off_all("Time Exit (15:25)")
            if not self.halted_for_day:
                self.halted_for_day = True
                self.status = "COMPLETED"
                self.status_message = f"Session closed at 3:25 PM. Day P&L ₹{self.daily_pnl:,.2f}"
                self._event("TIME_EXIT", "Branch 4: 3:25 PM square-off.")
                if self.has_entered:
                    self._settle_demo_pnl()
                self._save_state()
            return

        if self.halted_for_day:
            return

        self.status = "RUNNING"
        if self.broker:
            spot = self.broker.get_nifty_spot()
            if spot:
                self.spot = spot
        else:
            self._refresh_market()

        if not self.has_entered and now >= market_open:
            if not self._enter_structure("10:00 AM trigger"):
                self._save_state()
                return
            self.status_message = f"Entered {self.strategy_mode.replace('_', ' ')} • watching SL / guard / time exit"
            self._save_state()
            return

        if self.pending_reentry_at and now >= self.pending_reentry_at and not self._open_positions():
            if not self._enter_structure("15-minute re-entry", lots=self.reentry_lots,
                                         with_sl=bool(self.reentry_sl_pct), sl_pct=self.reentry_sl_pct):
                self._save_state()
                return
            self.in_reentry = True
            self.pending_reentry_at = None
            self._save_state()
            return

        sl_hit = None
        for pos in self._open_positions():
            self._refresh_price(pos)
            sl = pos.get("sl_price")
            if sl and pos.get("side") == "SELL" and pos.get("current_price", 0) >= sl:
                sl_hit = f"{pos['opt_type']} SL Hit ({pos['leg']} @ ₹{pos['current_price']})"

        self._mark_pnl()
        open_pnl = sum(p.get("pnl", 0) for p in self._open_positions())

        if sl_hit and self.in_reentry:
            # Re-entry stop-loss: close everything and stop for the day (no further re-entries).
            self._square_off_all(f"Re-entry {sl_hit}")
            self.halted_for_day = True
            self.status = "COMPLETED"
            self.status_message = f"Re-entry stop-loss hit ({sl_hit}). Done for the day."
            self._event("REENTRY_SL", self.status_message)
            self._settle_demo_pnl()
            self._save_state()
            return

        if sl_hit:
            self._square_off_all(sl_hit)
            self.pending_reentry_at = now + timedelta(minutes=self.reentry_wait_minutes)
            self._event("SL_TRIGGERED", f"{sl_hit}. Squared off all. Re-entry at {self.pending_reentry_at.strftime('%H:%M')}.")
            self.status_message = f"{sl_hit}. Waiting 15 minutes to re-enter 1 lot."
            self._save_state()
            return

        if self._open_positions():
            if open_pnl >= self.pl_target:
                self._square_off_all("Portfolio target hit")
                self.halted_for_day = True
                self.status = "COMPLETED"
                self.status_message = f"Guard target +₹{self.pl_target:,.0f} hit. Halted for the day."
                self._event("GUARD_TARGET", self.status_message)
                self._settle_demo_pnl()
                self._save_state()
                return
            if open_pnl <= self.pl_stop:
                self._square_off_all("Portfolio stop hit")
                self.halted_for_day = True
                self.status = "COMPLETED"
                self.status_message = f"Guard stop ₹{self.pl_stop:,.0f} hit. Halted for the day."
                self._event("GUARD_STOP", self.status_message)
                self._settle_demo_pnl()
                self._save_state()
                return

        self.status_message = (
            f"{'LIVE' if self.account_type == 'original' else 'DEMO'} running • "
            f"spot {self.spot:,.0f} • P&L ₹{self.daily_pnl:,.2f} • {len(self._open_positions())} open legs"
        )
        self._save_state()

    def _run_loop(self):
        logger.info("Algo execution loop started.")
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.exception("Error in algo worker loop")
                self._event("ENGINE_ERROR", str(exc))
                self._save_state()
            time.sleep(8)
        logger.info("Algo execution loop exited.")


algo_runner = LiveAlgoRunner()
