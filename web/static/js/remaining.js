/* ═══════════════════════════════════════════════════
   remaining.js — 影响力/关键词/训练/学习/发送/签到/积分/设置/环境/日志
   把小页面集中在一起，避免 11 个独立文件
   ═══════════════════════════════════════════════════ */

/* ─── INFLUENCE ─── */
async function infLoad() {
  const gid = $('inf-gid').value;
  if (!gid) { empty('#inf-tbl', '🌟', '请选择群'); return; }
  await fillTable('#inf-tbl', async () => (await api('/influence/' + gid + '?limit=30')).items,
    (e, i) => `<tr>
      <td class="num"><strong>#${i + 1}</strong></td>
      <td>${esc(e.user_name || e.user_qq)}</td>
      <td class="num">${(e.total_score || 0).toFixed(0)}</td>
    </tr>`,
    '🌟', '还没有影响力数据');
}

/* ─── KEYWORDS ─── */
async function kwLoad() {
  const q = $('kw-search').value;
  await fillTable('#kw-tbl', async () => (await api('/keywords?search=' + encodeURIComponent(q) + '&limit=200')).items,
    k => `<tr>
      <td>${esc(k.keyword)}</td>
      <td class="num">${k.weight}</td>
      <td><button class="btn xs danger" onclick="kwDel('${attr(k.keyword)}')">删除</button></td>
    </tr>`,
    q ? '🔍 没有匹配的关键词' : '🔑 关键词库为空');
}

function kwAdd() { $('kw-w').value = ''; $('kw-v').value = 3; openModal('m-kw'); }

async function kwSave() {
  const w = $('kw-w').value.trim();
  if (!w) { toast('请输入', 'err'); return; }
  try {
    await api('/keywords', { method: 'POST', body: JSON.stringify({ keyword: w, weight: num('kw-v', 3) }) });
    closeModal('m-kw'); kwLoad(); toast('已添加');
  } catch (e) { toast(e.message, 'err'); }
}

async function kwDel(k) {
  if (!confirm('删除 ' + k + '？')) return;
  try { await api('/keywords/' + encodeURIComponent(k), { method: 'DELETE' }); kwLoad(); toast('已删除'); }
  catch (e) { toast(e.message, 'err'); }
}

async function kwCleanup() {
  if (!confirm('清理低权重(<0.5)？')) return;
  try {
    const r = await api('/keywords/cleanup', { method: 'POST' });
    kwLoad(); toast('已清理 ' + (r.removed || 0));
  } catch (e) { toast(e.message, 'err'); }
}

/* ─── TRAINING ─── */
let _trPage = 1;

async function trLoad(p = 1) {
  _trPage = p;
  loading('#tr-tbl');
  try {
    const [s, d] = await Promise.all([api('/training-data/stats'), api('/training-data?page=' + p + '&page_size=30')]);
    const el = $('tr-stats');
    if (el) {
      el.innerHTML =
        statCard('📊', s.total || 0, '总数', '') +
        statCard('🚫', s.violation_count || 0, '违规', '') +
        statCard('✅', s.normal_count || 0, '正常', '') +
        statCard('⏳', s.unapplied_count || 0, '待应用', '');
    }
    const items = d.items || [];
    if (!items.length) { empty('#tr-tbl', '🧪', '还没有训练数据'); $('tr-pager').innerHTML = ''; return; }
    tbody('#tr-tbl').innerHTML = items.map(r => `<tr>
      <td class="num">${r.id}</td>
      <td class="wrap">${esc(trunc(r.message_text, 80))}</td>
      <td>${tag(r.label === 'violation' ? '违规' : '正常', r.label === 'violation' ? 'no' : 'ok')}</td>
      <td>${esc(r.category || '')}</td>
      <td>${r.applied ? '<span class="tag ok">✓</span>' : '<span class="muted">—</span>'}</td>
      <td><button class="btn xs danger" onclick="trDel(${r.id})">删除</button></td>
    </tr>`).join('');
    const pages = Math.ceil((s.total || 0) / 30);
    $('tr-pager').innerHTML = pages > 1
      ? Array.from({ length: pages }, (_, i) => `<button class="btn xs ${i + 1 === p ? 'pri' : ''}" onclick="trLoad(${i + 1})">${i + 1}</button>`).join('')
      : '';
  } catch (e) { errorState('#tr-tbl', e); }
}

function trAdd() { $('tr-msg').value = ''; $('tr-cat').value = ''; openModal('m-tr'); }

async function trSave() {
  try {
    await api('/training-data', {
      method: 'POST',
      body: JSON.stringify({
        message_text: $('tr-msg').value,
        label: $('tr-label').value,
        category: $('tr-cat').value,
        keywords: '',
      }),
    });
    closeModal('m-tr'); trLoad(_trPage); toast('已添加');
  } catch (e) { toast(e.message, 'err'); }
}

