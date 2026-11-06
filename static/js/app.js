/**
 * STRADDLECORE Web Backtest Studio
 * Interactive Frontend Controller
 */

// Application State
let currentResults = null;
let filteredTrades = [];
let currentPage = 1;
const rowsPerPage = 15;
let currentFilter = 'all';
let currentSearch = '';
let dateSortOrder = 'desc'; // 'desc' (latest first, default) or 'asc' (oldest first)
let currentStrategyMode = 'hedged_selling';
let currentAccountType = 'demo';
let currentBalance = 30000;
let currentAllocatedCapital = 30000;
let STRATEGY_META = {};  // id -> registry entry from /api/strategies (incl. capital_per_lot)
const LIVE_ALGO_CAPITAL = 30000;
let activeChartTab = 'equity';
let currentExecutionMode = 'fyers_calibrated';
// Dhan is the only broker and market-data source (backtests, signal scanner, live prices, orders).
const liveOrderBroker = 'dhan';
let dhanTokenSaved = false;
let dhanAutoLogin = false;

/**
 * Intelligent capital input parser.
 * Supports: "30k", "40k", "50k", "30000", "5 lakhs", "5L", "5 lac", "500k", "5,00,000", "500000".
 */
function parseCapitalInput(val) {
    if (!val) return 30000.0;
    let s = String(val).trim().toLowerCase();
    s = s.replace(/,/g, '').replace(/₹/g, '').replace(/rs\.?/g, '').trim();
    if (s.includes('lakh') || s.endsWith('l') || s.includes('lac')) {
        const num = parseFloat(s.replace(/lakhs?/, '').replace(/lacs?/, '').replace(/l/, '').trim());
        if (!isNaN(num)) return num * 100000.0;
    }
    if (s.includes('crore') || s.includes('cr')) {
        const num = parseFloat(s.replace(/crores?/, '').replace(/cr/, '').trim());
        if (!isNaN(num)) return num * 10000000.0;
    }
    if (s.endsWith('k')) {
        const num = parseFloat(s.replace(/k/, '').trim());
        if (!isNaN(num)) return num * 1000.0;
    }
    const parsed = parseFloat(s);
    if (!isNaN(parsed) && parsed > 0 && parsed <= 25) {
        // Single digits like 1, 2, 5, 10 interpret as Lakhs
        return parsed * 100000.0;
    }
    return (!isNaN(parsed) && parsed > 0) ? parsed : 30000.0;
}

// DOM Ready initialization
document.addEventListener('DOMContentLoaded', async () => {
    await loadStrategyMeta();
    await loadAccountType();
    loadTelegramConfig();
    loadSignalStatus();
    setInterval(loadSignalStatus, 30000);
    checkBrokerStatus();
    checkAlgoStatus();
    setInterval(checkAlgoStatus, 4000);
    setInterval(refreshBalance, 15000);
});

async function loadStrategyMeta() {
    try {
        const res = await fetch('/api/strategies');
        const data = await res.json();
        (data.data || []).forEach((m) => { STRATEGY_META[m.id] = m; });
    } catch (err) {
        console.warn('Failed to load strategy metadata', err);
    }
}

// ₹30k / ₹2L style short amounts for labels.
function formatShortINR(n) {
    const v = Number(n) || 0;
    if (v >= 100000) return `₹${+(v / 100000).toFixed(2)}L`;
    if (v >= 1000) return `₹${+(v / 1000).toFixed(1)}k`;
    return `₹${v}`;
}

// How many lots the given capital can fund for a strategy (0 = not enough).
function lotsFor(mode, capital) {
    const perLot = STRATEGY_META[mode]?.capital_per_lot || 30000;
    return Math.floor(capital / perLot);
}

function strategyFitText(mode, capital) {
    const perLot = STRATEGY_META[mode]?.capital_per_lot;
    if (!perLot) return '';
    const lots = lotsFor(mode, capital);
    return lots >= 1
        ? `${lots} lot${lots > 1 ? 's' : ''} with ${formatShortINR(capital)}`
        : `needs ~${formatShortINR(perLot)}/lot`;
}

// Strategy labels everywhere reflect the current capital instead of fixed "₹30k" text.
function refreshStrategyLabels() {
    const capital = currentAllocatedCapital;
    document.querySelectorAll('#bt-quick-strategy option').forEach((opt) => {
        if (!opt.dataset.base) opt.dataset.base = opt.textContent.trim();
        const fits = lotsFor(opt.value, capital) >= 1;
        opt.textContent = `${fits ? '' : '⚠ '}${opt.dataset.base} — ${strategyFitText(opt.value, capital)}`;
    });
    document.querySelectorAll('.strategy-pick').forEach((card) => {
        const tag = card.querySelector('.strat-tag');
        if (!tag) return;
        const fits = lotsFor(card.dataset.mode, capital) >= 1;
        tag.textContent = fits ? `Fits ${strategyFitText(card.dataset.mode, capital)}` : `⚠ Needs ~${formatShortINR(STRATEGY_META[card.dataset.mode]?.capital_per_lot)}/lot`;
        tag.classList.toggle('tag-orange', !fits);
    });
}

