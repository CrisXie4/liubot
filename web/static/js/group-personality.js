/* ═══════════════════════════════════════════════════
   group-personality.js — 群人设
   ═══════════════════════════════════════════════════ */

async function gpLoad() {
  await fillTable('#gp-tbl', async () => (await api('/group-personalities')).items,
    g => `<tr>
      <td class="num">${g.group_id}</td>
      <td>${g.bot_name ? esc(g.bot_name) : '<span class="muted">默认</span>'}</td>
      <td>${g.personality ? esc(g.personality) : '<span class="muted">默认</span>'}</td>
      <td class="num">${g.reply_frequency || 0.5}</td>
      <td>${esc((g.quirks || []).join(', ') || '-')}</td>
      <td><button class="btn xs" onclick="gpEdit(${g.group_id})">编辑</button></td>
    </tr>`,
    'user', '还没有群人设');
}

async function gpEdit(gid) {
  try {
    const d = await api('/group-personality/' + gid);
    $('gp-edit-gid').value = gid;
    $('gp-title').textContent = '编辑群级人设';
    $('gp-name').value = d.bot_name || '';
    $('gp-personality').value = d.personality || '';
    $('gp-freq').value = d.reply_frequency || 0.5;
    $('gp-emoji').value = d.emoji_style || 'mixed';
    $('gp-quirks').value = (d.quirks || []).join(', ');
    $('gp-del').style.display = 'inline-flex';
    openModal('m-gp');
  } catch (e) { toast(e.message, 'err'); }
}

function gpAdd() {
  $('gp-edit-gid').value = '';
  $('gp-title').textContent = '新增群级人设';
  ['gp-name', 'gp-personality', 'gp-quirks'].forEach(id => $(id).value = '');
  $('gp-freq').value = 0.5;
  $('gp-emoji').value = 'mixed';
  $('gp-del').style.display = 'none';
  openModal('m-gp');
}

async function gpSave() {
  const gid = $('gp-edit-gid').value;
  const body = {
    bot_name: $('gp-name').value,
    personality: $('gp-personality').value,
    reply_frequency: num('gp-freq', 0.5),
    emoji_style: $('gp-emoji').value,
    quirks: $('gp-quirks').value.split(',').map(s => s.trim()).filter(Boolean),
  };
  try {
    if (gid) await api('/group-personality/' + gid, { method: 'POST', body: JSON.stringify(body) });
    else { toast('需指定群号', 'err'); return; }
    closeModal('m-gp');
    gpLoad();
    toast('已保存');
  } catch (e) { toast(e.message, 'err'); }
}

async function gpDel() {
  const gid = $('gp-edit-gid').value;
  if (!gid || !confirm('删除？')) return;
  try {
    await api('/group-personality/' + gid, { method: 'DELETE' });
    closeModal('m-gp');
    gpLoad();
    toast('已删除');
  } catch (e) { toast(e.message, 'err'); }
}