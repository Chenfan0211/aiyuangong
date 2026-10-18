const chat = document.getElementById('chat');
const input = document.getElementById('input');
const send = document.getElementById('send');
const StoreSession = window.StoreSession;
let currentPermissions = [];
let authenticated = false;
let mfaToken = '';
let securityPending = false;
let mfaEnrollmentStarted = false;
let sessionId = sessionStorage.getItem('store-agent-session') || ('session-' + Date.now().toString(36));
sessionStorage.setItem('store-agent-session', sessionId);

const PRIORITY_LABEL = { urgent: '紧急', high: '高', medium: '中', low: '低' };
const escapeText = value => String(value ?? '');
const client = StoreSession.createClient({
  onUnauthorized: error => {
    if (authenticated) showLogin('登录已过期，请重新登录');
  }
});

function setHint(text, isError) {
  const el = document.getElementById('loginHint');
  el.textContent = text || '';
  el.className = 'login-hint' + (isError ? ' error' : '');
}

function showMfa() {
  document.getElementById('mfaBox').hidden = false;
  document.getElementById('mfaCode').focus();
}

function hideMfa() {
  const box = document.getElementById('mfaBox');
  if (box) box.hidden = true;
  const code = document.getElementById('mfaCode');
  if (code) code.value = '';
}

function setSecurityHint(text, isError) {
  const el = document.getElementById('mfaSetupHint');
  el.textContent = text || '';
  el.className = 'login-hint' + (isError ? ' error' : '');
}

function setPasswordHint(text, isError) {
  const el = document.getElementById('passwordHint');
  el.textContent = text || '';
  el.className = 'login-hint' + (isError ? ' error' : '');
}

function setNodeHidden(id, hidden) {
  const node = document.getElementById(id);
  if (!node) return;
  node.hidden = hidden;
  if (hidden) node.style.display = 'none';
  else node.style.removeProperty('display');
}

function resetSecurityGate() {
  securityPending = false;
  mfaEnrollmentStarted = false;
  setNodeHidden('securityGate', true);
  setNodeHidden('mfaSetupCard', true);
  setNodeHidden('recoveryCodesCard', true);
  setNodeHidden('passwordSetupCard', true);
  document.getElementById('mfaSecret').textContent = '';
  document.getElementById('mfaOtpauth').textContent = '';
  document.getElementById('mfaEnrollmentCode').value = '';
  document.getElementById('recoveryCodes').textContent = '';
  document.getElementById('securityStatus').textContent = '';
  setSecurityHint('');
  setPasswordHint('');
}

function setAppChromeVisible(visible) {
  ['mainHeader', 'dataStrip', 'opsSection', 'reviewPanel', 'chat', 'composer', 'appLinks', 'scopeCard'].forEach(id => {
    const node = document.getElementById(id);
    if (node) node.hidden = !visible;
  });
}

function flagValue(source, key) {
  if (!source || typeof source !== 'object') return null;
  const user = source.user && typeof source.user === 'object' ? source.user : {};
  const context = source.context && typeof source.context === 'object' ? source.context : {};
  for (const candidate of [source, context, user]) {
    if (typeof candidate[key] === 'boolean') return candidate[key];
  }
  return null;
}

function securityFlags(...sources) {
  let mfaEnrollmentRequired = null;
  let mustChangePassword = null;
  for (const source of sources) {
    if (mfaEnrollmentRequired === null) mfaEnrollmentRequired = flagValue(source, 'mfa_enrollment_required');
    if (mustChangePassword === null) mustChangePassword = flagValue(source, 'must_change_password');
  }
  return {
    mfaEnrollmentRequired: mfaEnrollmentRequired === true,
    mustChangePassword: mustChangePassword === true,
  };
}

function applyIdentity(context) {
  const source = context || {};
  const user = source.user || source.context || {};
  currentPermissions = source.permissions || currentPermissions;
  if (user.name || user.display_name) {
    document.getElementById('identityName').textContent = user.name || user.display_name;
    document.getElementById('currentUser').textContent = user.name || user.display_name;
  }
  if (user.role_label || user.role) {
    document.getElementById('currentRole').textContent = (user.role_label || user.role) + ' · ' + (source.store_id || '');
  }
}

