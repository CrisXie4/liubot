/* ═══════════════════════════════════════════════════
   core.js — 通用工具函数 + 渲染辅助
   ═══════════════════════════════════════════════════ */

const $ = id => document.getElementById(id);

const esc = s => {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
};

const attr = s =>
  String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

const trunc = (s, n) => ((s || '').length > n ? (s || '').slice(0, n) + '…' : s || '');
const ts = s => (s ? s.toString().replace('T', ' ').slice(0, 19) : '');

const num = (id, d) => {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) ? v : d;
};
const int = (id, d) => {
  const v = parseInt($(id).value, 10);
  return Number.isFinite(v) ? v : d;
};

/* ─── Toast ─── */
function toast(msg, type = 'ok') {
  const c = $('toasts');
  if (!c) return;
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transition = 'opacity .2s';
    setTimeout(() => t.remove(), 250);
  }, 2400);
}

/* ─── 模态框 ─── */
function openModal(id) {
  $(id).classList.add('show');
}
function closeModal(id) {
  $(id).classList.remove('show');
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'));
});
document.addEventListener('click', e => {
  document.querySelectorAll('.modal-bg').forEach(o => {
    if (e.target === o) o.classList.remove('show');
  });
});

/* ─── 表格 helpers ─── */
function tbody(sel) {
  return document.querySelector(sel + ' tbody');
}
function colsOf(sel) {
  return document.querySelectorAll(sel + ' thead th').length || 1;
}
function tbl(sel, html) {
  const b = tbody(sel);
  if (b) b.innerHTML = `<tr><td colspan="${colsOf(sel)}">${html}</td></tr>`;
}
function loading(sel) {
  tbl(sel, '<div class="empty-state"><div class="ico"><span class="spinner"></span></div>加载中…</div>');
}
function empty(sel, ico, msg) {
  tbl(sel, `<div class="empty-state"><div class="ico">${ico || '📭'}</div>${esc(msg || '暂无数据')}</div>`);
}
function errorState(sel, e) {
  tbl(sel, `<div class="err-state">⚠️ 加载失败：${esc((e && e.message) || e)}</div>`);
}

async function fillTable(sel, fetcher, render, emptyIco, emptyMsg) {
  loading(sel);
  try {
    const items = await fetcher();
    if (!items || !items.length) {
      empty(sel, emptyIco, emptyMsg);
      return [];
    }
    tbody(sel).innerHTML = items.map(render).join('');
    return items;
  } catch (e) {
    errorState(sel, e);
    return [];
  }
}

/* ─── Tag 渲染 ─── */
function tag(text, kind) {
  return `<span class="tag ${kind || ''}">${esc(text)}</span>`;
}

/* ─── Stat 卡片渲染 ─── */
function statCard(ico, val, lab, cls) {
  return `<div class="stat"><span class="ico">${ico}</span><div class="val">${val}</div><div class="lab">${esc(lab)}</div></div>`;
}

/* ─── 群列表缓存 ─── */
let _groups = null;
async function loadGroups(force) {
  if (_groups && !force) return _groups;
  try {
    const d = await api('/groups' + (force ? '?force=1' : ''));
    _groups = d.items || [];
  } catch (e) {
    _groups = [];
  }
  return _groups;
}
function fillGroupSelects(groups) {
  document.querySelectorAll('.grp-sel').forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = '<option value="">-- 选择群 --</option>';
    groups.forEach(g => {
      const o = document.createElement('option');
      o.value = g.group_id;
      o.textContent = `${g.group_name || g.group_id} (${g.group_id})`;
      sel.appendChild(o);
    });
    if (cur) sel.value = cur;
  });
}

/* ─── 导出 ─── */
async function exportData(type, fmt) {
  try {
    const r = await fetch('/api/export/' + type + '?format=' + fmt + '&limit=50000', {
      headers: TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {},
    });
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${type}_${new Date().toISOString().slice(0, 10)}.${fmt}`;
    a.click();
    URL.revokeObjectURL(url);
    toast('导出成功');
  } catch (e) {
    toast('导出失败: ' + e.message, 'err');
  }
}

/* ─── 主开关切换（仪表盘） ─── */
async function setMasterSwitch(input) {
  const key = input.dataset.key;
  const val = input.checked;
  input.disabled = true;
  try {
    const r = await api(input.dataset.path, { method: 'POST', body: JSON.stringify({ [key]: val }) });
    toast(`${key === 'bot_enabled' ? 'Bot' : 'AI'} → ${val ? '开' : '关'}`);
    if (r) {
      const botInput = $('ms-bot');
      const aiInput = $('ms-ai');
      if (botInput) botInput.checked = !!r.bot_enabled;
      if (aiInput) aiInput.checked = !!r.ai_enabled;
    }
  } catch (e) {
    input.checked = !val;
    if (/Method Not Allowed|404|Not Found/i.test(e.message)) {
      toast('后端还没升级，请重启 Bot 后再试', 'err');
    } else {
      toast('切换失败: ' + e.message, 'err');
    }
  } finally {
    input.disabled = false;
  }
}

document.addEventListener('change', e => {
  if (e.target.dataset && e.target.dataset.action === 'toggle') setMasterSwitch(e.target);
});

/* ─── 登录 ─── */
async function doLogin() {
  const pw = $('login-pw').value;
  const err = $('login-err');
  if (!pw) {
    err.textContent = '请输入密码';
    return;
  }
  err.textContent = '';
  try {
    const d = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    }).then(r => r.json());
    if (d.ok) {
      setToken(d.token);
      $('login').style.display = 'none';
      $('app').style.display = 'flex';
      window.navTo('dash');
      toast('登录成功');
    } else err.textContent = d.detail || '密码错误';
  } catch {
    err.textContent = '网络错误';
  }
}

function doLogout() {
  if (!confirm('退出登录？')) return;
  setToken('');
  try {
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
  } catch {}
  stopDashAuto();
  if (window._pollTimer) clearInterval(window._pollTimer);
  location.reload();
}