function formatINR(n) {
    const num = Number(n) || 0;
    return `₹${num.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

function switchStudioTab(tab) {
    const liveBtn = document.getElementById('tab-live-desk');
    const btBtn = document.getElementById('tab-backtest-studio');
    const livePanel = document.getElementById('live-desk-panel');
    const btPanel = document.getElementById('backtest-studio-panel');
    const cfgBtn = document.getElementById('open-config-btn');
    if (tab === 'live') {
        liveBtn?.classList.add('active');
        btBtn?.classList.remove('active');
        livePanel?.classList.remove('hidden');
        btPanel?.classList.add('hidden');
        cfgBtn?.classList.add('hidden');
    } else {
        liveBtn?.classList.remove('active');
        btBtn?.classList.add('active');
        livePanel?.classList.add('hidden');
        btPanel?.classList.remove('hidden');
        cfgBtn?.classList.remove('hidden');
        if (!currentResults) runBacktest();
    }
}

const STRATEGY_NAMES = {
    'hedged_selling': '⚡ Iron Butterfly',
    'nifty_10_12_put': '📉 10:12 Bearish Put',
    'nifty_gap_drive': '🚀 Gap Drive Momentum',
    'nifty_1pm_drop': '💥 1 PM Volatility Drop',
    'option_buying': '🎯 Long Straddle',
    'short_straddle': '🏛️ Classic Straddle',
    'trend_hedged_straddle': '🧭 Trend-Hedged Straddle',
    'gold_trend_futures': '🥇 Gold Mini Trend'
};

// Backtest-only strategies: the demo algo can't trade them, so picking one must not change the live strategy.
const BACKTEST_ONLY_STRATEGIES = ['gold_trend_futures'];

function selectLiveStrategy(mode) {
    // Only one strategy runs at a time; it can't be changed while the algo is armed/running.
    if (isAlgoActive && runningStrategyMode && mode !== runningStrategyMode) {
        alert(`${STRATEGY_NAMES[runningStrategyMode] || runningStrategyMode} is running. Stop the algo before switching strategy.`);
        return;
    }
    currentStrategyMode = mode;
    document.querySelectorAll('.strategy-pick').forEach((el) => {
        el.classList.toggle('active', el.dataset.mode === mode);
    });

    const badge = document.getElementById('live-active-strat-badge');
    const quickSelect = document.getElementById('bt-quick-strategy');
    if (quickSelect) quickSelect.value = mode;

    const names = STRATEGY_NAMES;
    const fit = strategyFitText(mode, currentAllocatedCapital);
    if (badge) badge.textContent = fit ? `${names[mode] || mode} • ${fit}` : (names[mode] || mode);

    const desc = document.getElementById('algo-status-desc');
    const modeLabel = currentAccountType === 'original' ? `ORIGINAL (${liveOrderBroker.toUpperCase()} LIVE)` : 'DEMO (PAPER)';
    if (desc && !isAlgoActive) {
        desc.innerHTML = `Algo engine ready on <strong>${modeLabel}</strong> • Active strategy: <strong>${names[mode] || mode}</strong>. Auto-filled capital: <strong>${formatINR(currentAllocatedCapital)}</strong>. When the strategy trigger hits, it places the order.`;
    }
}

// Show/hide everything that belongs to one account. Demo never shows Dhan live-funds details.
function applyAccountView(value) {
    currentAccountType = value;
    const isLive = value === 'original';
    document.getElementById('btn-acc-original')?.classList.toggle('active', isLive);
    document.getElementById('btn-acc-demo')?.classList.toggle('active', !isLive);
    document.querySelectorAll('.live-only').forEach((el) => el.classList.toggle('hidden', !isLive));
    document.getElementById('header-balance-edit')?.classList.toggle('hidden', isLive);
    const pill = document.getElementById('balance-pill');
    if (pill) pill.title = isLive ? 'Click to refresh live broker balance' : 'Click to edit demo capital';
    document.getElementById('original-keys-box')?.classList.remove('collapsed');

    const badge = document.getElementById('live-account-badge');
    if (badge) {
        badge.textContent = isLive ? `${brokerTitle(liveOrderBroker)} Live` : 'Demo Paper';
        badge.className = isLive ? 'badge badge-success' : 'badge badge-outline';
    }
    const headerLabel = document.getElementById('header-balance-label');
    if (headerLabel) headerLabel.textContent = isLive ? `${brokerTitle(liveOrderBroker)} Live Funds` : 'Demo Capital';
    syncBrokerLabels();

    // Clear any live values so nothing from the other account lingers on screen.
    ['header-balance-val', 'live-balance-value', 'live-capital-value'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
    });
    const inlineMsg = document.getElementById('broker-inline-msg');
    if (inlineMsg && !isLive) inlineMsg.textContent = '';
}

// The page opens on the account last chosen (saved server-side by switchAccount).
// A running/armed algo wins, so its account stays visible and can be stopped.
async function loadAccountType() {
    let accountType = 'demo';
    try {
        const cfgRes = await fetch('/api/broker/config');
        const cfg = (await cfgRes.json()).data || {};
        // Real account disabled for now: backtest + Telegram signal alerts only.
        // if (cfg.account_type === 'original') accountType = 'original';

        const res = await fetch('/api/algo/status');
        const st = (await res.json()).data || {};
        if (st.is_running || st.is_armed) accountType = st.account_type === 'original' ? 'original' : 'demo';
    } catch (err) {
        console.warn('Failed to load account type', err);
    }
    applyAccountView(accountType);
    await refreshBalance();
    syncCapitalToAccount();
}

async function switchAccount(value) {
    if (value === 'original') return; // Real account disabled for now.
    if (value === currentAccountType) return;
    if (isAlgoActive) {
        alert('Stop the algo before switching accounts.');
        applyAccountView(currentAccountType);
        return;
    }

    const res = await fetch('/api/broker/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ broker: 'dhan', account_type: value })
    });
    const data = await res.json().catch(() => ({}));
    if (!data.success) {
        alert(data.error || 'Could not switch account.');
        applyAccountView(currentAccountType);
        return;
    }

    applyAccountView(value);
    await refreshBalance();
    syncCapitalToAccount();
    checkBrokerStatus();
    checkAlgoStatus();
    if (currentResults) runBacktest();
}

function onBalancePillClick() {
    if (currentAccountType !== 'demo') {
        refreshBalance();
        return;
    }
    const input = document.getElementById('header-capital-input');
    const val = document.getElementById('header-balance-val');
    if (!input || !input.classList.contains('hidden')) return;
    input.value = currentAllocatedCapital.toLocaleString('en-IN');
    val?.classList.add('hidden');
    document.getElementById('header-balance-edit')?.classList.add('hidden');
    input.classList.remove('hidden');
    input.focus();
    input.select();
}

function onCapitalInputKey(event) {
    if (event.key === 'Enter') event.target.blur();
    if (event.key === 'Escape') {
        event.target.dataset.cancel = '1';
        event.target.blur();
    }
}

// The header pill is the single place demo capital is edited.
async function saveDemoCapital() {
    const input = document.getElementById('header-capital-input');
    if (!input || input.classList.contains('hidden')) return;
    const cancelled = input.dataset.cancel === '1';
    delete input.dataset.cancel;
    const raw = input.value.trim();
    input.classList.add('hidden');
    document.getElementById('header-balance-val')?.classList.remove('hidden');
    document.getElementById('header-balance-edit')?.classList.remove('hidden');

    const amount = parseCapitalInput(raw);
    if (cancelled || !raw || amount === currentAllocatedCapital) return;
    if (isAlgoActive) {
        alert('Stop the algo before changing capital.');
        return;
    }
    try {
        const res = await fetch('/api/demo/funds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set', amount })
        });
        const data = await res.json();
        if (!data.success) {
            alert(data.error || 'Could not update demo capital.');
            return;
        }
        await refreshBalance();
        syncCapitalToAccount();
        if (currentResults) runBacktest();
    } catch (err) {
        alert('Network error updating demo capital.');
    }
}

async function onAccountTypeChange(value) {
    const [account] = value.split(':');
    if (isAlgoActive) {
        alert('Stop the algo before changing the trading mode.');
        syncBrokerLabels();
        return;
    }
    if (account === 'original' && !dhanTokenSaved && !dhanAutoLogin) {
        alert('Add your Dhan keys first (Dhan API box: Client ID + access token, or PIN + TOTP).');
        syncBrokerLabels();
        toggleKeysDrawer();
        return;
    }
    if (account !== currentAccountType) {
        await switchAccount(account);
    } else {
        syncBrokerLabels();
        await refreshBalance();
        syncCapitalToAccount();
        checkBrokerStatus();
    }
}

function toggleInputMask(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.type = el.type === 'password' ? 'text' : 'password';
}

// Original account: let the user pick a custom algo capital or use all available Dhan funds.
function startCustomAllocation() {
    const input = document.getElementById('alloc-custom-input');
    if (!input) return;
    input.value = currentAllocatedCapital.toLocaleString('en-IN');
    input.classList.remove('hidden');
    input.focus();
    input.select();
}

function onAllocInputKey(event) {
    if (event.key === 'Enter') event.target.blur();
    if (event.key === 'Escape') {
        event.target.dataset.cancel = '1';
        event.target.blur();
    }
}

async function saveCustomAllocation() {
    const input = document.getElementById('alloc-custom-input');
    if (!input || input.classList.contains('hidden')) return;
    const cancelled = input.dataset.cancel === '1';
    delete input.dataset.cancel;
    const raw = input.value.trim();
    input.classList.add('hidden');
    if (cancelled || !raw) return;
    const amount = parseCapitalInput(raw);
    if (currentBalance > 0 && amount > currentBalance) {
        alert(`Only ${formatINR(currentBalance)} is available in Dhan. The algo will be capped at that.`);
    }
    await setAllocation('custom', amount);
}

async function setAllocation(mode, amount) {
    if (isAlgoActive) {
        alert('Stop the algo before changing capital.');
        return;
    }
    try {
        const res = await fetch('/api/algo/capital', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode, amount })
        });
        const data = await res.json();
        if (!data.success) {
            alert(data.error || 'Could not update algo capital.');
            return;
        }
        await refreshBalance();
        syncCapitalToAccount();
    } catch (err) {
        alert('Network error updating algo capital.');
    }
}

function applyBalanceToUI(balance, accountType, allocatedCapital, ucc, allocationMode) {
    // Ignore late responses for an account the user has already switched away from.
    if (accountType !== currentAccountType) return;
    const fundsVal = Number(balance) || 0;
    const fundsText = formatINR(fundsVal);
    const capitalVal = accountType === 'demo' ? fundsVal : Number(allocatedCapital || LIVE_ALGO_CAPITAL);
    const capitalText = formatINR(capitalVal);
    currentBalance = fundsVal;
    currentAllocatedCapital = capitalVal;
    const liveCapSub = document.getElementById('live-capital-sub');

    const headerBal = document.getElementById('header-balance-val');
    const liveBalVal = document.getElementById('live-balance-value');
    const liveCapVal = document.getElementById('live-capital-value');
    const liveBalTitle = document.getElementById('live-bal-title');
    const liveBalUcc = document.getElementById('live-bal-ucc');

    if (accountType === 'original') {
        if (headerBal) headerBal.textContent = fundsText;
        if (liveBalVal) liveBalVal.textContent = fundsText;
        if (liveCapVal) liveCapVal.textContent = capitalText;
        const brokerName = brokerTitle(liveOrderBroker);
        if (liveBalTitle) liveBalTitle.textContent = `${brokerName} Live Available Funds`;
        if (liveBalUcc) liveBalUcc.textContent = ucc ? `Client ID: ${ucc} • Active Segment: F&O Enabled` : `${brokerName} live account`;
        if (liveCapSub) {
            liveCapSub.textContent = allocationMode === 'full'
                ? 'Full available funds • Guard ±10%'
                : `Custom • ${capitalText} of ${fundsText}`;
        }
        document.getElementById('btn-alloc-full')?.classList.toggle('active', allocationMode === 'full');
        document.getElementById('btn-alloc-custom')?.classList.toggle('active', allocationMode !== 'full');
    } else {
        if (headerBal) headerBal.textContent = fundsText;
        if (liveBalVal) liveBalVal.textContent = fundsText;
        if (liveCapVal) liveCapVal.textContent = capitalText;
        if (liveBalTitle) liveBalTitle.textContent = 'Demo Capital';
        if (liveBalUcc) liveBalUcc.textContent = 'Paper trading • Edit with ✎ in the header';
        if (liveCapSub) liveCapSub.textContent = 'Uses full demo cash';
    }

}

// Prefill backtest + strategy text with the active account's capital (only on load / switch).
function syncCapitalToAccount() {
    // Guards default to ±10% of capital for small accounts, ±₹15,000 otherwise.
    const guard = currentAllocatedCapital <= 100000 ? Math.round(currentAllocatedCapital * 0.10) : 15000;
    const target = document.getElementById('cfg-pl-target');
    const stop = document.getElementById('cfg-pl-stop');
    if (target) target.value = guard;
    if (stop) stop.value = -guard;
    refreshStrategyLabels();
    updateCapitalPreview();
    selectLiveStrategy(currentStrategyMode);
}

async function refreshBalance() {
    try {
        const res = await fetch(`/api/broker/balance?account_type=${currentAccountType}`);
        const data = await res.json();
        if (data.success) {
            applyBalanceToUI(data.balance, data.account_type, data.allocated_capital, data.ucc, data.allocation_mode);
            const msg = document.getElementById('broker-inline-msg');
            if (msg && currentAccountType === 'original') {
                msg.className = 'inline-msg ok';
                msg.textContent = `Dhan Live Connected. Available funds: ${formatINR(data.balance)}${data.ucc ? ` (UCC: ${data.ucc})` : ''}.`;
            }
        } else if (currentAccountType === 'original') {
            const msg = document.getElementById('broker-inline-msg');
            if (msg) {
                msg.className = 'inline-msg err';
                msg.textContent = data.error || 'Could not fetch Dhan balance. Check your Dhan keys.';
            }
        }
    } catch (err) {
        console.warn('Balance refresh failed', err);
    }
}


function brokerTitle() {
    return 'Dhan';
}

// Keep the header dropdown, account card and labels in step with the account.
function syncBrokerLabels() {
    const select = document.getElementById('account-type-select');
    if (select) select.value = currentAccountType === 'original' ? 'original:dhan' : 'demo';
    if (currentAccountType !== 'original') return;
    const headerLabel = document.getElementById('header-balance-label');
    if (headerLabel) headerLabel.textContent = 'Dhan Live Funds';
    const badge = document.getElementById('live-account-badge');
    if (badge) badge.textContent = 'Dhan Live';
}

function selectLiveBroker() { /* only Dhan */ }

function toggleKeysDrawer() {
    document.getElementById('dhan-keys-box')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function testLiveBrokerKeys() {
    return testLiveDhanKeys();
}

// Testing also saves: a successful test stores the keys on the server.
function saveLiveBrokerKeys() {
    return testLiveDhanKeys();
}

function setDhanBadge(cfg) {
    const badge = document.getElementById('dhan-auth-badge');
    if (!badge) return;
    if (!cfg?.is_connected) {
        badge.textContent = 'Not connected';
    } else if (cfg.data_plan && cfg.data_plan !== 'Active') {
        badge.textContent = '🟠 Connected • Data API not active';
    } else {
        badge.textContent = cfg.has_auto_login ? '🟢 Connected • auto login' : '🟢 Connected';
    }
}

async function testLiveDhanKeys() {
    if (isAlgoActive) {
        alert('Stop the algo before changing Dhan keys.');
        return;
    }
    const val = (id) => document.getElementById(id)?.value.trim() || '';
    const creds = {
        client_id: val('live-dhan-client-id'),
        access_token: val('live-dhan-token'),
        pin: val('live-dhan-pin'),
        totp_secret: val('live-dhan-totp'),
    };
    const msg = document.getElementById('broker-inline-msg');
    const hasLogin = creds.access_token || (creds.pin && creds.totp_secret);
    if (!creds.client_id || (!hasLogin && !dhanTokenSaved && !dhanAutoLogin)) {
        if (msg) {
            msg.className = 'inline-msg err';
            msg.textContent = 'Enter your Dhan Client ID plus an Access Token, or PIN + TOTP secret for automatic daily login.';
        }
        return;
    }
    const btn = document.getElementById('btn-test-dhan');
    const oldHtml = btn ? btn.innerHTML : '';
    if (btn) btn.innerHTML = '<span>Connecting...</span>';
    try {
        const res = await fetch('/api/broker/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ broker: 'dhan', credentials: creds })
        });
        const data = await res.json();
        if (msg) {
            msg.className = data.success ? (data.data_plan === 'Active' ? 'inline-msg ok' : 'inline-msg err') : 'inline-msg err';
            msg.textContent = data.success
                ? `✅ ${data.message} [Latency: ${data.latency_ms}ms]`
                : `❌ ${data.error || 'Failed to authenticate with Dhan'}`;
        }
        if (data.success) {
            ['live-dhan-token', 'live-dhan-pin', 'live-dhan-totp'].forEach((id) => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            applyDhanConfig(data.data?.dhan);
            refreshBalance();
            checkBrokerStatus();
        }
    } catch (e) {
        if (msg) {
            msg.className = 'inline-msg err';
            msg.textContent = '❌ Network error connecting to Dhan.';
        }
    } finally {
        if (btn) btn.innerHTML = oldHtml;
    }
}

function applyDhanConfig(dhan) {
    dhanTokenSaved = !!dhan?.has_access_token;
    dhanAutoLogin = !!dhan?.has_auto_login;
    const client = document.getElementById('live-dhan-client-id');
    if (client && !client.value) client.value = dhan?.client_id || '';
    const saved = 'Saved on server • paste to replace';
    const token = document.getElementById('live-dhan-token');
    if (token) token.placeholder = dhanTokenSaved ? saved : 'Paste Dhan access token (valid 24 h)';
    const pin = document.getElementById('live-dhan-pin');
    if (pin) pin.placeholder = dhanAutoLogin ? saved : 'Dhan login PIN';
    const totp = document.getElementById('live-dhan-totp');
    if (totp) totp.placeholder = dhanAutoLogin ? saved : 'Text code under the TOTP QR';
    setDhanBadge(dhan);
}

function clearLiveEvents() {
    const log = document.getElementById('live-event-log');
    if (log) log.innerHTML = '<div class="timeline-item"><span class="timeline-desc text-muted">Log cleared. Standing by for new events.</span></div>';
}

async function emergencySquareOffAll() {
    if (!confirm('Are you sure you want to square off ALL open positions immediately at market price?')) return;
    try {
        const res = await fetch('/api/algo/square_off_all', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert(data.message || 'All positions squared off.');
            checkAlgoStatus();
        } else {
            alert('Square Off Error: ' + (data.error || 'Failed to square off'));
        }
    } catch (e) {
        alert('Network error squaring off positions.');
    }
}

// The backtest toolbar picks what to backtest. While the algo is running it must not
// touch the live strategy, so backtesting another strategy never prompts or switches it.
function onQuickStrategySelect(val) {
    if (BACKTEST_ONLY_STRATEGIES.includes(val)) {
        updateCapitalPreview();
    } else if (!isAlgoActive) {
        selectLiveStrategy(val);
        onStrategyModeChange(val);
    } else {
        updateCapitalPreview();
    }
    if (currentResults) runBacktest();
}

function backtestStrategyMode() {
    return document.getElementById('bt-quick-strategy')?.value || currentStrategyMode;
}

function onQuickPeriodSelect() {
    updateBacktestSummaryPreview();
    if (currentResults) runBacktest();
}

// Single source for backtest params: strategy & period from the toolbar,
// capital from the account (header), everything else from Backtest Config.
function runBacktest() {
    const strategy = document.getElementById('bt-quick-strategy')?.value || currentStrategyMode;
    const period = document.getElementById('bt-quick-period')?.value || '1 year';
    const initialLots = parseInt(document.getElementById('cfg-initial-lots')?.value) || 1;
    const num = (id, fallback) => {
        const v = parseFloat(document.getElementById(id)?.value);
        return isNaN(v) ? fallback : v;
    };

    triggerBacktest({
        strategy_mode: strategy,
        period: period,
        capital: currentAllocatedCapital,
        slippage: num('cfg-slippage', 0),
        exclude_last_30: !!document.getElementById('cfg-exclude-last-30')?.checked,
        initial_lots: initialLots,
        reentry_lots: Math.max(1, Math.round(initialLots / 4)),
        lot_size: parseInt(document.getElementById('cfg-lot-size')?.value) || 65,
        sl_pct: num('cfg-sl-pct', 20) / 100.0,
        reentry_wait_minutes: parseInt(document.getElementById('cfg-reentry-wait')?.value) || 15,
        reentry_sl_pct: num('cfg-reentry-sl-pct', 40) / 100.0,
        pl_target: num('cfg-pl-target', Math.round(currentAllocatedCapital * 0.10)),
        pl_stop: num('cfg-pl-stop', -Math.round(currentAllocatedCapital * 0.10)),
        execution_mode: currentExecutionMode
    });
}

// Modal Controls - Backtest Config
function openConfigModal() {
    const overlay = document.getElementById('config-modal-overlay');
    if (overlay) overlay.classList.remove('hidden');
    updateCapitalPreview();
}

function onStrategyModeChange(mode) {
    currentStrategyMode = mode;
    updateCapitalPreview();
}

function onLotsEdited() {
    // Typing a lot count means the user wants fixed lots, not auto-sizing.
    const fixed = document.querySelector('input[name="cfg-sizing-mode"][value="fixed_lots"]');
    if (fixed) fixed.checked = true;
    updateBacktestSummaryPreview();
}

function updateCapitalPreview() {
    const capital = currentAllocatedCapital;
    const sizingMode = document.querySelector('input[name="cfg-sizing-mode"]:checked')?.value || 'auto_capital';
    const lotsInput = document.getElementById('cfg-initial-lots');
    if (sizingMode === 'auto_capital' && lotsInput) {
        lotsInput.value = Math.max(1, lotsFor(backtestStrategyMode(), capital));
    }
    updateBacktestSummaryPreview();
}

function updateBacktestSummaryPreview() {
    const stratSelect = document.getElementById('bt-quick-strategy');
    const periodSelect = document.getElementById('bt-quick-period');
    const stratName = stratSelect?.selectedOptions[0]?.textContent.trim() || currentStrategyMode;
    const periodName = periodSelect?.selectedOptions[0]?.textContent.trim() || '1 Year';
    const lotCount = parseInt(document.getElementById('cfg-initial-lots')?.value) || 1;
    const lotSize = parseInt(document.getElementById('cfg-lot-size')?.value) || 65;
    const capitalText = formatINR(currentAllocatedCapital);

    const context = document.getElementById('modal-context-line');
    if (context) {
        context.innerHTML = `<strong>${stratName}</strong> • ${periodName} • Capital <strong>${capitalText}</strong>` +
            `<span class="text-muted"> — change strategy &amp; period in the toolbar, capital in the header.</span>`;
    }
    const summaryText = document.getElementById('modal-summary-text');
    if (summaryText) {
        summaryText.innerHTML = `Backtesting <strong>${stratName}</strong> over <strong>${periodName}</strong> with <strong>${capitalText}</strong> capital • ${lotCount} Lot${lotCount > 1 ? 's' : ''} (${lotCount * lotSize} qty per leg).`;
    }
}

function closeConfigModal() {
    const overlay = document.getElementById('config-modal-overlay');
    if (overlay) overlay.classList.add('hidden');
}

function handleOverlayClick(event) {
    if (event.target.id === 'config-modal-overlay') {
        closeConfigModal();
    }
}

// Modal Controls - Day Session Audit
function openDayModal() {
    const overlay = document.getElementById('day-modal-overlay');
    if (overlay) overlay.classList.remove('hidden');
}

function closeDayModal() {
    const overlay = document.getElementById('day-modal-overlay');
    if (overlay) overlay.classList.add('hidden');
}

function handleDayOverlayClick(event) {
    if (event.target.id === 'day-modal-overlay') {
        closeDayModal();
    }
}

// Dhan keys live in the "Dhan API" box on the Live tab; these keep old buttons working.
function openBrokerModal() {
    toggleKeysDrawer();
}

function closeBrokerModal() { }

async function checkBrokerStatus() {
    try {
        const res = await fetch('/api/broker/config');
        const data = await res.json();
        if (data.success && data.data) {
            const dhan = data.data.dhan || {};
            applyDhanConfig(dhan);
            syncBrokerLabels();
            if (currentAccountType === 'demo') {
                updateHeaderBrokerStatus(!!dhan.is_connected, 'dhan', 'paper');
            } else {
                updateHeaderBrokerStatus(!!dhan.is_connected, 'dhan', 'live');
            }
        }
    } catch (err) {
        console.warn('Failed to fetch broker status:', err);
    }
}

function updateHeaderBrokerStatus(isConnected, brokerName, mode) {
    const textEl = document.getElementById('broker-status-text');
    const dotEl = document.getElementById('broker-dot');
    if (!textEl || !dotEl) return;

    if (mode === 'paper' && currentAccountType === 'demo') {
        textEl.textContent = isConnected ? 'Paper Trading • Dhan data' : 'Paper Trading • add Dhan keys';
        dotEl.className = isConnected ? 'status-dot pulse-green' : 'status-dot pulse-amber';
    } else if (isConnected) {
        textEl.textContent = `Dhan API (${(mode || 'LIVE').toUpperCase()})`;
        dotEl.className = 'status-dot pulse-green';
    } else {
        textEl.textContent = 'Dhan: Disconnected';
        dotEl.className = 'status-dot pulse-amber';
    }
}

// Algo Automation Engine Controls
let isAlgoActive = false;
let runningStrategyMode = null;

// Mark the one strategy the engine is running and lock the others until it stops.
function markRunningStrategy(mode) {
    runningStrategyMode = mode;
    if (mode && currentStrategyMode !== mode) {
        currentStrategyMode = mode;  // the backtest toolbar keeps its own choice
    }
    document.querySelectorAll('.strategy-pick').forEach((el) => {
        const isRunning = Boolean(mode) && el.dataset.mode === mode;
        if (mode) el.classList.toggle('active', isRunning);
        el.classList.toggle('running', isRunning);
        el.classList.toggle('locked', Boolean(mode) && !isRunning);
    });
}

// When the algo's own order is rejected, pop up exactly what to place by hand, once per failure.
const MANUAL_TRADE_SEEN_KEY = 'manualTradeSeenId';
let manualTradeShownId = null;

function manualTradeSeenId() {
    try { return localStorage.getItem(MANUAL_TRADE_SEEN_KEY); } catch (e) { return null; }
}

function showManualTrade(mt) {
    if (!mt || !mt.legs || !mt.legs.length) return;
    if (mt.id === manualTradeShownId || mt.id === manualTradeSeenId()) return;
    manualTradeShownId = mt.id;

    const fmt = (n) => `₹${Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    document.getElementById('manual-trade-subtitle').textContent =
        `${STRATEGY_NAMES[mt.strategy_mode] || mt.strategy_mode} • failed at ${mt.id.slice(11)} • Reason: ${mt.reason}`;

    document.getElementById('manual-trade-tbody').innerHTML = mt.legs.map((l, i) => {
        let sl = 'No SL: exits with the trade';
        if (l.sl_price) {
            const pct = Math.round(l.sl_pct * 100);
            sl = l.side === 'SELL'
                ? `Buy back if price ≥ <strong>${fmt(l.sl_price)}</strong> (+${pct}%)`
                : `Sell if price ≤ <strong>${fmt(l.sl_price)}</strong> (−${pct}%)`;
        }
        const sideCls = l.side === 'BUY' ? 'text-success' : 'text-danger';
        return `<tr><td>${i + 1}</td><td class="${sideCls}"><strong>${l.side}</strong></td>
            <td style="font-family: var(--font-mono)">${l.trading_symbol}</td><td>${l.qty} (${l.lots} lot)</td>
            <td>${fmt(l.ref_price)}</td><td>${sl}</td></tr>`;
    }).join('');

    const stuck = document.getElementById('manual-trade-stuck');
    if (mt.stuck_legs && mt.stuck_legs.length) {
        stuck.textContent = `These legs were filled and could NOT be closed by the algo: ${mt.stuck_legs.join(', ')}. Check your Dhan positions first so you don't double up.`;
        stuck.classList.remove('hidden');
    } else {
        stuck.classList.add('hidden');
    }

    const notes = [
        'Place the orders in the order shown (hedge BUYs before SELLs, so margin stays low).',
        'Ref. price is the LTP when the algo failed. Recompute the stop-loss from your actual fill price using the same %.',
        `Daily guard: exit everything at ${fmt(mt.pl_target)} profit or ${fmt(Math.abs(mt.pl_stop))} loss.`,
        `If any stop-loss hits, close ALL legs. Otherwise exit everything by ${mt.exit_time}.`,
        'The algo is halted for today and will not manage these positions.',
    ];
    document.getElementById('manual-trade-notes').innerHTML = notes.map((n) => `<li>${n}</li>`).join('');
    document.getElementById('manual-trade-overlay').classList.remove('hidden');

    try {
        if ('Notification' in window && Notification.permission === 'granted') {
            new Notification('Algo entry failed: place trade manually', { body: mt.legs.map((l) => `${l.side} ${l.trading_symbol}`).join(', ') });
        }
    } catch (e) { /* notifications are optional */ }
}

function dismissManualTrade() {
    if (manualTradeShownId) {
        try { localStorage.setItem(MANUAL_TRADE_SEEN_KEY, manualTradeShownId); } catch (e) { /* ignore */ }
    }
    document.getElementById('manual-trade-overlay').classList.add('hidden');
}

// Telegram alerts: token is saved server-side; chat ID is detected from the user's /start message.
function setTelegramMsg(ok, text) {
    const msg = document.getElementById('tg-msg');
    if (!msg) return;
    msg.className = ok ? 'inline-msg ok' : 'inline-msg err';
    msg.textContent = text;
}

function applyTelegramConfig(t) {
    if (!t) return;
    const enabled = document.getElementById('tg-enabled');
    const label = document.getElementById('tg-enabled-label');
    if (enabled) enabled.checked = !!t.enabled;
    if (label) label.textContent = t.enabled ? 'On' : 'Off';
    const token = document.getElementById('tg-bot-token');
    if (token) token.placeholder = t.has_bot_token ? 'Saved on server • paste to replace' : '123456789:AA...';
    // Once a bot and chat are linked, show a short "connected" line instead of the setup steps.
    const connected = !!(t.has_bot_token && t.chat_id);
    const name = document.getElementById('tg-chat-name');
    if (name) name.textContent = t.chat_name || 'your chat';
    showTelegramSetup(!connected);
}

function showTelegramSetup(show) {
    document.getElementById('tg-setup')?.classList.toggle('hidden', !show);
    document.getElementById('tg-connected')?.classList.toggle('hidden', show);
}

async function loadTelegramConfig() {
    try {
        const res = await fetch('/api/telegram/config');
        const data = await res.json();
        if (data.success) applyTelegramConfig(data.data);
    } catch (e) { /* optional feature */ }
}

async function connectTelegram() {
    const token = document.getElementById('tg-bot-token')?.value.trim();
    setTelegramMsg(true, 'Looking for your /start message...');
    try {
        const res = await fetch('/api/telegram/detect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ bot_token: token })
        });
        const data = await res.json();
        if (!data.success) {
            setTelegramMsg(false, `❌ ${data.error}`);
            return;
        }
        document.getElementById('tg-bot-token').value = '';
        await saveTelegramEnabled(true);
        setTelegramMsg(true, `✅ Linked to ${data.chat.name || 'your chat'} (${data.chat.chat_id}). Alerts are ON. Click Send Test.`);
    } catch (e) {
        setTelegramMsg(false, '❌ Network error reaching Telegram.');
    }
}