async function beginMfaEnrollment() {
  if (mfaEnrollmentStarted) return;
  mfaEnrollmentStarted = true;
  setSecurityHint('正在生成 MFA 绑定信息…');
  try {
    const setup = await postJson('/api/auth/mfa/setup', {});
    document.getElementById('mfaSecret').textContent = setup.secret || '';
    document.getElementById('mfaOtpauth').textContent = setup.otpauth_uri || setup.otpauth || '';
    setSecurityHint('');
  } catch (error) {
    mfaEnrollmentStarted = false;
    setSecurityHint(error.message || 'MFA 绑定初始化失败', true);
  }
}

async function showSecurityGate(context, flags) {
  authenticated = true;
  securityPending = true;
  mfaToken = '';
  document.getElementById('loginBox').hidden = true;
  document.getElementById('userCard').hidden = false;
  hideMfa();
  applyIdentity(context);
  setAppChromeVisible(false);
  setNodeHidden('securityGate', false);

  const needsMfa = flags.mfaEnrollmentRequired;
  const needsPassword = flags.mustChangePassword;
  setNodeHidden('passwordSetupCard', needsMfa || !needsPassword);
  setNodeHidden('mfaSetupCard', !needsMfa);
  document.getElementById('securityStatus').textContent = needsMfa && needsPassword
    ? '请先完成 MFA 绑定，随后修改临时口令'
    : (needsMfa ? '必须先绑定 MFA 才能继续' : '必须先修改临时口令才能继续');
  if (needsMfa) await beginMfaEnrollment();
}

function showLogin(message) {
  resetSecurityGate();
  authenticated = false;
  currentPermissions = [];
  mfaToken = '';
  document.getElementById('userCard').hidden = true;
  document.getElementById('loginBox').hidden = false;
  hideMfa();
  setAppChromeVisible(true);
  document.getElementById('opsTitle').textContent = '今日三件事';
  document.getElementById('taskCards').innerHTML = '<div class="empty">登录后显示今日任务。</div>';
  const quality = document.getElementById('qualityStatus');
  quality.className = 'platform';
  quality.textContent = '数据质量：待登录';
  document.getElementById('taskCount').textContent = '尚未登录';
  document.getElementById('serviceStatus').textContent = '请先登录门店账号';
  if (message) setHint(message, true);
}

function showApp() {
  resetSecurityGate();
  authenticated = true;
  document.getElementById('loginBox').hidden = true;
  document.getElementById('userCard').hidden = false;
  hideMfa();
  setAppChromeVisible(true);
  setHint('');
}

async function refreshSecurityGateAfterForbidden(error) {
  if (!error || error.status !== 403) return false;
  const message = String(error.message || '');
  if (!/MFA|绑定|临时口令|改密/.test(message)) return false;
  try {
    const me = await client.getJson('/api/auth/me');
    const flags = securityFlags(me);
    if (flags.mfaEnrollmentRequired || flags.mustChangePassword) {
      await showSecurityGate(me, flags);
      return true;
    }
  } catch (_error) {
    // 下一次业务请求仍会再次校验，保留原始 403 更利于调用方处理。
  }
  return false;
}

async function getJson(path) {
  try {
    return await client.getJson(path);
  } catch (error) {
    if (await refreshSecurityGateAfterForbidden(error)) {
      const handled = new Error('请先完成账户安全设置');
      handled.status = 403;
      throw handled;
    }
    throw error;
  }
}

async function postJson(path, body) {
  try {
    return await client.postJson(path, body);
  } catch (error) {
    if (await refreshSecurityGateAfterForbidden(error)) {
      const handled = new Error('请先完成账户安全设置');
      handled.status = 403;
      throw handled;
    }
    throw error;
  }
}