async function trDel(id) {
  if (!confirm('删除 #' + id + '？')) return;
  try { await api('/training-data/' + id, { method: 'DELETE' }); trLoad(); }
  catch (e) { toast(e.message, 'err'); }
}

async function trApply() {
  if (!confirm('应用到学习引擎？')) return;
  try {
    const r = await api('/training-data/apply', { method: 'POST' });
    toast(r.message || '已应用'); trLoad();
  } catch (e) { toast(e.message, 'err'); }
}

/* ─── LEARN ─── */
async function learnLoad() {
  try {
    const s = await api('/learner/status');
    const el = $('learn-stats');
    if (el) {
      el.innerHTML =
        statCard('🔑', s.learned_keywords_count || 0, '关键词', '') +
        statCard('📐', s.learned_patterns_count || 0, '模式', '') +
        statCard('⚠️', s.false_positives_count || 0, '误判', '') +
        statCard('📈', '阶段 ' + s.phase, '学习', '');
    }
    tbody('#learn-tbl').innerHTML = (s.top_keywords || []).map(k => `<tr>
      <td>${esc(k.keyword)}</td>
      <td class="num">${k.weight}</td>
    </tr>`).join('') || '<tr><td colspan="2" class="muted" style="padding:20px;text-align:center">无数据</td></tr>';
  } catch (e) { toast('加载失败', 'err'); }
}

/* ─── SEND ─── */
async function sendMsg() {
  const gid = $('send-gid').value, msg = $('send-msg').value;
  if (!gid || !msg) { toast('请填写完整', 'err'); return; }
  try {
    await api('/bot/send?group_id=' + gid + '&message=' + encodeURIComponent(msg), { method: 'POST' });
    toast('已发送'); $('send-msg').value = '';
  } catch (e) { toast(e.message, 'err'); }
}

/* ─── CHECKIN / POINTS ─── */
async function ckLoad() {
  const gs = await loadGroups(); fillGroupSelects(gs);
  const sel = $('ck-gid');
  if (!sel.value && gs.length) sel.value = gs[0].group_id;
  sel.onchange = ckLoad;
  const gid = sel.value;
  if (!gid) { empty('#ck-tbl', '📅', '请选择群'); return; }
  await fillTable('#ck-tbl', async () => { try { return (await api('/checkin/leaderboard/' + gid)).items || []; } catch { return []; } },
    (e, i) => `<tr>
      <td><strong>#${i + 1}</strong></td>
      <td>${esc(e.user_name || e.user_qq)}</td>
      <td class="num">${e.streak || 0}</td>
      <td class="num">${e.total || 0}</td>
    </tr>`,
    '📅', '还没有签到');
}

async function ptLoad() {
  const gs = await loadGroups(); fillGroupSelects(gs);
  const sel = $('pt-gid');
  if (!sel.value && gs.length) sel.value = gs[0].group_id;
  sel.onchange = ptLoad;
  const gid = sel.value;
  if (!gid) { empty('#pt-tbl', '💰', '请选择群'); return; }
  await fillTable('#pt-tbl', async () => { try { return (await api('/points/leaderboard/' + gid)).items || []; } catch { return []; } },
    (e, i) => `<tr>
      <td><strong>#${i + 1}</strong></td>
      <td>${esc(e.user_name || e.user_qq)}</td>
      <td class="num">${e.points || 0}</td>
    </tr>`,
    '💰', '还没有积分');
}

/* ─── BOT SETTINGS ─── */
async function bsLoad() {
  try {
    const s = await api('/bot/settings');
    $('bs-name').value = s.bot_name || '';
    $('bs-ctx').value = s.chat_context_size || 10;
    $('bs-invite').checked = s.auto_accept_invite || false;
    $('bs-automod').checked = s.auto_mod_enabled !== false;
    $('bs-bot-qq').value = s.bot_qq || '';
    $('bs-owner-qq').value = s.owner_qq || '';
    const n = s.owner_notifications || {};
    $('bs-n-viol').checked = n.on_violation !== false;
    $('bs-n-high').checked = n.on_high_risk !== false;
    $('bs-n-kick').checked = n.on_kick_request !== false;
    $('bs-n-err').checked = n.on_error !== false;
    const a = s.auto_actions || {};
    $('bs-m-low').value = a.mute_duration_low || 600;
    $('bs-m-high').value = a.mute_duration_high || 3600;
    $('bs-a-mute').checked = a.auto_mute_enabled !== false;
    $('bs-a-recall').checked = a.auto_recall_enabled !== false;
  } catch (e) { toast('加载失败', 'err'); }
}

