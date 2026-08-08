/* ═══════════════════════════════════════════════════
   violations.js — 违规记录
   ═══════════════════════════════════════════════════ */

let _vPage = 1;

async function vLoad(p = 1) {
  _vPage = p;
  loading('#v-tbl');
  try {
    const src = $('v-src').value;
    const d = await api(`/violations?page=${p}&page_size=30` + (src ? `&source=${src}` : ''));
    const items = d.items || d.records || [];
    if (!items.length) {
      empty('#v-tbl', '✅', '没有违规记录');
      $('v-pager').innerHTML = '';
      const del = $('v-del'); if (del) del.disabled = true;
      return;
    }
    tbody('#v-tbl').innerHTML = items.map(r => `<tr>
      <td><input type="checkbox" class="v-cb" value="${r.id}"></td>
      <td class="num">${r.id}</td>
      <td class="muted">${esc(ts(r.recorded_at))}</td>
      <td class="num">${r.group_id}</td>
      <td class="num">${r.violator_qq}</td>
      <td class="wrap">${esc(trunc(r.raw_message, 80))}</td>
      <td>${esc((r.keywords || []).slice(0, 4).join(', '))}</td>
      <td class="num">${((r.confidence || 0) * 100).toFixed(0)}%</td>
      <td>${tag(r.source === 'auto' ? '自动' : '手动', r.source === 'auto' ? 'info' : 'ok')}</td>
    </tr>`).join('');
    const pages = Math.ceil((d.total || 0) / 30);
    $('v-pager').innerHTML = pages > 1
      ? Array.from({ length: pages }, (_, i) => `<button class="btn xs ${i + 1 === p ? 'pri' : ''}" onclick="vLoad(${i + 1})">${i + 1}</button>`).join('')
      : '';
    const del = $('v-del'); if (del) del.disabled = true;
    document.querySelectorAll('.v-cb').forEach(c => c.addEventListener('change', () => {
      const d = $('v-del'); if (d) d.disabled = !document.querySelector('.v-cb:checked');
    }));
  } catch (e) { errorState('#v-tbl', e); }
}

function vToggleAll(on) {
  document.querySelectorAll('.v-cb').forEach(c => c.checked = on);
  const d = $('v-del'); if (d) d.disabled = !on;
}

async function vDel() {
  const ids = [...document.querySelectorAll('.v-cb:checked')].map(c => c.value);
  if (!ids.length || !confirm('删除 ' + ids.length + ' 条？')) return;
  try {
    for (const id of ids) await api('/violations/' + id, { method: 'DELETE' });
    toast('已删除');
    vLoad(_vPage);
  } catch (e) { toast(e.message, 'err'); }
}