async function login() {
  sessionId = 'session-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  sessionStorage.setItem('store-agent-session', sessionId);
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  if (!username || !password) {
    setHint('请输入账号与口令', true);
    return;
  }
  setHint('登录中…');
  try {
    const data = await client.postJson('/api/auth/login', { username, password });
    document.getElementById('password').value = '';
    if (data.mfa_required) {
      mfaToken = data.mfa_token;
      showMfa();
      setHint('请输入动态验证码或一次性恢复码');
      return;
    }
    await bootAll(data);
  } catch (error) {
    setHint(error.message || '登录失败', true);
  }
}

async function verifyMfa() {
  const code = document.getElementById('mfaCode').value.trim();
  if (!code || !mfaToken) {
    setHint('请输入动态验证码或恢复码', true);
    return;
  }
  setHint('验证中…');
  try {
    const data = await client.postJson('/api/auth/mfa/verify', { mfa_token: mfaToken, code });
    mfaToken = '';
    await bootAll(data);
  } catch (error) {
    setHint(error.message || '验证失败', true);
  }
}

function renderRecoveryCodes(codes) {
  const list = document.getElementById('recoveryCodes');
  list.textContent = '';
  codes.forEach(code => {
    const item = document.createElement('li');
    item.textContent = code;
    list.appendChild(item);
  });
}

async function enableMfa(event) {
  if (event) event.preventDefault();
  const code = document.getElementById('mfaEnrollmentCode').value.trim();
  if (!code) {
    setSecurityHint('请输入验证器中的 6 位验证码', true);
    return;
  }
  const button = document.getElementById('enableMfaBtn');
  button.disabled = true;
  setSecurityHint('正在验证并启用 MFA…');
  try {
    const result = await postJson('/api/auth/mfa/enable', { code });
    const codes = Array.isArray(result.recovery_codes) ? result.recovery_codes : [];
    if (!codes.length) throw new Error('服务端未返回恢复码');
    renderRecoveryCodes(codes);
    setNodeHidden('mfaSetupCard', true);
    setNodeHidden('recoveryCodesCard', false);
    setSecurityHint('');
    document.getElementById('securityStatus').textContent = '请立即保存恢复码；关闭后不会再次显示';
    document.getElementById('finishMfaBtn').focus();
  } catch (error) {
    setSecurityHint(error.message || 'MFA 绑定失败', true);
  } finally {
    button.disabled = false;
  }
}

async function finishMfaSetup() {
  document.getElementById('recoveryCodes').textContent = '';
  setNodeHidden('recoveryCodesCard', true);
  document.getElementById('securityStatus').textContent = '正在刷新账户安全状态…';
  try {
    await bootAll();
  } catch (error) {
    setSecurityHint(error.message || '账户安全状态刷新失败', true);
  }
}

async function changePassword(event) {
  if (event) event.preventDefault();
  const currentPassword = document.getElementById('currentPassword').value;
  const newPassword = document.getElementById('newPassword').value;
  const confirmPassword = document.getElementById('confirmPassword').value;
  if (!currentPassword || !newPassword || !confirmPassword) {
    setPasswordHint('请填写全部口令字段', true);
    return;
  }
  if (newPassword !== confirmPassword) {
    setPasswordHint('两次输入的新口令不一致', true);
    return;
  }
  if (newPassword.length < 10) {
    setPasswordHint('新口令至少需要 10 个字符', true);
    return;
  }
  const button = document.getElementById('changePasswordBtn');
  button.disabled = true;
  setPasswordHint('正在修改口令…');
  try {
    await postJson('/api/auth/password', {
      current_password: currentPassword,
      new_password: newPassword
    });
    document.getElementById('currentPassword').value = '';
    document.getElementById('newPassword').value = '';
    document.getElementById('confirmPassword').value = '';
    showLogin('口令已修改，请使用新口令重新登录');
  } catch (error) {
    setPasswordHint(error.message || '口令修改失败', true);
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await postJson('/api/auth/logout', {});
  } catch (_error) {
    // 会话可能已经失效；本地仍按退出处理。
  }
  sessionId = 'session-' + Date.now().toString(36);
  sessionStorage.setItem('store-agent-session', sessionId);
  chat.innerHTML = '<div class="welcome" id="welcome"><h2>已退出登录</h2><p>请重新登录后继续。</p></div>';
  showLogin('已退出登录');
}

