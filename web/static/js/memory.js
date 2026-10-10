/* ═══════════════════════════════════════════════════
   memory.js — 记忆
   ═══════════════════════════════════════════════════ */

async function memLoad() {
  try {
    const s = await api('/memory/stats');
    const el = $('mem-stats');
    if (el) {
      el.innerHTML =
        statCard('user', s.total_profiles || 0, '用户画像', '') +
        statCard('message', s.total_memories || 0, '对话记忆', '') +
        statCard('tags', s.total_facts || 0, '知识碎片', '');
    }
  } catch {}
  await fillTable('#mem-tbl', async () => {
    try { return (await api('/memory/memories?limit=30')).items || []; } catch { return []; }
  },
    m => `<tr>
      <td class="muted">${esc(ts(m.created_at))}</td>
      <td class="num">${m.group_id}</td>
      <td>${esc(m.topic || '')}</td>
      <td class="wrap">${esc(trunc(m.summary, 140))}</td>
    </tr>`,
    '💭', '还没有记忆');
}