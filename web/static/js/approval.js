/* ═══════════════════════════════════════════════════
   approval.js — 待审批
   ═══════════════════════════════════════════════════ */

async function apLoad() {
  await fillTable('#ap-tbl', async () => {
    try { return (await api('/guardian/pending')).items || []; } catch { return []; }
  },
    p => `<tr>
      <td class="num">${p.id}</td>
      <td>${tag(p.action === 'kick' ? '踢人' : '拉黑', p.action === 'kick' ? 'warn' : 'no')}</td>
      <td>${esc(p.user_name || '')} <span class="num muted">(${p.user_qq || ''})</span></td>
      <td class="num">${p.group_id || ''}</td>
      <td class="wrap">${esc(trunc(p.reason || '', 40))}</td>
      <td class="wrap">${esc(trunc(p.message || '', 40))}</td>
      <td><div class="btn-row">
        <button class="btn xs pri" onclick="apOK('${attr(p.id)}','approve')">同意</button>
        <button class="btn xs danger" onclick="apOK('${attr(p.id)}','reject')">拒绝</button>
      </div></td>
    </tr>`,
    'audit', '没有待审批');
}

async function apOK(id, act) {
  try {
    await api('/guardian/' + act + '/' + id, { method: 'POST' });
    toast('已处理');
    apLoad();
  } catch (e) { toast(e.message, 'err'); }
}