function addMessage(role, text, meta) {
  const details = meta || {};
  document.getElementById('welcome')?.remove();
  const row = document.createElement('div');
  row.className = 'message ' + role + (details.error ? ' error' : '');
  if (role !== 'user') {
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = '运';
    row.appendChild(avatar);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = escapeText(text);
  if ((details.sources && details.sources.length) || details.approval) {
    const tags = document.createElement('div');
    tags.className = 'meta';
    (details.sources || []).forEach(source => {
      const tag = document.createElement('span');
      tag.className = 'source';
      tag.textContent = '依据：' + (source && source.label ? source.label : source);
      tags.appendChild(tag);
    });
    if (details.approval) {
      const tag = document.createElement('span');
      tag.className = 'approval';
      tag.textContent = '需要审批';
      tags.appendChild(tag);
    }
    bubble.appendChild(tags);
  }
  row.appendChild(bubble);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
}

function renderPlatformStatus(key, item) {
  const el = document.getElementById(key + 'Status');
  el.className = 'platform ' + ((item && item.state) || '');
  if (!item || item.state === 'not_connected') {
    el.textContent = (key === 'meituan' ? '美团' : '抖音') + '：未同步';
    return;
  }
  const label = item.state === 'fresh' ? '已同步' : '数据过期';
  el.textContent = `${item.label}：${label}至 ${item.max_date}`;
}

async function bootAll(prefetched) {
  const me = await getJson('/api/auth/me');
  const flags = securityFlags(prefetched, me);
  applyIdentity(me);
  if (flags.mfaEnrollmentRequired || flags.mustChangePassword) {
    await showSecurityGate(me, flags);
    return;
  }
  showApp();
  currentPermissions = me.permissions || [];
  document.getElementById('identityName').textContent = me.user.name;
  document.getElementById('currentUser').textContent = me.user.name;
  document.getElementById('currentRole').textContent = me.user.role_label + ' · ' + me.store_id;
  const isManager = currentPermissions.indexOf('report:read') >= 0;
  document.getElementById('scopeTitle').textContent = me.user.role_label + '权限';
  document.getElementById('scopeText').textContent = isManager
    ? '可查看全店任务、经营指标与日报；高风险动作仍需审批。'
    : '只查看本人任务、操作步骤与非敏感聚合数据。';
  const boot = await getJson('/api/bootstrap');
  document.getElementById('taskCount').textContent = `${me.user.role_label}权限 · 今日可见 ${boot.task_count} 项任务`;
  document.getElementById('serviceStatus').textContent = boot.llm_enabled ? '对话 Agent 在线 · 大模型已接入' : '对话 Agent 在线 · 规则模式';
  renderPlatformStatus('meituan', boot.data_status?.platforms?.meituan);
  renderPlatformStatus('douyin', boot.data_status?.platforms?.douyin);
  await Promise.all([loadTasks(), loadReviewQueue(), loadQuality()]);
}

async function loadQuality() {
  const el = document.getElementById('qualityStatus');
  if (!currentPermissions.includes('quality:read')) {
    el.className = 'platform';
    el.textContent = '数据质量：仅店长可查看';
    return;
  }
  try {
    const data = await getJson('/api/data-quality?days=30');
    const summary = data.summary || {};
    const problems = (summary.missing_day_count || 0) + (summary.anomaly_count || 0);
    el.className = 'platform ' + (summary.has_data ? (problems ? 'stale' : 'fresh') : '');
    el.textContent = summary.has_data
      ? (problems ? '数据质量：缺 ' + summary.missing_day_count + ' 天 / 跳变 ' + summary.anomaly_count + ' 处' : '数据质量：正常')
      : '数据质量：无数据';
  } catch (_error) {
    el.textContent = '数据质量：读取失败';
  }
}

function actionButton(task, action, label) {
  const button = document.createElement('button');
  button.textContent = label;
  button.onclick = () => runTaskAction(task, action);
  return button;
}

function taskCard(task) {
  const card = document.createElement('div');
  card.className = 'card';
  const prio = document.createElement('span');
  prio.className = 'prio ' + (task.priority || '');
  prio.textContent = (PRIORITY_LABEL[task.priority] || task.priority) + (task.approval_required ? ' · 需审批' : '');
  const title = document.createElement('div');
  title.className = 't';
  title.textContent = task.title;
  const sub = document.createElement('div');
  sub.className = 's';
  sub.textContent = task.assignee_id + ' · 截止 ' + String(task.due_at || '').slice(5, 16).replace('T', ' ') + ' · ' + task.status;
  card.append(prio, title, sub);
  const acts = document.createElement('div');
  acts.className = 'acts';
  if (task.status === 'ready') acts.appendChild(actionButton(task, 'accept', '接受任务'));
  if (task.status === 'accepted') {
    acts.appendChild(actionButton(task, 'image', '上传图片证据'));
    acts.appendChild(actionButton(task, 'complete', '提交证据并完成'));
  }
  card.appendChild(acts);
  return card;
}

async function loadTasks() {
  const box = document.getElementById('taskCards');
  try {
    const data = await getJson('/api/tasks/today?status=open');
    const tasks = (data.tasks || []).slice(0, 3);
    const storeWide = currentPermissions.indexOf('tasks:read:store') >= 0;
    document.getElementById('opsTitle').textContent = storeWide ? '今日三件事 · 全店' : '今日三件事 · 我的任务';
    box.innerHTML = '';
    if (!tasks.length) {
      box.innerHTML = '<div class="empty">今天没有待办任务。</div>';
      return;
    }
    tasks.forEach(task => box.appendChild(taskCard(task)));
  } catch (error) {
    box.innerHTML = '<div class="empty">任务读取失败：' + error.message + '</div>';
  }
}

async function uploadEvidence(task, file) {
  const data = await client.upload(
    '/api/tasks/' + encodeURIComponent(task.task_id) + '/evidence-upload?filename=' + encodeURIComponent(file.name) + '&session_id=' + encodeURIComponent(sessionId),
    file
  );
  addMessage('assistant', '图片证据已上传：' + data.evidence_file.evidence_id);
  await loadTasks();
}

async function runTaskAction(task, action) {
  try {
    if (action === 'image') {
      const picker = document.createElement('input');
      picker.type = 'file';
      picker.accept = 'image/jpeg,image/png,image/webp';
      picker.onchange = async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        try {
          await uploadEvidence(task, file);
        } catch (error) {
          addMessage('assistant', '图片证据上传失败：' + error.message, { error: true });
        }
      };
      picker.click();
      return;
    }
    if (action === 'accept') {
      await postJson('/api/tasks/' + encodeURIComponent(task.task_id) + '/status', {
        session_id: sessionId,
        status: 'accepted',
        reason: '在界面接受任务'
      });
    } else {
      const ref = window.prompt('请输入脱敏订单号、截图引用或其他证据引用：');
      if (!ref) return;
      await postJson('/api/tasks/' + encodeURIComponent(task.task_id) + '/evidence', {
        session_id: sessionId,
        evidence_type: '脱敏订单号',
        object_ref: ref,
        note: '界面提交'
      });
      await postJson('/api/tasks/' + encodeURIComponent(task.task_id) + '/status', {
        session_id: sessionId,
        status: 'done',
        reason: '已按要求执行并提交证据'
      });
    }
    addMessage('assistant', '任务已更新：' + task.title);
    await loadTasks();
  } catch (error) {
    addMessage('assistant', '任务更新失败：' + error.message, { error: true });
  }
}

