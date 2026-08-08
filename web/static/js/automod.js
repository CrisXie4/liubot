/* ═══════════════════════════════════════════════════
   automod.js — 自动审核
   ═══════════════════════════════════════════════════ */

async function amLoad() {
  try {
    const s = await api('/automod/status');
    const el = $('am-stats');
    if (el) {
      el.innerHTML =
        statCard('📊', s.violation_count || 0, '违规样本', '') +
        statCard('✅', s.normal_samples || 0, '正常样本', '') +
        statCard('🧠', s.ml_model_ready ? '✓' : '—', 'ML 模型', s.ml_model_ready ? 'ok' : 'muted') +
        statCard('🎯', ((s.auto_mod_threshold || 0) * 100).toFixed(0) + '%', '阈值', '');
    }
    const tog = $('am-tog'); if (tog) tog.textContent = s.auto_mod_enabled ? '🟢 已开启' : '⚪ 已关闭';
  } catch (e) { toast('加载失败', 'err'); }
  await fillTable('#am-log-tbl', async () => (await api('/automod/log?limit=40')).items,
    l => `<tr>
      <td class="muted">${esc(ts(l.action_taken_at))}</td>
      <td class="wrap">${esc(trunc(l.raw_message, 60))}</td>
      <td class="num">${((l.confidence || 0) * 100).toFixed(0)}%</td>
      <td>${esc((l.keywords_matched || []).slice(0, 4).join(', '))}</td>
      <td>${tag(l.action, l.action === 'recalled' ? 'no' : l.action === 'logged' ? 'warn' : 'info')}</td>
    </tr>`,
    '📋', '还没有审核记录');
}

async function amToggle() {
  try {
    const d = await api('/automod/toggle', { method: 'POST' });
    toast(d.enabled ? '已开启' : '已关闭');
    amLoad();
  } catch (e) { toast(e.message, 'err'); }
}

async function amTest() {
  const msg = $('am-test').value.trim();
  if (!msg) { toast('请输入消息', 'err'); return; }
  const r = $('am-res');
  r.style.display = 'block';
  r.textContent = '评分中…';
  try {
    const d = await api('/automod/test?message=' + encodeURIComponent(msg));
    r.textContent = `关键词: ${(d.keyword_score.confidence * 100).toFixed(1)}%  命中: ${(d.keyword_score.keywords_matched || []).join(',') || '无'}\nML: ${d.ml_score ? `${(d.ml_score.confidence * 100).toFixed(1)}% / 违规 ${(d.ml_score.violation_prob * 100).toFixed(1)}%` : '未训练'}`;
  } catch (e) { r.textContent = '错误: ' + e.message; }
}