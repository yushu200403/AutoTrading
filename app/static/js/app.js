/**
 * OpenNOF1 前端逻辑
 */

const API_BASE = '/api';
const REFRESH_INTERVAL = 5000;
const SYMBOLS = ['BTC', 'ETH', 'BNB', 'SOL', 'DOGE'];
const COLOR_MODE_KEY = 'opennof1_color_mode';

function getColorMode() {
    const value = localStorage.getItem(COLOR_MODE_KEY);
    if (value === 'green_up') return 'green_up';
    return 'red_up';
}

function applyColorModeToCSS(mode) {
    const root = document.documentElement;
    if (!root) return;
    if (mode === 'red_up') {
        root.style.setProperty('--up-color', '#ff1744');
        root.style.setProperty('--down-color', '#00c853');
    } else {
        root.style.setProperty('--up-color', '#00c853');
        root.style.setProperty('--down-color', '#ff1744');
    }
}

function setColorMode(mode) {
    const finalMode = mode === 'red_up' ? 'red_up' : 'green_up';
    localStorage.setItem(COLOR_MODE_KEY, finalMode);
    applyColorModeToCSS(finalMode);
    applyColorModeToCharts();
    updateTickers();
    updateEquityChart();
}

function applyColorModeToCharts() {
    const mode = getColorMode();
    const upColor = mode === 'red_up' ? '#ff1744' : '#00c853';
    const downColor = mode === 'red_up' ? '#00c853' : '#ff1744';
    const upBg = mode === 'red_up' ? 'rgba(255, 23, 68, 0.1)' : 'rgba(0, 200, 83, 0.1)';
    const downBg = mode === 'red_up' ? 'rgba(0, 200, 83, 0.1)' : 'rgba(255, 23, 68, 0.1)';
    
    if (equityChart && equityChart.data && equityChart.data.datasets && equityChart.data.datasets[0]) {
        const values = equityChart.data.datasets[0].data || [];
        const lastValue = values.length ? (values[values.length - 1] || 0) : 0;
        const isPositive = lastValue >= 0;
        equityChart.data.datasets[0].borderColor = isPositive ? upColor : downColor;
        equityChart.data.datasets[0].backgroundColor = isPositive ? upBg : downBg;
        equityChart.update('none');
    }
}

// 页面状态
let equityChart = null;
let miniCharts = {};

// 使用浏览器本地时区格式化时间显示
function formatTimeWithTZ(isoString, options = {}) {
    if (!isoString) return '';
    
    // 如果是不带时区的时间（无 Z 或 +8:00），补 Z 后按 UTC 解释
    if (!/([zZ]|[+-]\d{2}:\d{2})$/.test(isoString)) {
        isoString += 'Z';
    }

    const date = new Date(isoString);
    if (isNaN(date.getTime())) return '';
    
    const defaultOptions = {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false
    };
    // 过滤掉 undefined 的选项，防止覆盖默认值
    const finalOptions = { ...defaultOptions };
    for (const k in options) {
        if (options[k] !== undefined) {
            finalOptions[k] = options[k];
        } else {
            delete finalOptions[k];
        }
    }

    return date.toLocaleString('zh-CN', finalOptions);
}

// 获取本地时区的分组键 (分钟级别)
function getLocalGroupKey(isoString) {
    if (!isoString) return 'unknown';
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return 'unknown';
    // 使用本地时区的 YYYY-MM-DDTHH:MM 格式作为分组键
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    const hour = String(date.getHours()).padStart(2, '0');
    const minute = String(date.getMinutes()).padStart(2, '0');
    return `${year}-${month}-${day}T${hour}:${minute}`;
}

document.addEventListener('DOMContentLoaded', async () => {
    initTabs();

    applyColorModeToCSS(getColorMode());

    initEquityChart();
    initMiniCharts();

    setupEventListeners();

    await Promise.allSettled([
        updateStatus(),
        updateAccountSummary(),
        updateEquityChart(),
        updateTickers(),
        fetchDecisions(),
        fetchPositions(),
        fetchMemory(),
        fetchRecords(),
        fetchInstructions()
    ]);

    schedulePolling(() => Promise.allSettled([
        updateStatus(),
        updateAccountSummary(),
        updateTickers(),
        fetchPositions()
    ]), REFRESH_INTERVAL);

    schedulePolling(() => Promise.allSettled([
        fetchDecisions(),
        updateEquityChart(),
        fetchRecords()
    ]), 15000);
});

