/* ═══════════════════════════════════════════════════
   modes.js — 群模式
   ═══════════════════════════════════════════════════ */

async function modesLoad() {
  loading('#modes-tbl');
  try {
    const [gs, ms] = await Promise.all([loadGroups(), api('/group-modes')]);
    if (!gs.length) {
      empty('#modes-tbl', '⚖️', '尚未加入任何群');
      return;
    }
    const modes = ms.modes || {};
    tbody('#modes-tbl').innerHTML = gs.map(g => {
      const m = modes[String(g.group_id)] || 'auto';
      const name = { admin: '管理', friend: '群友', auto: '自动' }[m] || m;
      return `<tr>
        <td class="num">${g.group_id}</td>
        <td>${esc(g.group_name || '')}</td>
        <td>${g.bot_is_admin ? '<span class="tag ok">是</span>' : '<span class="tag muted">否</span>'}</td>
        <td>${tag(name, m === 'admin' ? 'warn' : m === 'friend' ? 'ok' : 'info')}</td>
        <td><div class="btn-row">
          <button class="btn xs pri" onclick="modeSet(${g.group_id},'admin')">管理</button>
          <button class="btn xs" onclick="modeSet(${g.group_id},'friend')">群友</button>
          <button class="btn xs" onclick="modeSet(${g.group_id},'auto')">自动</button>
        </div></td>
      </tr>`;
    }).join('');
  } catch (e) { errorState('#modes-tbl', e); }
}

async function modeSet(g, m) {
  try {
    await api('/group-modes', { method: 'POST', body: JSON.stringify({ group_id: g, mode: m }) });
    toast('已切换');
    modesLoad();
  } catch (e) { toast(e.message, 'err'); }
}