/* 腾讯会议预订平台 - 前端逻辑 */
'use strict';

const state = {
  token: localStorage.getItem('mt_token') || '',
  user: null,
  tab: 'meetings',
  statPeriod: 'month',
  statScope: 'me',
  filters: {},
  selected: new Set(),
  meetings: [],
  users: [],
  groups: [],
};

/* ---------- 工具 ---------- */
const $ = (id) => document.getElementById(id);

function toast(msg, type = 'info') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 3500);
}

async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (res.status === 401 && state.token) { // token 失效
    doLogout();
    throw new Error((data && data.error) || '登录已过期，请重新登录');
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || ('请求失败(' + res.status + ')'));
    err.data = data; // 保留 conflicts 等附加信息
    throw err;
  }
  return data;
}

function fmtDur(min) {
  if (min == null) return '-';
  const h = Math.floor(min / 60), m = min % 60;
  return h ? (h + ' 小时' + (m ? ' ' + m + ' 分钟' : '')) : (m + ' 分钟');
}

function statusTag(s) {
  // 兼容旧调用：字符串 → 只显示流程状态
  const map = {
    pending: ['badge-pending', '预订中'],
    booked: ['badge-booked', '已预订'],
    failed: ['badge-failed', '预订失败'],
    cancelled: ['badge-cancelled', '已取消'],
    upcoming: ['badge-booked', '已预订'],
    ongoing: ['badge-ongoing', '会议中'],
    ended: ['badge-ended', '已结束'],
  };
  const [cls, txt] = map[s] || ['badge-pending', s];
  return '<span class="badge ' + cls + '">' + txt + '</span>';
}

// 会议展示状态：已预订 / 会议中 / 已结束 / 预订中 / 预订失败 / 已取消
// 后端已按时间推导 display_status，这里仅做渲染；前端兜底按时间再算一次，
// 避免列表停留过久导致「已预订」应变为「会议中」而页面未刷新。
function meetingDisplayStatus(m) {
  if (m.status === 'booked') {
    const now = nowStr();
    if (now < m.start_at) return 'upcoming';
    if (now < m.end_at) return 'ongoing';
    return 'ended';
  }
  return m.status;
}

function statusCell(m) {
  const phase = meetingDisplayStatus(m);
  let html = statusTag(phase);
  if (m.status_anomaly) {
    html += `<div class="tm-error" title="${esc(m.anomaly_reason || '状态异常')}">`
      + `⚠ ${esc(m.anomaly_reason || '状态异常')}</div>`;
  } else if (m.tm_error && m.status === 'failed') {
    html += `<div class="tm-error" title="${esc(m.tm_error)}">⚠ ${esc(m.tm_error)}</div>`;
  }
  return html;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* ---------- 视图切换 ---------- */
function showView(name) {
  $('login-view').classList.toggle('hidden', name !== 'login');
  $('app-view').classList.toggle('hidden', name !== 'app');
}

function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tabpane').forEach((p) =>
    p.classList.toggle('hidden', p.id !== 'tab-' + tab));
  if (tab === 'meetings') loadMeetings();
  if (tab === 'stats') loadStats();
  if (tab === 'admin' && state.user && state.user.role === 'admin') loadAdmin();
}

/* ---------- 登录 ---------- */
async function doLogin() {
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  $('login-error').textContent = '';
  if (!username || !password) { $('login-error').textContent = '请输入用户名和密码'; return; }
  $('login-btn').disabled = true;
  try {
    const data = await api('/api/login', { method: 'POST', body: { username, password } });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem('mt_token', data.token);
    enterApp();
  } catch (e) {
    $('login-error').textContent = e.message;
  } finally {
    $('login-btn').disabled = false;
  }
}

function enterApp() {
  $('user-chip').textContent = (state.user.display_name || state.user.username)
    + (state.user.role === 'admin' ? '（管理员）' : '');
  const isAdmin = state.user.role === 'admin';
  $('tab-admin-btn').style.display = isAdmin ? '' : 'none';
  // 统计范围切换（我的 / 全平台）仅管理员可见
  $('stat-scope-seg').classList.toggle('hidden', !isAdmin);
  if (!isAdmin) {
    state.statScope = 'me';
    $('stat-title').textContent = '我的会议统计（仅已预订成功）';
  }
  showView('app');
  showTab('meetings');
}