function schedulePolling(task, interval) {
    const run = async () => {
        try {
            await task();
        } finally {
            window.setTimeout(run, interval);
        }
    };
    window.setTimeout(run, interval);
}

// --- 标签页系统 ---

const CSRF_TOKEN_KEY = 'opennof1_csrf_token';

function isSettingsAuthenticated() {
    return Boolean(sessionStorage.getItem(CSRF_TOKEN_KEY));
}

// 只读接口默认也要求认证，未认证时给出明确提示而非空面板
function setAuthBannerVisible(visible) {
    const banner = document.getElementById('auth-banner');
    if (banner) banner.hidden = !visible;
}

// 更新设置标签的显示状态
function updateSettingsAuthState() {
    const authContainer = document.getElementById('settings-auth');
    const contentContainer = document.getElementById('settings-content');
    const isAuth = isSettingsAuthenticated();
    
    if (authContainer) authContainer.style.display = isAuth ? 'none' : 'flex';
    if (contentContainer) contentContainer.style.display = isAuth ? 'block' : 'none';
}

async function verifySettingsPassword() {
    const input = document.getElementById('settings-password');
    const error = document.getElementById('password-error');
    const password = input?.value || '';
    
    const result = await fetchAPI('/verify-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
    });
    
    if (result?.success) {
        sessionStorage.setItem(CSRF_TOKEN_KEY, result.csrf_token);
        updateSettingsAuthState();
        fetchInstructions();
        fetchMemory();
    } else {
        if (error) error.style.display = 'block';
        if (input) input.value = '';
    }
}

function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.dataset.tab;

            // 更新按钮
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // 更新内容
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            const target = document.getElementById(`tab-${tabId}`);
            if (target) target.classList.add('active');
            
            // 切换到设置标签时更新认证状态
            if (tabId === 'settings') {
                updateSettingsAuthState();
            }
            // 切换到记录标签时获取最新记录
            if (tabId === 'records') {
                fetchRecords();
            }
        });
    });
    
    // 密码验证按钮
    document.getElementById('btn-verify-password')?.addEventListener('click', verifySettingsPassword);
    
    // 回车键提交
    document.getElementById('settings-password')?.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') verifySettingsPassword();
    });
    
    // 初始化设置标签认证状态
    updateSettingsAuthState();
}

// --- 币种 24 小时迷你走势 ---

function initMiniCharts() {
    SYMBOLS.forEach(symbol => {
        const ctx = document.getElementById(`mini-${symbol}`);
        if (!ctx) return;
        
        miniCharts[symbol] = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    data: [],
                    borderColor: '#666',
                    borderWidth: 2.5,
                    fill: false,
                    tension: 0.3,
                    pointRadius: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { enabled: false }
                },
                scales: {
                    x: { display: false },
                    y: { display: false }
                }
            }
        });
    });
}

