/* ═══════════════════════════════════════════════════
   llm.js — LLM 审核
   ═══════════════════════════════════════════════════ */

async function llmLoad() {
  try {
    const s = await api('/llm-mod/stats');
    const el = $('llm-stats');
    if (el) {
      el.innerHTML =
        statCard('bar-chart', s.total || 0, '审核总数', '') +
        statCard('stop', s.llm_confirmed || 0, '违规', '') +
        statCard('check-circle', s.llm_rejected || 0, '正常', '') +
        statCard('thunderbolt', (s.avg_latency_ms || 0) + 'ms', '平均耗时', '');
    }
    const c = await api('/llm-mod/config');
    const tog = $('llm-tog'); if (tog) tog.textContent = c.enabled ? '🟢 已开启' : '⚪ 已关闭';
  } catch {}
  await fillTable('#llm-log-tbl', async () => (await api('/llm-mod/logs?limit=30')).items,
    l => `<tr>
      <td class="muted">${esc(l.time || '')}</td>
      <td class="wrap">${esc(trunc(l.message, 50))}</td>
      <td class="num">${((l.kw_confidence || 0) * 100).toFixed(0)}%</td>
      <td>${tag(l.llm_result === 'violation' ? '违规' : l.llm_result === 'skipped' ? '跳过' : '正常', l.llm_result === 'violation' ? 'no' : 'ok')}</td>
      <td class="num">${((l.llm_confidence || 0) * 100).toFixed(0)}%</td>
      <td>${tag(l.final_result === 'violation' ? '违规' : '正常', l.final_result === 'violation' ? 'no' : 'ok')}</td>
      <td class="num">${l.latency_ms || 0}ms</td>
    </tr>`,
    'thunderbolt', '还没有 AI 审核记录');
}

async function llmToggle() {
  try {
    const d = await api('/llm-mod/toggle', { method: 'POST' });
    toast(d.enabled ? '已开启' : '已关闭');
    llmLoad();
  } catch (e) { toast(e.message, 'err'); }
}

async function llmTest() {
  const msg = $('llm-test').value.trim();
  if (!msg) { toast('请输入消息', 'err'); return; }
  const r = $('llm-res');
  r.style.display = 'block';
  r.textContent = '审核中…';
  try {
    const d = await api('/llm-mod/test', { method: 'POST', body: JSON.stringify({ message: msg }) });
    r.textContent = `${d.is_violation ? '⚠️ 违规' : '✅ 正常'} 置信: ${(d.confidence * 100).toFixed(1)}%\n来源: ${d.source}  AI 置信: ${(d.llm_confidence * 100).toFixed(1)}%\n理由: ${d.llm_reason || '-'}\n建议: ${d.suggested_action || '-'}\n命中词: ${(d.keywords_matched || []).join(', ') || '无'}\n耗时: ${d.latency_ms}ms`;
  } catch (e) { r.textContent = '错误: ' + e.message; }
}