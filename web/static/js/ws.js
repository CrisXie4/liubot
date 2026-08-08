/* ═══════════════════════════════════════════════════
   ws.js — WebSocket + Bot 在线状态轮询
   ═══════════════════════════════════════════════════ */

let ws = null;
let wsDirty = false;

function connectWS() {
  if (!TOKEN) return;
  try {
    ws = new WebSocket(
      (location.protocol === 'https:' ? 'wss://' : 'ws://') +
        location.host +
        '/api/ws?token=' +
        encodeURIComponent(TOKEN)
    );
    ws.onmessage = () => {
      wsDirty = true;
      const dashActive = document.querySelector('.section[data-sec="dash"].active');
      if (dashActive && document.visibilityState === 'visible' && typeof dashLoad === 'function') {
        dashLoad().catch(() => {});
      }
    };
    ws.onclose = () => {
      ws = null;
      if (TOKEN) setTimeout(connectWS, 5000);
    };
    ws.onerror = () => {
      try { ws.close(); } catch {}
    };
  } catch {}
}

async function pollStatus() {
  try {
    const s = await api('/bot/status');
    const on = !!s.connected;
    ['bot-dot', 'bot-dot2'].forEach(id => {
      const el = $(id);
      if (el) el.classList.toggle('on', on);
    });
    ['bot-state', 'bot-state2'].forEach(id => {
      const el = $(id);
      if (el) el.textContent = on ? '在线' : '离线';
    });
  } catch {}
}