async function doLogout() {
  if (state.token) { try { await api('/api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ } }
  localStorage.removeItem('mt_token');
  state.token = '';
  state.user = null;
  showView('login');
}

/* ---------- 会议列表 ---------- */
function readFilters() {
  const st = $('f-status').value;
  return {
    from: $('f-from').value || '',
    to: $('f-to').value || '',
    creator: $('f-creator').value.trim(),
    subject: $('f-subject').value.trim(),
    status: st,
    sort_key: $('f-sort').value,
    sort_dir: $('f-dir').value,
  };
}

async function loadMeetings() {
  const f = readFilters();
  state.filters = f;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) if (v) q.set(k, v);
  q.delete('status'); // 状态不作为普通参数透传
  // 「仅状态异常」走 anomaly 参数，其余状态走 display_status
  if (f.status === 'anomaly') {
    q.set('anomaly', '1');
  } else if (f.status) {
    q.set('display_status', f.status);
  }
  try {
    const data = await api('/api/meetings?' + q.toString());
    renderMeetings(data.items || []);
  } catch (e) {
    toast(e.message, 'error');
  }
}

function meetingOps(m) {
  const me = state.user;
  const isCreator = me.id === m.creator_id;
  const isExpired = m.start_at < nowStr();
  const isCancelled = m.status === 'cancelled';
  // 已取消的会议不可再编辑（后端也会拒绝），但仍可由管理员/创建者删除
  const canModify = (me.role === 'admin' || (isCreator && !isExpired)) && !isCancelled;
  const canDelete = canModify || (isCancelled && (me.role === 'admin' || isCreator));
  const canRetry = m.status === 'failed' && (me.role === 'admin' || isCreator);
  // 邀请信息：创建者 / 管理员 / 同组（由后端二次校验，前端只做展示判断）
  const canInvite = !isCancelled
    && (me.role === 'admin' || isCreator || (me.group_name && m.creator_group
      && me.group_name === m.creator_group));
  let html = '';
  if (canInvite) html += `<button class="btn btn-mini" data-act="invite" data-id="${m.id}">复制邀请</button>`;
  if (canModify) html += `<button class="btn btn-mini" data-act="edit" data-id="${m.id}">编辑</button>`;
  if (canDelete)
    html += `<button class="btn btn-mini btn-danger" data-act="del" data-id="${m.id}">删除</button>`;
  if (canRetry) html += `<button class="btn btn-mini" data-act="retry" data-id="${m.id}">重试</button>`;
  return html || '<span class="muted">-</span>';
}

function nowStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderMeetings(rows) {
  const tb = $('meeting-tbody');
  state.selected = new Set();
  state.meetings = rows || [];
  const isAdmin = state.user && state.user.role === 'admin';
  $('check-all').checked = false;
  $('check-all').classList.toggle('hidden', !isAdmin);
  updateBatchBtn();
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="11" class="empty">暂无会议</td></tr>'; return; }
  tb.innerHTML = rows.map((m) => `
    <tr${m.status_anomaly ? ' class="row-anomaly"' : ''}>
      <td class="td-check">${isAdmin
        ? `<input type="checkbox" class="row-check" data-id="${m.id}">` : ''}</td>
      <td class="td-subject">${esc(m.subject)}</td>
      <td>${esc(m.start_at)}</td>
      <td>${m.duration_min}</td>
      <td>${esc(m.end_at)}</td>
      <td>${esc(m.creator_display_name || m.creator_username || '（已删除用户）')}</td>
      <td>${statusCell(m)}</td>
      <td>${m.tm_meeting_id ? esc(fmtMeetingNo(m.tm_meeting_id)) : '<span class="muted">-</span>'}</td>
      <td>${m.meeting_password ? esc(m.meeting_password) : '<span class="muted">-</span>'}</td>
      <td>${esc(m.created_at)}</td>
      <td class="td-ops">${meetingOps(m)}</td>
    </tr>`).join('');
  bindRowChecks();
}

/* ---------- 批量删除（管理员） ---------- */
function selectedIds() { return Array.from(state.selected || []); }

function updateBatchBtn() {
  const n = (state.selected || new Set()).size;
  const isAdmin = state.user && state.user.role === 'admin';
  $('batch-del-btn').classList.toggle('hidden', !isAdmin);
  $('batch-purge-btn').classList.toggle('hidden', !isAdmin);
  $('batch-del-btn').textContent = `删除选中（${n}）`;
  $('batch-purge-btn').textContent = `彻底删除（${n}）`;
  $('batch-del-btn').disabled = n === 0;
  $('batch-purge-btn').disabled = n === 0;
}

function bindRowChecks() {
  document.querySelectorAll('.row-check').forEach((cb) => {
    cb.addEventListener('change', () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) state.selected.add(id); else state.selected.delete(id);
      updateBatchBtn();
    });
  });
}

