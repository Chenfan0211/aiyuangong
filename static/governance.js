const StoreSession = window.StoreSession;
const client = StoreSession.createClient();
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

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

async function api(path, options) {
  return client.json(path, options);
}

async function post(path, body) {
  return client.postJson(path, body);
}

const fmtTime = value => esc(value).slice(0, 19).replace('T', ' ');
const tagOf = status => `<span class="tag ${esc(status)}">${esc(status)}</span>`;

async function loadQueue() {
  try {
    const data = await api('/api/approvals?status=' + encodeURIComponent($('statusFilter').value));
    $('who').textContent = (data.store_id || '') + ' · ' + data.approvals.length + ' 条';
    $('counts').textContent = Object.entries(data.count_by_status || {}).map(([key, value]) => key + ' ' + value).join(' / ') || '无审批单';
    const box = $('queue');
    box.innerHTML = '';
    if (!(data.approvals || []).length) {
      box.innerHTML = '<div class="empty">该状态下没有审批单。</div>';
      return;
    }
    data.approvals.forEach(approval => {
      const card = document.createElement('div');
      card.className = 'card';
      const payload = JSON.stringify(approval.payload_snapshot || {});
      card.innerHTML = `<div class="t">${esc((approval.payload_snapshot || {}).title || approval.action_type)}</div><div>${tagOf(approval.status)}<span class="tag">${esc(approval.action_type)}</span></div><div class="s">申请人 ${esc(approval.requested_by)} · ${fmtTime(approval.requested_at)}${approval.task_id ? ' · 任务 ' + esc(approval.task_id) : ''}</div><div class="s mono">${esc(payload).slice(0, 160)}</div>`;
      if (approval.status === 'pending') {
        const acts = document.createElement('div');
        acts.className = 'acts';
        const ok = document.createElement('button');
        ok.className = 'ok';
        ok.textContent = '通过';
        ok.onclick = () => decide(approval, 'approved');
        const no = document.createElement('button');
        no.className = 'no';
        no.textContent = '驳回';
        no.onclick = () => decide(approval, 'rejected');
        acts.append(ok, no);
        card.appendChild(acts);
      }
      box.appendChild(card);
    });
  } catch (error) {
    if (error.status !== 401) notice('审批队列读取失败：' + error.message, true);
    $('queue').innerHTML = '<div class="empty">读取失败</div>';
  }
}

async function decide(approval, decision) {
  let reason = '';
  if (decision === 'rejected') {
    reason = window.prompt('驳回原因（必填）：') || '';
    if (!reason) {
      notice('驳回必须填写原因', true);
      return;
    }
  } else {
    reason = window.prompt('通过备注（可留空）：') || '';
  }
  try {
    await post('/api/approvals/' + encodeURIComponent(approval.approval_id) + '/decide', { decision, reason });
    notice('已' + (decision === 'approved' ? '通过' : '驳回') + '：' + ((approval.payload_snapshot && approval.payload_snapshot.title) || approval.action_type), false);
    loadQueue();
    loadAudit();
  } catch (error) {
    if (error.status !== 401) notice('审批失败：' + error.message, true);
  }
}

async function loadAudit() {
  try {
    const params = new URLSearchParams({ limit: '50' });
    if ($('eventType').value) params.set('event_type', $('eventType').value);
    if ($('actorFilter').value.trim()) params.set('actor_id', $('actorFilter').value.trim());
    const data = await api('/api/audit?' + params.toString());
    const summary = data.summary || {};
    $('auditSummary').textContent = `近 ${summary.days} 天 ${summary.total} 条 · 越权 ${summary.permission_denied} · 登录失败 ${summary.login_failed}`;
    const select = $('eventType');
    if (select.options.length <= 1) {
      (data.event_types || []).forEach(type => {
        const option = document.createElement('option');
        option.value = type;
        option.textContent = type;
        select.appendChild(option);
      });
    }
    const body = $('auditBody');
    body.innerHTML = '';
    if (!(data.events || []).length) {
      body.innerHTML = '<tr><td colspan="5" class="empty">没有匹配的审计事件。</td></tr>';
      return;
    }
    data.events.forEach(event => {
      const row = document.createElement('tr');
      row.innerHTML = `<td class="mono">${fmtTime(event.created_at)}</td><td>${tagOf(event.event_type)}</td><td class="mono">${esc(event.actor_id)}</td><td class="mono">${esc(event.object_type)}#${esc(event.object_id).slice(0, 40)}</td><td class="mono">${esc(JSON.stringify(event.after || event.before || {})).slice(0, 110)}</td>`;
      body.appendChild(row);
    });
  } catch (error) {
    if (error.status !== 401) notice('审计读取失败：' + error.message, true);
    $('auditBody').innerHTML = '<tr><td colspan="5" class="empty">读取失败（可能缺少 audit:read 权限）</td></tr>';
  }
}

async function loadToolRuns() {
  try {
    const data = await api('/api/tool-runs?limit=30');
    const body = $('toolBody');
    body.innerHTML = '';
    if (!(data.runs || []).length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">暂无工具调用记录。</td></tr>';
      return;
    }
    data.runs.forEach(run => {
      const row = document.createElement('tr');
      row.innerHTML = `<td class="mono">${fmtTime(run.created_at)}</td><td>${esc(run.tool_name)}</td><td class="mono">${esc(run.actor_id)}</td><td class="mono">${esc(run.session_id || '-')}</td><td>${tagOf(run.status)}</td><td class="mono">${esc(JSON.stringify(run.input_redacted || {})).slice(0, 80)}</td>`;
      body.appendChild(row);
    });
  } catch (error) {
    if (error.status !== 401) notice('工具调用读取失败：' + error.message, true);
    $('toolBody').innerHTML = '<tr><td colspan="6" class="empty">读取失败（需要 audit:read 权限）</td></tr>';
  }
}

$('refresh').onclick = loadQueue;
$('refreshAudit').onclick = loadAudit;
$('statusFilter').onchange = loadQueue;
$('eventType').onchange = loadAudit;
loadQueue();
loadAudit();
loadToolRuns();