function reviewCard(task) {
  const card = document.createElement('div');
  card.className = 'card';
  const title = document.createElement('div');
  title.className = 't';
  title.textContent = task.title;
  const sub = document.createElement('div');
  sub.className = 's';
  sub.textContent = (task.assignee_id || '') + ' · 截止 ' + String(task.due_at || '').slice(5, 16).replace('T', ' ');
  const acts = document.createElement('div');
  acts.className = 'acts';
  const ok = document.createElement('button');
  ok.textContent = '验收通过';
  ok.onclick = () => reviewTask(task, 'verified');
  const no = document.createElement('button');
  no.className = 'ghost';
  no.textContent = '驳回';
  no.onclick = () => reviewTask(task, 'rejected');
  acts.append(ok, no);
  card.append(title, sub, acts);
  return card;
}

async function loadReviewQueue() {
  const panel = document.getElementById('reviewPanel');
  if (!currentPermissions.includes('tasks:verify')) {
    panel.hidden = true;
    return;
  }
  const box = document.getElementById('reviewCards');
  try {
    const data = await getJson('/api/tasks/today?status=done');
    const tasks = (data.tasks || []).slice(0, 3);
    panel.hidden = !tasks.length;
    document.getElementById('reviewCount').textContent = tasks.length + ' 项';
    box.innerHTML = '';
    tasks.forEach(task => box.appendChild(reviewCard(task)));
  } catch (_error) {
    panel.hidden = true;
  }
}