async function testTelegram() {
    try {
        const res = await fetch('/api/telegram/test', { method: 'POST' });
        const data = await res.json();
        setTelegramMsg(data.success, data.success ? '✅ Test message sent. Check Telegram.' : `❌ ${data.error}`);
    } catch (e) {
        setTelegramMsg(false, '❌ Network error reaching Telegram.');
    }
}

async function saveTelegramEnabled(enabled) {
    try {
        const res = await fetch('/api/telegram/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
        const data = await res.json();
        if (data.success) applyTelegramConfig(data.data);
    } catch (e) {
        setTelegramMsg(false, '❌ Could not save the alert setting.');
    }
}

async function checkAlgoStatus() {
    try {
        const res = await fetch('/api/algo/status');
        const data = await res.json();
        if (data.success && data.data) {
            updateAlgoStatusUI(data.data);
        }
    } catch (e) {
        console.warn('Algo status check note:', e);
    }
}

function updateAlgoStatusUI(st) {
    if (!st) return;
    isAlgoActive = Boolean(st.is_running || st.is_armed);
    if (st.manual_trade) showManualTrade(st.manual_trade);
    const btnNav = document.getElementById('btn-toggle-algo');
    const btnNavIcon = document.getElementById('algo-btn-icon');
    const btnNavText = document.getElementById('algo-btn-text');
    const btnBar = document.getElementById('algo-bar-action-btn');
    const btnBarIcon = document.getElementById('algo-bar-action-icon');
    const btnBarText = document.getElementById('algo-bar-action-text');
    const badge = document.getElementById('algo-status-badge');
    const desc = document.getElementById('algo-status-desc');

    const modeLabel = (st.account_type === 'original' || currentAccountType === 'original')
        ? `ORIGINAL (${'DHAN'} LIVE)`
        : 'DEMO (PAPER TRADING)';

    if (isAlgoActive) {
        if (btnNav) {
            btnNav.className = 'btn btn-stop-algo';
            if (btnNavIcon) btnNavIcon.textContent = '⏹';
            if (btnNavText) btnNavText.textContent = 'Stop Algo';
        }
        if (btnBar) {
            btnBar.className = 'btn btn-sm btn-stop-algo';
            if (btnBarIcon) btnBarIcon.textContent = '⏹';
            if (btnBarText) btnBarText.textContent = 'Stop Algo';
        }
        if (badge) {
            if (st.status === 'RUNNING') {
                badge.className = 'algo-status-badge status-running';
                badge.textContent = `● LIVE RUNNING (${st.account_type === 'original' ? 'DHAN' : 'PAPER'})`;
            } else if (st.status === 'ENTRY_FAILED' || st.status === 'ERROR') {
                badge.className = 'algo-status-badge status-error';
                badge.textContent = st.status === 'ERROR' ? '⚠ CHECK DHAN NOW' : '✖ ENTRY FAILED';
            } else if (st.status === 'COMPLETED') {
                badge.className = 'algo-status-badge status-idle';
                badge.textContent = '✔ DONE FOR TODAY';
            } else {
                badge.className = 'algo-status-badge status-armed';
                badge.textContent = '⏳ ARMED & SCHEDULED';
            }
        }
        const stratName = STRATEGY_NAMES[st.strategy_mode] || st.strategy_mode;
        if (desc) {
            desc.innerHTML = `<strong>${st.status} on ${modeLabel}</strong> • Strategy: <strong>${stratName}</strong> (${st.initial_lots || 1} lot) • ${st.status_message}`;
        }
        const stratBadge = document.getElementById('live-active-strat-badge');
        if (stratBadge) stratBadge.textContent = `${st.status === 'RUNNING' ? '● Running' : '⏳ Armed'}: ${stratName}`;
        markRunningStrategy(st.strategy_mode);
    } else {
        if (runningStrategyMode) {
            markRunningStrategy(null);
            selectLiveStrategy(currentStrategyMode);
        }
        if (btnNav) {
            btnNav.className = 'btn btn-start-algo';
            if (btnNavIcon) btnNavIcon.textContent = '▶';
            if (btnNavText) btnNavText.textContent = 'Start Algo';
        }
        if (btnBar) {
            btnBar.className = 'btn btn-sm btn-start-algo';
            if (btnBarIcon) btnBarIcon.textContent = '▶';
            if (btnBarText) btnBarText.textContent = 'Start Algo';
        }
        if (badge) {
            badge.className = 'algo-status-badge status-idle';
            badge.textContent = 'ALGO IDLE';
        }
        if (desc) {
            desc.innerHTML = `Algo engine is idle on <strong>${modeLabel}</strong>. Capital: <strong>${formatINR(currentAllocatedCapital)}</strong>. Click <strong>Start Algo</strong> to arm execution, or click <strong>⚡ Test Trigger</strong> to simulate an instant cycle.`;
        }
    }

    // 1. Update Live KPIs
    const dayPnlEl = document.getElementById('live-day-pnl');
    if (dayPnlEl) {
        // Before today's entry there is no P&L; ignore anything left over from a Test Trigger.
        const pnl = isAlgoActive && !st.has_entered ? 0 : Number(st.daily_pnl || 0);
        const sign = pnl > 0 ? '+' : '';
        dayPnlEl.textContent = `${sign}₹${pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
        dayPnlEl.className = pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '');
    }

    const spotEl = document.getElementById('live-spot-val');
    if (spotEl && st.spot) {
        spotEl.textContent = `₹${Number(st.spot).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
    }

    const openCountEl = document.getElementById('live-open-count');
    const openLegs = (st.open_positions || []).length;
    if (openCountEl) {
        openCountEl.textContent = `${openLegs} Open Leg${openLegs === 1 ? '' : 's'}`;
    }

    const guardEl = document.getElementById('live-guard-val');
    if (guardEl && st.pl_target) {
        // While idle, show the guard the next run will use (same rule as the engine: ±10% under ₹1L).
        const idleGuard = currentAllocatedCapital <= 100000 ? Math.round(currentAllocatedCapital * 0.10) : 15000;
        const target = isAlgoActive ? st.pl_target : idleGuard;
        const stop = isAlgoActive ? st.pl_stop : -idleGuard;
        guardEl.textContent = `+₹${Math.round(target).toLocaleString('en-IN')} / ₹${Math.round(stop).toLocaleString('en-IN')}`;
    }

    // 2. Update Active Positions Table
    const tbody = document.getElementById('live-positions-tbody');
    const posCountBadge = document.getElementById('positions-count-badge');
    const positions = st.active_positions || [];
    if (posCountBadge) {
        posCountBadge.textContent = `${positions.length} Position${positions.length === 1 ? '' : 's'}`;
    }

    if (tbody) {
        if (positions.length === 0) {
            tbody.innerHTML = `<tr><td colspan="9" class="text-muted" style="text-align: center; padding: 24px;">No positions yet. Click <strong>Start Algo</strong> to arm automation, or click <strong>⚡ Test Trigger / Cycle</strong> to simulate an instant execution cycle.</td></tr>`;
        } else {
            tbody.innerHTML = '';
            positions.forEach(p => {
                const tr = document.createElement('tr');
                const pnl = Number(p.pnl || 0);
                const pnlSign = pnl > 0 ? '+' : '';
                const pnlClass = pnl > 0 ? 'text-success' : (pnl < 0 ? 'text-danger' : '');
                const sideBadge = p.side === 'BUY'
                    ? '<span class="strat-tag tag-green">BUY</span>'
                    : '<span class="strat-tag tag-purple">SELL</span>';
                const statusBadge = p.is_open
                    ? '<span class="badge badge-success">● OPEN</span>'
                    : `<span class="badge badge-outline">${p.exit_reason || 'CLOSED'}</span>`;

                tr.innerHTML = `
                    <td><strong>${p.leg || '-'}</strong></td>
                    <td><span class="badge badge-outline">${p.branch || 'Active Leg'}</span></td>
                    <td>${sideBadge}</td>
                    <td>${p.qty || 65}</td>
                    <td>₹${Number(p.entry_price || 0).toFixed(2)}</td>
                    <td>₹${Number(p.current_price || p.exit_price || p.entry_price || 0).toFixed(2)}</td>
                    <td><strong class="${pnlClass}">${pnlSign}₹${pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</strong></td>
                    <td>${statusBadge}</td>
                    <td><code style="font-size: 11px; color: var(--text-muted);">${p.order_id || 'DHAN-MKT'}</code></td>
                `;
                tbody.appendChild(tr);
            });
        }
    }

    // 3. Update Real-Time Event Log
    const eventLogEl = document.getElementById('live-event-log');
    const events = st.events || [];
    if (eventLogEl && events.length > 0) {
        eventLogEl.innerHTML = '';
        const rev = [...events].reverse().slice(0, 30);
        rev.forEach(ev => {
            const item = document.createElement('div');
            item.className = 'timeline-item';
            let typeTag = `<span class="strat-tag tag-cyan" style="font-size:10px; margin-right:6px;">${ev.type || 'INFO'}</span>`;
            if (ev.type && ev.type.includes('ORDER')) typeTag = `<span class="strat-tag tag-green" style="font-size:10px; margin-right:6px;">${ev.type}</span>`;
            else if (ev.type && (ev.type.includes('SL') || ev.type.includes('ERROR'))) typeTag = `<span class="strat-tag tag-amber" style="font-size:10px; margin-right:6px;">${ev.type}</span>`;
            else if (ev.type && ev.type.includes('EXIT')) typeTag = `<span class="strat-tag tag-purple" style="font-size:10px; margin-right:6px;">${ev.type}</span>`;

            item.innerHTML = `
                <span class="timeline-time">${ev.timestamp || ''}</span>
                <span class="timeline-desc">${typeTag}${ev.detail || ''}</span>
            `;
            eventLogEl.appendChild(item);
        });
    }
}

async function toggleAlgoExecution() {
    // Ask once so a failed entry can raise a desktop notification, not just the in-page alert.
    try {
        if (!isAlgoActive && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
    } catch (e) { /* optional */ }
    const btn = document.getElementById('btn-toggle-algo');
    const btnBar = document.getElementById('algo-bar-action-btn');
    if (btn) btn.disabled = true;
    if (btnBar) btnBar.disabled = true;

    try {
        const endpoint = isAlgoActive ? '/api/algo/stop' : '/api/algo/start';
        const payload = isAlgoActive ? {} : {
            capital: currentAllocatedCapital,
            account_type: currentAccountType,
            strategy_mode: currentStrategyMode,
            lot_size: 65,
            initial_lots: 1
        };

        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.success) {
            updateAlgoStatusUI(data.state);
        } else {
            alert('Algo Error: ' + (data.error || 'Failed to update algo state'));
        }
    } catch (e) {
        console.error('Error toggling algo:', e);
        alert('Network error while toggling algo execution.');
    } finally {
        if (btn) btn.disabled = false;
        if (btnBar) btnBar.disabled = false;
    }
}

async function runPaperTestCycle() {
    const desc = document.getElementById('algo-status-desc');
    if (desc) desc.innerHTML = '<strong>Executing Test Cycle:</strong> Simulating strategy entry, live pricing, stop-loss checks, and execution...';

    try {
        const res = await fetch('/api/algo/test_cycle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ strategy_mode: currentStrategyMode })
        });
        const data = await res.json();
        if (data.success) {
            const isPos = data.net_pnl >= 0;
            if (desc) {
                desc.innerHTML = `✅ <strong>Test Cycle Executed (${data.session_date} • ${data.strategy || currentStrategyMode}):</strong> Net P&L <span class="${isPos ? 'text-success' : 'text-danger'}">${isPos ? '+' : ''}₹${Number(data.net_pnl).toLocaleString('en-IN', {minimumFractionDigits: 2})}</span> • Exit: ${data.stop_reason} • ${data.positions.length} Positions Executed.`;
            }
            if (data.state) updateAlgoStatusUI(data.state);
        } else {
            alert('Test Error: ' + (data.error || 'Failed to run test cycle'));
        }
    } catch (e) {
        console.error('Error running test cycle:', e);
        alert('Network error running test cycle.');
    }
}

function toggleArchitecture() {
    const body = document.getElementById('arch-body');
    const label = document.getElementById('arch-toggle-label');
    if (!body) return;
    const nowHidden = body.classList.toggle('hidden');
    if (label) label.textContent = nowHidden ? 'Show ▾' : 'Hide ▴';
}

// Execute Backtest from Modal values
function executeBacktestFromModal() {
    closeConfigModal();
    runBacktest();
}

// API Call to execute backtest
async function triggerBacktest(params) {
    const loadingBar = document.getElementById('loading-bar-container');
    if (loadingBar) loadingBar.classList.remove('hidden');

    try {
        const response = await fetch('/api/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params)
        });

        const data = await response.json();
        if (data.success) {
            currentResults = data.data;
            renderDashboard(currentResults);
        } else {
            alert('Backtest Error: ' + (data.error || 'Failed to execute'));
        }
    } catch (err) {
        console.error('Backtest error:', err);
        alert('Network or server error while running backtest.');
    } finally {
        if (loadingBar) loadingBar.classList.add('hidden');
    }
}