async function batchDelete() {
  const ids = selectedIds();
  if (!ids.length) { toast('请先勾选要删除的会议', 'error'); return; }
  const cancelled = (state.meetings || [])
    .filter((m) => ids.includes(m.id) && m.status === 'cancelled').length;
  const extra = cancelled ? `\n其中 ${cancelled} 场已取消的会议将被彻底移除记录。` : '';
  if (!confirm(`确定删除选中的 ${ids.length} 场会议？\n\n`
    + '未取消的会议将标记为「已取消」；已预订成功的会同步取消腾讯会议侧的预订。' + extra)) return;
  try {
    const r = await api('/api/meetings/batch-delete', { method: 'POST', body: { ids } });
    const skip = r.skipped && r.skipped.length ? `，跳过 ${r.skipped.length} 条` : '';
    const gone = r.purged_ids && r.purged_ids.length ? `，移除 ${r.purged_ids.length} 条已取消记录` : '';
    toast(r.message + skip + gone, 'success');
    loadMeetings();
  } catch (e) { toast(e.message, 'error'); }
}

// 彻底删除（连记录一起抹掉），用于状态本身错误、无法修正的会议
async function purgeMeetings() {
  const ids = selectedIds();
  if (!ids.length) { toast('请先勾选要删除的会议', 'error'); return; }
  if (!confirm(`确定彻底删除选中的 ${ids.length} 场会议？\n\n`
    + '记录将从数据库直接移除，不可恢复；已预订成功的会同步取消腾讯会议侧的预订。')) return;
  try {
    const r = await api('/api/meetings/batch-delete',
      { method: 'POST', body: { ids, purge: true } });
    const skip = r.skipped && r.skipped.length ? `，跳过 ${r.skipped.length} 条` : '';
    toast(r.message + skip, 'success');
    loadMeetings();
  } catch (e) { toast(e.message, 'error'); }
}

// 9 位腾讯会议号显示为 3-3-3
function fmtMeetingNo(id) {
  const s = String(id || '').replace(/\D/g, '');
  return s.length === 9 ? `${s.slice(0, 3)}-${s.slice(3, 6)}-${s.slice(6)}` : String(id || '');
}

// 复制文本：优先 Clipboard API（需 https/localhost），降级 execCommand
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 继续降级 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}

/* ---------- 预订/编辑弹窗 ---------- */
// datetime-local 用 "YYYY-MM-DDTHH:MM"，后端用 "YYYY-MM-DD HH:MM"，集中转换
function toLocalInput(s) { return (s || '').replace(' ', 'T').slice(0, 16); }
function fromLocalInput(s) { return (s || '').replace('T', ' '); }

