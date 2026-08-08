/* ═══════════════════════════════════════════════════
   model.js — 模型设置
   ═══════════════════════════════════════════════════ */

async function mLoad() {
  try {
    const d = await api('/model-settings');
    $('m-url').value = d.api_url || '';
    $('m-key').value = '';
    $('m-key').placeholder = d.api_key_masked ? '已设置 (' + d.api_key_masked + ')' : 'sk-...';
    $('m-model').value = d.model || '';
    $('m-temp').value = d.temperature || 0.8;
    $('m-max').value = d.max_tokens || 800;
    $('m-ctx').value = d.context_size || 20;
    $('m-search').checked = d.web_search_enabled !== false;
  } catch (e) { toast('加载失败', 'err'); }
}

async function mSave() {
  try {
    const body = {
      api_url: $('m-url').value,
      model: $('m-model').value,
      temperature: num('m-temp', 0.8),
      max_tokens: int('m-max', 800),
      context_size: int('m-ctx', 20),
      web_search_enabled: $('m-search').checked,
    };
    if ($('m-key').value) body.api_key = $('m-key').value;
    await api('/model-settings', { method: 'POST', body: JSON.stringify(body) });
    toast('已保存');
  } catch (e) { toast(e.message, 'err'); }
}