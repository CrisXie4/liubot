/* ═══════════════════════════════════════════════════
   automod.js — 自动审核
   ═══════════════════════════════════════════════════ */

async function amLoad() {
  try {
    const s = await api('/automod/status');
    const el = $('am-stats');
    if (el) {
      el.innerHTML =
        statCard('bar-chart', s.violation_count || 0, '违规样本', '') +
        statCard('check-circle', s.normal_samples || 0, '正常样本', '') +
        statCard('bulb', s.ml_model_ready ? '✓' : '—', '识别引擎', s.ml_model_ready ? 'ok' : 'muted') +
        statCard('aim', ((s.auto_mod_threshold || 0) * 100).toFixed(0) + '%', '阈值', '');
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
    'audit', '还没有审核记录');
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
    r.textContent = `违规词库得分: ${(d.keyword_score.confidence * 100).toFixed(1)}%  命中词: ${(d.keyword_score.keywords_matched || []).join(',') || '无'}\n识别引擎: ${d.ml_score ? `${(d.ml_score.confidence * 100).toFixed(1)}% / 违规概率 ${(d.ml_score.violation_prob * 100).toFixed(1)}%` : '暂无经验，先去「审核样本」应用几条'}`;
  } catch (e) { r.textContent = '错误: ' + e.message; }
}