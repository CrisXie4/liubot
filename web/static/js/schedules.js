/* ═══════════════════════════════════════════════════
   schedules.js — 定时消息
   ═══════════════════════════════════════════════════ */

async function schLoad() {
  await fillTable('#sch-tbl', async () => {
    const d = await api('/schedules');
    return (d.items || []).map((s, i) => ({ ...s, _idx: i }));
  },
    s => `<tr>
      <td class="num">${s.group_id || ''}</td>
      <td class="wrap">${esc(trunc(s.message || '', 80))}</td>
      <td class="mono">${esc(s.cron || s.interval || '')}</td>
      <td><button class="btn xs danger" onclick="schDel(${s._idx})">删除</button></td>
    </tr>`,
    'clock-circle', '还没有定时任务');
}

function schAdd() {
  $('sch-msg').value = '';
  $('sch-cron').value = '';
  openModal('m-sch');
}

async function schSave() {
  try {
    await api('/schedules', {
      method: 'POST',
      body: JSON.stringify({
        group_id: +$('sch-gid').value || 0,
        message: $('sch-msg').value,
        cron: $('sch-cron').value,
      }),
    });
    closeModal('m-sch');
    schLoad();
    toast('已添加');
  } catch (e) { toast(e.message, 'err'); }
}

async function schDel(i) {
  if (!confirm('删除？')) return;
  try {
    await api('/schedules/' + encodeURIComponent(i), { method: 'DELETE' });
    schLoad();
    toast('已删除');
  } catch (e) { toast(e.message, 'err'); }
}