// Render Dashboard components
function renderDashboard(results) {
    // 1. KPI Cards
    const netPnlEl = document.getElementById('val-net-pnl');
    const netPnlCard = document.getElementById('kpi-net-pnl');
    const roiBadge = document.getElementById('kpi-roi-badge');
    
    const isNetPos = results.total_net_pnl >= 0;
    netPnlEl.textContent = `${isNetPos ? '+' : ''}₹${results.total_net_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
    netPnlEl.className = 'kpi-value ' + (isNetPos ? 'text-success' : 'text-danger');
    roiBadge.textContent = `${isNetPos ? '+' : ''}${results.roi_pct}% ROI`;
    roiBadge.className = 'kpi-badge ' + (isNetPos ? 'text-success' : 'text-danger');

    document.getElementById('val-gross-pnl').textContent = `₹${results.total_gross_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
    document.getElementById('val-charges').textContent = `₹${results.total_charges.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;

    const winRateEl = document.getElementById('val-win-rate');
    winRateEl.textContent = `${results.win_rate}%`;
    winRateEl.className = 'kpi-value ' + (results.win_rate >= 50 ? 'text-success' : 'text-danger');
    
    const totalTradesText = results.total_trades ? `${results.total_trades} Orders (${results.total_days} Days)` : `${results.total_days} Sessions`;
    document.getElementById('val-total-trades').textContent = totalTradesText;
    document.getElementById('val-win-days').textContent = results.win_days;
    document.getElementById('val-loss-days').textContent = results.loss_days;

    document.getElementById('val-profit-factor').textContent = results.profit_factor.toFixed(2);
    document.getElementById('val-avg-win').textContent = `₹${Math.round(results.avg_win).toLocaleString('en-IN')}`;
    document.getElementById('val-avg-loss').textContent = `₹${Math.round(results.avg_loss).toLocaleString('en-IN')}`;

    document.getElementById('val-max-dd').textContent = `₹${results.max_drawdown.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
    document.getElementById('val-max-dd-pct').textContent = `-${results.max_drawdown_pct}%`;
    document.getElementById('val-worst-day').textContent = `₹${results.worst_day.toLocaleString('en-IN')}`;
    document.getElementById('val-best-day').textContent = `+₹${results.best_day.toLocaleString('en-IN')}`;

    document.getElementById('val-sharpe').textContent = results.sharpe_ratio.toFixed(2);
    document.getElementById('val-sharpe-num').textContent = results.sharpe_ratio.toFixed(2);
    document.getElementById('val-sortino-num').textContent = results.sortino_ratio.toFixed(2);

    const avgDailyEl = document.getElementById('val-avg-daily');
    const isAvgPos = results.avg_daily_pnl >= 0;
    avgDailyEl.textContent = `${isAvgPos ? '+' : ''}₹${results.avg_daily_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}`;
    avgDailyEl.className = 'kpi-value ' + (isAvgPos ? 'text-success' : 'text-danger');

    // Display Active Period & Capital Prominently
    const periodDisplay = results.params ? results.params.period.toUpperCase() : "1 YEAR";
    const capitalDisplay = results.params ? `₹${results.params.capital.toLocaleString('en-IN')}` : "₹40,000";
    const lotSize = results.params?.lot_size || 65;
    const lotsNum = results.params ? results.params.initial_lots : 1;
    const isGold = (results.strategy_id || results.strategy_mode) === 'gold_trend_futures';
    const lotsDisplay = isGold ? `${lotsNum} Lot${lotsNum > 1 ? 's' : ''} (${lotsNum * 100} g Gold Mini)` : `${lotsNum} Lot${lotsNum > 1 ? 's' : ''} (${lotsNum * lotSize} CE / ${lotsNum * lotSize} PE)`;

    // Strategy Mode Label
    const stMode = results.strategy_id || results.strategy_mode || currentStrategyMode || 'hedged_selling';
    const stratNames = {
        'hedged_selling': 'Hedged Selling (Iron Butterfly)',
        'nifty_10_12_put': '10:12 Bearish Put Breakdown',
        'nifty_gap_drive': 'Nifty Gap Drive Momentum',
        'nifty_1pm_drop': '1 PM Volatility Drop',
        'option_buying': 'Option Buying (Long Straddle)',
        'short_straddle': 'Classic Short Straddle',
        'trend_hedged_straddle': 'Trend-Hedged Short Straddle',
        'gold_trend_futures': 'Gold Mini Trend Futures'
    };
    const modeText = results.strategy_name || stratNames[stMode] || stMode;

    // Update Header Navigation Badges
    const headerMode = document.getElementById('header-mode-val');
    if (headerMode) headerMode.textContent = modeText;

    const headerPeriod = document.getElementById('header-period-val');
    if (headerPeriod) headerPeriod.textContent = results.params ? results.params.period : "1 year";

    const headerCapital = document.getElementById('header-capital-val');
    if (headerCapital) headerCapital.textContent = capitalDisplay;

    // Update Active Configuration Overview Bar
    const barMode = document.getElementById('bar-mode-val');
    if (barMode) barMode.textContent = modeText;

    const barPeriod = document.getElementById('bar-period-val');
    if (barPeriod) barPeriod.textContent = `${periodDisplay} (${results.total_days} Trading Days)`;

    const barCapital = document.getElementById('bar-capital-val');
    if (barCapital) barCapital.textContent = capitalDisplay;

    const barLots = document.getElementById('bar-lots-val');
    if (barLots) barLots.textContent = lotsDisplay;

    const barRoi = document.getElementById('bar-roi-val');
    if (barRoi) {
        barRoi.textContent = `${isNetPos ? '+' : ''}${results.roi_pct}% ROI`;
        barRoi.className = 'active-config-value ' + (isNetPos ? 'text-success' : 'text-danger');
    }

    const barGuard = document.getElementById('bar-guard-val');
    if (barGuard && isGold) {
        barGuard.textContent = 'SL 0.5% • Target +1% • Exit 11:00 PM';
    } else if (barGuard && results.params) {
        barGuard.textContent = `SL ${(results.params.sl_pct * 100).toFixed(0)}% • Target +₹${Math.round(results.params.pl_target).toLocaleString('en-IN')} • Stop ₹${Math.round(results.params.pl_stop).toLocaleString('en-IN')}`;
    }

    // Update Card 6 (Avg Daily P&L footer)
    const valCapUsed = document.getElementById('val-capital-used');
    if (valCapUsed) valCapUsed.textContent = capitalDisplay;

    const valPeriodUsed = document.getElementById('val-period-used');
    if (valPeriodUsed) valPeriodUsed.textContent = results.params ? results.params.period : "1 year";

    // 2. Branch Trigger Analytics
    const bb = results.branch_breakdown || {};
    const tot = results.total_days || 1;
    document.getElementById('stat-ce-sl').textContent = `${bb.ce_sl_count || 0} (${((bb.ce_sl_count || 0) / tot * 100).toFixed(1)}%)`;
    document.getElementById('stat-pe-sl').textContent = `${bb.pe_sl_count || 0} (${((bb.pe_sl_count || 0) / tot * 100).toFixed(1)}%)`;
    document.getElementById('stat-both-sl').textContent = `${bb.both_sl_count || 0}`;
    document.getElementById('stat-reentry-days').textContent = `${bb.reentry_days_count || 0}`;
    document.getElementById('stat-guard-target').textContent = `${bb.guard_target_count || 0}`;
    document.getElementById('stat-guard-stop').textContent = `${bb.guard_stop_count || 0}`;
    document.getElementById('stat-time-exit').textContent = `${bb.time_exit_count || 0}`;

    // 3. Render Charts
    renderEquityCurve(results.equity_curve);
    renderMonthlyMatrix(results.monthly_matrix);
    renderPnLDistribution(results.trade_logs);

    // 4. Populate Table
    applyFilterAndSearch();
}

// Plotly Interactive Cumulative Equity & Drawdown Chart
function renderEquityCurve(equityData) {
    if (!equityData || equityData.length === 0) return;

    const dates = equityData.map(d => d.date);
    const cumPnL = equityData.map(d => d.cumulative_pnl);

    // Compute High Water Mark & Drawdowns
    let peak = 0;
    const hwm = [];
    const drawdown = [];

    cumPnL.forEach(val => {
        if (val > peak) peak = val;
        hwm.push(peak);
        drawdown.push(val - peak);
    });

    const traceEquity = {
        x: dates,
        y: cumPnL,
        type: 'scatter',
        mode: 'lines',
        name: 'Cumulative P&L (₹)',
        line: { color: '#06b6d4', width: 2.5 },
        fill: 'tozeroy',
        fillcolor: 'rgba(6, 182, 212, 0.08)',
        yaxis: 'y1'
    };

    const traceHWM = {
        x: dates,
        y: hwm,
        type: 'scatter',
        mode: 'lines',
        name: 'High Water Mark',
        line: { color: '#10b981', width: 1.5, dash: 'dot' },
        yaxis: 'y1'
    };

    const traceDD = {
        x: dates,
        y: drawdown,
        type: 'scatter',
        mode: 'lines',
        name: 'Drawdown (₹)',
        line: { color: '#f43f5e', width: 1.5 },
        fill: 'tozeroy',
        fillcolor: 'rgba(244, 63, 94, 0.15)',
        yaxis: 'y2'
    };

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { l: 60, r: 50, t: 20, b: 40 },
        hovermode: 'x unified',
        legend: { orientation: 'h', x: 0.02, y: 1.15, font: { color: '#94a3b8', size: 12 } },
        grid: { rows: 2, columns: 1, pattern: 'independent', roworder: 'top to bottom' },
        xaxis: {
            gridcolor: 'rgba(255, 255, 255, 0.05)',
            tickfont: { color: '#64748b', size: 11 }
        },
        yaxis: {
            title: 'Cumulative P&L (₹)',
            titlefont: { color: '#06b6d4', size: 12 },
            gridcolor: 'rgba(255, 255, 255, 0.06)',
            tickfont: { color: '#94a3b8', size: 11 },
            domain: [0.35, 1.0]
        },
        yaxis2: {
            title: 'Underwater Drawdown (₹)',
            titlefont: { color: '#f43f5e', size: 12 },
            gridcolor: 'rgba(255, 255, 255, 0.04)',
            tickfont: { color: '#94a3b8', size: 11 },
            domain: [0.0, 0.28]
        }
    };

    Plotly.newPlot('plotly-equity-chart', [traceEquity, traceHWM, traceDD], layout, { responsive: true, displayModeBar: false });
}

// Render Monthly Returns Matrix Heatmap
function renderMonthlyMatrix(matrix) {
    const container = document.getElementById('monthly-matrix-container');
    if (!container || !matrix || matrix.length === 0) return;

    const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    
    let html = `<table class="monthly-table"><thead><tr><th>Year</th>`;
    monthNames.forEach(m => html += `<th>${m}</th>`);
    html += `<th>Total (₹)</th></tr></thead><tbody>`;

    matrix.forEach(row => {
        html += `<tr><td class="year-cell">${row.year}</td>`;
        for (let m = 1; m <= 12; m++) {
            const val = row[`m_${m}`] || 0;
            let cls = 'cell-zero';
            if (val > 0) cls = 'cell-pos';
            else if (val < 0) cls = 'cell-neg';
            
            const displayVal = val === 0 ? '-' : (val > 0 ? `+${Math.round(val).toLocaleString('en-IN')}` : Math.round(val).toLocaleString('en-IN'));
            html += `<td class="${cls}">${displayVal}</td>`;
        }
        const totCls = row.total >= 0 ? 'text-success' : 'text-danger';
        html += `<td class="total-cell ${totCls}">${row.total >= 0 ? '+' : ''}₹${Math.round(row.total).toLocaleString('en-IN')}</td></tr>`;
    });

    html += `</tbody></table>`;
    container.innerHTML = html;
}

// Render Daily P&L Distribution
function renderPnLDistribution(trades) {
    if (!trades || trades.length === 0) return;

    const pnls = trades.map(t => t.net_pnl);
    const colors = pnls.map(p => p >= 0 ? '#10b981' : '#f43f5e');

    const trace = {
        x: pnls,
        type: 'histogram',
        marker: { color: '#38bdf8' },
        opacity: 0.85,
        nbinsx: 35
    };

    const layout = {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        margin: { l: 50, r: 30, t: 20, b: 40 },
        xaxis: {
            title: 'Net Daily P&L (₹)',
            titlefont: { color: '#94a3b8', size: 12 },
            gridcolor: 'rgba(255, 255, 255, 0.05)',
            tickfont: { color: '#64748b', size: 11 }
        },
        yaxis: {
            title: 'Session Frequency',
            titlefont: { color: '#94a3b8', size: 12 },
            gridcolor: 'rgba(255, 255, 255, 0.05)',
            tickfont: { color: '#64748b', size: 11 }
        }
    };

    Plotly.newPlot('plotly-dist-chart', [trace], layout, { responsive: true, displayModeBar: false });
}

// Chart Tab switcher
function switchChartTab(tab) {
    activeChartTab = tab;
    
    const btnEquity = document.getElementById('tab-btn-equity');
    const btnMonthly = document.getElementById('tab-btn-monthly');
    const btnDist = document.getElementById('tab-btn-distribution');
    
    const tabEquity = document.getElementById('tab-equity');
    const tabMonthly = document.getElementById('tab-monthly');
    const tabDist = document.getElementById('tab-distribution');

    [btnEquity, btnMonthly, btnDist].forEach(b => { if (b) b.classList.remove('active'); });
    [tabEquity, tabMonthly, tabDist].forEach(c => { if (c) c.classList.add('hidden'); });

    if (tab === 'equity') {
        if (btnEquity) btnEquity.classList.add('active');
        if (tabEquity) tabEquity.classList.remove('hidden');
        setTimeout(() => { Plotly.relayout('plotly-equity-chart', { autosize: true }); }, 50);
    } else if (tab === 'monthly') {
        if (btnMonthly) btnMonthly.classList.add('active');
        if (tabMonthly) tabMonthly.classList.remove('hidden');
    } else if (tab === 'distribution') {
        if (btnDist) btnDist.classList.add('active');
        if (tabDist) tabDist.classList.remove('hidden');
        setTimeout(() => { Plotly.relayout('plotly-dist-chart', { autosize: true }); }, 50);
    }
}

function resetChartZoom() {
    if (activeChartTab === 'equity') {
        Plotly.relayout('plotly-equity-chart', { 'xaxis.autorange': true, 'yaxis.autorange': true });
    } else if (activeChartTab === 'distribution') {
        Plotly.relayout('plotly-dist-chart', { 'xaxis.autorange': true, 'yaxis.autorange': true });
    }
}

// Table Filtering and Search
function filterTable(filter) {
    currentFilter = filter;
    document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
    const btn = document.querySelector(`.filter-chip[data-filter="${filter}"]`);
    if (btn) btn.classList.add('active');
    currentPage = 1;
    applyFilterAndSearch();
}

function searchTable(query) {
    currentSearch = query.trim().toLowerCase();
    currentPage = 1;
    applyFilterAndSearch();
}

function toggleDateSort() {
    dateSortOrder = (dateSortOrder === 'desc') ? 'asc' : 'desc';
    const icon = document.getElementById('sort-date-icon');
    if (icon) {
        icon.textContent = dateSortOrder === 'desc' ? '▼ (Latest)' : '▲ (Oldest)';
    }
    currentPage = 1;
    applyFilterAndSearch();
}

function applyFilterAndSearch() {
    if (!currentResults || !currentResults.trade_logs) return;

    let list = [...currentResults.trade_logs];

    // Sort by Date (Descending by default: newest sessions first)
    if (dateSortOrder === 'desc') {
        list.sort((a, b) => b.date.localeCompare(a.date));
    } else {
        list.sort((a, b) => a.date.localeCompare(b.date));
    }

    // Apply Filter
    if (currentFilter === 'win') {
        list = list.filter(t => t.net_pnl > 0);
    } else if (currentFilter === 'loss') {
        list = list.filter(t => t.net_pnl <= 0);
    } else if (currentFilter === 'ce_sl') {
        list = list.filter(t => t.ce_sl_hit);
    } else if (currentFilter === 'pe_sl') {
        list = list.filter(t => t.pe_sl_hit);
    } else if (currentFilter === 'guard') {
        list = list.filter(t => t.exit_reason && t.exit_reason.toLowerCase().includes('guard'));
    } else if (currentFilter === 'reentry') {
        list = list.filter(t => t.reentry_count > 0);
    }

    // Apply Search
    if (currentSearch) {
        list = list.filter(t => t.date.includes(currentSearch) || t.day_name.toLowerCase().includes(currentSearch) || (t.exit_reason && t.exit_reason.toLowerCase().includes(currentSearch)));
    }

    filteredTrades = list;
    document.getElementById('trade-count-badge').textContent = `${filteredTrades.length} Sessions`;

    renderTablePage();
}

function renderTablePage() {
    const tbody = document.getElementById('trades-tbody');
    tbody.innerHTML = '';

    const total = filteredTrades.length;
    const totalPages = Math.max(1, Math.ceil(total / rowsPerPage));
    if (currentPage > totalPages) currentPage = totalPages;

    const startIdx = (currentPage - 1) * rowsPerPage;
    const endIdx = Math.min(startIdx + rowsPerPage, total);
    const pageItems = filteredTrades.slice(startIdx, endIdx);

    document.getElementById('pagination-info').textContent = `Showing ${pageItems.length > 0 ? startIdx + 1 : 0} to ${endIdx} of ${total} sessions`;
    document.getElementById('page-num-indicator').textContent = `${currentPage} / ${totalPages}`;
    document.getElementById('prev-page-btn').disabled = currentPage <= 1;
    document.getElementById('next-page-btn').disabled = currentPage >= totalPages;

    pageItems.forEach((trade, idx) => {
        const tr = document.createElement('tr');
        
        const isPos = trade.net_pnl >= 0;
        const pnlCol = isPos ? 'text-success' : 'text-danger';

        const reasonBadge = `<span class="badge-tag ${exitBadgeClass(trade)}" title="${escapeAttr(trade.exit_reason || '')}">${simpleExitLabel(trade)}</span>`;

        const ceStatus = trade.ce_sl_hit ? '<span class="text-danger" style="font-size:11px;">(SL Hit)</span>' : '<span class="text-success" style="font-size:11px;">(Closed)</span>';
        const peStatus = trade.pe_sl_hit ? '<span class="text-danger" style="font-size:11px;">(SL Hit)</span>' : '<span class="text-success" style="font-size:11px;">(Closed)</span>';

        const sideBadge = trade.side === 'BUY' 
            ? '<span class="badge-tag badge-buy" style="margin-right: 4px;">BUY</span>' 
            : '<span class="badge-tag badge-sell" style="margin-right: 4px;">SELL</span>';
        const legCell = (strike, type, entry, exit, status) => hasLeg(strike, entry)
            ? `${sideBadge}${strike} ${type} @ ₹${entry} ➔ ₹${exit} ${status}`
            : '<span class="text-muted">—</span>';

        tr.innerHTML = `
            <td><strong>${trade.date}</strong></td>
            <td><span class="badge">${trade.day_name}</span></td>
            <td>${trade.spot_at_entry}</td>
            <td>${legCell(trade.ce_strike, 'CE', trade.ce_entry, trade.ce_exit, ceStatus)}</td>
            <td>${legCell(trade.pe_strike, 'PE', trade.pe_entry, trade.pe_exit, peStatus)}</td>
            <td>${trade.reentry_count > 0 ? `<span class="badge-tag badge-reentry">${trade.reentry_count} Set (₹${trade.reentry_pnl})</span>` : '-'}</td>
            <td>${reasonBadge}</td>
            <td>₹${trade.gross_pnl.toFixed(2)}</td>
            <td>₹${trade.charges.toFixed(2)}</td>
            <td><strong class="${pnlCol}">${isPos ? '+' : ''}₹${trade.net_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}</strong></td>
            <td>
                <button class="btn btn-sm btn-outline" onclick="inspectDay('${trade.date}')">
                    <span>Inspect</span>
                </button>
            </td>
        `;
        tbody.appendChild(tr);

        const story = document.createElement('tr');
        story.className = 'trade-story-row';
        story.innerHTML = `<td colspan="11"><div class="story-text"><span class="story-label">What happened:</span> ${tradeStory(trade)}</div></td>`;
        tbody.appendChild(story);
    });
}

// ---- Plain-English trade explanations ----

function escapeAttr(str) {
    return String(str).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function hasLeg(strike, entry) {
    return strike && strike !== '-' && Number(entry) > 0;
}

function simpleExitLabel(trade) {
    const r = (trade.exit_reason || '').toLowerCase();
    if (r.includes('guard') && r.includes('profit')) return '🎯 Daily profit target hit';
    if (r.includes('guard') && r.includes('loss')) return '🛑 Daily loss limit hit';
    if (r.includes('time')) return '⏰ Closed at 3:25 PM';
    if (r.includes('trailing')) return '📉 Trailing stop exit';
    if (r.includes('target')) return '🎯 Target hit';
    if (r.includes('sl')) return '🛑 Stop-loss hit';
    return trade.exit_reason || 'Closed';
}

function exitBadgeClass(trade) {
    const r = (trade.exit_reason || '').toLowerCase();
    if (r.includes('guard')) return 'badge-guard';
    if (r.includes('time')) return 'badge-time';
    if (r.includes('sl')) return 'badge-sl';
    return '';
}

function rupees(n) {
    const v = Number(n) || 0;
    return `₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
}

// Builds a short step-by-step sentence like:
// "10:00 AM: Sold Call 22700 @ ₹76.58 and Put 22700 @ ₹68.06 → Call rose 21% … → Result: +₹2,807 after ₹217 charges."
function tradeStory(trade) {
    const selling = trade.side !== 'BUY';
    const verb = selling ? 'Sold' : 'Bought';
    const params = currentResults?.params || {};
    const legs = [];
    if (hasLeg(trade.ce_strike, trade.ce_entry)) legs.push({ name: 'Call', strike: trade.ce_strike, entry: trade.ce_entry, exit: trade.ce_exit, sl: trade.ce_sl_hit });
    if (hasLeg(trade.pe_strike, trade.pe_entry)) legs.push({ name: 'Put', strike: trade.pe_strike, entry: trade.pe_entry, exit: trade.pe_exit, sl: trade.pe_sl_hit });
    if (!legs.length) return 'No trade taken this session (entry condition not met).';

    const steps = [];
    const legText = legs.map((l) => `${l.name} ${l.strike} @ ₹${l.entry}`).join(' and ');
    const wings = params.strategy_mode === 'hedged_selling' ? ', plus cheap far-away protection options to cap the loss' : '';
    steps.push(`<strong>${trade.entry_time || ''}</strong> ${verb} ${legText}${wings}${selling ? ' (collected premium; profit if prices fall)' : ' (profit if price rises)'}`);

    const slLegs = legs.filter((l) => l.sl);
    slLegs.forEach((l) => {
        const pct = Math.round(((l.exit - l.entry) / l.entry) * 100);
        const moved = pct >= 0 ? `rose ${pct}%` : `fell ${Math.abs(pct)}%`;
        steps.push(`${l.name} ${moved} (₹${l.entry} → ₹${l.exit}) and hit its stop-loss${legs.length > 1 ? ', so all positions were closed' : ''}`);
    });

    if (trade.reentry_count > 0) {
        const wait = params.reentry_wait_minutes || 15;
        const res = trade.reentry_pnl >= 0 ? `made ${rupees(trade.reentry_pnl)}` : `lost ${rupees(trade.reentry_pnl)}`;
        const reSl = params.reentry_sl_pct ? `with a ${Math.round(params.reentry_sl_pct * 100)}% stop-loss` : 'no stop-loss';
        steps.push(`after ${wait} min, re-entered a fresh ${selling ? 'short' : 'long'} straddle (${reSl}) which ${res}`);
    }

    const r = (trade.exit_reason || '').toLowerCase();
    if (r.includes('guard') && r.includes('profit')) steps.push(`total profit for the day reached the ${rupees(params.pl_target)} target, so everything was closed`);
    else if (r.includes('guard') && r.includes('loss')) steps.push(`total loss for the day reached the ${rupees(params.pl_stop)} limit, so everything was closed to stop further loss`);
    else if (r.includes('time')) steps.push('remaining positions were closed at 3:25 PM');
    else if (r.includes('trailing')) steps.push('the trailing stop closed the trade to lock in the move');
    else if (r.includes('target')) steps.push('price reached the profit target and the trade was closed');
    else if (!slLegs.length && trade.exit_reason) steps.push(`closed: ${trade.exit_reason}`);

    const isPos = trade.net_pnl >= 0;
    steps.push(`<strong class="${isPos ? 'text-success' : 'text-danger'}">Result: ${isPos ? '+' : '−'}${rupees(trade.net_pnl)}</strong> after ${rupees(trade.charges)} charges`);
    return steps.join(' → ');
}

function changePage(delta) {
    currentPage += delta;
    renderTablePage();
}

// Day Detail Inspection Modal
async function inspectDay(dateStr) {
    try {
        const response = await fetch(`/api/day_detail/${dateStr}`);
        const data = await response.json();
        if (!data.success) {
            alert('Failed to load day details.');
            return;
        }

        const day = data.data;
        document.getElementById('day-modal-title').textContent = `Session Audit: ${day.date} (${day.day_name})`;
        document.getElementById('day-modal-subtitle').textContent = `Spot @ 10:00: ${day.spot_at_entry} | Exit Reason: ${day.exit_reason}`;

        // Top Badges
        const isPos = day.net_pnl >= 0;
        const banner = document.getElementById('day-stats-banner');
        banner.innerHTML = `
            <div class="stats-pill"><span class="pill-label">Gross P&L:</span><span class="pill-val">₹${day.gross_pnl.toFixed(2)}</span></div>
            <div class="stats-pill"><span class="pill-label">Charges:</span><span class="pill-val">₹${day.charges.toFixed(2)}</span></div>
            <div class="stats-pill"><span class="pill-label">Net P&L:</span><strong class="pill-val ${isPos ? 'text-success' : 'text-danger'}">${isPos ? '+' : ''}₹${day.net_pnl.toFixed(2)}</strong></div>
            <div class="stats-pill"><span class="pill-label">CE SL:</span><span class="pill-val ${day.ce_sl_hit ? 'text-danger' : 'text-success'}">${day.ce_sl_hit ? 'HIT' : 'NO'}</span></div>
            <div class="stats-pill"><span class="pill-label">PE SL:</span><span class="pill-val ${day.pe_sl_hit ? 'text-danger' : 'text-success'}">${day.pe_sl_hit ? 'HIT' : 'NO'}</span></div>
            <div class="stats-pill"><span class="pill-label">Re-entries:</span><span class="pill-val text-cyan">${day.reentry_count} Sets</span></div>
        `;

        // Positions sub-table
        const posTbody = document.getElementById('day-positions-tbody');
        posTbody.innerHTML = '';
        (day.positions_detail || []).forEach(pos => {
            const pnlPos = pos.pnl >= 0;
            const r = document.createElement('tr');
            r.innerHTML = `
                <td><strong>${pos.leg}</strong></td>
                <td><span class="badge">${pos.branch}</span></td>
                <td>${pos.qty}</td>
                <td>₹${pos.entry_price.toFixed(2)}</td>
                <td>₹${pos.exit_price.toFixed(2)}</td>
                <td><span class="badge-tag">${pos.exit_reason || '-'}</span></td>
                <td><strong class="${pnlPos ? 'text-success' : 'text-danger'}">${pnlPos ? '+' : ''}₹${pos.pnl.toFixed(2)}</strong></td>
            `;
            posTbody.appendChild(r);
        });

        // Timeline Items
        const timelineBox = document.getElementById('day-timeline-items');
        timelineBox.innerHTML = '';
        (day.events || []).forEach(ev => {
            const item = document.createElement('div');
            item.className = 'timeline-item';
            item.innerHTML = `
                <span class="timeline-time">${ev.timestamp}</span>
                <span class="timeline-desc"><strong>[${ev.type}]</strong> ${ev.detail || ev.reason || `${ev.leg} squared off @ ₹${ev.price}`}</span>
            `;
            timelineBox.appendChild(item);
        });

        // Intraday Chart
        if (day.intraday_curve && day.intraday_curve.length > 0) {
            const times = day.intraday_curve.map(c => c.time);
            const pnls = day.intraday_curve.map(c => c.pnl);

            const trace = {
                x: times,
                y: pnls,
                type: 'scatter',
                mode: 'lines+markers',
                line: { color: isPos ? '#10b981' : '#f43f5e', width: 2.5 },
                marker: { size: 4 },
                name: 'Intraday P&L (₹)'
            };

            const shapeTarget = {
                type: 'line', x0: times[0], x1: times[times.length-1], y0: 15000, y1: 15000,
                line: { color: 'rgba(16, 185, 129, 0.6)', width: 1.5, dash: 'dash' }
            };

            const shapeStop = {
                type: 'line', x0: times[0], x1: times[times.length-1], y0: -15000, y1: -15000,
                line: { color: 'rgba(244, 63, 94, 0.6)', width: 1.5, dash: 'dash' }
            };

            const dayLayout = {
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent',
                margin: { l: 60, r: 20, t: 10, b: 35 },
                xaxis: { gridcolor: 'rgba(255, 255, 255, 0.05)', tickfont: { color: '#64748b', size: 10 } },
                yaxis: { title: 'P&L (₹)', titlefont: { color: '#94a3b8', size: 11 }, gridcolor: 'rgba(255, 255, 255, 0.05)', tickfont: { color: '#94a3b8', size: 10 } },
                shapes: [shapeTarget, shapeStop]
            };

            openDayModal();
            setTimeout(() => {
                Plotly.newPlot('plotly-day-chart', [trace], dayLayout, { responsive: true, displayModeBar: false });
            }, 60);
        } else {
            openDayModal();
        }
    } catch (err) {
        console.error('Inspect day error:', err);
    }
}

// Export CSV handler
function exportCSV() {
    window.location.href = '/api/export_csv';
}

// ---------- Strategy signal alerts (Telegram only, no orders) ----------

const SIGNAL_STATUS_TEXT = {
    WAITING: '⏳ Waiting', OPEN: '🔔 Signal open', REENTRY_WAIT: '🔁 Re-entry soon',
    CLOSED: '✅ Closed', SKIPPED: '— No trade', '—': '—'
};

function setSignalMsg(ok, text) {
    const msg = document.getElementById('sig-msg');
    if (!msg) return;
    msg.className = ok ? 'inline-msg ok' : 'inline-msg err';
    msg.textContent = text;
}

function renderSignalStatus(d) {
    const enabledBox = document.getElementById('sig-enabled');
    const label = document.getElementById('sig-enabled-label');
    if (enabledBox) enabledBox.checked = !!d.settings.enabled;
    if (label) label.textContent = d.settings.enabled ? 'On' : 'Off';
    const list = document.getElementById('sig-strategy-list');
    if (!list) return;
    const on = new Set(d.settings.strategies);
    const today = d.date === new Date().toISOString().slice(0, 10);
    list.innerHTML = d.strategies.map((s) => {
        const status = today ? (SIGNAL_STATUS_TEXT[s.status] || s.status) : '';
        const pnl = today && s.pnl ? `<span class="${s.pnl >= 0 ? 'text-success' : 'text-danger'}">${s.pnl >= 0 ? '+' : '−'}₹${Math.abs(s.pnl).toLocaleString('en-IN')}</span>` : '';
        const note = today && s.note ? `<small title="${s.note}">${s.note}</small>` : '';
        return `<label class="sig-row">
            <input type="checkbox" data-sig="${s.id}" ${on.has(s.id) ? 'checked' : ''} onchange="saveSignalSettings()">
            <span class="sig-name">${s.name}</span>
            <span class="sig-status">${status} ${pnl}</span>
            ${note}
        </label>`;
    }).join('');
}

async function loadSignalStatus() {
    try {
        const res = await fetch('/api/signals/status');
        const data = await res.json();
        if (data.success) renderSignalStatus(data.data);
    } catch (e) { /* optional feature */ }
}

async function saveSignalSettings() {
    const enabled = !!document.getElementById('sig-enabled')?.checked;
    const strategies = [...document.querySelectorAll('#sig-strategy-list input[data-sig]:checked')].map((el) => el.dataset.sig);
    try {
        const res = await fetch('/api/signals/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, strategies })
        });
        const data = await res.json();
        if (!data.success) return setSignalMsg(false, `❌ ${data.error}`);
        renderSignalStatus(data.data);
        setSignalMsg(true, enabled ? `Watching ${strategies.length} strateg${strategies.length === 1 ? 'y' : 'ies'}.` : 'Signal alerts off.');
    } catch (e) {
        setSignalMsg(false, 'Network error saving signal settings.');
    }
}

async function sendSampleSignal() {
    setSignalMsg(true, 'Sending...');
    try {
        const res = await fetch('/api/signals/test', { method: 'POST' });
        const data = await res.json();
        setSignalMsg(data.success, data.success ? '✅ Sample signal sent. Check Telegram.' : `❌ ${data.error}`);
    } catch (e) {
        setSignalMsg(false, 'Network error sending sample.');
    }
}
