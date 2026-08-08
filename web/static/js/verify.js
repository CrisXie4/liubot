/* ═══════════════════════════════════════════════════
   verify.js — 加群验证
   ═══════════════════════════════════════════════════ */

async function verifyLoad() {
  try {
    const d = await api('/verify/history?limit=100');
    const s = d.stats || {};
    const total = (s.approved || 0) + (s.rejected || 0) + (s.timeout || 0) || 1;

    // 4 张统计卡（用 stat-row 类，横向 grid 布局）
    const statsEl = $('verify-stats');
    if (statsEl) {
      statsEl.innerHTML =
        statCard('✅', s.approved || 0, '通过', 'ok') +
        statCard('❌', s.rejected || 0, '拒绝', 'warn') +
        statCard('⏰', s.timeout || 0, '超时', 'no') +
        statCard('📈', ((s.approved || 0) / total * 100).toFixed(0) + '%', '通过率', '');
    }

    await fillTable('#verify-tbl', async () => d.items || [],
      v => `<tr>
        <td class="num">${ts(v.time || '')}</td>
        <td>${esc(v.user_name || '')}</td>
        <td class="num">${v.user_id || ''}</td>
        <td class="num">${v.group_id || ''}</td>
        <td>${v.result === 'approved' ? tag('通过', 'ok') : v.result === 'rejected' ? tag('拒绝', 'warn') : tag('超时', 'no')}</td>
        <td class="wrap">${esc(trunc(v.question || '', 40))}</td>
        <td class="wrap num">${esc(trunc(v.answer || v.reason || '', 30))}</td>
      </tr>`,
      '🔑', '暂无验证记录');
  } catch (e) {
    toast('加载验证记录失败: ' + e.message, 'err');
  }
}