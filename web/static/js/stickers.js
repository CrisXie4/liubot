/* ═══════════════════════════════════════════════════
   stickers.js — 表情库
   ═══════════════════════════════════════════════════ */

async function stLoad() {
  try {
    const s = await api('/stickers/stats');
    const el = $('st-stats');
    if (el) {
      el.innerHTML =
        statCard('cloud', s.total || 0, '收藏总数', '') +
        statCard('smile', s.by_type?.face || 0, 'QQ 表情', '') +
        statCard('picture', s.by_type?.image || 0, '图片', '') +
        statCard('fire', s.most_used?.[0]?.use_count || 0, '最高使用', '');
    }
  } catch {}
  await fillTable('#st-tbl', async () => (await api('/stickers?limit=100')).items,
    s => `<tr>
      <td class="num">#${s.id}</td>
      <td>${tag({ face: 'QQ', mface: '商城', image: '图片' }[s.sticker_type] || s.sticker_type, 'info')}</td>
      <td><span class="mono">${esc(trunc(s.cq_code || '', 50))}</span></td>
      <td>${esc((s.tags || []).slice(0, 3).join(', ') || '-')}</td>
      <td class="num">${s.group_id || '全局'}</td>
      <td class="num">${s.use_count || 0}</td>
      <td><button class="btn xs danger" onclick="stDel(${s.id})">删除</button></td>
    </tr>`,
    '🎨', '表情库为空');
}

function stAdd() {
  $('st-cq').value = ''; $('st-tags').value = '';
  openModal('m-st');
}

async function stSave() {
  try {
    await api('/stickers', {
      method: 'POST',
      body: JSON.stringify({
        sticker_type: $('st-type').value,
        cq_code: $('st-cq').value,
        tags: $('st-tags').value.split(',').map(s => s.trim()).filter(Boolean),
      }),
    });
    closeModal('m-st'); stLoad(); toast('已添加');
  } catch (e) { toast(e.message, 'err'); }
}

async function stDel(id) {
  if (!confirm('删除 #' + id + '？')) return;
  try { await api('/stickers/' + id, { method: 'DELETE' }); stLoad(); toast('已删除'); }
  catch (e) { toast(e.message, 'err'); }
}