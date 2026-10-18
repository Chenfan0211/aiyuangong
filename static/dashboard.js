const StoreSession = window.StoreSession;
const client = StoreSession.createClient();
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const num = value => value === null || value === undefined ? '缺数' : Number(value).toLocaleString('zh-CN');
const pct = value => value === null || value === undefined ? '缺数' : value + '%';

function notice(text, isError) {
  const el = $('notice');
  if (!text) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.textContent = text;
  el.style.background = isError ? '#fee4e2' : '#fff1cc';
  el.style.color = isError ? '#b42318' : '#93370d';
}

async function api(path) {
  return client.getJson(path);
}

const cls = value => value === null ? 'flat' : (value > 0 ? 'up' : (value < 0 ? 'down' : 'flat'));
const arrow = value => value === null ? '缺对比' : (value > 0 ? '▲ ' : '▼ ') + value + '%';
const tag = value => `<span class="tag ${esc(value)}">${esc(value)}</span>`;

function renderKpis(data) {
  const k = data.kpis;
  const change = data.change_percent;
  const cards = [
    ['曝光', num(k.impressions), change.impressions],
    ['详情访问', num(k.detail_views), change.detail_views],
    ['支付订单', num(k.paid_orders), change.paid_orders],
    ['核销订单', num(k.verified_orders), change.verified_orders],
    ['访问率', pct(k.view_rate), null],
    ['支付转化率', pct(k.pay_rate), null],
    ['核销率', pct(k.verify_rate), null]
  ];
  if (data.financial_included) {
    cards.splice(4, 0, ['GMV', '¥' + num(k.gmv), change.gmv]);
    cards.push(['退款率', pct(k.refund_rate), null]);
  }
  $('kpis').innerHTML = cards.map(([label, value, delta]) => {
    const changeHtml = delta === null || delta === undefined ? '' : `<div class="${cls(delta)}">${arrow(delta)}</div>`;
    return `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div>${changeHtml}</div>`;
  }).join('');
}

function renderTrend(data) {
  const rows = data.trend || [];
  if (!rows.length) {
    $('trend').innerHTML = '<div class="empty">该区间没有数据。</div>';
    return;
  }
  const max = Math.max(...rows.map(row => row.impressions || 0)) || 1;
  $('trend').innerHTML = rows.map(row => `<div class="bar" title="${esc(row.date)}｜曝光 ${num(row.impressions)}｜支付 ${num(row.paid_orders)}"><i style="height:${Math.max(2, Math.round((row.impressions || 0) / max * 100))}%"></i><b>${esc(row.date).slice(5)}</b></div>`).join('');
}

function renderFunnel(data) {
  const k = data.kpis;
  const steps = [
    ['曝光', k.impressions, '—'],
    ['详情访问', k.detail_views, pct(k.view_rate)],
    ['支付订单', k.paid_orders, pct(k.pay_rate)],
    ['核销订单', k.verified_orders, pct(k.verify_rate)]
  ];
  $('funnel').innerHTML = steps.map(([label, value, rate]) => `<tr><th style="width:130px">${label}</th><td>${num(value)}</td><td class="mono">${rate}</td></tr>`).join('');
}

function renderPackages(data) {
  const rows = data.packages || [];
  if (!rows.length) {
    $('packages').innerHTML = '<tr><td colspan="6" class="empty">该区间没有套餐明细（先导入套餐报表）。</td></tr>';
    return;
  }
  $('packages').innerHTML = rows.map(item => `<tr><td>${esc(item.name)}</td><td>${esc(item.platform)}</td><td>${num(item.detail_views)}</td><td>${num(item.paid_orders)}</td><td>${pct(item.conversion_rate)}</td><td>${item.gross_margin_rate === null ? '缺成本' : (item.gross_margin_rate * 100).toFixed(1) + '%'}</td></tr>`).join('');
}

function renderSide(data) {
  const quality = data.data_quality;
  $('quality').innerHTML = `<tr><th>窗口</th><td>${esc(data.window.start)} ~ ${esc(data.window.end)}</td></tr><tr><th>新鲜度</th><td>${Object.entries(quality.freshness).map(([key, value]) => esc(key) + ' ' + tag(value)).join(' ')}</td></tr><tr><th>缺日</th><td>${quality.missing_day_count} 天</td></tr><tr><th>跳变</th><td>${quality.anomaly_count} 处</td></tr>`;
  const tasks = data.tasks;
  const approvals = data.approvals || {};
  $('tasks').innerHTML = `<tr><th>任务</th><td>${tasks.total} 项（未关闭 ${tasks.open_count}／逾期 ${tasks.overdue_count}）</td></tr><tr><th>完成率</th><td>${pct(tasks.completion_rate)}</td></tr><tr><th>待审批</th><td>${approvals.pending || 0} 张</td></tr>`;
  $('issues').innerHTML = (data.issues || []).length
    ? data.issues.map(item => `<li>${esc(item)}</li>`).join('')
    : '<li class="empty">未发现异常</li>';
}

async function load() {
  try {
    const data = await api('/api/dashboard?days=' + encodeURIComponent($('days').value));
    renderKpis(data);
    renderTrend(data);
    renderFunnel(data);
    renderPackages(data);
    renderSide(data);
    notice(data.has_data ? '' : '该区间没有已校验的平台数据：请先导出并导入官方报表。', !data.has_data);
  } catch (error) {
    if (error.status !== 401) notice('看板加载失败：' + error.message, true);
    $('kpis').innerHTML = '<div class="empty">加载失败</div>';
  }
}

async function report(kind) {
  try {
    const data = await api('/api/reports/' + kind);
    $('report').textContent = data.report;
    notice('已生成' + data.title, false);
  } catch (error) {
    if (error.status !== 401) notice('生成失败：' + error.message, true);
  }
}

$('days').onchange = load;
$('weekly').onclick = () => report('weekly');
$('monthly').onclick = () => report('monthly');
load();