async function updateTickers() {
    const data = await fetchAPI('/tickers');
    if (!data) return;
    
    SYMBOLS.forEach(symbol => {
        const ticker = data.find(t => t.symbol && t.symbol.startsWith(symbol));
        if (!ticker) return;
        
        const item = document.querySelector(`#ticker-${symbol}`);
        if (!item) return;
        
        const priceEl = item.querySelector('.ticker-price');
        const changeEl = item.querySelector('.ticker-change');
        
        if (priceEl) {
            const price = parseFloat(ticker.price);
            priceEl.textContent = price >= 1000 ? `$${price.toFixed(0)}` : 
                                  price >= 1 ? `$${price.toFixed(2)}` : 
                                  `$${price.toFixed(4)}`;
        }
        
        if (changeEl) {
            const change = parseFloat(ticker.change_24h) || 0;
            changeEl.textContent = `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
            changeEl.className = `ticker-change ${change >= 0 ? 'positive' : 'negative'}`;
        }
        
        // 根据 24 小时涨跌更新迷你图颜色
        const chart = miniCharts[symbol];
        if (chart && ticker.change_24h !== undefined) {
            const changeValue = parseFloat(ticker.change_24h) || 0;
            const mode = getColorMode();
            const upColor = mode === 'red_up' ? '#ff1744' : '#00c853';
            const downColor = mode === 'red_up' ? '#00c853' : '#ff1744';
            const color = changeValue >= 0 ? upColor : downColor;
            chart.data.datasets[0].borderColor = color;
            chart.update('none');
        }
        
        // 使用接口返回的迷你走势更新图表数据
        if (ticker.sparkline && chart) {
            chart.data.labels = ticker.sparkline.map((_, i) => i);
            chart.data.datasets[0].data = ticker.sparkline;
            chart.update('none');
        }
    });
}

// --- 净值图表 ---

function initEquityChart() {
    const ctx = document.getElementById('equityChart');
    if (!ctx) return;

    const mode = getColorMode();
    const upColor = mode === 'red_up' ? '#ff1744' : '#00c853';
    const upBg = mode === 'red_up' ? 'rgba(255, 23, 68, 0.1)' : 'rgba(0, 200, 83, 0.1)';

    equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: '收益率 %',
                data: [],
                borderColor: upColor,
                backgroundColor: upBg,
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                pointHoverRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: (context) => `${context.parsed.y.toFixed(2)}%`
                    }
                }
            },
            scales: {
                x: {
                    display: true,
                    grid: {
                        color: 'rgba(0,0,0,0.05)',
                        drawBorder: false
                    },
                    ticks: {
                        maxTicksLimit: 6,
                        font: { size: 10 }
                    }
                },
                y: {
                    display: true,
                    // 初始时 0 轴在中心
                    suggestedMin: -5,
                    suggestedMax: 5,
                    grid: {
                        color: (context) => context.tick.value === 0 ? 'rgba(0,0,0,0.3)' : 'rgba(0,0,0,0.05)',
                        drawBorder: false
                    },
                    ticks: {
                        callback: (v) => `${v.toFixed(1)}%`,
                        font: { size: 10 }
                    }
                }
            },
            interaction: {
                mode: 'nearest',
                axis: 'x',
                intersect: false
            }
        }
    });
}

async function updateEquityChart() {
    const data = await fetchAPI('/equity-history?limit=1000');
    if (!data || !data.data || !equityChart) return;

    const labels = data.data.map(d => {
        // 使用 formatTimeWithTZ 确保时区与对话/记录面板一致
        // 显示 MM/DD HH:mm
        return formatTimeWithTZ(d.timestamp, { 
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit', 
            minute: '2-digit',
            year: undefined
        });
    });

    const values = data.data.map(d => d.profit_pct);

    const lastValue = values[values.length - 1] || 0;
    const mode = getColorMode();
    const upColor = mode === 'red_up' ? '#ff1744' : '#00c853';
    const downColor = mode === 'red_up' ? '#00c853' : '#ff1744';
    const upBg = mode === 'red_up' ? 'rgba(255, 23, 68, 0.1)' : 'rgba(0, 200, 83, 0.1)';
    const downBg = mode === 'red_up' ? 'rgba(0, 200, 83, 0.1)' : 'rgba(255, 23, 68, 0.1)';
    const isPositive = lastValue >= 0;
    equityChart.data.datasets[0].borderColor = isPositive ? upColor : downColor;
    equityChart.data.datasets[0].backgroundColor = isPositive ? upBg : downBg;

    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = values;
    
    // 先调用 resize 确保图表尺寸正确计算，再更新数据
    equityChart.resize();
    equityChart.update('none');
}

// --- 账户总览 ---

async function updateAccountSummary() {
    const data = await fetchAPI('/account-summary');
    if (!data) return;

    // 总净值显示
    const totalEl = document.getElementById('total-value');
    const changeEl = document.getElementById('total-change');
    if (totalEl) totalEl.textContent = `$${formatNumber(data.total_equity)}`;
    if (changeEl) {
        const pct = data.total_profit_pct || 0;
        changeEl.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
        changeEl.className = `change ${pct >= 0 ? 'positive' : 'negative'}`;
    }

    // 统计指标
    updateStat('stat-total', data.total_equity);
    // 活动资产 = 已占用保证金 (total - free)
    const usedMargin = data.total_equity - data.free_balance;
    updateStat('stat-active', usedMargin, false);
    document.getElementById('stat-positions').textContent = `${data.position_count} 持仓`;

    updateStat('stat-profit', data.total_profit, true);
    const profitPctEl = document.getElementById('stat-profit-pct');
    if (profitPctEl) {
        const pct = data.total_profit_pct || 0;
        profitPctEl.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
        profitPctEl.className = `sub ${pct >= 0 ? 'positive' : 'negative'}`;
    }

    updateStat('stat-profit-24h', data.profit_24h, true);
    const profit24hPctEl = document.getElementById('stat-profit-24h-pct');
    if (profit24hPctEl) {
        const pct = data.profit_24h_pct || 0;
        profit24hPctEl.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
        profit24hPctEl.className = `sub ${pct >= 0 ? 'positive' : 'negative'}`;
    }
}

function updateStat(id, value, showSign = false) {
    const el = document.getElementById(id);
    if (!el) return;
    const formatted = formatNumber(value);
    el.textContent = showSign && value > 0 ? `+$${formatted}` : `$${formatted}`;
    if (showSign) {
        el.className = `value ${value >= 0 ? 'positive' : 'negative'}`;
    }
}

function formatNumber(num) {
    return toFiniteNumber(num).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function toFiniteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}

// --- 接口请求 ---

async function fetchAPI(endpoint, options = {}) {
    try {
        const requestOptions = { ...options, credentials: 'same-origin' };
        const method = (requestOptions.method || 'GET').toUpperCase();
        const headers = new Headers(requestOptions.headers || {});
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const csrfToken = sessionStorage.getItem(CSRF_TOKEN_KEY);
            if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
        }
        requestOptions.headers = headers;
        const response = await fetch(`${API_BASE}${endpoint}`, requestOptions);
        const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
        if (!response.ok) {
            if (response.status === 401 || response.status === 403) {
                sessionStorage.removeItem(CSRF_TOKEN_KEY);
                updateSettingsAuthState();
                setAuthBannerVisible(true);
            }
            return { ...data, http_status: response.status };
        }
        setAuthBannerVisible(false);
        return data;
    } catch (error) {
        console.error(`接口请求失败 (${endpoint}):`, error);
        return null;
    }
}

// --- 服务状态 ---

async function updateStatus() {
    const data = await fetchAPI('/status');

    const dotEl = document.getElementById('status-dot');
    const textEl = document.getElementById('status-text');
    const startBtn = document.getElementById('btn-start');
    const stopBtn = document.getElementById('btn-stop');

    if (!data) {
        if (dotEl) dotEl.className = 'status-dot disconnected';
        if (textEl) textEl.textContent = '已断开';
        if (startBtn) startBtn.disabled = true;
        if (stopBtn) stopBtn.disabled = true;
        return;
    }

    if (data.running) {
        if (dotEl) dotEl.className = 'status-dot running';
        if (textEl) textEl.textContent = '运行中';
        if (startBtn) startBtn.disabled = true;
        if (stopBtn) stopBtn.disabled = false;
    } else {
        if (dotEl) dotEl.className = 'status-dot stopped';
        if (textEl) textEl.textContent = '已停止';
        if (startBtn) startBtn.disabled = false;
        if (stopBtn) stopBtn.disabled = true;
    }

    const liveToggle = document.getElementById('live-trading');
    if (liveToggle) liveToggle.checked = data.live_trading || false;
}

// --- 模型决策 ---
// 已显示的决策组键集合 (用于增量更新)
let displayedGroupKeys = new Set();

async function fetchDecisions() {
    const data = await fetchAPI('/decisions');
    const container = document.getElementById('decisions-list');
    if (!container) return;

    // 验证返回的是数组而非错误对象
    if (!data || !Array.isArray(data)) {
        if (displayedGroupKeys.size === 0) {
            container.innerHTML = '<div class="text-muted text-center">暂无模型输出</div>';
        }
        return;
    }

    if (data.length === 0) {
        if (displayedGroupKeys.size === 0) {
            container.innerHTML = '<div class="text-muted text-center">暂无模型输出</div>';
        }
        return;
    }

    // 按时间戳分组 (同一分钟内的决策视为同一轮)
    const groups = {};
    data.forEach(d => {
        // 使用浏览器本地时区的分钟级别时间戳作为分组键
        const ts = getLocalGroupKey(d.timestamp);
        if (!groups[ts]) {
            groups[ts] = {
                key: ts,
                timestamp: d.timestamp,
                reasoning: d.reasoning || '',
                tools: []
            };
        }
        // 如果当前决策有更长的分析文本，使用它
        if (d.reasoning && d.reasoning.length > groups[ts].reasoning.length) {
            groups[ts].reasoning = d.reasoning;
        }
        groups[ts].tools.push(d);
    });

    // 按时间倒序排列分组
    const sortedKeys = Object.keys(groups).sort().reverse().slice(0, 10);

    // 找出新的分组 (尚未显示的)
    const newKeys = sortedKeys.filter(key => !displayedGroupKeys.has(key));

    // 如果是首次加载或需要完全刷新
    if (displayedGroupKeys.size === 0 || container.querySelector('.text-muted')) {
        container.innerHTML = '';
        displayedGroupKeys.clear();
        
        // 渲染所有分组
        sortedKeys.forEach(key => {
            const groupHtml = renderDecisionGroup(groups[key]);
            container.insertAdjacentHTML('beforeend', groupHtml);
            displayedGroupKeys.add(key);
        });
    } else if (newKeys.length > 0) {
        // 增量更新：只在顶部添加新分组
        newKeys.reverse().forEach(key => {
            const groupHtml = renderDecisionGroup(groups[key]);
            container.insertAdjacentHTML('afterbegin', groupHtml);
            displayedGroupKeys.add(key);
        });
        
        // 移除超出限制的旧分组
        const allGroups = container.querySelectorAll('.decision-group');
        if (allGroups.length > 10) {
            for (let i = 10; i < allGroups.length; i++) {
                const oldKey = allGroups[i].dataset.groupKey;
                if (oldKey) displayedGroupKeys.delete(oldKey);
                allGroups[i].remove();
            }
        }
    }
}

// 渲染单个决策分组
function renderDecisionGroup(group) {
    // 使用配置的时区格式化时间
    const time = formatTimeWithTZ(group.timestamp);

    // AI 分析文本 (如果有)
    let reasoningHtml = '';
    if (group.reasoning && group.reasoning.trim()) {
        const lines = group.reasoning.trim().split('\n').map(line => 
            `<p style="margin: 0.25rem 0;">${escapeHtml(line)}</p>`
        ).join('');
        reasoningHtml = `<div class="ai-reasoning">${lines}</div>`;
    }

    // 工具名称汉化映射
    const TOOL_NAMES = {
        'trade_in': '开仓',
        'close_position': '平仓',
        'set_leverage': '设置杠杆',
        'set_margin_mode': '保证金模式',
        'modify_position': '修改止盈止损',
        'cancel_orders': '取消挂单',
        'cancel_order': '取消单个订单',
        'update_memory': '更新记忆'
    };

    // 工具调用卡片
    let toolsHtml = '';
    group.tools.forEach((d, idx) => {
        const actionClass = (d.action === 'BUY' || d.action === 'LONG') ? 'buy' : 
                           (d.action === 'SELL' || d.action === 'SHORT' || d.action === 'CLOSE') ? 'sell' : '';
        const toolName = d.tool_name || 'unknown';
        const displayName = TOOL_NAMES[toolName] || toolName.toUpperCase();
        const uniqueId = `${group.key}-${idx}`.replace(/[^a-zA-Z0-9-]/g, '-');
        
        // 格式化工具参数
        let argsHtml = '';
        if (d.args && Object.keys(d.args).length > 0) {
            const argsLines = Object.entries(d.args).map(([k, v]) => 
                `<div class="args-line"><span class="args-key">${escapeHtml(k)}:</span> <span class="args-value">${escapeHtml(String(v))}</span></div>`
            ).join('');
            argsHtml = `<div class="tool-args" id="args-${uniqueId}" style="display: none;">${argsLines}</div>`;
        }
        
        // 状态图标
        let statusHtml = '';
        if (d.status) {
            const statusClass = d.status === 'SUCCESS' ? 'positive' : 'negative';
            const statusIcon = d.status === 'SUCCESS' ? '✓' : '✗';
            statusHtml = `<span class="tool-status ${statusClass}">${statusIcon}</span>`;
        }
        
        toolsHtml += `
            <div class="tool-card ${actionClass}">
                <div class="tool-card-header" data-tool-toggle="${escapeHtml(uniqueId)}">
                    <span class="tool-badge">${escapeHtml(displayName)}</span>
                    <span class="tool-info">${escapeHtml(d.info || '无描述')}</span>
                    ${statusHtml}
                    <span class="tool-toggle" id="toggle-${uniqueId}">▼</span>
                </div>
                ${argsHtml}
            </div>
        `;
    });

    return `
        <div class="decision-group" data-group-key="${escapeHtml(group.key)}">
            <div class="decision-header">
                <span class="decision-time">${escapeHtml(time)}</span>
            </div>
            ${reasoningHtml}
            <div class="tool-cards">
                ${toolsHtml}
            </div>
        </div>
    `;
}

// 用事件委托替代内联 onclick，以便启用不含 unsafe-inline 的 CSP
function initToolArgsToggle() {
    document.addEventListener('click', (event) => {
        const header = event.target.closest('[data-tool-toggle]');
        if (header) toggleToolArgs(header.dataset.toolToggle);
    });
}

// 展开/折叠工具参数
function toggleToolArgs(uniqueId) {
    const args = document.getElementById(`args-${uniqueId}`);
    const toggle = document.getElementById(`toggle-${uniqueId}`);
    if (!args) return;
    
    if (args.style.display === 'none') {
        args.style.display = 'block';
        if (toggle) toggle.textContent = '▲';
    } else {
        args.style.display = 'none';
        if (toggle) toggle.textContent = '▼';
    }
}

// HTML 转义函数
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function toggleMcpDetails(idx) {
    const details = document.getElementById(`details-${idx}`);
    const toggle = document.getElementById(`toggle-${idx}`);
    if (details.style.display === 'none') {
        details.style.display = 'block';
        toggle.textContent = '▲';
    } else {
        details.style.display = 'none';
        toggle.textContent = '▼';
    }
}

// --- 当前持仓 ---

async function fetchPositions() {
    const data = await fetchAPI('/positions');
    const tbody = document.getElementById('positions-table-body');
    if (!tbody) return;

    if (!Array.isArray(data) || data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">当前无持仓</td></tr>';
        return;
    }

    let html = '';
    data.forEach(pos => {
        const pnl = toFiniteNumber(pos.unrealized_pnl);
        const percentage = toFiniteNumber(pos.percentage);
        const contracts = toFiniteNumber(pos.contracts);
        const leverage = toFiniteNumber(pos.leverage, 1);
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';
        const sideLabel = pos.side === 'LONG' ? '做多' : '做空';
        html += `
            <tr>
                <td>${escapeHtml(String(pos.symbol || '').replace('/USDT', ''))}</td>
                <td><span class="badge ${pos.side === 'LONG' ? 'badge-success' : 'badge-danger'}">${sideLabel}</span></td>
                <td>${leverage}x</td>
                <td>${contracts.toFixed(4)}</td>
                <td class="${pnlClass}">$${pnl.toFixed(2)}<br><small>${percentage.toFixed(2)}%</small></td>
            </tr>
        `;
    });

    tbody.innerHTML = html;
}

// --- 记忆白板 ---

async function fetchMemory() {
    const data = await fetchAPI('/memory');
    const el = document.getElementById('memory-content');
    if (!el) return;

    if (data && data.content) {
        el.innerHTML = `<pre style="white-space: pre-wrap; margin: 0;">${escapeHtml(data.content)}</pre>`;
    } else {
        el.innerHTML = '<span class="text-muted">暂无记忆内容</span>';
    }
}

// --- 事件监听 ---

function setupEventListeners() {
    initToolArgsToggle();
    document.getElementById('modal-close-btn')?.addEventListener('click', closeModal);

    // 启动按钮
    document.getElementById('btn-start')?.addEventListener('click', async () => {
        const result = await fetchAPI('/start', { method: 'POST' });
        if (result?.success) {
            updateStatus();
        } else {
            showModal('启动失败', result?.error || '未知错误');
        }
    });

    // 停止按钮
    document.getElementById('btn-stop')?.addEventListener('click', async () => {
        const result = await fetchAPI('/stop', { method: 'POST' });
        if (result?.success) {
            updateStatus();
        } else {
            showModal('停止失败', result?.error || '未知错误');
        }
    });

    // 单次运行按钮
    document.getElementById('btn-run-once')?.addEventListener('click', async (e) => {
        const btn = e.target;
        const originalText = btn.textContent;
        btn.textContent = '运行中...';
        btn.disabled = true;
        
        try {
            const result = await fetchAPI('/run-once', { method: 'POST' });
            if (result?.success) {
                showModal('提示', '单次循环执行完成');
                fetchDecisions();
                updateAccountSummary();
            } else {
                showModal('执行失败', result?.error || '未知错误');
            }
        } finally {
            btn.textContent = originalText;
            btn.disabled = false;
        }
    });

    // 一键全平按钮采用两阶段确认
    let closeAllStage = 0; // 0: 初始, 1: 等待确认
    let closeAllTimer = null;
    
    document.getElementById('btn-close-all')?.addEventListener('click', async (e) => {
        const btn = e.target;
        
        if (closeAllStage === 0) {
            // 第一阶段：变黄色，开始倒计时
            closeAllStage = 1;
            btn.textContent = '确认全平 (2s)';
            btn.style.backgroundColor = '#f59e0b';
            btn.style.borderColor = '#f59e0b';
            btn.disabled = true;
            
            // 2秒后启用按钮
            let countdown = 2;
            closeAllTimer = setInterval(() => {
                countdown--;
                if (countdown > 0) {
                    btn.textContent = `确认全平 (${countdown}s)`;
                } else {
                    clearInterval(closeAllTimer);
                    btn.textContent = '⚠️ 点击确认';
                    btn.disabled = false;
                }
            }, 1000);
            
            // 5秒后自动重置（如果未点击）
            setTimeout(() => {
                if (closeAllStage === 1) {
                    resetCloseAllBtn(btn);
                }
            }, 5000);
            
        } else if (closeAllStage === 1) {
            // 第二阶段：执行全平
            clearInterval(closeAllTimer);
            btn.textContent = '正在平仓...';
            btn.style.backgroundColor = '#ef4444';
            btn.style.borderColor = '#ef4444';
            btn.disabled = true;
            
            try {
                const result = await fetchAPI('/close-all', { method: 'POST' });
                
                if (result?.success) {
                    const closedCount = Array.isArray(result.results?.closed) ? result.results.closed.length : 0;
                    const cancelledCount = Array.isArray(result.results?.cancelled) ? result.results.cancelled.length : 0;
                    const msg = `${result.message || '操作已完成'}\n\n已平仓: ${closedCount} 个\n已撤单: ${cancelledCount} 个`;
                    showModal('操作完成', msg);
                } else {
                    let msg = `平仓完成，但有错误：\n${result?.message || '未知错误'}`;
                    if (result?.results?.errors?.length > 0) {
                        msg += '\n\n错误详情：\n' + result.results.errors
                            .map(error => typeof error === 'string' ? error : JSON.stringify(error))
                            .join('\n');
                    }
                    showModal('操作结果', msg);
                }
                
                fetchPositions();
                updateAccountSummary();
            } catch (err) {
                showModal('错误', '一键全平失败: ' + err.message);
            } finally {
                resetCloseAllBtn(btn);
            }
        }
    });
    
    function resetCloseAllBtn(btn) {
        closeAllStage = 0;
        if (closeAllTimer) clearInterval(closeAllTimer);
        btn.textContent = '一键全平';
        btn.style.backgroundColor = '';
        btn.style.borderColor = '';
        btn.disabled = false;
    }

    // 实盘交易开关
    document.getElementById('live-trading')?.addEventListener('change', async (e) => {
        const enable = e.target.checked;
        
        if (enable) {
            // 先恢复关闭状态，等待用户确认
            e.target.checked = false;
            
            showModal('风险警告', '⚠️ 确定启用实盘交易？\n系统将使用您的账户资金执行真实订单！', {
                type: 'confirm',
                onConfirm: async () => {
                    // 手动更新界面状态
                    e.target.checked = true;
                    closeModal();
                    
                    // 调用控制接口
                    const result = await fetchAPI('/live', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enable: true })
                    });
                    if (!result?.success) {
                        e.target.checked = false;
                        showModal('切换失败', result?.error || '无法启用实盘交易');
                    }
                }
            });
        } else {
            // 关闭实盘无需二次确认，但失败时恢复开关状态
            const result = await fetchAPI('/live', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enable: false })
            });
            if (!result?.success) {
                e.target.checked = true;
                showModal('切换失败', result?.error || '无法关闭实盘交易');
            }
        }
    });

    const colorModeCheckbox = document.getElementById('color-mode-red-up');
    if (colorModeCheckbox) {
        colorModeCheckbox.checked = getColorMode() === 'red_up';
        colorModeCheckbox.addEventListener('change', () => {
            const mode = colorModeCheckbox.checked ? 'red_up' : 'green_up';
            setColorMode(mode);
        });
    }

    // 保存自定义指令
    document.getElementById('btn-save-instructions')?.addEventListener('click', async () => {
        const instructions = document.getElementById('custom-instructions')?.value || '';
        const result = await fetchAPI('/instructions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instructions })
        });
        if (result?.success) {
            showModal('提示', '指令已保存');
        } else {
            showModal('保存失败', result?.error || '无法保存指令');
        }
    });
}

// --- 模态框系统 ---

function showModal(title, message, options = {}) {
    const modal = document.getElementById('custom-modal');
    const titleEl = document.getElementById('modal-title');
    const msgEl = document.getElementById('modal-message');
    const confirmBtn = document.getElementById('modal-confirm');
    const cancelBtn = document.getElementById('modal-cancel');

    if (!modal || !titleEl || !msgEl || !confirmBtn || !cancelBtn) return;

    titleEl.textContent = title || '提示';
    msgEl.textContent = message || '';
    msgEl.style.whiteSpace = 'pre-line';

    // 配置按钮类型
    const type = options.type || 'alert';
    
    // 重置按钮
    cancelBtn.style.display = type === 'confirm' ? 'inline-block' : 'none';
    cancelBtn.onclick = () => closeModal();

    // 确认按钮逻辑
    confirmBtn.onclick = () => {
        if (options.onConfirm) {
            options.onConfirm();
        } else {
            closeModal();
        }
    };

    // 显示模态框
    modal.style.display = 'flex';
}

function closeModal() {
    const modal = document.getElementById('custom-modal');
    if (modal) modal.style.display = 'none';
}

// --- 交易记录时间轴 ---

// 工具名称汉化映射
const TOOL_NAMES_TIMELINE = {
    'trade_in': '开仓',
    'close_position': '平仓',
    'set_leverage': '设置杠杆',
    'set_margin_mode': '保证金模式',
    'modify_position': '修改止盈止损',
    'cancel_orders': '取消挂单',
    'cancel_order': '取消单个订单',
    'update_memory': '更新记忆'
};

async function fetchRecords() {
    const container = document.getElementById('records-timeline');
    if (!container) return;
    
    const data = await fetchAPI('/records?limit=200');
    
    if (!data || !Array.isArray(data) || data.length === 0) {
        container.innerHTML = '<div class="timeline-empty">暂无交易记录</div>';
        return;
    }
    
    // 按时间倒序排列（最新的在上面），并过滤掉记忆更新
    const sorted = [...data]
        .filter(record => record.tool_name !== 'update_memory')
        .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    
    if (sorted.length === 0) {
        container.innerHTML = '<div class="timeline-empty">暂无交易记录</div>';
        return;
    }
    
    // 渲染时间轴
    let html = '<div class="timeline">';
    
    sorted.forEach(record => {
        const time = formatTimeWithTZ(record.timestamp);
        const toolName = TOOL_NAMES_TIMELINE[record.tool_name] || String(record.tool_name || '未知').toUpperCase();
        const info = record.info || '无描述';
        const status = record.status === 'SUCCESS' ? 'success' : 
                      record.status === 'FAILED' ? 'failed' : '';
        const statusText = record.status === 'SUCCESS' ? '✓' : 
                          record.status === 'FAILED' ? '✗' : '';
        
        // 根据操作类型确定节点颜色
        const actionClass = (record.action === 'BUY' || record.action === 'LONG') ? 'buy' : 
                          (record.action === 'SELL' || record.action === 'SHORT' || record.action === 'CLOSE') ? 'sell' : 
                          status;
        
        html += `
            <div class="timeline-item ${actionClass}">
                <div class="timeline-content">
                    <div class="timeline-time">${escapeHtml(time)}</div>
                    <div>
                        <span class="timeline-tool">${escapeHtml(toolName)}</span>
                        ${statusText ? `<span class="timeline-status ${status}">${statusText}</span>` : ''}
                    </div>
                    <div class="timeline-info">${escapeHtml(info)}</div>
                </div>
            </div>
        `;
    });
    
    html += '</div>';
    container.innerHTML = html;
}

// --- 自定义指令 ---

async function fetchInstructions() {
    const textarea = document.getElementById('custom-instructions');
    if (!textarea) return;
    
    const data = await fetchAPI('/instructions');
    
    if (data && data.instructions !== undefined) {
        // 只在输入框为空时填充，避免覆盖用户正在编辑的内容
        if (!textarea.value || textarea.value === textarea.defaultValue) {
            textarea.value = data.instructions;
        }
    }
}