function defaultStart() {
  // 默认当前时间 +1 小时并归到整点，保证满足提前量
  const t = new Date(Date.now() + 60 * 60 * 1000);
  t.setMinutes(0, 0, 0);
  const p = (n) => String(n).padStart(2, '0');
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}T${p(t.getHours())}:${p(t.getMinutes())}`;
}

function openModal(m) {
  $('modal-title').textContent = m ? '编辑会议' : '预订会议';
  $('m-id').value = m ? m.id : '';
  $('m-subject').value = m ? m.subject : '';
  $('m-start').value = m ? toLocalInput(m.start_at) : defaultStart();
  // 时长下拉：历史数据可能非 30 分钟档，动态补一个选项以保留原值
  const sel = $('m-duration');
  const dv = String(m ? m.duration_min : 60);
  if (!Array.from(sel.options).some((o) => o.value === dv)) {
    const opt = document.createElement('option');
    opt.value = dv;
    opt.textContent = dv + ' 分钟（历史值）';
    sel.appendChild(opt);
  }
  sel.value = dv;
  $('m-password').value = m ? (m.meeting_password || '') : '';
  $('modal-error').textContent = '';
  updateEndPreview();
  $('modal-mask').classList.remove('hidden');
  $('m-subject').focus();
}

function closeModal() { $('modal-mask').classList.add('hidden'); }

function updateEndPreview() {
  const start = fromLocalInput($('m-start').value.trim());
  const dur = parseInt($('m-duration').value, 10);
  const box = $('modal-end-preview');
  if (!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(start) || !(dur >= 1)) { box.textContent = ''; return; }
  const [y, mo, d, h, mi] = start.split(/[-: ]/).map(Number);
  const t = new Date(y, mo - 1, d, h, mi + dur);
  const p = (n) => String(n).padStart(2, '0');
  const end = `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}`;
  const cross = end.slice(0, 10) !== start.slice(0, 10);
  box.textContent = '预计结束时间：' + end + (cross ? '　⚠ 将跨天，不允许预订' : '');
  box.classList.toggle('warn', cross);
}

async function submitModal() {
  const id = $('m-id').value;
  const body = {
    subject: $('m-subject').value.trim(),
    start_at: fromLocalInput($('m-start').value.trim()),
    duration_min: parseInt($('m-duration').value, 10),
  };
  const pwd = $('m-password').value.trim();
  if (pwd) body.meeting_password = pwd;
  if (!body.subject || !body.start_at || !(body.duration_min >= 15) ||
      body.duration_min % 15 !== 0 || body.duration_min > 240) {
    $('modal-error').textContent = '请完整填写主题、开始时间，时长须为 15 分钟的倍数（最长 4 小时）';
    return;
  }
  if (pwd && !/^\d{6}$/.test(pwd)) {
    $('modal-error').textContent = '会议密码须为 6 位数字（例如 123456）';
    return;
  }
  $('modal-submit').disabled = true;
  try {
    if (id) {
      await api('/api/meetings/' + id, { method: 'PUT', body });
      toast('会议已更新', 'success');
    } else {
      await api('/api/meetings', { method: 'POST', body });
      toast('会议已提交预订', 'success');
    }
    closeModal();
    loadMeetings();
  } catch (e) {
    $('modal-error').textContent = e.message
      + (e.data && e.data.conflicts ? '\n冲突会议：' + e.data.conflicts.map((c) =>
        `#${c.id} ${c.subject}（${c.start_at} ~ ${c.end_at}，${c.creator || c.status}）`).join('；') : '');
  } finally {
    $('modal-submit').disabled = false;
  }
}

