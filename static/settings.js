const StoreSession = window.StoreSession;
const client = StoreSession.createClient();
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
let configRevision = null;

function notice(text, isError) {
  const el = $('notice');
  el.hidden = !text;
  el.textContent = text || '';
  el.className = 'notice' + (isError ? ' error' : '');
}

async function api(path, options) {
  return client.json(path, options);
}

const RETENTION_KEYS = ['platform_metrics','package_metrics','metric_import_batches','reviews','action_tasks','task_evidence','task_events','approvals','audit_events','agent_messages','agent_sessions','agent_tool_runs','notifications','auth_sessions','login_attempts','webhook_nonces','users','memberships','stores','store_configs'];
function renderPackageCosts(items) {
  const box = $('packageCosts');
  box.innerHTML = '';
  (items || []).forEach(item => {
    const row = document.createElement('div');
    row.className = 'cost-row';
    row.innerHTML = '<input data-cost="platform" placeholder="meituan/douyin" value="' + esc(item.platform || '') + '"><input data-cost="package_id" placeholder="套餐 ID" value="' + esc(item.package_id || '') + '"><input data-cost="sale_price" type="number" placeholder="售价" value="' + esc(item.sale_price ?? '') + '"><input data-cost="variable_cost" type="number" placeholder="变动成本" value="' + esc(item.variable_cost ?? '') + '"><button type="button" class="ghost remove-cost">删除</button>';
    row.querySelector('.remove-cost').onclick = () => row.remove();
    box.appendChild(row);
  });
}
function renderRetention(retention) {
  const box = $('retentionFields');
  box.innerHTML = '';
  RETENTION_KEYS.forEach(key => {
    const label = document.createElement('label');
    label.textContent = key;
    const input = document.createElement('input');
    input.type = 'number'; input.min = '0'; input.dataset.retention = key;
    input.value = (retention && retention[key] !== undefined) ? retention[key] : 0;
    label.appendChild(input); box.appendChild(label);
  });
}
async function loadConfig() {
  const data = await api('/api/admin/config');
  const config = data.config;
  configRevision = data.revision || null;
  $('cycleStart').value = config.review_cycle.cycle_start;
  $('cycleEnd').value = config.review_cycle.cycle_end;
  $('targetQualified').value = config.review_cycle.target_qualified_month || 0;
  $('freshDays').value = config.rules.fresh_days || 2;
  $('slaHours').value = config.rules.negative_review_sla_hours || 4;
  $('personManager').value = config.people.store_manager || '';
  $('personReview').value = config.people.review_operator || config.people.store_manager || '';
  $('personOnline').value = config.people.online_operator || config.people.store_manager || '';
  $('personArea').value = config.people.area_manager || '';
  $('packageConversionFloor').value = config.rules.package_conversion_floor || 0.05;
  $('packageMinViews').value = config.rules.package_min_views || 50;
  $('packageMarginFloor').value = config.rules.package_margin_floor || 0.25;
  $('jumpAlertPercent').value = config.rules.jump_alert_percent || 30;
  renderPackageCosts(config.package_costs || []);
  renderRetention(config.retention || {});
}
$('addPackageCost').onclick = () => {
  const current = [...$('packageCosts').querySelectorAll('.cost-row')].map(row => ({
    platform: row.querySelector('[data-cost=platform]').value,
    package_id: row.querySelector('[data-cost=package_id]').value,
    sale_price: Number(row.querySelector('[data-cost=sale_price]').value || 0),
    variable_cost: Number(row.querySelector('[data-cost=variable_cost]').value || 0)
  }));
  current.push({ platform: 'meituan', package_id: '', sale_price: 0, variable_cost: 0 });
  renderPackageCosts(current);
};

async function loadMembers() {
  const data = await api('/api/admin/members');
  const body = $('memberBody');
  body.innerHTML = '';
  if (!data.members.length) {
    body.innerHTML = '<tr><td colspan="5">暂无成员</td></tr>';
    return;
  }
  data.members.forEach(member => {
    const row = document.createElement('tr');
    row.innerHTML = '<td>' + esc(member.username) + '</td>'
      + '<td><input data-name value="' + esc(member.display_name) + '"></td>'
      + '<td><select data-role>' + ['employee','operator','manager','area_manager','owner'].map(role => '<option value="' + role + '"' + (role === member.role ? ' selected' : '') + '>' + role + '</option>').join('') + '</select></td>'
      + '<td>' + esc(member.status) + (member.must_change_password ? ' · 待改密' : '') + '</td><td></td>';
    const actions = row.lastElementChild;
    const save = document.createElement('button');
    save.className = 'ghost'; save.textContent = '保存成员';
    save.onclick = async () => {
      try {
        await client.patchJson('/api/admin/members/' + encodeURIComponent(member.user_id), {
          display_name: row.querySelector('[data-name]').value,
          role: row.querySelector('[data-role]').value
        });
        notice('成员信息已保存', false); loadMembers();
      } catch (error) { if (error.status !== 401) notice(error.message, true); }
    };
    const toggle = document.createElement('button');
    toggle.className = 'ghost'; toggle.textContent = member.status === 'active' ? '停用' : '启用';
    toggle.onclick = async () => {
      try {
        await client.postJson('/api/admin/members/' + encodeURIComponent(member.user_id) + '/status', {
          status: member.status === 'active' ? 'disabled' : 'active'
        });
        notice('成员状态已更新', false); loadMembers();
      } catch (error) { if (error.status !== 401) notice(error.message, true); }
    };
    const password = document.createElement('button');
    password.className = 'ghost'; password.textContent = '重置口令';
    password.onclick = async () => {
      try {
        const result = await client.postJson('/api/admin/members/' + encodeURIComponent(member.user_id) + '/reset-password', {});
        notice('临时口令（仅显示一次）：' + result.temporary_password, false);
      } catch (error) { if (error.status !== 401) notice(error.message, true); }
    };
    const mfa = document.createElement('button');
    mfa.className = 'ghost'; mfa.textContent = '重置 MFA';
    mfa.onclick = async () => {
      try {
        await client.postJson('/api/admin/members/' + encodeURIComponent(member.user_id) + '/reset-mfa', {});
        notice('MFA 已重置', false);
      } catch (error) { if (error.status !== 401) notice(error.message, true); }
    };
    actions.append(save, ' ', toggle, ' ', password, ' ', mfa);
    body.appendChild(row);
  });
}

