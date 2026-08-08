/* ═══════════════════════════════════════════════════
   personality.js — 人设
   ═══════════════════════════════════════════════════ */

async function pLoad() {
  try {
    const d = await api('/personality');
    $('p-name').value = d.name || '';
    $('p-personality').value = d.personality || '活泼';
    $('p-style').value = d.reply_style || 'conversational';
    $('p-traits').value = (d.traits || []).join(', ');
    $('p-temp').value = d.temperature || 0.8;
    $('p-auto').checked = d.auto_chat || false;
    $('p-prob').value = d.auto_chat_probability || 0.05;
    $('p-prompt').value = d.custom_prompt || '';
  } catch (e) { toast('加载失败', 'err'); }
}

async function pSave() {
  try {
    await api('/personality', {
      method: 'POST',
      body: JSON.stringify({
        name: $('p-name').value,
        personality: $('p-personality').value,
        reply_style: $('p-style').value,
        traits: $('p-traits').value.split(',').map(s => s.trim()).filter(Boolean),
        temperature: num('p-temp', 0.8),
        auto_chat: $('p-auto').checked,
        auto_chat_probability: num('p-prob', 0.05),
        custom_prompt: $('p-prompt').value,
      }),
    });
    toast('已保存');
  } catch (e) { toast(e.message, 'err'); }
}