async function reviewTask(task, status) {
  let reason = status === 'rejected' ? window.prompt('驳回原因（必填）：') : '界面验收通过';
  if (status === 'rejected' && !reason) return;
  try {
    await postJson('/api/tasks/' + encodeURIComponent(task.task_id) + '/status', {
      session_id: sessionId,
      status,
      reason
    });
    addMessage('assistant', '任务已' + (status === 'verified' ? '验收' : '驳回') + '：' + task.title);
    await Promise.all([loadTasks(), loadReviewQueue()]);
  } catch (error) {
    addMessage('assistant', '验收失败：' + error.message, { error: true });
  }
}

async function loadReport() {
  try {
    const data = await getJson('/api/reports/daily');
    addMessage('assistant', data.report, { sources: data.sources });
  } catch (error) {
    addMessage('assistant', '日报生成失败：' + error.message, { error: true });
  }
}

async function ask(message) {
  const clean = message.trim();
  if (!clean) return;
  if (!authenticated) {
    setHint('请先登录', true);
    return;
  }
  if (securityPending) {
    document.getElementById('securityStatus').textContent = '请先完成账户安全设置';
    return;
  }
  addMessage('user', clean);
  input.value = '';
  send.disabled = true;
  try {
    const data = await postJson('/api/chat', { session_id: sessionId, message: clean });
    addMessage('assistant', data.answer, { sources: data.sources, approval: data.requires_approval });
  } catch (error) {
    addMessage('assistant', '暂时无法回答：' + error.message, { error: true });
  } finally {
    send.disabled = false;
    input.focus();
  }
}

document.getElementById('loginBtn').addEventListener('click', login);
document.getElementById('mfaVerifyBtn').addEventListener('click', verifyMfa);
document.getElementById('mfaSetupCard').addEventListener('submit', enableMfa);
document.getElementById('passwordSetupCard').addEventListener('submit', changePassword);
document.getElementById('finishMfaBtn').addEventListener('click', finishMfaSetup);
document.getElementById('password').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    login();
  }
});
document.getElementById('mfaCode').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    verifyMfa();
  }
});
document.getElementById('logoutBtn').addEventListener('click', logout);
document.getElementById('reportBtn').addEventListener('click', loadReport);
send.addEventListener('click', () => ask(input.value));
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    ask(input.value);
  }
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => ask(button.dataset.prompt)));

(async function bootSession() {
  try {
    await bootAll();
  } catch (error) {
    if (!error || error.status !== 401) {
      showLogin('会话检查失败：' + (error && error.message ? error.message : '未知错误'));
    } else {
      showLogin('请使用门店账号登录');
    }
  }
})();