$('configForm').onsubmit = async event => {
  event.preventDefault();
  try {
    const packageCosts = [...$('packageCosts').querySelectorAll('.cost-row')].map(row => ({
      platform: row.querySelector('[data-cost=platform]').value.trim(),
      package_id: row.querySelector('[data-cost=package_id]').value.trim(),
      sale_price: Number(row.querySelector('[data-cost=sale_price]').value || 0),
      variable_cost: Number(row.querySelector('[data-cost=variable_cost]').value || 0)
    })).filter(item => item.package_id);
    const retention = {};
    $('retentionFields').querySelectorAll('[data-retention]').forEach(input => {
      retention[input.dataset.retention] = Number(input.value || 0);
    });
    const result = await client.patchJson('/api/admin/config', {
      revision: configRevision,
      config: {
        review_cycle: {
          cycle_start: $('cycleStart').value,
          cycle_end: $('cycleEnd').value,
          target_qualified_month: Number($('targetQualified').value)
        },
        people: {
          store_manager: $('personManager').value,
          review_operator: $('personReview').value,
          online_operator: $('personOnline').value,
          area_manager: $('personArea').value
        },
        package_costs: packageCosts,
        retention,
        rules: {
          fresh_days: Number($('freshDays').value),
          negative_review_sla_hours: Number($('slaHours').value),
          package_conversion_floor: Number($('packageConversionFloor').value),
          package_min_views: Number($('packageMinViews').value),
          package_margin_floor: Number($('packageMarginFloor').value),
          jump_alert_percent: Number($('jumpAlertPercent').value)
        }
      }
    });
    configRevision = result.revision || configRevision;
    notice('配置已保存', false);
  } catch (error) {
    if (error.status !== 401) notice(error.message, true);
  }
};

$('memberForm').onsubmit = async event => {
  event.preventDefault();
  try {
    const result = await client.postJson('/api/admin/members', {
      username: $('memberUsername').value,
      display_name: $('memberName').value,
      role: $('memberRole').value
    });
    notice('成员已创建，临时口令：' + result.temporary_password, false);
    $('memberForm').reset();
    loadMembers();
  } catch (error) {
    if (error.status !== 401) notice(error.message, true);
  }
};

$('refreshMembers').onclick = () => loadMembers().catch(error => {
  if (error.status !== 401) notice(error.message, true);
});

async function loadOpsRuns() {
  const data = await api('/api/admin/ops-runs?limit=50');
  const body = $('opsRunBody'); body.innerHTML = '';
  if (!data.runs.length) { body.innerHTML = '<tr><td colspan="7">暂无运行记录</td></tr>'; return; }
  data.runs.forEach(run => {
    const row = document.createElement('tr');
    row.innerHTML = '<td>' + esc(run.job_name) + '</td><td>' + esc(run.step_name) + '</td><td>' + esc(run.status) + '</td><td>' + esc(run.attempt) + '</td><td>' + esc(run.alert_status) + '</td><td>' + esc(run.started_at) + '</td><td></td>';
    if (run.status === 'failed' || run.status === 'skipped') {
      const button = document.createElement('button'); button.className = 'ghost'; button.textContent = '重试';
      button.onclick = async () => { try { await client.postJson('/api/admin/ops-runs/' + run.id + '/retry', {}); notice('重试已进入队列', false); loadOpsRuns(); } catch (error) { if (error.status !== 401) notice(error.message, true); } };
      row.lastElementChild.appendChild(button);
    }
    body.appendChild(row);
  });
}
async function loadRetries() {
  const data = await api('/api/admin/retries');
  const body = $('retryBody'); body.innerHTML = '';
  if (!data.retries.length) { body.innerHTML = '<tr><td colspan="5">暂无重试请求</td></tr>'; return; }
  data.retries.forEach(item => {
    const row = document.createElement('tr');
    row.innerHTML = '<td>' + esc(item.source_run_id) + '</td><td>' + esc(item.step_name) + '</td><td>' + esc(item.status) + '</td><td>' + esc(item.attempt + '/' + item.max_attempts) + '</td><td>' + esc(item.requested_by) + '</td>';
    body.appendChild(row);
  });
}
$('refreshOps').onclick = () => Promise.all([loadOpsRuns(), loadRetries()]).catch(error => { if (error.status !== 401) notice(error.message, true); });

Promise.all([loadConfig(), loadMembers(), loadOpsRuns(), loadRetries()]).catch(error => {
  if (error.status !== 401) notice(error.message, true);
});
