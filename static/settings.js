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

async function loadConfig() {
  const data = await api('/api/admin/config');
  const config = data.config;
  configRevision = data.revision || null;
  $('cycleStart').value = config.review_cycle.cycle_start;
  $('cycleEnd').value = config.review_cycle.cycle_end;
  $('targetQualified').value = config.review_cycle.target_qualified_month || 0;
  $('freshDays').value = config.rules.fresh_days || 2;
  $('slaHours').value = config.rules.negative_review_sla_hours || 4;
}

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
    row.innerHTML = '<td>' + esc(member.username) + '</td><td>' + esc(member.display_name) + '</td><td>' + esc(member.role) + '</td><td>' + esc(member.status) + '</td><td></td>';
    const actions = row.lastElementChild;
    const password = document.createElement('button');
    password.className = 'ghost';
    password.textContent = '重置口令';
    password.onclick = async () => {
      try {
        const result = await client.postJson('/api/admin/members/' + encodeURIComponent(member.user_id) + '/reset-password', {});
        notice('临时口令（仅显示一次）：' + result.temporary_password, false);
      } catch (error) {
        if (error.status !== 401) notice(error.message, true);
      }
    };
    const mfa = document.createElement('button');
    mfa.className = 'ghost';
    mfa.textContent = '重置 MFA';
    mfa.onclick = async () => {
      try {
        await client.postJson('/api/admin/members/' + encodeURIComponent(member.user_id) + '/reset-mfa', {});
        notice('MFA 已重置', false);
      } catch (error) {
        if (error.status !== 401) notice(error.message, true);
      }
    };
    actions.append(password, ' ', mfa);
    body.appendChild(row);
  });
}

$('configForm').onsubmit = async event => {
  event.preventDefault();
  try {
    const result = await client.patchJson('/api/admin/config', {
      revision: configRevision,
      config: {
        review_cycle: {
          cycle_start: $('cycleStart').value,
          cycle_end: $('cycleEnd').value,
          target_qualified_month: Number($('targetQualified').value)
        },
        rules: {
          fresh_days: Number($('freshDays').value),
          negative_review_sla_hours: Number($('slaHours').value)
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

Promise.all([loadConfig(), loadMembers()]).catch(error => {
  if (error.status !== 401) notice(error.message, true);
});
