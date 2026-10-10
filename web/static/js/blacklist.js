/* ═══════════════════════════════════════════════════
   blacklist.js — 黑名单
   ═══════════════════════════════════════════════════ */

async function blLoad() {
  await fillTable('#bl-tbl', async () => (await api('/blacklist')).items,
    b => `<tr>
      <td class="num">${b.user_qq}</td>
      <td>${esc(b.user_name || '')}</td>
      <td class="num">${b.group_id || '全局'}</td>
      <td class="wrap">${esc(trunc(b.reason || '', 40))}</td>
      <td class="muted">${esc(ts(b.blacklisted_at))}</td>
      <td><button class="btn xs danger" onclick="blDel(${b.user_qq},${b.group_id || 0})">删除</button></td>
    </tr>`,
    'user-delete', '黑名单为空');
}

function blAdd() {
  $('bl-qq').value = ''; $('bl-name').value = ''; $('bl-reason').value = '';
  openModal('m-bl');
}

async function blAddConfirm() {
  const qq = $('bl-qq').value;
  if (!qq) { toast('请输入 QQ', 'err'); return; }
  try {
    await api('/blacklist', {
      method: 'POST',
      body: JSON.stringify({
        user_qq: +qq,
        user_name: $('bl-name').value,
        group_id: +$('bl-gid').value || 0,
        reason: $('bl-reason').value,
      }),
    });
    closeModal('m-bl');
    blLoad();
    toast('已拉黑');
  } catch (e) { toast(e.message, 'err'); }
}

async function blDel(qq, gid) {
  if (!confirm('移除 ' + qq + '？')) return;
  try {
    await api('/blacklist/' + qq + '?group_id=' + gid, { method: 'DELETE' });
    blLoad();
    toast('已移除');
  } catch (e) { toast(e.message, 'err'); }
}