async function bsSave() {
  try {
    await api('/bot/settings', {
      method: 'POST',
      body: JSON.stringify({
        bot_name: $('bs-name').value,
        chat_context_size: int('bs-ctx', 10),
        auto_accept_invite: $('bs-invite').checked,
        auto_mod_enabled: $('bs-automod').checked,
        owner_notifications: {
          on_violation: $('bs-n-viol').checked,
          on_high_risk: $('bs-n-high').checked,
          on_kick_request: $('bs-n-kick').checked,
          on_error: $('bs-n-err').checked,
        },
        auto_actions: {
          mute_duration_low: int('bs-m-low', 600),
          mute_duration_high: int('bs-m-high', 3600),
          auto_mute_enabled: $('bs-a-mute').checked,
          auto_recall_enabled: $('bs-a-recall').checked,
        },
      }),
    });
    toast('已保存');
  } catch (e) { toast(e.message, 'err'); }
}

/* ─── ENV ─── */
let _envData = {};

async function envLoad() {
  try {
    const d = await api('/env'); _envData = d;
    const editable = d._editable || [], sensitive = d._sensitive || [];
    const keys = Object.keys(d).filter(k => !k.startsWith('_')).sort();
    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px">';
    for (const k of keys) {
      const v = d[k] || '';
      const isSensitive = sensitive.includes(k);
      html += `<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--bg3);border-radius:var(--r-xs)">
        <span style="font-size:11.5px;font-weight:600;color:var(--accent);min-width:140px;font-family:var(--mono)">${esc(k)}</span>`;
      if (isSensitive) {
        html += `<span class="mono muted" style="flex:1">🔒 已加密</span>
               <button class="btn xs" onclick="envEditSensitive('${attr(k)}')">修改</button>`;
      } else {
        html += `<input value="${esc(v)}" data-env="${attr(k)}" class="mono" style="flex:1;padding:4px 8px;background:var(--bg2);font-size:11.5px">`;
      }
      html += '</div>';
    }
    html += '</div><div style="margin-top:14px"><button class="btn pri" onclick="envSave()">保存配置</button> <span class="muted" style="font-size:11px;margin-left:10px">修改后需重启 Bot 生效</span></div>';
    $('env-body').innerHTML = html;
  } catch (e) { $('env-body').innerHTML = '<div class="err-state">' + esc(e.message) + '</div>'; }
}

async function envSave() {
  const data = {};
  document.querySelectorAll('[data-env]').forEach(i => { data[i.dataset.env] = i.value.trim(); });
  if (!Object.keys(data).length) { toast('无修改', 'warn'); return; }
  try {
    const r = await api('/env', { method: 'POST', body: JSON.stringify(data) });
    if (r.ok) { toast('已保存 ' + r.updated.length + ' 项'); envLoad(); } else toast('保存失败', 'err');
  } catch (e) { toast(e.message, 'err'); }
}

async function envEditSensitive(k) {
  const pw = prompt('请输入当前 Web 密码以修改 ' + k + ':');
  if (!pw) return;
  const nv = prompt('新的 ' + k + ' (空=保留):');
  if (nv === null) return;
  try {
    const r = await api('/env/reload', { method: 'POST', body: JSON.stringify({ key: k, current_password: pw, value: nv }) });
    if (r.ok) { toast('已更新'); envLoad(); } else toast('修改失败', 'err');
  } catch (e) { toast(e.message, 'err'); }
}

/* ─── LOGS ─── */
let _logTimer = null;

async function logLoad() {
  const lines = $('log-lines')?.value || '200';
  const level = $('log-level')?.value || '';
  try {
    const d = await api('/logs?lines=' + lines + '&level=' + encodeURIComponent(level));
    if (!d.ok) { const o = $('log-out'); if (o) o.textContent = '加载失败: ' + (d.error || ''); return; }
    const o = $('log-out');
    if (!o) return;
    o.innerHTML = (d.lines || []).map(l => {
      let cls = 'muted';
      if (l.includes('| ERROR')) cls = 'err';
      else if (l.includes('| WARNING')) cls = 'warn';
      else if (l.includes('| INFO')) cls = 'info';
      return `<span style="display:block" class="${cls}">${esc(l)}</span>`;
    }).join('');
    o.scrollTop = o.scrollHeight;
  } catch (e) { const o = $('log-out'); if (o) o.textContent = '错误: ' + e.message; }
}

function logToggleAuto() {
  if ($('log-auto')?.checked) _logTimer = setInterval(logLoad, 5000);
  else { if (_logTimer) clearInterval(_logTimer); _logTimer = null; }
}