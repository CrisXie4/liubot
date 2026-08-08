/* ═══════════════════════════════════════════════════
   humanize.js — 拟人化设置
   ═══════════════════════════════════════════════════ */

async function hzLoad() {
  try {
    const s = await api('/bot/settings');
    const r = s.reply_policy || {};
    const q = s.quiet_hours || {};
    $('hz-gap-msgs').value = r.quote_gap_msgs ?? 3;
    $('hz-gap-secs').value = r.quote_gap_seconds ?? 180;
    $('hz-max-quotes').value = r.max_quotes ?? 1;
    $('hz-at-absent').checked = r.at_when_absent !== false;
    $('hz-quiet-on').checked = q.enabled !== false;
    $('hz-quiet-start').value = q.start ?? 1;
    $('hz-quiet-end').value = q.end ?? 7;
  } catch (e) { toast('加载失败', 'err'); }

  try {
    const d = await api('/consciousness/time-facts');
    let txt = d.time_facts || '(不可用)';
    if (d.quiet_now) txt += '\n\n▶ 当前处于静默时段，不会主动发消息';
    $('hz-time-facts').textContent = txt;
  } catch { $('hz-time-facts').textContent = '(读取失败)'; }
}

async function hzSave() {
  try {
    await api('/bot/settings', {
      method: 'POST',
      body: JSON.stringify({
        reply_policy: {
          quote_gap_msgs: int('hz-gap-msgs', 3),
          quote_gap_seconds: int('hz-gap-secs', 180),
          max_quotes: int('hz-max-quotes', 1),
          at_when_absent: $('hz-at-absent').checked,
        },
        quiet_hours: {
          enabled: $('hz-quiet-on').checked,
          start: int('hz-quiet-start', 1),
          end: int('hz-quiet-end', 7),
        },
      }),
    });
    toast('已保存，立即生效');
    hzLoad();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}