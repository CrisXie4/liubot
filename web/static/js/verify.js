/* ═══════════════════════════════════════════════════
   verify.js — 加群验证（出题 + 网站认证）
   ═══════════════════════════════════════════════════ */

let _verifyRatio = 50;

async function verifyLoad() {
  await Promise.all([loadVerifyRatio(), loadVerifyHistory()]);
}

async function loadVerifyRatio() {
  try {
    const d = await api('/verify/ratio');
    _verifyRatio = d.ratio;
    const slider = $('verify-ratio-slider');
    if (slider) slider.value = _verifyRatio;
    const val = $('verify-ratio-val');
    if (val) val.textContent = _verifyRatio + '%';
  } catch (e) { /* 静默 */ }
}

async function saveVerifyRatio() {
  const slider = $('verify-ratio-slider');
  if (!slider) return;
  const ratio = parseInt(slider.value);
  const msg = $('verify-ratio-msg');
  try {
    await api('/verify/ratio', { method: 'POST', body: JSON.stringify({ ratio }) });
    _verifyRatio = ratio;
    $('verify-ratio-val').textContent = ratio + '%';
    if (msg) { msg.textContent = '已保存 ✓'; msg.className = 'tag ok'; }
    setTimeout(() => { if (msg) msg.textContent = ''; }, 2000);
  } catch (e) {
    if (msg) { msg.textContent = '保存失败: ' + e.message; msg.className = 'tag no'; }
  }
}

async function loadVerifyHistory() {
  try {
    const d = await api('/verify/history?limit=100');
    const s = d.stats || {};
    const total = (s.approved || 0) + (s.rejected || 0) + (s.timeout || 0)
                + (s.approved_web || 0) + (s.rejected_web || 0) + (s.timeout_web || 0) || 1;
    const allApproved = (s.approved || 0) + (s.approved_web || 0);
    const allRejected = (s.rejected || 0) + (s.rejected_web || 0);
    const allTimeout = (s.timeout || 0) + (s.timeout_web || 0);
    const webTotal = (s.approved_web || 0) + (s.rejected_web || 0) + (s.timeout_web || 0);

    const statsEl = $('verify-stats');
    if (statsEl) {
      statsEl.innerHTML =
        statCard('✅', allApproved, '通过', 'ok') +
        statCard('❌', allRejected, '拒绝', 'warn') +
        statCard('⏰', allTimeout, '超时', 'no') +
        statCard('🌐', webTotal, '网站模式', '') +
        statCard('📈', (allApproved / total * 100).toFixed(0) + '%', '通过率', '');
    }

    await fillTable('#verify-tbl', async () => d.items || [],
      v => {
        const isWeb = (v.method === 'web') || (v.result || '').endsWith('_web');
        const methodTag = isWeb ? tag('网站', '') : tag('出题', 'ok');
        const resultLabel = (v.result || '').startsWith('approved') ? '通过'
                          : (v.result || '').startsWith('rejected') ? '拒绝'
                          : (v.result || '').startsWith('timeout') ? '超时'
                          : (v.result === 'manual_approved' ? '放行' : v.result);
        const resultClass = (v.result || '').startsWith('approved') ? 'ok'
                          : (v.result || '').startsWith('rejected') ? 'warn' : 'no';
        const attempts = (v.attempts != null) ? v.attempts : '-';
        return `<tr>
          <td class="num">${ts(v.time || '')}</td>
          <td>${esc(v.user_name || '')}</td>
          <td class="num">${v.user_id || ''}</td>
          <td class="num">${v.group_id || ''}</td>
          <td>${methodTag}</td>
          <td>${tag(resultLabel, resultClass)}</td>
          <td class="wrap">${esc(trunc(v.question || '', 40))}</td>
          <td class="wrap num">${esc(trunc(v.answer || v.reason || '', 30))}</td>
          <td class="num">${attempts}</td>
        </tr>`;
      },
      '🔑', '暂无验证记录');
  } catch (e) {
    toast('加载验证记录失败: ' + e.message, 'err');
  }
}
