/* ═══════════════════════════════════════════════════
   brain.js — AI 状态（意识/情绪/日记/关系）
   ═══════════════════════════════════════════════════ */

async function csLoad() {
  try {
    const d = await api('/consciousness');
    const t = d.today || {};
    const statsEl = $('cs-stats');
    if (statsEl) {
      statsEl.innerHTML =
        statCard('smile', (d.mood_score_pct || 0) + '%', '心情', d.mood_score > 0.6 ? 'ok' : d.mood_score > 0.4 ? 'warn' : 'muted') +
        statCard('send', t.messages_sent || 0, '今日发言', '') +
        statCard('user', t.interactions || 0, '今日互动', '') +
        statCard('clock-circle', d.sleeping ? '是' : '否', '睡觉中', d.sleeping ? 'warn' : 'ok');
    }

    const emoKeys = ['joy', 'energy', 'calm', 'curiosity', 'social', 'satisfaction', 'boredom', 'loneliness', 'irritation'];
    const emoLabels = d.emotion_labels || {};
    const emotions = d.emotions || {};
    const emoEl = $('cs-emotions');
    if (emoEl) {
      emoEl.innerHTML =
        emoKeys
          .map(k => {
            const e = emotions[k];
            if (e === undefined) return '';
            const pct = typeof e === 'object' ? e.percent : Math.round(e * 100);
            let cls = '';
            if (['irritation', 'boredom', 'loneliness'].includes(k)) cls = pct > 50 ? 'red' : pct > 30 ? 'warn' : '';
            else cls = pct > 65 ? 'ok' : pct > 40 ? 'info' : '';
            return `<div class="kv"><span class="k">${esc(emoLabels[k] || k)}</span><span class="v">${pct}%</span><div class="bar" style="flex:1;min-width:120px"><div class="${cls}" style="width:${pct}%"></div></div></div>`;
          })
          .join('') || '<div class="muted">无数据</div>';
    }

    const todEl = $('cs-tod');
    if (todEl) todEl.textContent = d.time_of_day || '';

    const cir = d.circadian || {};
    const cirLabels = { energy: '精力', social: '社交欲', curiosity: '好奇心', boredom: '无聊', joy: '快乐', calm: '平静', irritation: '烦躁', loneliness: '孤独', satisfaction: '满足' };
    const cirEntries = Object.entries(cir);
    const cirEl = $('cs-circadian');
    if (cirEl) {
      if (!cirEntries.length) cirEl.innerHTML = '<div class="muted">无数据</div>';
      else {
        cirEl.innerHTML = cirEntries.map(([k, v]) => {
          const num = Number(v);
          const pct = Math.round(((num + 1) / 2) * 100);
          const cls = num > 0.1 ? 'ok' : num < -0.1 ? 'red' : 'warn';
          return `<div class="kv"><span class="k">${esc(cirLabels[k] || k)}</span><span class="v" style="color:var(--${num > 0 ? 'accent' : num < 0 ? 'red' : 'fg3'})">${num > 0 ? '+' : ''}${num.toFixed(2)}</span><div class="bar" style="flex:1;min-width:120px"><div class="${cls}" style="width:${pct}%"></div></div></div>`;
        }).join('');
      }
    }

    const diary = d.diary || [];
    const diaryEl = $('cs-diary');
    if (diaryEl) {
      if (!diary.length) diaryEl.innerHTML = '<div class="empty-state">还没有日记</div>';
      else {
        diaryEl.innerHTML = diary.slice(0, 15).map(e => `
          <div class="diary-item">
            <div class="meta">${esc(e.time || '')} <span class="tag muted">${esc(e.mood || '')}</span></div>
            <div class="body">${esc(e.thought || '')}</div>
          </div>`).join('');
      }
    }

    const rels = d.relationships || [];
    const relsEl = $('cs-rels');
    if (relsEl) {
      if (!rels.length) relsEl.innerHTML = '<div class="empty-state">还没有关系数据</div>';
      else {
        relsEl.innerHTML = '<table class="tbl"><thead><tr><th>名字</th><th>亲密度</th><th>印象</th><th class="num">互动</th><th>最近</th></tr></thead><tbody>' +
          rels.map(r => `<tr><td>${esc(r.name)}</td><td><div style="display:flex;align-items:center;gap:6px"><div class="bar" style="width:80px"><div class="${r.closeness > 0.6 ? 'ok' : r.closeness > 0.4 ? 'info' : ''}" style="width:${(r.closeness * 100).toFixed(0)}%"></div></div><span class="num">${(r.closeness * 100).toFixed(0)}%</span></div></td><td>${esc(r.impression || '')}</td><td class="num">${r.interactions || 0}</td><td class="muted">${esc(r.last || '')}</td></tr>`).join('') + '</tbody></table>';
      }
    }

    const traitLabels = { extroversion: '外向度', agreeableness: '宜人性', openness: '开放性', conscientiousness: '尽责性', emotional_stability: '情绪稳定性', playfulness: '调皮度', stubbornness: '倔强度' };
    const traits = d.personality || d.traits || {};
    const traitsEl = $('cs-traits');
    if (traitsEl) {
      traitsEl.innerHTML = Object.entries(traits).map(([k, v]) => {
        const pct = Math.round((Number(v) || 0) * 100);
        return `<div class="kv"><span class="k">${esc(traitLabels[k] || k)}</span><span class="v">${pct}%</span><div class="bar" style="flex:1;min-width:160px"><div style="width:${pct}%"></div></div></div>`;
      }).join('') || '<div class="muted">无数据</div>';
    }
  } catch (e) {
    console.error('csLoad:', e);
  }
}