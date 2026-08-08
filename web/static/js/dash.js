/* ═══════════════════════════════════════════════════
   dash.js — 仪表盘
   ═══════════════════════════════════════════════════ */

let _cKw, _cHr;

async function dashLoad() {
  try {
    // 主开关
    try {
      const s = await api('/bot/master-switches');
      const b = $('ms-bot');
      const a = $('ms-ai');
      if (b) b.checked = !!s.bot_enabled;
      if (a) a.checked = !!s.ai_enabled;
    } catch {}

    const d = await api('/dashboard');
    const v = d.violations_total || 0;
    const ac = d.violations_auto || 0;
    const mc = v - ac;
    const statsEl = $('dash-stats');
    if (statsEl) {
      statsEl.innerHTML =
        statCard('🚫', v, '违规总数', 'red') +
        statCard('🤖', ac, '自动识别', '') +
        statCard('👤', mc, '手动标记', '') +
        statCard('⛔', d.blacklist_count || 0, '黑名单', '') +
        statCard('🎯', ((d.threshold || 0) * 100).toFixed(0) + '%', '阈值', '') +
        statCard('🧠', d.ml_ready ? '✓' : '—', 'ML 模型', d.ml_ready ? 'ok' : 'muted');
    }

    const kws = d.top_keywords || [];
    if (_cKw) _cKw.destroy();
    const kwCanvas = $('c-kw');
    if (kwCanvas && typeof Chart !== 'undefined') {
      _cKw = new Chart(kwCanvas, {
        type: 'bar',
        data: {
          labels: kws.map(k => k.keyword),
          datasets: [{ data: kws.map(k => k.count), backgroundColor: 'rgba(47,111,87,.7)', borderRadius: 4 }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: { grid: { color: 'rgba(0,0,0,.04)' }, ticks: { color: '#8d8f99' } },
            x: { ticks: { color: '#8d8f99' } },
          },
        },
      });
    }

    const hrs = d.hour_distribution || [];
    const hd = new Array(24).fill(0);
    hrs.forEach(h => { if (h.hour >= 0 && h.hour < 24) hd[h.hour] = h.cnt });
    if (_cHr) _cHr.destroy();
    const hrCanvas = $('c-hr');
    if (hrCanvas && typeof Chart !== 'undefined') {
      _cHr = new Chart(hrCanvas, {
        type: 'line',
        data: {
          labels: Array.from({ length: 24 }, (_, i) => i + '时'),
          datasets: [{
            data: hd, borderColor: '#2f6f57',
            backgroundColor: 'rgba(47,111,87,.08)', fill: true,
            tension: 0.35, pointRadius: 3, pointBackgroundColor: '#2f6f57', borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            y: { grid: { color: 'rgba(0,0,0,.04)' }, ticks: { color: '#8d8f99' } },
            x: { ticks: { color: '#8d8f99' } },
          },
        },
      });
    }

    const rec = d.recent_violations || [];
    const rEl = $('dash-recent');
    if (!rEl) return;
    if (!rec.length) {
      rEl.innerHTML = '<div class="empty-state"><div class="ico">✅</div>没有最近违规</div>';
      return;
    }
    rEl.innerHTML =
      '<table class="tbl"><thead><tr><th>时间</th><th class="num">群</th><th class="num">QQ</th><th>消息</th><th>来源</th></tr></thead><tbody>' +
      rec
        .map(
          r =>
            `<tr><td class="muted">${esc(ts(r.recorded_at))}</td><td class="num">${r.group_id}</td><td class="num">${r.violator_qq}</td><td class="wrap">${esc(trunc(r.raw_message, 80))}</td><td>${tag(r.source === 'auto' ? '自动' : '手动', r.source === 'auto' ? 'info' : 'ok')}</td></tr>`
        )
        .join('') +
      '</tbody></table>';
  } catch (e) {
    console.error('dash:', e);
  }
}