/* ═══════════════════════════════════════════════════
   welcome.js — 欢迎
   ═══════════════════════════════════════════════════ */

async function wcLoad() {
  try {
    const cfg = await api('/welcome/config');
    const gid = $('wc-gid').value;
    let on = true, msg = cfg.default_message || '';
    if (gid && cfg.group_overrides && cfg.group_overrides[gid]) {
      const ov = cfg.group_overrides[gid];
      on = ov.enabled !== false;
      msg = ov.message || msg;
    } else if (cfg.enabled !== undefined) on = cfg.enabled;
    $('wc-on').checked = on;
    $('wc-msg').value = msg;
  } catch {}
}

async function wcSave() {
  const gid = $('wc-gid').value;
  if (!gid) { toast('请先选群', 'err'); return; }
  try {
    await api('/welcome/group/' + gid, {
      method: 'POST',
      body: JSON.stringify({ enabled: $('wc-on').checked, message: $('wc-msg').value }),
    });
    toast('已保存');
  } catch (e) { toast(e.message, 'err'); }
}