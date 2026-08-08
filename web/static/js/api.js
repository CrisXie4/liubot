/* ═══════════════════════════════════════════════════
   api.js — fetch 封装 + 认证 + token 管理
   ═══════════════════════════════════════════════════ */

let TOKEN = localStorage.xie_token || '';
const setToken = t => {
  TOKEN = t || '';
  t ? localStorage.setItem('xie_token', t) : localStorage.removeItem('xie_token');
};

// 401 reload 节流：短时间内多次 401 只触发一次 reload，避免死循环
let _last401Reload = 0;

async function api(path, opts = {}) {
  const h = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (TOKEN) h.Authorization = 'Bearer ' + TOKEN;
  const r = await fetch('/api' + path, { ...opts, headers: h });
  if (r.status === 401) {
    setToken('');
    const now = Date.now();
    if (now - _last401Reload > 2000) {
      _last401Reload = now;
      location.reload();
    }
    throw new Error('未登录');
  }
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const e = await r.json();
      msg = e.detail || msg;
    } catch {}
    throw new Error(msg);
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r;
}