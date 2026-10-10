/* ═══════════════════════════════════════════════════
   groups.js — 群成员
   ═══════════════════════════════════════════════════ */

let _gmAll = [];

async function gmLoad() {
  const gid = $('gm-gid').value;
  if (!gid) {
    empty('#gm-tbl', 'team', '请选择群');
    _gmAll = [];
    $('gm-info').textContent = '';
    return;
  }
  loading('#gm-tbl');
  try {
    const d = await api('/groups/' + gid + '/members');
    _gmAll = d.items || d.data || [];
    $('gm-info').textContent = '共 ' + _gmAll.length + ' 人';
    gmRender();
  } catch (e) {
    errorState('#gm-tbl', e);
  }
}

function gmRender() {
  const gid = $('gm-gid').value;
  if (!gid || !_gmAll.length) {
    empty('#gm-tbl', 'team', '请选择群');
    return;
  }
  const q = ($('gm-search')?.value || '').toLowerCase();
  const list = q
    ? _gmAll.filter(m =>
        String(m.user_id).includes(q) ||
        (m.nickname || '').toLowerCase().includes(q) ||
        (m.card || '').toLowerCase().includes(q)
      )
    : _gmAll;
  if (!list.length) {
    empty('#gm-tbl', 'search', '没有匹配的成员');
    return;
  }
  tbody('#gm-tbl').innerHTML = list.map(m => {
    const role = m.role === 'owner' ? '<span class="tag warn">群主</span>' : m.role === 'admin' ? '<span class="tag info">管理</span>' : '<span class="muted">成员</span>';
    const vc = m.violation_count > 0 ? `<span class="num" style="color:var(--red)">${m.violation_count}</span>` : '<span class="num muted">0</span>';
    const bl = m.is_blacklisted ? '<span class="tag no">是</span>' : '<span class="tag ok">否</span>';
    const nm = attr(m.card || m.nickname || m.user_id);
    return `<tr>
      <td class="num">${m.user_id}</td>
      <td>${esc(m.nickname || '-')}</td>
      <td>${esc(m.card || '-')}</td>
      <td>${role}</td>
      <td>${vc}</td>
      <td>${bl}</td>
      <td><div class="btn-row">
        <button class="btn xs" data-gm="mute" data-d="600">🔇10m</button>
        <button class="btn xs" data-gm="mute" data-d="3600">🔇1h</button>
        <button class="btn xs" data-gm="unmute">🔊</button>
        <button class="btn xs danger" data-gm="kick" data-n="${nm}">👢</button>
        <button class="btn xs danger" data-gm="blk" data-n="${nm}">⛔</button>
      </div></td>
    </tr>`;
  }).join('');
}

document.addEventListener('click', async e => {
  const b = e.target.closest('#gm-tbl button[data-gm]');
  if (!b) return;
  const gid = $('gm-gid').value;
  if (!gid) return;
  const tr = b.closest('tr');
  const uid = tr?.querySelector('.num')?.textContent;
  if (!uid) return;
  try {
    if (b.dataset.gm === 'mute') {
      await api('/groups/' + gid + '/mute', { method: 'POST', body: JSON.stringify({ user_id: +uid, duration: +b.dataset.d }) });
      toast('已禁言');
    } else if (b.dataset.gm === 'unmute') {
      await api('/groups/' + gid + '/unmute', { method: 'POST', body: JSON.stringify({ user_id: +uid }) });
      toast('已解禁');
    } else if (b.dataset.gm === 'kick') {
      if (!confirm('踢出 ' + b.dataset.n + '？')) return;
      await api('/groups/' + gid + '/kick', { method: 'POST', body: JSON.stringify({ user_id: +uid }) });
      toast('已踢出');
      gmLoad();
    } else if (b.dataset.gm === 'blk') {
      if (!confirm('拉黑并踢出 ' + b.dataset.n + '？')) return;
      await api('/groups/' + gid + '/blacklist-member', { method: 'POST', body: JSON.stringify({ user_id: +uid, user_name: b.dataset.n, reason: 'Web 操作' }) });
      toast('已拉黑+踢出');
      gmLoad();
    }
  } catch (err) { toast(err.message, 'err'); }
});