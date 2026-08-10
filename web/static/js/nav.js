/* ═══════════════════════════════════════════════════
   nav.js — 路由 + 动态加载 HTML 片段 + 切 tab
   ═══════════════════════════════════════════════════ */

const TABS = [
  'dash', 'humanize', 'brain',
  'groups', 'modes', 'welcome', 'schedules',
  'violations', 'blacklist', 'automod', 'llm', 'approval', 'verify',
  'personality', 'group-personality', 'model', 'memory', 'stickers', 'influence',
  'keywords', 'training', 'learn',
  'send', 'checkin', 'points', 'bot-settings',
  'env', 'logs',
];

const TITLES = {
  dash: '仪表盘',
  humanize: '拟人化',
  brain: 'AI 状态',
  groups: '群成员',
  modes: '群模式',
  welcome: '欢迎',
  schedules: '定时消息',
  violations: '违规记录',
  blacklist: '黑名单',
  automod: '自动审核',
  llm: 'LLM 审核',
  approval: '待审批',
  verify: '加群验证',
  personality: '人设',
  'group-personality': '群人设',
  model: '模型',
  memory: '记忆',
  stickers: '表情',
  influence: '影响力榜',
  keywords: '关键词',
  training: '训练',
  learn: '学习',
  send: '发消息',
  checkin: '签到榜',
  points: '积分榜',
  'bot-settings': '设置',
  env: '环境变量',
  logs: '日志',
};

/* ─── 页面 HTML 缓存 ─── */
const _pageCache = {};

/* ─── 动态加载页面 ─── */
async function loadPage(tab) {
  if (_pageCache[tab]) return _pageCache[tab];
  try {
    const r = await fetch(`/static/pages/${tab}.html`, { cache: 'no-cache' });
    if (!r.ok) throw new Error('加载失败');
    const html = await r.text();
    _pageCache[tab] = html;
    return html;
  } catch (e) {
    console.error('loadPage:', tab, e);
    return '<div class="err-state">⚠️ 页面加载失败</div>';
  }
}

/* ─── Loader 注册表 ─── */
const loaders = {
  dash: () => typeof dashLoad === 'function' && dashLoad(),
  humanize: () => typeof hzLoad === 'function' && hzLoad(),
  brain: () => typeof csLoad === 'function' && csLoad(),
  groups: () => typeof gmLoad === 'function' && gmLoad(),
  modes: () => typeof modesLoad === 'function' && modesLoad(),
  welcome: () => typeof wcLoad === 'function' && wcLoad(),
  schedules: () => typeof schLoad === 'function' && schLoad(),
  violations: () => typeof vLoad === 'function' && vLoad(1),
  blacklist: () => typeof blLoad === 'function' && blLoad(),
  automod: () => typeof amLoad === 'function' && amLoad(),
  llm: () => typeof llmLoad === 'function' && llmLoad(),
  approval: () => typeof apLoad === 'function' && apLoad(),
  verify: () => typeof verifyLoad === 'function' && verifyLoad(),
  personality: () => typeof pLoad === 'function' && pLoad(),
  'group-personality': () => typeof gpLoad === 'function' && gpLoad(),
  model: () => typeof mLoad === 'function' && mLoad(),
  memory: () => typeof memLoad === 'function' && memLoad(),
  stickers: () => typeof stLoad === 'function' && stLoad(),
  influence: () => typeof infLoad === 'function' && infLoad(),
  keywords: () => typeof kwLoad === 'function' && kwLoad(),
  training: () => typeof trLoad === 'function' && trLoad(1),
  learn: () => typeof learnLoad === 'function' && learnLoad(),
  send: () => {},
  checkin: () => typeof ckLoad === 'function' && ckLoad(),
  points: () => typeof ptLoad === 'function' && ptLoad(),
  'bot-settings': () => typeof bsLoad === 'function' && bsLoad(),
  env: () => typeof envLoad === 'function' && envLoad(),
  logs: () => typeof logLoad === 'function' && logLoad(),
};

/* ─── 切 tab ─── */
async function navTo(tab) {
  if (!TABS.includes(tab)) return;

  // 更新侧边栏激活态
  document.querySelectorAll('#nav a').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));

  // 隐藏所有 section
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));

  // 找到或创建目标 section
  let sec = document.querySelector(`.section[data-sec="${tab}"]`);
  if (!sec) {
    sec = document.createElement('section');
    sec.className = 'section';
    sec.dataset.sec = tab;
    document.getElementById('content-body').appendChild(sec);
  }

  // 加载 HTML（缓存）
  if (!sec.dataset.loaded) {
    sec.innerHTML = '<div class="empty-state"><div class="ico"><span class="spinner"></span></div>加载中…</div>';
    const html = await loadPage(tab);
    sec.innerHTML = html;
    sec.dataset.loaded = '1';
  }

  sec.classList.add('active');
  $('page-title').textContent = TITLES[tab] || '';

  // 仪表盘特殊处理
  if (tab === 'dash') wsDirty = false;

  // 触发 loader
  try {
    if (loaders[tab]) loaders[tab]();
  } catch (e) {
    console.error('loader[' + tab + ']:', e);
  }

  // 仪表盘可见性 → 启停自动刷新
  if (tab === 'dash') startDashAuto();
  else stopDashAuto();
}

/* ─── 移动端抽屉 ─── */
function toggleMob() {
  $('sb').classList.toggle('open');
  $('mob-mask').classList.toggle('show');
}
function closeMob() {
  $('sb').classList.remove('open');
  $('mob-mask').classList.remove('show');
}

document.querySelectorAll('#nav a').forEach(a => {
  a.addEventListener('click', e => {
    e.preventDefault();
    const tab = a.dataset.tab;
    navTo(tab);
    if (window.innerWidth < 768) closeMob();
  });
});

/* ─── 屏幕尺寸变化 ─── */
let _lastW = window.innerWidth;
window.addEventListener('resize', () => {
  const w = window.innerWidth;
  if (w > _lastW || (_lastW < 768 && w >= 768)) closeMob();
  _lastW = w;
});

/* ─── 仪表盘自动刷新 ─── */
let _dashTimer = null;
function startDashAuto() {
  stopDashAuto();
  if (document.visibilityState !== 'visible') return;
  _dashTimer = setInterval(() => {
    if (!document.querySelector('.section[data-sec="dash"].active')) return;
    if (document.visibilityState !== 'visible') return;
    if (typeof dashLoad === 'function') dashLoad().catch(() => {});
  }, 30000);
}
function stopDashAuto() {
  if (_dashTimer) {
    clearInterval(_dashTimer);
    _dashTimer = null;
  }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (document.querySelector('.section[data-sec="dash"].active') && typeof dashLoad === 'function') {
      dashLoad().catch(() => {});
    }
    startDashAuto();
  } else stopDashAuto();
});

window.navTo = navTo;