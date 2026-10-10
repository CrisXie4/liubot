/* ═══════════════════════════════════════════════════
   dash.js — 仪表盘
   ═══════════════════════════════════════════════════ */

let _cKw, _cHr, _cTok, _cMsg;

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

    const [d, usage] = await Promise.all([
      api('/dashboard'),
      api('/usage/hourly?hours=48').catch(() => null),
    ]);
    const v = d.violations_total || 0;
    const ac = d.violations_auto || 0;
    const mc = v - ac;
    const statsEl = $('dash-stats');
    if (statsEl) {
      const sm = (usage && usage.summary) || {};
      const tokTotal = (sm.tokens_in || 0) + (sm.tokens_cached || 0) + (sm.tokens_out || 0);
      const fmtTok = n => (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : n || 0);
      statsEl.innerHTML =
        statCard('stop', v.toLocaleString(), '违规总数', 'red') +
        statCard('robot', ac.toLocaleString(), '自动识别', '') +
        statCard('user', mc.toLocaleString(), '手动标记', '') +
        statCard('user-delete', (d.blacklist_count || 0).toLocaleString(), '黑名单', '') +
        statCard('aim', ((d.threshold || 0) * 100).toFixed(0) + '%', '阈值', '') +
        statCard('bulb', d.ml_ready ? '✓' : '—', '识别引擎', d.ml_ready ? 'ok' : 'muted') +
        statCard('bar-chart', fmtTok(tokTotal), '24h Token 用量', '') +
        statCard('cloud', (sm.cache_hit_rate || 0) + '%', '24h 缓存命中率', (sm.cache_hit_rate || 0) >= 50 ? 'ok' : '') +
        statCard('message', fmtTok(sm.msgs_in || 0), '24h 消息数', '');
    }

    // ── 用量图表（每小时） ──
    if (usage && usage.items) {
      const items = usage.items;
      const labels = items.map(r => (r.hour || '').slice(5, 13) + '时');
      const tk = $('c-tok');
      if (_cTok) _cTok.destroy();
      if (tk && typeof Chart !== 'undefined') {
        _cTok = new Chart(tk, {
          type: 'bar',
          data: {
            labels,
            datasets: [
              { label: '缓存命中输入', data: items.map(r => r.tokens_cached), backgroundColor: 'rgba(47,111,87,.75)', stack: 't', borderWidth: 0 },
              { label: '未命中输入', data: items.map(r => r.tokens_in), backgroundColor: 'rgba(201,118,43,.75)', stack: 't', borderWidth: 0 },
              { label: '输出', data: items.map(r => r.tokens_out), backgroundColor: 'rgba(91,127,191,.8)', stack: 't', borderWidth: 0 },
            ],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#8d8f99', boxWidth: 12 } } },
            scales: {
              x: { stacked: true, ticks: { color: '#8d8f99', maxTicksLimit: 12 } },
              y: { stacked: true, grid: { color: 'rgba(0,0,0,.04)' }, ticks: { color: '#8d8f99' } },
            },
          },
        });
      }
      const ms = $('c-msg');
      if (_cMsg) _cMsg.destroy();
      if (ms && typeof Chart !== 'undefined') {
        _cMsg = new Chart(ms, {
          data: {
            labels,
            datasets: [
              { type: 'bar', label: '群消息数', data: items.map(r => r.msgs_in), backgroundColor: 'rgba(47,111,87,.55)', borderRadius: 3, yAxisID: 'y' },
              { type: 'line', label: 'AI 调用数', data: items.map(r => r.llm_calls), borderColor: '#c9762b', backgroundColor: 'rgba(201,118,43,.1)', tension: 0.35, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' },
            ],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#8d8f99', boxWidth: 12 } } },
            scales: {
              x: { ticks: { color: '#8d8f99', maxTicksLimit: 12 } },
              y: { position: 'left', grid: { color: 'rgba(0,0,0,.04)' }, ticks: { color: '#8d8f99' } },
              y1: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: '#8d8f99' } },
            },
          },
        });
      }
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