/* ---------- 统计 ---------- */
async function loadStats() {
  try {
    const d = await api('/api/stats?period=' + state.statPeriod + '&scope=' + state.statScope);
    const th = $('stat-period-th');
    const label = { month: '月份', quarter: '季度', year: '年份' }[state.statPeriod];
    th.textContent = label;
    const sum = $('stat-summary');
    const who = state.statScope === 'all' ? '全平台' : '我';
    sum.textContent = who + '累计 ' + d.count + ' 场会议，合计 ' + fmtDur(d.total_min);
    const tb = $('stat-tbody');
    if (!d.items.length) { tb.innerHTML = '<tr><td colspan="3" class="empty">暂无已预订成功的会议</td></tr>'; }
    else tb.innerHTML = d.items.map((it) =>
      `<tr><td>${esc(it.period)}</td><td>${it.count}</td><td>${fmtDur(it.total_min)}</td></tr>`).join('');

    // 全平台：按用户明细
    const wrap = $('stat-user-wrap');
    const isAll = state.statScope === 'all';
    wrap.classList.toggle('hidden', !isAll);
    if (isAll) {
      $('stat-user-period-th').textContent = label;
      const ut = $('stat-user-tbody');
      const rows = d.by_user || [];
      ut.innerHTML = rows.length ? rows.map((r) =>
        `<tr><td>${esc(r.display_name || r.username)}<span class="muted"> @${esc(r.username)}</span></td>
         <td>${esc(r.period)}</td><td>${r.count}</td><td>${fmtDur(r.total_min)}</td></tr>`).join('')
        : '<tr><td colspan="4" class="empty">暂无数据</td></tr>';
    }
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------- 管理员 ---------- */
async function loadAdmin() {
  try {
    const udata = await api('/api/users');
    const users = udata.items || [];
    state.users = users;
    await loadGroups();
    const tb = $('user-tbody');
    const adminCount = users.filter((x) => x.role === 'admin').length;
    tb.innerHTML = users.map((u) => {
      const isSelf = u.id === state.user.id;
      let ops = `<button class="btn btn-mini" data-uact="edit" data-id="${u.id}">编辑</button>`;
      if (!isSelf) {
        ops += `<button class="btn btn-mini" data-uact="pwd" data-id="${u.id}" data-username="${esc(u.username)}">重置密码</button>`;
        if (u.role !== 'admin' || adminCount > 1)
          ops += `<button class="btn btn-mini" data-uact="role" data-id="${u.id}" data-role="${u.role}">${u.role === 'admin' ? '设为普通用户' : '设为管理员'}</button>`;
        ops += `<button class="btn btn-mini btn-danger" data-uact="del" data-id="${u.id}" data-username="${esc(u.username)}">删除</button>`;
      }
      return `<tr>
        <td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.display_name)}</td>
        <td>${u.role === 'admin' ? '<span class="badge badge-booked">管理员</span>' : '普通用户'}</td>
        <td>${u.group_name ? esc(u.group_name) : '<span class="muted">-</span>'}</td>
        <td>${u.failed_logins}</td><td>${u.locked_until ? esc(u.locked_until) : '<span class="muted">-</span>'}</td>
        <td>${esc(u.created_at)}</td><td class="td-ops">${ops}</td>
      </tr>`;
    }).join('');
    const qdata = await api('/api/queue');
    const q = qdata.items || [];
    const qt = $('queue-tbody');
    qt.innerHTML = q.length ? q.map((t) => `<tr>
      <td>${t.id}</td><td>${t.kind === 'book' ? '预订' : '取消'}</td>
      <td>${t.meeting_id != null ? t.meeting_id : '-'}</td><td>${esc(t.subject)}</td>
      <td>${t.tm_meeting_id ? esc(t.tm_meeting_id) : '<span class="muted">-</span>'}</td>
      <td>${statusTag({ pending: 'pending', queued: 'pending', running: 'pending', done: 'booked', error: 'failed' }[t.status] || 'pending')}</td>
      <td>${t.next_run_at ? esc(t.next_run_at) : '<span class="muted">-</span>'}</td>
      <td class="td-note">${esc(t.error || '')}</td>
    </tr>`).join('') : '<tr><td colspan="8" class="empty">队列为空</td></tr>';
  } catch (e) { toast(e.message, 'error'); }
}

// 已有群组名 → datalist 提示（新建/编辑用户时可直接选或手填）
async function loadGroups() {
  try {
    const d = await api('/api/groups');
    state.groups = d.items || [];
    const dl = $('group-options');
    dl.innerHTML = state.groups.map((g) => `<option value="${esc(g)}"></option>`).join('');
  } catch (e) { /* 群组提示失败不影响主流程 */ }
}

function openUserModal(u) {
  $('user-modal-title').textContent = '修改用户信息';
  $('u-id').value = u.id;
  $('u-username').value = u.username || '';
  $('u-display').value = u.display_name || '';
  $('u-role').value = u.role === 'admin' ? 'admin' : 'user';
  $('u-group').value = u.group_name || '';
  // 不能取消自己的管理员身份
  $('u-role').disabled = u.id === state.user.id;
  $('user-modal-error').textContent = '';
  $('user-mask').classList.remove('hidden');
  $('u-username').focus();
}

function closeUserModal() { $('user-mask').classList.add('hidden'); }

async function submitUserModal() {
  const id = $('u-id').value;
  const body = {
    username: $('u-username').value.trim(),
    display_name: $('u-display').value.trim(),
    group_name: $('u-group').value.trim(),
  };
  if (String(id) !== String(state.user.id)) body.role = $('u-role').value;
  if (!body.username) { $('user-modal-error').textContent = '用户名不能为空'; return; }
  $('user-submit').disabled = true;
  try {
    const r = await api('/api/users/' + id, { method: 'PUT', body });
    toast(r.message || '用户信息已更新', 'success');
    closeUserModal();
    // 改的是自己 → 同步顶栏显示
    if (String(id) === String(state.user.id)) {
      state.user.username = r.username;
      state.user.display_name = r.display_name;
      state.user.role = r.role;
      state.user.group_name = r.group_name;
      $('user-chip').textContent = (state.user.display_name || state.user.username)
        + (state.user.role === 'admin' ? '（管理员）' : '');
    }
    loadAdmin();
  } catch (e) {
    $('user-modal-error').textContent = e.message;
  } finally {
    $('user-submit').disabled = false;
  }
}

async function createUser() {
  const body = {
    username: $('nu-username').value.trim(),
    display_name: $('nu-display').value.trim(),
    role: $('nu-role').value,
    password: $('nu-password').value,
    group_name: $('nu-group').value.trim(),
  };
  if (!body.username) { toast('请输入用户名', 'error'); return; }
  try {
    const r = await api('/api/users', { method: 'POST', body });
    toast(r.message + '：' + r.password, 'success');
    $('nu-username').value = ''; $('nu-display').value = ''; $('nu-password').value = '';
    $('nu-group').value = '';
    loadAdmin();
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------- 事件绑定 ---------- */
function bind() {
  $('login-form').addEventListener('submit', (e) => { e.preventDefault(); doLogin(); });
  $('logout-btn').addEventListener('click', doLogout);
  document.querySelectorAll('.tab').forEach((b) =>
    b.addEventListener('click', () => showTab(b.dataset.tab)));
  $('book-open-btn').addEventListener('click', () => openModal(null));
  $('f-query').addEventListener('click', loadMeetings);
  $('f-reset').addEventListener('click', () => {
    ['f-from', 'f-to', 'f-creator', 'f-subject'].forEach((id) => $(id).value = '');
    $('f-status').value = '';
    $('f-sort').value = 'created_at'; $('f-dir').value = 'desc';
    loadMeetings();
  });
  $('m-subject').addEventListener('input', updateEndPreview);
  $('m-start').addEventListener('input', updateEndPreview);
  $('m-duration').addEventListener('input', updateEndPreview);
  $('modal-close').addEventListener('click', closeModal);
  $('modal-cancel').addEventListener('click', closeModal);
  $('modal-form').addEventListener('submit', (e) => { e.preventDefault(); submitModal(); });
  $('modal-mask').addEventListener('click', (e) => { if (e.target.id === 'modal-mask') closeModal(); });
  $('nu-create').addEventListener('click', createUser);

  // 用户编辑弹窗
  $('user-close').addEventListener('click', closeUserModal);
  $('user-cancel').addEventListener('click', closeUserModal);
  $('user-form').addEventListener('submit', (e) => { e.preventDefault(); submitUserModal(); });
  $('user-mask').addEventListener('click', (e) => { if (e.target.id === 'user-mask') closeUserModal(); });

  // 批量删除
  $('batch-del-btn').addEventListener('click', batchDelete);
  $('batch-purge-btn').addEventListener('click', purgeMeetings);
  $('check-all').addEventListener('change', (e) => {
    const on = e.target.checked;
    document.querySelectorAll('.row-check').forEach((cb) => {
      cb.checked = on;
      const id = Number(cb.dataset.id);
      if (on) state.selected.add(id); else state.selected.delete(id);
    });
    updateBatchBtn();
  });

  // 会议操作按钮
  $('meeting-tbody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const id = btn.dataset.id;
    if (btn.dataset.act === 'edit') {
      const data = await (async () => { try { return await api('/api/meetings'); } catch (err) { return { items: [] }; } })();
      const m = (data.items || []).find((r) => r.id === Number(id));
      if (m) openModal(m);
    } else if (btn.dataset.act === 'invite') {
      try {
        const r = await api('/api/meetings/' + id + '/copy-invite', { method: 'POST' });
        const ok = await copyText(r.text);
        toast(ok ? '邀请信息已复制到剪贴板' : '复制失败，请手动选择：\n' + r.text, ok ? 'success' : 'error');
      } catch (err) { toast(err.message, 'error'); }
    } else if (btn.dataset.act === 'del') {
      const row = (state.meetings || []).find((r) => String(r.id) === String(id));
      const isCancelled = row && row.status === 'cancelled';
      const msg = isCancelled
        ? '该会议已取消，确定从列表中彻底移除记录？此操作不可恢复。'
        : '确定删除该会议？若已预订成功将同时取消腾讯会议侧的预订。';
      if (!confirm(msg)) return;
      try {
        await api('/api/meetings/' + id, { method: 'DELETE' });
        toast(isCancelled ? '记录已移除' : '会议已删除', 'success');
        loadMeetings();
      } catch (err) { toast(err.message, 'error'); }
    } else if (btn.dataset.act === 'retry') {
      try { await api('/api/meetings/' + id + '/retry', { method: 'POST' }); toast('已加入重试队列', 'success'); loadMeetings(); }
      catch (err) { toast(err.message, 'error'); }
    }
  });

  // 用户操作按钮
  $('user-tbody').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-uact]');
    if (!btn) return;
    const id = btn.dataset.id;
    try {
      if (btn.dataset.uact === 'edit') {
        const u = (state.users || []).find((x) => String(x.id) === String(id));
        if (u) openUserModal(u);
      } else if (btn.dataset.uact === 'pwd') {
        const r = await api('/api/users/' + id + '/reset-password', { method: 'POST', body: { password: '' } });
        toast(r.message + '：' + r.password, 'success');
        loadAdmin();
      } else if (btn.dataset.uact === 'role') {
        const newRole = btn.dataset.role === 'admin' ? 'user' : 'admin';
        await api('/api/users/' + id, { method: 'PUT', body: { role: newRole } });
        toast('角色已更新', 'success');
        loadAdmin();
      } else if (btn.dataset.uact === 'del') {
        if (!confirm(`确定删除用户 ${btn.dataset.username}？其历史会议将保留但创建者显示为"已删除用户"。`)) return;
        await api('/api/users/' + id, { method: 'DELETE' });
        toast('用户已删除', 'success');
        loadAdmin();
      }
    } catch (err) { toast(err.message, 'error'); }
  });

  // 统计周期切换
  document.querySelectorAll('#tab-stats .seg-btn[data-period]').forEach((b) =>
    b.addEventListener('click', () => {
      state.statPeriod = b.dataset.period;
      document.querySelectorAll('#tab-stats .seg-btn[data-period]').forEach((x) =>
        x.classList.toggle('active', x === b));
      loadStats();
    }));

  // 统计范围切换（管理员：我的 / 全平台）
  document.querySelectorAll('#stat-scope-seg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      state.statScope = b.dataset.scope;
      document.querySelectorAll('#stat-scope-seg .seg-btn').forEach((x) =>
        x.classList.toggle('active', x === b));
      $('stat-title').textContent = state.statScope === 'all'
        ? '全平台会议统计（仅已预订成功）'
        : '我的会议统计（仅已预订成功）';
      loadStats();
    }));
}

/* ---------- 启动 ---------- */
(async function init() {
  bind();
  if (state.token) {
    try {
      const d = await api('/api/me');
      state.user = d.user;
      enterApp();
      return;
    } catch (e) { /* token 无效 */ }
    localStorage.removeItem('mt_token');
    state.token = '';
  }
  showView('login');
})();
