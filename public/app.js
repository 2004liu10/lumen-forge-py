/* Lumen Forge 前端：多画布 + 账号状态 + 任务进度 */

/** 连续触发时只跑最后一次（搜索框这类边输入边重渲染的场景必须有） */
function debounce(fn, wait = 200) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, wait);
  };
}

const state = {
  accounts: [],
  canvases: [],
  tasks: [],
  defaults: {},
  imageModels: [],
  imageTasks: [],
  imageDefaults: {},
  // 视频生成模块（独立于自用模块的一套模型 / 任务 / 输入图）
  videoModels: [],
  videoTasks: [],
  videoInputs: [],
  videoDefaults: {},
  // 素材模块：只有分类配置，素材列表是远端数据、由 materials.js 自己按需拉
  materialSources: [],
  // 节点画布：整张图（节点 + 连线 + 视图）与本地素材库，交给 nodecanvas.js 渲染
  nodeCanvases: [],
  nodeAssets: [],
  // 分镜（小剧场）：剧本列表 + 写剧本的模型配置，交给 storyboard.js 渲染
  storyboards: [],
  storyDefaults: {},
  // 「API 任务」弹窗的列表：独立按需拉取（含最久 400 条），不参与每 1.5 秒的状态轮询
  apiTasks: [],
  // Topaz Video AI 联动：是否探测到、可选档位、正在跑的提升任务（后端 /api/state 下发）
  topaz: { found: false, tiers: [], jobs: [] },
};
const PENDING = new Set(['queued', 'preparing', 'remote_queued', 'running', 'polishing', 'web_running']);
const STATUS_LABEL = {
  queued: '排队中',
  preparing: '准备中',
  remote_queued: '排队中',
  running: '生成中',
  polishing: '去水印中',
  web_running: '网页端运行中',
  success: '已完成',
  failed: '失败',
  canceled: '已取消',
};
// 图片生成任务的状态文案（注意后端用的是 cancelled，两个 l）
const IMAGE_STATUS_LABEL = {
  queued: '排队中',
  running: '生成中',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
const FILE_TYPES = new Set(['IMAGE', 'VIDEO', 'AUDIO']);
const GEN_COLLAPSED_KEY = 'rh.genPanel.collapsed';

// 图片生成面板的临时状态（不参与接口数据，仅避免刷新时打断用户操作）
// refs：图生图当前选中的参考图，本机上传的是后端登记的记录（带 id），
// 外网链接图是本地临时对象（带 url、无 id），只用来渲染缩略图与提交
// swapKey：点了哪张图的「替换」，选好文件后按它原位替换
const genState = { submitting: false, refs: [], swapKey: '' };

/* --------------------- 桌面端能力（内嵌登录 / 领取 RH 币） -------------------- */

// 只有 Electron 版才有这个桥；网页版（npm run web）为 null，相关按钮自动隐藏
const desktop = window.rhDesktop || null;
const desktopState = { enabled: true, time: '09:00', lastRunDate: '', running: false, login: {} };

/** 账号行上的领取状态：是否登录 + 最近一次领取结果 */
/**
 * 账号那行的登录态文案。
 * 登录态是 cookie，会过期 —— 以前只判断「有没有」，过期了也显示「已登录」，
 * 直到领币时余额没变才被发现。现在把过期时刻也摆出来，快到期就提前提醒。
 */
function claimText(account) {
  if (!desktop) return '';
  const info = desktopState.login?.[account.id];
  // 老版本这里给的是布尔，新版本是 { logged, expiresAt, stale }，两种都认
  const logged = typeof info === 'object' && info ? Boolean(info.logged) : Boolean(info);
  // 登录在哪个站要说出来：国际站账号被误判成国内站，正是以前「一直掉」的根源
  const siteLabel = info?.site === 'ai' ? '国际站 .ai' : info?.site === 'cn' ? '国内站 .cn' : '';
  let status;
  if (!logged) {
    status = '未登录（点「登录」）';
  } else if (info?.stale) {
    status = '登录态已过期，请重新登录';
  } else if (info?.expiresAt) {
    const days = Math.ceil((info.expiresAt - Date.now()) / 86400000);
    // 实测站点不会因为保活访问而重发 JWT，所以「还剩几天」是硬倒计时，到点只能重登
    status = days <= 7
      ? `网页已登录（登录态 ${days} 天后过期，到点只能重登）`
      : `网页已登录（登录态到 ${new Date(info.expiresAt).toLocaleDateString('zh-CN')}）`;
  } else {
    status = '网页已登录';
  }
  const parts = [siteLabel ? `${siteLabel} · ${status}` : status];
  // access token 快没了但 refresh 还有效 —— 实测站点**不会**拿 refresh 换新 access
  // （tools/rh-refresh-probe.mjs：只带 refresh 打首页，只回了 rh_locale/userLanguage），
  // 所以这里不能写「保活即可续上」，那是在骗人。保活只维持活跃度，真正到期只能重登。
  if (logged && !info?.stale && info?.needsRefresh) parts.push('访问令牌已过期，只能重新登录');
  if (account.claimNote) parts.push(`最近：${account.claimNote}`);
  return parts.join(' · ');
}

/** 登录态是不是快过期/已过期：给那行加个警示色 */
function claimTrouble(account) {
  if (!desktop) return false;
  const info = desktopState.login?.[account.id];
  return Boolean(info && typeof info === 'object' && (info.stale || (info.expiresAt && info.expiresAt - Date.now() < 7 * 86400000)));
}

/** 图生图参考图上限，由后端下发，避免前后端各写一份 */
const maxRefs = () => Number(state.imageDefaults.maxRefs) || 10;

const $ = (sel, root = document) => root.querySelector(sel);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function api(path, { method = 'GET', body, timeoutMs } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    // 本地服务偶发不响应时，请求不能永远挂着：refresh() 有个「正在刷新」的锁，
    // 一次卡死的请求会把锁一直占住，之后每 1.5 秒的刷新全被挡掉，界面就再也不更新了
    signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined,
  });
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (!res.ok) {
    throw new Error(json?.error || `请求失败（HTTP ${res.status}）`);
  }
  return json;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('err', isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

/* --------------------------- 应用内确认弹窗（替代 window.confirm） --------------------------- */
// Electron 里用原生 confirm()，弹窗关掉后窗口会整个点不动（渲染进程卡死），
// 所以统一换成应用内弹窗。返回 Promise<boolean>，用法和 confirm 一样，只是要 await。
let confirmResolve = null;
function confirmDialog(message, { title = '确认操作', okText = '确定', danger = true } = {}) {
  const modal = $('#modalConfirm');
  if (!modal) return Promise.resolve(window.confirm(message));
  if (confirmResolve) confirmResolve(false); // 上一个还没关就又弹，先把上一个按取消收尾
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $('#confirmTitle').textContent = title;
    $('#confirmMsg').textContent = message;
    const ok = $('#confirmOk');
    ok.textContent = okText;
    ok.classList.toggle('danger', danger);
    ok.classList.toggle('primary', !danger);
    modal.hidden = false;
    ok.focus();
  });
}
function resolveConfirm(result) {
  const modal = $('#modalConfirm');
  const resolve = confirmResolve;
  confirmResolve = null;
  if (modal) modal.hidden = true;
  if (resolve) resolve(result);
}

{
  const modal = $('#modalConfirm');
  if (modal) {
    modal.addEventListener('click', (event) => {
      if (event.target.closest('#confirmOk')) return resolveConfirm(true);
      if (
        event.target.closest('#confirmCancel') ||
        event.target.closest('[data-close]') ||
        event.target.id === 'modalConfirm'
      ) {
        return resolveConfirm(false);
      }
    });
    // Esc 在捕获阶段先拦下：按取消收尾并阻断冒泡。否则全局「关掉所有弹窗」那段会先把弹窗
    // 藏起来，却没 resolve 这个 Promise，调用方会一直 await 着。
    modal.addEventListener(
      'keydown',
      (event) => {
        if (event.key !== 'Escape') return;
        event.preventDefault();
        event.stopPropagation();
        resolveConfirm(false);
      },
      true,
    );
  }
}

function fmtSize(bytes) {
  if (bytes === undefined || bytes === null) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let n = Number(bytes);
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 || n >= 10 ? 0 : 1)}${units[i]}`;
}

// video.js 先于本文件加载，只能通过 window 拿这些工具函数（它用到时才取，不在加载时缓存）
// confirmDialog 一并挂出去：Electron 里原生 confirm() 会让窗口卡死（见上面那段注释），
// 其它模块要弹确认框必须用这个，不能退回 window.confirm。
window.RHUtil = {
  api,
  toast,
  esc,
  // 节点画布的聊天气泡要用（复制一条分镜回复）：桌面端 clipboard 偶发不可用，
  // 这个实现里带了 execCommand 兜底，别在外面再写第二套
  copyText,
  fmtSize,
  confirmDialog,
  refresh: () => refresh(),
  // 视频模块的「送去提升画质」要用到这两个：
  //   openTopaz   —— 提交完把提升画质弹窗拉起来看进度
  //   getTopazTier —— 读用户当前选的档位（没选过就是 'max'）
  // 用函数而不是直接暴露变量：topazUI 在本文件靠后处才用 const 初始化，
  // 直接取值可能在初始化前就被读到（TDZ 报错），套一层函数就没这个风险。
  openTopaz: () => openTopazModal(),
  getTopazTier: () => topazUI.tier,
  // 素材模块的「放大抖音」会把顶栏藏起来，顶栏高度变量要跟着重算 ——
  // 不然底下那些 sticky 元素（分类行、发现入口）会钉在错误的位置上。
  syncTopbar: () => syncTopbarHeight(),
};

function fmtDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m ? `${m}分${String(s).padStart(2, '0')}秒` : `${s}秒`;
}

/** 免费额度刷新倒计时（小时级，所以只显示到时/分） */
function fmtCountdown(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return `${h}时${String(m).padStart(2, '0')}分`;
  return `${m}分${String(total % 60).padStart(2, '0')}秒`;
}

/** 账号额度文案：正常显示剩余次数，用尽显示刷新倒计时 */
function quotaText(account) {
  if (!account.quotaLimit) return '额度不限';
  if (account.quotaLeft > 0) return `额度 ${account.quotaLeft}/${account.quotaLimit}`;
  const remain = (account.quotaResetAt || 0) - Date.now();
  return remain > 0 ? `额度用尽 · ${fmtCountdown(remain)}后刷新` : '额度用尽 · 即将刷新';
}

const isCooling = (account) => Boolean(account.quotaLimit) && account.quotaLeft <= 0;

/* -------------------------------- RH 币余额 -------------------------------- */

/** 余额查询成功但余额为 0，或压根没查出来，都值得提醒一下 */
function balanceTrouble(account) {
  const b = account.balance;
  if (!b) return false;
  if (b.error) return true;
  return b.remainCoins === 0;
}

function balanceText(account) {
  const b = account.balance;
  if (!b) return 'RH 币 --';
  if (b.remainCoins === null || b.remainCoins === undefined) return 'RH 币 --';
  return `RH 币 ${b.remainCoins}`;
}

/** 余额单元格的悬停详情：金额、账户类型、平台侧任务数、更新时间 */
function balanceTitle(account) {
  const b = account.balance;
  if (!b) return '还没有查询过余额';
  const parts = [];
  if (b.error) parts.push(`查询失败：${b.error}`);
  if (b.remainMoney !== null && b.remainMoney !== undefined) {
    parts.push(`钱包余额：${b.currency ? `${b.currency} ` : ''}${b.remainMoney}`);
  }
  if (b.apiType) parts.push(`账户类型：${b.apiType}`);
  if (b.currentTaskCounts !== null && b.currentTaskCounts !== undefined) {
    parts.push(`平台侧在跑：${b.currentTaskCounts} 个`);
  }
  if (b.updatedAt) parts.push(`更新于 ${new Date(b.updatedAt).toLocaleString()}`);
  return parts.join(' · ');
}

const isFileField = (field) => FILE_TYPES.has(String(field.fieldType || '').toUpperCase());

/** 按字段类型限制「选择文件」弹窗里能看到的文件，避免图片格子里选出视频 */
const ASSET_ACCEPT = { IMAGE: 'image/*', VIDEO: 'video/*', AUDIO: 'audio/*' };
const ASSET_LABEL = { IMAGE: '图片', VIDEO: '视频', AUDIO: '音频' };
const ASSET_EXT_RE = {
  IMAGE: /\.(png|jpe?g|webp|bmp|gif|tiff?)$/i,
  VIDEO: /\.(mp4|mov|webm|mkv|avi|m4v)$/i,
  AUDIO: /\.(mp3|wav|m4a|aac|flac|ogg)$/i,
};

/** 判断文件属于哪类素材：优先看 MIME，识别不出来再按扩展名兜底 */
function assetKindOf(file) {
  const mime = String(file?.type || '');
  if (mime.startsWith('image/')) return 'IMAGE';
  if (mime.startsWith('video/')) return 'VIDEO';
  if (mime.startsWith('audio/')) return 'AUDIO';
  const name = String(file?.name || '');
  return Object.keys(ASSET_EXT_RE).find((k) => ASSET_EXT_RE[k].test(name)) || '';
}

/* --------------------------------- 状态刷新 --------------------------------- */

let refreshing = false;
let loadErrorShown = false;

/**
 * 渲染步骤之间互相隔离。
 * refresh() 是一条长流水线（视频模块 → 账号条 → 账号表 → 模型表 → 生图面板 → 画布网格），
 * 以前任何一步抛异常都会把后面所有渲染一起带停 —— 表现就是「打开后什么都点不了、点了没反应」。
 * 现在某一步出错只记日志，后面的步骤照常渲染。
 */
function step(name, fn) {
  try {
    return fn();
  } catch (err) {
    console.error(`[refresh:${name}]`, err);
  }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const next = await api('/api/state', { timeoutMs: 15000 });
    state.accounts = next.accounts || [];
    updateAccountCounts();
    state.canvases = next.canvases || [];
    state.tasks = next.tasks || [];
    state.defaults = next.defaults || {};
    state.imageModels = next.imageModels || [];
    state.imageTasks = next.imageTasks || [];
    state.imageDefaults = next.imageDefaults || {};
    state.videoModels = next.videoModels || [];
    state.videoTasks = next.videoTasks || [];
    state.videoInputs = next.videoInputs || [];
    state.videoDefaults = next.videoDefaults || {};
    state.materialSources = next.materialSources || [];
    // 节点画布 + 本地素材库
    state.nodeCanvases = next.nodeCanvases || [];
    state.nodeAssets = next.nodeAssets || [];
    // 分镜（小剧场）：剧本列表 + 写剧本的模型配置
    state.storyboards = next.storyboards || [];
    state.storyDefaults = next.storyDefaults || {};
    // 0.7.15 全局线路池：素材模块靠它拼「基地址/编号.m3u8」
    state.materialLinePool = next.materialLinePool || [];
    // 0.7.22 素材搜图：图源选项也由后端下发，前端不写死
    state.imageSearchSources = next.imageSearchSources || [];
    // 0.7.25 素材图片排行榜：榜单列表（日榜/周榜/月榜/男性向/女性向）同样由后端下发
    state.imageRankModes = next.imageRankModes || [];
    // 「设置 → 存储位置」：位置清单 + db.json 所在的数据目录
    state.storage = next.storage || [];
    state.dataDir = next.dataDir || '';
    // Topaz 联动：探测结果 / 档位 / 提升任务进度
    state.topaz = next.topaz || { found: false, tiers: [], jobs: [] };
    // 视频模块前端逻辑拆在 video.js 里，这里只负责把状态喂过去
    step('video', () => window.RHVideo?.sync(state));
    // 分镜（小剧场）拆在 storyboard.js 里，同样只喂状态
    step('storyboard', () => window.RHStoryboard?.sync(state));
    // 素材模块同理，拆在 materials.js 里（sync 只同步分类配置，不发远端请求）
    step('material', () => window.RHMaterial?.sync(state));
    // 节点画布：只同步数据与生成任务状态，正在编辑的节点不会被重建
    step('node', () => window.RHNode?.sync(state));
    step('accountsStrip', renderAccountsStrip);
    // 领取状态只有桌面端有，且只在账号弹窗打开时去问，避免每 1.5 秒白跑一次 IPC
    if (desktop && !$('#modalAccounts').hidden) await syncDesktopState().catch(() => {});
    // 正在编辑时不要重建账号列表，否则输入框会失焦
    if (!$('#modalAccounts').hidden && !$('#accountList').contains(document.activeElement)) {
      step('accountList', renderAccountList);
    }
    // 同理：模型管理弹窗里正在输入时不要重建列表
    if (!$('#modalImageModels').hidden && !$('#imageModelList').contains(document.activeElement)) {
      step('imageModelList', renderImageModelList);
    }
    step('genPanel', syncGenPanel);
    step('grid', syncGrid);
    // Topaz 弹窗开着时同步进度
    step('topaz', syncTopaz);
    // 设置弹窗开着时同步一遍路径，改了目录后能立刻看到新位置
    if (!$('#modalSettings').hidden) step('storage', renderStorageTable);
    loadErrorShown = false;
  } catch (err) {
    console.error('[refresh]', err);
    // 首屏就拉不到状态时明确说出来，而不是留一个「点什么都没反应」的空界面
    if (!loadErrorShown) {
      loadErrorShown = true;
      step('toast', () =>
        toast(`界面数据加载失败：${err.message}　可在窗口里按 Ctrl+R 刷新，或关掉程序重开`, true),
      );
    }
  } finally {
    refreshing = false;
  }
}

const latestTaskFor = (canvasId) => state.tasks.find((t) => t.canvasId === canvasId) || null;

/** 拉取桌面端的领取配置与各账号登录态，并同步到账号弹窗里的控件上 */
async function syncDesktopState() {
  try {
    Object.assign(desktopState, await desktop.claim.status());
  } catch (err) {
    console.error('[desktop] 领取状态获取失败', err);
    return;
  }
  $('#claimBar').hidden = false;
  const enabled = $('#claimEnabled');
  if (document.activeElement !== enabled) enabled.checked = desktopState.enabled;
  const time = $('#claimTime');
  if (document.activeElement !== time && time.value !== desktopState.time) time.value = desktopState.time;
  $('#claimHint').textContent = desktopState.running
    ? '正在领取…'
    : desktopState.lastRunDate
      ? `最近自动领取：${desktopState.lastRunDate}`
      : '';

  // 保活是后加的通道，老版本主进程没有这几条 IPC：拉不到就把整条栏藏着，不影响别的
  try {
    const keep = await desktop.keep.status();
    $('#keepBar').hidden = false;
    const on = $('#keepEnabled');
    if (document.activeElement !== on) on.checked = Boolean(keep.enabled);
    const t = $('#keepTime');
    if (document.activeElement !== t && t.value !== keep.time) t.value = keep.time || '03:00';
    $('#keepHint').textContent = keep.running
      ? '正在保活…'
      : keep.lastRunDate
        ? `最近自动保活：${keep.lastRunDate}`
        : '';
  } catch {
    $('#keepBar').hidden = true;
  }
}

function renderAccountsStrip() {
  const wrap = $('#accountsStrip');
  if (!state.accounts.length) {
    wrap.innerHTML = '<span class="hint">还没有账号 → 打开「账号管理」添加 3 个 API Key，就能同时跑 3 个任务</span>';
    return;
  }
  // 账号多的时候这条会横向滚动。以前每 1.5 秒无条件重建 innerHTML，
  // 会把 scrollLeft 一起重置回 0 —— 想看第 5 个账号，刚滑过去就被弹回来了。
  // 现在按内容签名比对：内容没变就完全不碰 DOM。
  const sig = state.accounts
    .map((a) =>
      [
        a.id,
        a.name,
        a.enabled ? 1 : 0,
        a.running,
        a.capacity,
        quotaText(a),
        isCooling(a) ? 1 : 0,
        balanceText(a),
        balanceTrouble(a) ? 1 : 0,
        a.stats?.total || 0,
      ].join(':'),
    )
    .join('|');
  if (wrap.dataset.sig === sig) {
    // 内容没变，但「点了会跳到哪张画布」可能变了（换了账号绑定 / 又跑了一单）。
    // 只改 title 不动 DOM，横向滚动位置才不会被打断。
    refreshChipTitles(wrap);
    return;
  }
  wrap.dataset.sig = sig;

  wrap.innerHTML = state.accounts
    .map((a) => {
      const cooling = isCooling(a);
      const cls = !a.enabled ? 'off' : cooling ? 'cool' : a.running > 0 ? 'busy' : 'ready';
      const title = chipTitle(a);
      // 用 button 而不是 span：点了能跳到对应画布（键盘 Tab + 回车也能按）
      return `<button type="button" class="acct-chip ${cls}" data-acc-id="${esc(a.id)}" title="${esc(title)}">
        <span class="dot"></span>
        <span class="name">${esc(a.name)}</span>
        <span class="load">运行 ${a.running}/${a.capacity}</span>
        <span class="load ${cooling ? 'warn' : ''}">${esc(quotaText(a))}</span>
        <span class="load ${balanceTrouble(a) ? 'warn' : ''}" title="${esc(balanceTitle(a))}">${esc(balanceText(a))}</span>
        <span class="load">累计 ${a.stats?.total || 0}</span>
      </button>`;
    })
    .join('');
  syncTopbarHeight(); // 账号条出现/消失会改变顶栏高度，重新量一次给下面的 sticky 用
}

function chipTitle(a) {
  return [
    a.remark || '',
    a.quotaResetAt ? `额度刷新时间：${new Date(a.quotaResetAt).toLocaleString()}` : '',
    accountCanvasHint(a.id),
  ]
    .filter(Boolean)
    .join(' · ');
}

function refreshChipTitles(wrap) {
  const byId = new Map(state.accounts.map((a) => [a.id, a]));
  for (const chip of wrap.querySelectorAll('.acct-chip')) {
    const account = byId.get(chip.dataset.accId);
    if (!account) continue;
    const title = chipTitle(account);
    if (chip.title !== title) chip.title = title;
  }
}

/**
 * 这个账号对应的画布有哪些。
 *
 * 优先认「画布右上角下拉里指定了它」的；一张都没有时，退回「最近用它跑过的」——
 * 默认是「自动分配」，绝大多数人根本没手动绑过账号，只按绑定关系找等于永远找不到。
 */
function canvasesOfAccount(accountId) {
  const pinned = state.canvases.filter((c) => c.accountId === accountId);
  if (pinned.length) return pinned;
  return state.canvases
    .filter((c) => latestTaskFor(c.id)?.accountId === accountId)
    .sort((a, b) => (latestTaskFor(b.id)?.createdAt || 0) - (latestTaskFor(a.id)?.createdAt || 0));
}

/** 悬停提示里那句「点了会跳到哪」—— 没有画布时如实说，别让人点了没反应还以为坏了 */
function accountCanvasHint(accountId) {
  const mine = canvasesOfAccount(accountId);
  if (!mine.length) return '还没有画布用过这个账号';
  if (mine.length === 1) return `点击跳到画布「${mine[0].name || '未命名画布'}」`;
  return `点击跳到这个账号的画布（共 ${mine.length} 张）`;
}

/**
 * 点账号胶囊 → 跳到这个账号的画布。
 *
 * 一张画布可以指定账号（画布右上角的下拉），但反过来一个账号可能被好几张画布用。
 * 这时优先跳「正在跑」的那张 —— 会去点「运行 1/1」的胶囊，想看的多半就是它。
 */
function jumpToAccountCanvas(accountId) {
  const mine = canvasesOfAccount(accountId);
  if (!mine.length) {
    toast('这个账号还没有跑过画布 —— 在画布右上角的账号下拉里指定它，之后就能从这儿跳了');
    return;
  }
  const running = mine.find((c) => PENDING.has(latestTaskFor(c.id)?.status));
  const target = running || mine[0];
  const card = document.querySelector(`.canvas[data-canvas-id="${target.id}"]`);
  if (!card) return;

  card.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
  card.classList.add('flash');
  clearTimeout(card.flashTimer);
  card.flashTimer = setTimeout(() => card.classList.remove('flash'), 1200);

  if (mine.length > 1) {
    toast(`这个账号下有 ${mine.length} 张画布，已跳到「${target.name || '未命名画布'}」${running ? '（正在运行的那张）' : ''}`);
  }
}

$('#accountsStrip')?.addEventListener('click', (event) => {
  const chip = event.target.closest('.acct-chip');
  if (!chip) return;
  jumpToAccountCanvas(chip.dataset.accId);
});

/**
 * 顶栏高度会随「账号条」显隐变化（素材/视频模块没有账号条）。
 * 量出来塞进 CSS 变量，素材模块「子Tab + 分类下拉」那行才能稳稳贴在顶栏下面 ——
 * 下滑时它不动，随时能切分类。
 */
function syncTopbarHeight() {
  /* 布局重构后顶栏高度是固定的 CSS 变量 --topbar-h（48px），不再随内容变化。
     这里不再动态测量，保留函数体是为了兼容旧调用（如 syncTopbar）。 */
}
window.addEventListener('resize', syncTopbarHeight);

/* ---------------------------------- 画布渲染 --------------------------------- */

function syncGrid() {
  const grid = $('#canvasGrid');
  for (const card of [...grid.querySelectorAll('.canvas')]) {
    if (!state.canvases.some((c) => c.id === card.dataset.canvasId)) card.remove();
  }

  if (!state.canvases.length) {
    if (!grid.querySelector('.empty-hint')) {
      grid.innerHTML = '<div class="empty-hint">还没有画布，点击右上角「+ 新建画布」</div>';
    }
    return;
  }
  grid.querySelector('.empty-hint')?.remove();

  for (const canvas of state.canvases) {
    let card = grid.querySelector(`.canvas[data-canvas-id="${canvas.id}"]`);
    if (!card) {
      card = buildCard(canvas);
      grid.appendChild(card);
    }
    updateCard(card, canvas);
  }
  syncParamsToggleBtn();
  syncCanvasJump();
}

/* 画布定位器：顶栏下拉列出所有画布，选一个就平滑滚过去并闪一下边 */
function syncCanvasJump() {
  const sel = $('#cvJump');
  if (!sel) return;
  const sig = state.canvases.map((c) => `${c.id}:${c.name}`).join('|');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML =
    '<option value="">跳到画布…</option>' +
    state.canvases
      .map((c) => `<option value="${c.id}">${esc(c.name || '未命名画布')}</option>`)
      .join('');
  sel.value = '';
}

$('#cvJump')?.addEventListener('change', (event) => {
  const id = event.target.value;
  if (!id) return;
  const card = document.querySelector(`.canvas[data-canvas-id="${id}"]`);
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 1200);
  }
  event.target.value = '';
});

function buildCard(canvas) {
  const el = document.createElement('article');
  // 「其他参数」默认收起来：读完参数后字段多的能撑到两千多像素，4 张画布叠起来就滑不完了。
  // 收起后一张卡只剩「画布名 + 配置 + 结果 + 开始生成」，一屏能看全，点标题行随时展开。
  // 上传图片 / 视频的字段不收（要经常换素材，收起来等于白收，见 style.css 那条 :not() 规则）。
  // 顶栏「收起参数 / 展开参数」能一键切换，偏好记在 localStorage 里，新建的画布跟着走。
  el.className = canvasParamsCollapsed() ? 'canvas params-collapsed' : 'canvas';
  el.dataset.canvasId = canvas.id;
  el.innerHTML = `
    <div class="cv-head">
      <input class="cv-name" data-role="name" placeholder="画布名称" />
      <span class="badge" data-role="status">空闲</span>
      <select class="cv-account" data-role="account"></select>
      <button class="icon-btn" data-act="delete" title="删除画布">✕</button>
    </div>
    <div class="cv-body">
      <div class="cv-col cv-col-config">
        <div class="cv-config">
          <div class="seg" data-role="mode">
            <button data-mode="app">AI 应用</button>
            <button data-mode="workflow">ComfyUI 工作流</button>
          </div>
          <div class="row">
            <input data-role="appid" autocomplete="off" />
            <button class="btn" data-act="inspect">读取参数</button>
          </div>
          <div class="hint" data-role="appname"></div>
          <div data-role="wfslot"></div>
          <div class="row wm-row">
            <label class="switch" title="读取参数后自动把水印相关的参数置为关闭，生成出来的视频就不带水印"><input type="checkbox" data-role="remove-watermark" />自动关闭水印参数</label>
            <input data-role="polish-appid" autocomplete="off" placeholder="去水印应用 webappId（可留空）" title="填了就在生成成功后自动把视频送进这个应用再处理一遍，会额外消耗 RH 币" />
          </div>
          <div class="row wm-row">
            <label class="switch" title="接口被平台判「余额不足」时，用这个账号在网页里的登录态打开 AI 应用页跑一遍。网页扣的是 RH 币，跟 API 那本账互不相干"><input type="checkbox" data-role="web-fallback" />接口没余额时用网页端跑</label>
            <input data-role="web-url" autocomplete="off" placeholder="网页地址（可留空，默认 /ai-detail/&lt;webappId&gt;）" title="留空按 AI 应用页地址自动拼；ComfyUI 工作流请自己贴应用页地址" />
          </div>
        </div>
        <div class="cv-params">
          <button class="cv-params-toggle" type="button" data-act="toggle-params">
            <span class="cv-caret">▾</span>
            <span>其他参数</span>
            <span class="hint" data-role="params-count"></span>
          </button>
        </div>
      </div>
      <div class="cv-col cv-col-fields"><div class="cv-fields" data-role="fields"></div></div>
      <div class="cv-col cv-col-result"><div class="cv-result" data-role="result"></div></div>
    </div>
    <footer class="cv-foot">
      <button class="btn primary" data-act="run">开始生成</button>
      <button class="btn" data-act="cancel">取消</button>
      <span class="cv-last" data-role="last-run" title="上次开始生成的时间"></span>
      <span class="cv-meta" data-role="meta"></span>
    </footer>
    <details class="cv-log"><summary>运行日志</summary><pre data-role="log">（暂无日志）</pre></details>
  `;
  return el;
}

function updateCard(card, canvas) {
  const nameInput = card.querySelector('[data-role="name"]');
  if (document.activeElement !== nameInput && nameInput.value !== canvas.name) nameInput.value = canvas.name;

  const accSel = card.querySelector('[data-role="account"]');
  const accSig = state.accounts.map((a) => `${a.id}:${a.name}:${a.enabled}`).join('|');
  if (card.dataset.accSig !== accSig) {
    card.dataset.accSig = accSig;
    accSel.innerHTML =
      '<option value="">账号：自动分配</option>' +
      state.accounts
        .map(
          (a) =>
            `<option value="${a.id}" ${a.enabled ? '' : 'disabled'}>${esc(a.name)}${a.enabled ? '' : '（停用）'}</option>`,
        )
        .join('');
  }
  accSel.value = canvas.accountId || '';

  for (const btn of card.querySelectorAll('[data-role="mode"] button')) {
    btn.classList.toggle('active', btn.dataset.mode === canvas.mode);
  }

  const appIdInput = card.querySelector('[data-role="appid"]');
  if (document.activeElement !== appIdInput && appIdInput.value !== (canvas.appId || '')) {
    appIdInput.value = canvas.appId || '';
  }
  appIdInput.placeholder =
    canvas.mode === 'workflow' ? 'workflowId（工作流链接末尾的数字）' : 'webappId（AI 应用链接末尾的数字）';

  const wfSlot = card.querySelector('[data-role="wfslot"]');
  if (canvas.mode === 'workflow' && !wfSlot.dataset.built) {
    wfSlot.dataset.built = '1';
    wfSlot.innerHTML = `
      <div class="row" style="margin-bottom:8px">
        <input type="file" accept=".json,application/json" data-role="wf-file" />
        <span class="hint">选择导出的 api.json</span>
      </div>
      <textarea data-role="wf-json" placeholder="或直接粘贴 RunningHub 导出的工作流 api.json 内容"></textarea>`;
  } else if (canvas.mode !== 'workflow' && wfSlot.dataset.built) {
    wfSlot.dataset.built = '';
    wfSlot.innerHTML = '';
  }

  const appNameHint = card.querySelector('[data-role="appname"]');
  if (!appNameHint.dataset.sticky) {
    appNameHint.className = 'hint';
    appNameHint.textContent = canvas.appName ? `当前应用：${canvas.appName}` : '';
  }

  const wmToggle = card.querySelector('[data-role="remove-watermark"]');
  const wmOn = canvas.removeWatermark !== false;
  if (wmToggle.checked !== wmOn) wmToggle.checked = wmOn;
  const polishInput = card.querySelector('[data-role="polish-appid"]');
  if (document.activeElement !== polishInput && polishInput.value !== (canvas.polishAppId || '')) {
    polishInput.value = canvas.polishAppId || '';
  }
  const wfToggle = card.querySelector('[data-role="web-fallback"]');
  const wfOn = canvas.webFallback !== false;
  if (wfToggle.checked !== wfOn) wfToggle.checked = wfOn;
  const webUrlInput = card.querySelector('[data-role="web-url"]');
  if (document.activeElement !== webUrlInput && webUrlInput.value !== (canvas.webUrl || '')) {
    webUrlInput.value = canvas.webUrl || '';
  }

  const fieldsWrap = card.querySelector('[data-role="fields"]');
  const fieldsSig = JSON.stringify(
    (canvas.fields || []).map((f) => [
      f.nodeId,
      f.fieldName,
      f.fieldType,
      f.description,
      (f.options || []).length,
      canvas.assets?.[`${f.nodeId}::${f.fieldName}`] ? 1 : 0,
    ]),
  );
  if (card.dataset.fieldsSig !== fieldsSig) {
    card.dataset.fieldsSig = fieldsSig;
    renderFields(fieldsWrap, canvas);
  } else {
    syncFieldValues(fieldsWrap, canvas);
  }

  // 折叠标题上标一下收了几项（只数会被收起来的那些：图片/视频字段一直露在外面，不算）
  const countEl = card.querySelector('[data-role="params-count"]');
  const fields = canvas.fields || [];
  const folded = fields.filter((f) => !isFileField(f)).length;
  const countText = fields.length ? `${folded} 项` : '未读取';
  if (countEl.textContent !== countText) countEl.textContent = countText;

  renderStatus(card, canvas, latestTaskFor(canvas.id));
}

function fieldHtml(canvas, field) {
  const key = `${field.nodeId}::${field.fieldName}`;
  const head = `<div class="field-head">
      <span class="fname">${esc(field.description || field.fieldName)}</span>
      <span class="fmeta">${esc(`${field.fieldName} · node ${field.nodeId} · ${field.fieldType}`)}</span>
    </div>`;

  if (isFileField(field)) {
    const asset = canvas.assets?.[key];
    const fieldType = String(field.fieldType || '').toUpperCase();
    let media = `<span class="dz-hint">点击选择${ASSET_LABEL[fieldType] || '文件'}</span>`;
    if (asset) {
      const mime = String(asset.mimeType || '');
      if (mime.startsWith('video')) media = `<video src="${asset.localUrl}" muted playsinline preload="metadata"></video>`;
      else if (mime.startsWith('audio')) media = `<span class="dz-hint">${esc(asset.originalName)}</span>`;
      else media = `<img src="${asset.localUrl}" alt="" />`;
    }
    return `<div class="field" data-key="${esc(key)}" data-field-type="${esc(fieldType)}">
      ${head}
      <label class="dropzone" data-role="drop">${media}<input type="file" hidden accept="${esc(ASSET_ACCEPT[fieldType] || '')}" data-role="file" /></label>
      <div class="field-foot">
        <span class="fname-ellipsis">${asset ? `${esc(asset.originalName)} · ${fmtSize(asset.size)}` : '未选择文件'}</span>
        ${asset ? '<button class="btn tiny" data-act="clear-asset">清除</button>' : ''}
      </div>
    </div>`;
  }

  const value = canvas.values?.[key] ?? field.defaultValue ?? '';
  let control;
  if (field.fieldType === 'LIST' && (field.options || []).length) {
    control = `<select data-role="value">${field.options
      .map(
        (o) =>
          `<option value="${esc(o.index)}" ${String(o.index) === String(value) ? 'selected' : ''}>${esc(o.label)}</option>`,
      )
      .join('')}</select>`;
  } else {
    control = `<input data-role="value" type="${field.fieldType === 'NUMBER' ? 'number' : 'text'}" value="${esc(value)}" />`;
  }
  return `<div class="field" data-key="${esc(key)}">${head}${control}</div>`;
}

function renderFields(wrap, canvas) {
  wrap.innerHTML = (canvas.fields || []).map((f) => fieldHtml(canvas, f)).join('');
}

function syncFieldValues(wrap, canvas) {
  for (const fieldEl of wrap.querySelectorAll('.field')) {
    const control = fieldEl.querySelector('[data-role="value"]');
    if (!control || control === document.activeElement) continue;
    const value = canvas.values?.[fieldEl.dataset.key];
    if (value !== undefined && String(control.value) !== String(value)) control.value = value;
  }
}

function mediaHtml(output) {
  const src = output.localUrl || output.fileUrl;
  const type = String(output.fileType || '').toLowerCase();
  const isVideo = ['mp4', 'webm', 'mov', 'mkv', 'avi'].includes(type) || /\.(mp4|webm|mov|mkv)$/i.test(src);
  const isImage = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'].includes(type) || /\.(png|jpe?g|webp|gif|bmp)$/i.test(src);
  if (isVideo) return `<video src="${esc(src)}" controls preload="metadata"></video>`;
  if (isImage) {
    return `<a href="${esc(src)}" target="_blank" rel="noreferrer"><img src="${esc(src)}" alt="" loading="lazy" /></a>`;
  }
  return `<a href="${esc(src)}" target="_blank" rel="noreferrer">${esc(type || '结果文件')}</a>`;
}

function historyHtml(canvas, currentTask) {
  const items = state.tasks.filter(
    (t) => t.canvasId === canvas.id && t.status === 'success' && t.outputs?.length && t.id !== currentTask?.id,
  );
  if (!items.length) return '';
  const thumbs = items
    .slice(0, 10)
    .flatMap((t) => t.outputs.slice(0, 1).map((o) => ({ t, o })))
    .map(({ t, o }) => {
      const src = o.localUrl || o.fileUrl;
      const isVideo = /\.(mp4|webm|mov)$/i.test(src) || ['mp4', 'webm', 'mov'].includes(String(o.fileType || '').toLowerCase());
      const inner = isVideo
        ? `<video src="${esc(src)}" muted preload="metadata"></video>`
        : `<img src="${esc(src)}" alt="" loading="lazy" />`;
      return `<a href="${esc(src)}" target="_blank" rel="noreferrer" title="${esc(new Date(t.createdAt).toLocaleString())}">${inner}</a>`;
    })
    .join('');
  return `<div class="result-history"><div class="hint">历史结果（${items.length}）</div><div class="history-strip">${thumbs}</div></div>`;
}

function renderStatus(card, canvas, task) {
  const status = task?.status;
  const pending = Boolean(status && PENDING.has(status));

  card.classList.toggle('is-running', ['preparing', 'remote_queued', 'running', 'web_running'].includes(status));
  card.classList.toggle('is-success', status === 'success');
  card.classList.toggle('is-failed', status === 'failed');

  const badge = card.querySelector('[data-role="status"]');
  badge.className = `badge ${pending ? (status === 'running' ? 'running' : 'queued') : status || ''}`;
  if (!task) badge.textContent = '空闲';
  else {
    const elapsed = task.startedAt ? fmtDuration((task.finishedAt || Date.now()) - task.startedAt) : '';
    badge.textContent = `${STATUS_LABEL[status] || status}${pending && elapsed ? ` ${elapsed}` : ''}`;
  }

  const runBtn = card.querySelector('[data-act="run"]');
  runBtn.disabled = pending;
  runBtn.textContent = pending ? '生成中…' : '开始生成';
  card.querySelector('[data-act="cancel"]').disabled = !pending;

  const bits = [];
  if (task) {
    bits.push(task.accountName ? `账号 ${task.accountName}` : '账号待分配');
    if (task.remoteTaskId) bits.push(`#${task.remoteTaskId}`);
    if ((task.attempts || 0) > 1) bits.push(`第 ${task.attempts} 次尝试`);
    if (task.startedAt && task.finishedAt) bits.push(`耗时 ${fmtDuration(task.finishedAt - task.startedAt)}`);
  } else if (canvas.accountId) {
    bits.push('已锁定账号');
  }
  card.querySelector('[data-role="meta"]').textContent = bits.join(' · ');

  // 上次执行时间：取这张画布最近发起的一条任务，显示它开始生成的时间
  const lastTask = state.tasks
    .filter((t) => t.canvasId === canvas.id && t.createdAt)
    .sort((a, b) => b.createdAt - a.createdAt)[0];
  card.querySelector('[data-role="last-run"]').textContent = lastTask
    ? `上次 ${new Date(lastTask.createdAt).toLocaleString()}`
    : '';

  const resultWrap = card.querySelector('[data-role="result"]');
  let body = '';
  if (task?.status === 'success' && task.outputs?.length) {
    body = `<div class="result-box">
      <div class="result-media">${task.outputs.map(mediaHtml).join('')}</div>
      <div class="result-actions">
        ${task.outputs
          .map(
            (o, i) =>
              o.localUrl
                ? `<a href="${o.localUrl}" download>下载结果 ${i + 1}${o.polished ? '（已去水印）' : ''}</a>`
                : `<a href="${esc(o.fileUrl)}" target="_blank" rel="noreferrer">远端文件 ${i + 1}</a>`,
          )
          .join('')}
      </div>
    </div>`;
  } else if (pending) {
    body = `<div class="result-box"><div class="progress">
      <div class="progress-bar"><span></span></div>
      <div class="hint" style="margin-top:7px">${esc(task.message || '处理中')}${task.remoteTaskId ? ` · 远端任务 #${esc(task.remoteTaskId)}` : ''}</div>
    </div></div>`;
  } else if (task?.status === 'failed') {
    body = `<div class="error-box">${esc(task.error || task.message || '任务失败')}</div>
      <div class="result-actions" data-task-id="${esc(task.id)}" style="padding:0 0 10px">
        <button class="btn tiny" data-act="retry">重新生成</button>
      </div>`;
  } else if (task?.status === 'canceled') {
    body = '<div class="hint" style="padding-bottom:10px">任务已取消</div>';
  }
  // 每 1.5 秒刷新一次，如果每次都重建 innerHTML，结果里的 <video> 会被反复销毁重建，
  // 表现就是画面闪烁、视频永远加载不完。内容没变就不动它，播放状态才能保住。
  const outputsSig = (task?.outputs || [])
    .map((o) => `${o.localUrl || o.fileUrl}|${o.polished ? 1 : 0}|${o.topaz ? o.topaz.at : ''}`)
    .join(',');
  const historySig = state.tasks
    .filter((t) => t.canvasId === canvas.id && t.status === 'success' && t.outputs?.length && t.id !== task?.id)
    .map((t) => `${t.id}:${t.outputs[0].localUrl || t.outputs[0].fileUrl}`)
    .join(',');
  const sig = [task?.id, task?.status, task?.message, task?.remoteTaskId, task?.error, outputsSig, historySig].join('|');
  if (resultWrap.dataset.sig !== sig) {
    resultWrap.dataset.sig = sig;
    resultWrap.innerHTML = body + historyHtml(canvas, task);
  }

  const logEl = card.querySelector('[data-role="log"]');
  const lines = task?.log || [];
  logEl.textContent = lines.length
    ? lines.map((l) => `[${new Date(l.t).toLocaleTimeString()}] ${l.text}`).join('\n')
    : '（暂无日志）';
}

/* --------------------------------- 交互：按钮 -------------------------------- */

$('#canvasGrid').addEventListener('click', async (event) => {
  const card = event.target.closest('.canvas');
  if (!card) return;
  const canvasId = card.dataset.canvasId;

  const modeBtn = event.target.closest('[data-role="mode"] button');
  if (modeBtn) {
    if (modeBtn.dataset.mode === state.canvases.find((c) => c.id === canvasId)?.mode) return;
    await api(`/api/canvases/${canvasId}`, { method: 'PATCH', body: { mode: modeBtn.dataset.mode } });
    await refresh();
    return;
  }

  const action = event.target.closest('[data-act]')?.dataset.act;
  if (!action) return;

  try {
    if (action === 'toggle-params') {
      card.classList.toggle('params-collapsed');
    } else if (action === 'delete') {
      if (!(await confirmDialog('删除这个画布？正在运行的任务会被取消。'))) return;
      await api(`/api/canvases/${canvasId}`, { method: 'DELETE' });
      await refresh();
    } else if (action === 'inspect') {
      await inspectCanvas(card, canvasId);
    } else if (action === 'run') {
      await api(`/api/canvases/${canvasId}/run`, { method: 'POST', body: {} });
      toast('任务已加入队列');
      await refresh();
    } else if (action === 'cancel') {
      const task = latestTaskFor(canvasId);
      if (!task) return;
      await api(`/api/tasks/${task.id}/cancel`, { method: 'POST', body: {} });
      await refresh();
    } else if (action === 'clear-asset') {
      const fieldEl = event.target.closest('.field');
      await api(`/api/canvases/${canvasId}/assets/${encodeURIComponent(fieldEl.dataset.key)}`, { method: 'DELETE' });
      await refresh();
    } else if (action === 'retry') {
      const taskId = event.target.closest('[data-task-id]')?.dataset.taskId;
      if (!taskId) return;
      await api(`/api/tasks/${taskId}/retry`, { method: 'POST', body: {} });
      await refresh();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

/* --------------------------------- 交互：表单 -------------------------------- */

$('#canvasGrid').addEventListener('change', async (event) => {
  const card = event.target.closest('.canvas');
  if (!card) return;
  const canvasId = card.dataset.canvasId;

  if (event.target.matches('[data-role="account"]')) {
    await api(`/api/canvases/${canvasId}`, { method: 'PATCH', body: { accountId: event.target.value || null } });
    await refresh();
    return;
  }

  if (event.target.matches('[data-role="file"]')) {
    const file = event.target.files?.[0];
    const fieldEl = event.target.closest('.field');
    event.target.value = '';
    if (file && fieldEl) await uploadAsset(canvasId, fieldEl, file);
    return;
  }

  if (event.target.matches('[data-role="wf-file"]')) {
    const file = event.target.files?.[0];
    if (!file) return;
    card.querySelector('[data-role="wf-json"]').value = await file.text();
    toast('已载入工作流 JSON，点击「读取参数」解析');
    return;
  }

  if (event.target.matches('[data-role="remove-watermark"]')) {
    await api(`/api/canvases/${canvasId}`, {
      method: 'PATCH',
      body: { removeWatermark: event.target.checked },
    });
    await refresh();
    return;
  }

  if (event.target.matches('[data-role="web-fallback"]')) {
    await api(`/api/canvases/${canvasId}`, {
      method: 'PATCH',
      body: { webFallback: event.target.checked },
    });
    await refresh();
    return;
  }

  if (event.target.matches('[data-role="value"]')) {
    const fieldEl = event.target.closest('.field');
    queueValueSave(canvasId, fieldEl.dataset.key, event.target.value);
  }
});

$('#canvasGrid').addEventListener('input', (event) => {
  const card = event.target.closest('.canvas');
  if (!card) return;
  const canvasId = card.dataset.canvasId;
  if (event.target.matches('[data-role="name"]')) queueFieldSave(canvasId, 'name', event.target.value);
  else if (event.target.matches('[data-role="appid"]')) queueFieldSave(canvasId, 'appId', event.target.value);
  else if (event.target.matches('[data-role="polish-appid"]')) {
    queueFieldSave(canvasId, 'polishAppId', event.target.value);
  } else if (event.target.matches('[data-role="web-url"]')) {
    queueFieldSave(canvasId, 'webUrl', event.target.value);
  } else if (event.target.matches('input[data-role="value"]')) {
    const fieldEl = event.target.closest('.field');
    queueValueSave(canvasId, fieldEl.dataset.key, event.target.value);
  }
});

const saveTimers = new Map();

function queueFieldSave(canvasId, key, value) {
  const timerKey = `${canvasId}:${key}`;
  clearTimeout(saveTimers.get(timerKey));
  saveTimers.set(
    timerKey,
    setTimeout(() => {
      saveTimers.delete(timerKey);
      api(`/api/canvases/${canvasId}`, { method: 'PATCH', body: { [key]: value } }).catch((err) =>
        toast(err.message, true),
      );
    }, 450),
  );
}

function queueValueSave(canvasId, fieldKeyValue, value) {
  const timerKey = `${canvasId}:v:${fieldKeyValue}`;
  clearTimeout(saveTimers.get(timerKey));
  saveTimers.set(
    timerKey,
    setTimeout(() => {
      saveTimers.delete(timerKey);
      api(`/api/canvases/${canvasId}`, { method: 'PATCH', body: { values: { [fieldKeyValue]: value } } }).catch(
        (err) => toast(err.message, true),
      );
    }, 450),
  );
}

async function uploadAsset(canvasId, fieldEl, file) {
  // accept 只过滤了选择框，用户仍可手动切到「所有文件」，这里再拦一道
  const fieldType = String(fieldEl.dataset.fieldType || '').toUpperCase();
  const kind = assetKindOf(file);
  if (fieldType && kind && kind !== fieldType) {
    toast(`这个格子只能放${ASSET_LABEL[fieldType] || fieldType}，你选的是${ASSET_LABEL[kind] || kind}文件`, true);
    return;
  }
  try {
    const res = await fetch(`/api/canvases/${canvasId}/assets/${encodeURIComponent(fieldEl.dataset.key)}`, {
      method: 'POST',
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'x-filename': encodeURIComponent(file.name),
      },
      body: file,
    });
    const json = await res.json().catch(() => null);
    if (!res.ok) throw new Error(json?.error || `上传失败（HTTP ${res.status}）`);
    toast(`已添加素材：${file.name}`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

async function inspectCanvas(card, canvasId) {
  const canvas = state.canvases.find((c) => c.id === canvasId);
  const appId = card.querySelector('[data-role="appid"]').value.trim();
  const hint = card.querySelector('[data-role="appname"]');
  const button = card.querySelector('[data-act="inspect"]');

  if (!appId) {
    hint.dataset.sticky = '1';
    hint.className = 'hint err';
    hint.textContent = '请先填写 webappId / workflowId';
    return;
  }

  hint.dataset.sticky = '1';
  hint.className = 'hint';
  hint.textContent = '正在读取参数…';
  button.disabled = true;

  try {
    const body = { appId };
    if (canvas.mode === 'workflow') {
      const json = card.querySelector('[data-role="wf-json"]')?.value || '';
      if (!json.trim()) throw new Error('工作流模式请先选择或粘贴 api.json 内容');
      body.workflowJson = json;
    }
    const updated = await api(`/api/canvases/${canvasId}/inspect`, { method: 'POST', body });
    hint.className = 'hint ok';
    const adjusted = updated.watermarkAdjusted || [];
    hint.textContent =
      `已读取「${updated.appName}」的 ${updated.fields.length} 个参数` +
      (adjusted.length ? `，已自动关闭水印参数：${adjusted.join('、')}` : '');
    card.classList.remove('params-collapsed'); // 刚读完参数，直接把参数区摊开
    await refresh();
  } catch (err) {
    hint.className = 'hint err';
    hint.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

/* --------------------------------- 账号弹窗 --------------------------------- */

// 正在「换 Key」的那一行：换 Key 是行内编辑，输入的内容存在这里，
// 免得期间列表被刷新（比如自动查余额回来）把用户刚粘的 Key 冲掉。
let editingKeyId = null;
let keyDraft = '';

// 正在编辑「网页端登录账密」的那一行（和换 Key 同款行内编辑模式）
let editingLoginId = null;

/**
 * 账号个数统计：侧边栏「账号管理」上的角标 + 弹窗里的一行汇总。
 * 只统计 API Key 账号（网页平台账号另算，口径不同不混在一起）。
 */
function updateAccountCounts() {
  const list = state.accounts || [];
  const enabled = list.filter((a) => a.enabled).length;
  const capacity = list.filter((a) => a.enabled).reduce((sum, a) => sum + (Number(a.capacity) || 0), 0);

  const badge = $('#accCountBadge');
  if (badge) {
    badge.textContent = String(list.length);
    badge.hidden = !list.length;
    badge.title = `共 ${list.length} 个账号，启用 ${enabled} 个`;
  }

  const stat = $('#accTotalStat');
  if (stat) {
    if (!list.length) {
      stat.textContent = '共 0 个账号';
    } else {
      stat.innerHTML =
        `共 <strong>${list.length}</strong> 个账号 · 启用 <strong>${enabled}</strong> 个` +
        (capacity ? ` · 并发合计 <strong>${capacity}</strong>` : '');
    }
  }
}

function renderAccountList() {
  updateAccountCounts();
  const wrap = $('#accountList');
  if (!state.accounts.length) {
    wrap.innerHTML = '<p class="hint">还没有账号。建议添加 3 个，这样可以同时跑 3 个视频任务。</p>';
    return;
  }
  wrap.innerHTML = state.accounts
    .map(
      (a) => `<div class="account-row" data-account-id="${a.id}">
        <span class="acc-name">${esc(a.name)}</span>
        <button class="acc-site${a.site === 'ai' ? ' is-ai' : ''}" type="button" data-act="toggle-site" title="这个账号走哪个站：登录、领币、任务都用对应的域名。点一下在 国内站 .cn / 国际站 .ai 之间切换（加错了站不用删号重加）">${a.site === 'ai' ? '国际站 .ai' : '国内站 .cn'}</button>
        ${
          a.id === editingKeyId
            ? `<input class="acc-key-input" data-role="new-key" placeholder="粘贴新的 API Key" autocomplete="off" spellcheck="false" value="${esc(
                keyDraft,
              )}" title="企业级-共享类型的 Key 扣的是「钱包余额」，用不了每日赠送的 RH 币" />`
            : `<span class="acc-key">${esc(a.apiKey)}</span>`
        }
        ${
          a.id === editingLoginId
            ? `<input class="acc-key-input" data-role="login-name" placeholder="网页端登录账号（手机号/邮箱）" autocomplete="off" spellcheck="false" value="${esc(a.loginName || '')}" />
        <input class="acc-key-input" data-role="login-pass" type="password" placeholder="网页端登录密码" autocomplete="new-password" value="${esc(a.loginPass || '')}" />`
            : ''
        }
        <span class="acc-stat">运行 ${a.running}/${a.capacity} · 成功 ${a.stats?.success || 0} · 失败 ${a.stats?.failed || 0}</span>
        <span class="acc-quota ${isCooling(a) ? 'warn' : ''}" title="${
          a.quotaResetAt ? `额度刷新时间：${esc(new Date(a.quotaResetAt).toLocaleString())}` : ''
        }">${esc(quotaText(a))}</span>
        <span class="acc-coins ${balanceTrouble(a) ? 'warn' : ''}" data-role="coins" title="${esc(
          balanceTitle(a),
        )}">${esc(balanceText(a))}</span>
        <span class="spacer"></span>
        <span class="acc-mini">额度</span>
        <input class="conc" type="number" min="0" max="999" value="${a.quotaLimit ?? 0}" data-role="quota" title="每个 24 小时窗口期可提交次数，提交一次扣一次、倒计时从提交那刻开始；0 = 不限制（账号还有 RH 币时刷新余额会自动解除此限制）" />
        <span class="acc-mini">并发</span>
        <input class="conc" type="number" min="1" max="10" value="${a.capacity}" data-role="conc" title="并发上限" />
        <label class="switch"><input type="checkbox" data-role="enabled" ${a.enabled ? 'checked' : ''} />启用</label>
        <button class="btn tiny" data-act="copy-account" title="一键复制这个账号的 API Key（复制后可直接粘到别处）">复制</button>
        ${
          a.id === editingLoginId
            ? '<button class="btn tiny primary" data-act="save-login">保存账密</button><button class="btn tiny" data-act="cancel-login">取消</button>'
            : `<button class="btn tiny" data-act="edit-login" title="${a.loginName ? '已存了网页端登录账密：点「登录」会自动填写。点这里修改' : '存一下网页端的登录账号和密码，之后点「登录」就会自动填表（只存本机）'}">账密${a.loginName ? ' ✓' : ''}</button>`
        }
        ${
          a.id === editingKeyId
            ? '<button class="btn tiny primary" data-act="save-key">保存</button><button class="btn tiny" data-act="cancel-key">取消</button>'
            : '<button class="btn tiny" data-act="change-key" title="用新的 API Key 替换当前这个（留空不修改）">换 Key</button>'
        }
        <button class="btn tiny" data-act="test-account">测试</button>
        <button class="btn tiny" data-act="refresh-balance">查余额</button>
        ${desktop ? '<button class="btn tiny" data-act="login-web" title="打开内嵌窗口登录 RunningHub 网页端，登一次长期有效">登录</button>' : ''}
        <button class="btn tiny danger" data-act="delete-account">删除</button>
        <span class="test-result" data-role="test-result"></span>
        ${desktop ? `<span class="acc-claim ${claimTrouble(a) ? 'warn' : ''}" data-role="claim">${esc(claimText(a))}</span>` : ''}
      </div>`,
    )
    .join('');
}

/* ---------------------- 网页平台账号（多账户额度池） ---------------------- */

/**
 * 豆包 / 千问 / 元宝这类平台的免费额度跟着账号走，一个号一天就那么几次。
 * 这里把每个账号的登录态放进独立分区、各自记账，合起来就是一个额度池：
 * 今天哪个号还有额度就用哪个，用完了自动换下一个。
 */

let webAccCache = { accounts: [], providers: [] };

async function loadWebAccounts() {
  const [accounts, providers] = await Promise.all([
    api('/api/web-accounts').catch(() => ({ accounts: [] })),
    api('/api/web-providers').catch(() => ({ providers: [] })),
  ]);
  webAccCache = { accounts: accounts?.accounts || [], providers: providers?.providers || [] };
  return webAccCache;
}

const providerName = (slug) =>
  webAccCache.providers.find((p) => p.slug === slug)?.name || slug || '未知平台';

function webAccQuotaText(a) {
  if (a.quotaLimit === null || a.quotaLimit === undefined) {
    return a.quotaLeftUsed ? `已用 ${a.quotaLeftUsed} 次 · 不限` : '不限';
  }
  const left = a.quotaLeft ?? 0;
  return left > 0 ? `剩 ${left} / ${a.quotaLimit}` : '今日已用完';
}

function webAccLoginText(a) {
  if (a.logged) {
    if (a.loginStale) return '登录态已过期';
    if (!a.loginExpiresAt) return '已登录';
    const days = Math.ceil((a.loginExpiresAt - Date.now()) / 86400000);
    return days <= 7 ? `已登录（${days} 天后过期）` : `已登录（到 ${new Date(a.loginExpiresAt).toLocaleDateString('zh-CN')}）`;
  }
  return '未登录（点「登录」）';
}

function renderWebAccounts() {
  const box = $('#webAccountList');
  if (!box) return;
  const list = webAccCache.accounts;
  if (!list.length) {
    box.innerHTML = '<p class="hint">还没绑网页平台账号。上面选一个平台、起个名字，点「添加账号」，然后登录一次就能用了。</p>';
    return;
  }
  box.innerHTML = list
    .map(
      (a) => `<div class="account-row" data-wa-id="${esc(a.id)}">
        <span class="acc-name">${esc(a.name)}</span>
        <span class="acc-site">${esc(providerName(a.provider))}</span>
        <span class="acc-quota ${a.quotaLeft === 0 ? 'warn' : ''}" title="${
          a.quotaResetAt ? `额度刷新：${esc(new Date(a.quotaResetAt).toLocaleString('zh-CN'))}` : ''
        }">${esc(webAccQuotaText(a))}</span>
        <span class="acc-stat">成功 ${a.stats?.success || 0} · 失败 ${a.stats?.failed || 0}</span>
        <span class="acc-claim ${a.loginStale || !a.logged ? 'warn' : ''}">${esc(webAccLoginText(a))}</span>
        <input class="wa-prompt" data-role="prompt" placeholder="提示词…" spellcheck="false" title="填好提示词后点「生成」，会用这个账号的额度" />
        <span class="spacer"></span>
        <button class="btn tiny" data-wa-act="login">登录</button>
        <button class="btn tiny primary" data-wa-act="submit">生成</button>
        <button class="btn tiny" data-wa-act="open" title="开一个窗口自己操作：平台自己的下载会被自动登记">开窗口</button>
        <button class="btn tiny" data-wa-act="keep" title="用隐藏窗口访问一次首页，把 cookie 续上">保活</button>
        <button class="btn tiny" data-wa-act="probe" title="现查这个账号的登录态：登在哪个站、令牌到哪天">体检</button>
        <label class="switch"><input type="checkbox" data-wa-act="enabled" ${a.enabled === false ? '' : 'checked'} />启用</label>
        <button class="btn tiny danger" data-wa-act="del">删除</button>
      </div>`,
    )
    .join('');
}

/** 平台下拉：只在第一次拉到清单时重建，避免每次开弹窗都重置用户的选择 */
function fillWebAccProviders() {
  const sel = $('#waProvider');
  if (!sel) return;
  const sig = webAccCache.providers.map((p) => `${p.slug}:${p.name}`).join('|');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  const keep = sel.value;
  sel.innerHTML = webAccCache.providers
    .filter((p) => p.enabled !== false)
    .map((p) => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`)
    .join('');
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

async function refreshWebAccounts() {
  if (!desktop || !desktop.webacc) return;
  await loadWebAccounts();
  fillWebAccProviders();
  renderWebAccounts();
}

async function webAccAct(id, act, row) {
  const bridge = desktop.webacc;
  const account = webAccCache.accounts.find((a) => a.id === id);
  if (act === 'login') {
    const out = await bridge.login(id);
    toast(out?.ok ? `已登录「${account?.name || id}」` : out?.error || '登录未完成', !out?.ok);
  } else if (act === 'probe') {
    const out = await bridge.probe(id);
    toast(out?.logged ? `登录态正常${out.expiresAt ? `，到 ${new Date(out.expiresAt).toLocaleDateString('zh-CN')}` : ''}` : '没检测到登录态，点「登录」', !out?.logged);
  } else if (act === 'keep') {
    const out = await bridge.keep(id);
    // 网页平台账号（豆包/千问这类）的保活：访问一次首页，让平台自己的会话 cookie 续上。
    // 结果如实报：成功就带上到期的日子，失败就说清是「没登录」还是「访问后掉了」。
    const when = out?.expiresAt ? `，登录态到 ${new Date(out.expiresAt).toLocaleDateString('zh-CN')}` : '';
    toast(out?.ok ? `已访问一次${when}` : out?.error || '保活失败', !out?.ok);
  } else if (act === 'open') {
    await bridge.open(id);
    toast('窗口已打开：在里面正常操作，平台自己的下载会自动登记');
  } else if (act === 'submit') {
    const prompt = String(row?.querySelector('[data-role="prompt"]')?.value || '').trim();
    if (!prompt) {
      toast('先填提示词再点生成', true);
      return;
    }
    const out = await bridge.submit(id, prompt);
    toast(out?.ok ? '已提交，出图后会自动登记为生成结果' : out?.error || '生成失败', !out?.ok);
  } else if (act === 'enabled') {
    const on = row.querySelector('[data-wa-act="enabled"]')?.checked;
    await api(`/api/web-accounts/${encodeURIComponent(id)}`, { method: 'PATCH', body: { enabled: on } });
  } else if (act === 'del') {
    if (!window.confirm(`删除账号「${account?.name || id}」？它的浏览器分区不会被删，重新加回来还能接着用。`)) return;
    await api(`/api/web-accounts/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }
  await refreshWebAccounts();
}

/** 账号分区里拦到一次下载：登记成生成结果，并给这个账号记一笔消耗 */
async function onWebAccDownloaded(payload) {
  const out = payload || {};
  if (!out.ok || !out.localPath) {
    toast(`拦到一次下载但没落盘：${out.error || '原因未知'}`, true);
    return;
  }
  try {
    await api('/api/image-import', {
      method: 'POST',
      body: {
        localFile: out.localPath,
        platform: out.platform || out.provider || 'web',
        prompt: '',
        sourceUrl: out.sourceUrl || '',
      },
    });
    // 平台自己下载的（手动通道）也要记账，否则额度池会一直以为这个号没用过
    if (out.accountId) {
      await api(`/api/web-accounts/${encodeURIComponent(out.accountId)}/commit`, { method: 'POST', body: {} }).catch(() => {});
    }
    toast('平台下载了一张图，已登记为生成结果');
    await refresh();
  } catch (err) {
    toast(`登记失败：${err.message}`, true);
  }
}

$('#btnAccounts').addEventListener('click', async () => {
  $('#modalAccounts').hidden = false;
  renderAccountList();
  if (desktop) {
    await syncDesktopState();
    renderAccountList();
    // 网页平台账号只有桌面端有（要开浏览器分区），浏览器版整块藏着
    if (desktop.webacc) {
      $('#webaccBlock').hidden = false;
      await refreshWebAccounts();
    }
  }
});

$('#webAccountList').addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-wa-act]');
  if (!btn) return;
  const row = btn.closest('.account-row');
  const id = row?.dataset.waId;
  if (!id) return;
  // 勾选框自己就是那个「动作」：change 会跟着 click 来，这里统一处理即可
  try {
    btn.disabled = true;
    await webAccAct(id, btn.dataset.waAct, row);
  } catch (err) {
    toast(err.message || String(err), true);
  } finally {
    btn.disabled = false;
  }
});

$('#waAdd')?.addEventListener('click', async () => {
  const provider = String($('#waProvider')?.value || '').trim();
  const name = String($('#waName')?.value || '').trim();
  if (!provider) {
    toast('先选一个平台', true);
    return;
  }
  if (!name) {
    toast('给这个账号起个名字（比如「豆包-小号2」）', true);
    return;
  }
  try {
    await api('/api/web-accounts', { method: 'POST', body: { provider, name } });
    $('#waName').value = '';
    toast(`已添加「${name}」，点它那行的「登录」登一次就能用了`);
    await refreshWebAccounts();
  } catch (err) {
    toast(err.message, true);
  }
});

// 账号分区里拦到平台自己的下载 → 登记成生成结果（整场生命周期只注册一次）
if (desktop?.webacc?.onDownloaded) {
  desktop.webacc.onDownloaded((payload) => onWebAccDownloaded(payload));
}

$('#modalAccounts').addEventListener('click', (event) => {
  if (event.target.id === 'modalAccounts' || event.target.closest('[data-close]')) {
    $('#modalAccounts').hidden = true;
  }
});

$('#accountForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    await api('/api/accounts', {
      method: 'POST',
      body: {
        name: form.name.value.trim(),
        apiKey: form.apiKey.value.trim(),
        site: form.site.value,
        concurrency: Number(form.concurrency.value) || 1,
      },
    });
    form.reset();
    form.concurrency.value = 1;
    toast('账号已添加');
    await refresh();
    renderAccountList();
  } catch (err) {
    toast(err.message, true);
  }
});

// 选了国际站就把「去拿 Key」的链接也指过去，免得拿错站的 Key（两边账号不通用）
document.querySelector('#accountForm select[name="site"]')?.addEventListener('change', (event) => {
  const link = document.querySelector('#accountForm .key-src a');
  if (!link) return;
  const ai = event.target.value === 'ai';
  link.href = ai ? 'https://www.runninghub.ai/' : 'https://www.runninghub.cn/';
  link.textContent = ai
    ? '去 RunningHub 国际站拿 Key（用户中心 → API Key）↗'
    : '去 RunningHub 拿 Key（用户中心 → API Key）↗';
});

// 一键复制全部账号：名称 · 站点 · API Key，一行一个（备份 / 换机导入用）
$('#btnCopyAllAccounts')?.addEventListener('click', async () => {
  if (!state.accounts.length) {
    toast('还没有账号可复制', true);
    return;
  }
  // /api/state 里的 Key 是打过码的，逐个取明文
  const creds = await Promise.all(
    state.accounts.map((a) => api(`/api/accounts/${a.id}/credentials`).catch(() => null)),
  );
  const siteName = (s) => (s === 'ai' ? '国际站' : '国内站');
  const text = state.accounts
    .map((a, i) => `${a.name}\t${siteName(a.site)}\t${creds[i]?.apiKey || ''}`)
    .join('\n');
  const ok = await copyText(text);
  toast(ok ? `已复制 ${state.accounts.length} 个账号（每行：名称 · 站点 · API Key）` : '复制失败，请手动选中后再复制', !ok);
});

$('#accountList').addEventListener('click', async (event) => {
  const row = event.target.closest('[data-account-id]');
  if (!row) return;
  const id = row.dataset.accountId;
  const action = event.target.closest('[data-act]')?.dataset.act;
  try {
    if (action === 'delete-account') {
      if (!(await confirmDialog('删除该账号？'))) return;
      await api(`/api/accounts/${id}`, { method: 'DELETE' });
      await refresh();
      renderAccountList();
    } else if (action === 'toggle-site') {
      // 快速修正加错的站：点徽标直接在 国内 .cn / 国际 .ai 之间切换（服务端会作废旧余额、按新站重查）
      const acc = state.accounts.find((x) => x.id === id);
      if (!acc) return;
      const next = acc.site === 'ai' ? 'cn' : 'ai';
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { site: next } });
      acc.site = next;
      toast(`「${acc.name}」已切换为${next === 'ai' ? '国际站 .ai' : '国内站 .cn'}，余额会按新站重新查询`);
      renderAccountList();
      return;
    } else if (action === 'change-key') {
      editingKeyId = id;
      keyDraft = '';
      renderAccountList();
      $('#accountList').querySelector('[data-role="new-key"]')?.focus();
      return;
    } else if (action === 'cancel-key') {
      editingKeyId = null;
      keyDraft = '';
      renderAccountList();
      return;
    } else if (action === 'save-key') {
      const key = (row.querySelector('[data-role="new-key"]')?.value || '').trim();
      if (!key) {
        toast('请先粘贴新的 API Key', true);
        return;
      }
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { apiKey: key } });
      editingKeyId = null;
      keyDraft = '';
      toast('API Key 已更换，正在重新查余额');
      await refresh();
      renderAccountList();
      return;
    } else if (action === 'copy-account') {
      // /api/state 里的 Key 是打过码的，明文要走 credentials 接口
      const cred = await api(`/api/accounts/${id}/credentials`);
      if (!cred?.apiKey) return;
      const name = state.accounts.find((x) => x.id === id)?.name || '';
      const ok = await copyText(cred.apiKey);
      toast(ok ? `已复制「${name}」的 API Key` : '复制失败，请手动选中 Key 再按 Ctrl+C', !ok);
      return;
    } else if (action === 'edit-login') {
      editingLoginId = id;
      renderAccountList();
      $('#accountList').querySelector('[data-role="login-name"]')?.focus();
      return;
    } else if (action === 'cancel-login') {
      editingLoginId = null;
      renderAccountList();
      return;
    } else if (action === 'save-login') {
      const loginName = (row.querySelector('[data-role="login-name"]')?.value || '').trim();
      const loginPass = row.querySelector('[data-role="login-pass"]')?.value || '';
      if (!loginName || !loginPass) {
        toast('登录账号和密码都要填（不想存了就把两个都清空再保存）', true);
        return;
      }
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { loginName, loginPass } });
      editingLoginId = null;
      toast('已保存登录账密：之后点「登录」会自动填写');
      renderAccountList();
      return;
    } else if (action === 'test-account') {
      const out = row.querySelector('[data-role="test-result"]');
      out.className = 'test-result';
      out.textContent = '测试中…';
      const result = await api(`/api/accounts/${id}/test`, { method: 'POST', body: {} });
      out.className = `test-result ${result.ok ? 'ok' : 'err'}`;
      out.textContent = result.message;
    } else if (action === 'login-web') {
      // 按账号的站打开登录窗口（国际站账号登 .ai，国内站登 .cn）
      const site = state.accounts.find((x) => x.id === id)?.site;
      const out = await desktop.login.open(id, site);
      if (!out?.ok) toast(out?.error || '打开登录窗口失败', true);
      else toast('登录窗口已打开：在里面登录一次，之后每天自动领币');
      return;
    } else if (action === 'refresh-balance') {
      const hint = row.querySelector('[data-role="test-result"]');
      if (hint) {
        hint.className = 'test-result';
        hint.textContent = '查询余额中…';
      }
      const result = await api(`/api/accounts/${id}/balance`, { method: 'POST', body: {} });
      const balance = result.balance || {};
      // 先刷新（可能会重建列表），再往（新的）那一行写结果
      await refresh();
      const fresh = $('#accountList').querySelector(`[data-account-id="${id}"]`) || row;
      const out = fresh.querySelector('[data-role="test-result"]');
      if (out) {
        out.className = `test-result ${balance.error ? 'err' : 'ok'}`;
        const money =
          balance.remainMoney === null || balance.remainMoney === undefined
            ? ''
            : ` · 钱包 ${balance.currency || ''} ${balance.remainMoney}`;
        out.textContent = balance.error
          ? `余额查询失败：${balance.error}`
          : `RH 币 ${balance.remainCoins ?? '--'}${money}`;
      }
    }
  } catch (err) {
    toast(err.message, true);
  }
});

$('#accountList').addEventListener('change', async (event) => {
  const row = event.target.closest('[data-account-id]');
  if (!row) return;
  const id = row.dataset.accountId;
  try {
    if (event.target.matches('[data-role="conc"]')) {
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { concurrency: Number(event.target.value) || 1 } });
      await refresh();
      renderAccountList();
    } else if (event.target.matches('[data-role="quota"]')) {
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { dailyLimit: Number(event.target.value) } });
      await refresh();
      renderAccountList();
    } else if (event.target.matches('[data-role="enabled"]')) {
      await api(`/api/accounts/${id}`, { method: 'PATCH', body: { enabled: event.target.checked } });
      await refresh();
      renderAccountList();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

// 换 Key 是行内输入框：边打边存草稿，回车直接保存
$('#accountList').addEventListener('input', (event) => {
  if (event.target.dataset.role === 'new-key') keyDraft = event.target.value;
});

$('#accountList').addEventListener('keydown', (event) => {
  if (event.target.dataset.role !== 'new-key') return;
  if (event.key === 'Escape') {
    editingKeyId = null;
    keyDraft = '';
    renderAccountList();
  } else if (event.key === 'Enter') {
    event.target.closest('[data-account-id]')?.querySelector('[data-act="save-key"]')?.click();
  }
});

/* -------------------------------- 图片生成面板 ------------------------------- */

/** 面板总刷新入口：datalist / 模型下拉 / 参考图 / 状态提示 / 结果区 */
function syncGenPanel() {
  renderGenDatalists();
  renderGenModels();
  syncGenMode();
  renderGenRefs();
  syncGenStatus();
  syncGenResults();
}

const genMode = () => $('#genMode').value;
const isImageMode = () => genMode() === 'image';

/** 参考图区只在图生图模式下出现 */
function syncGenMode() {
  $('#genRefRow').hidden = !isImageMode();
}

/** 参考图的唯一键：图库记录用 id，外网链接图用 url */
const refKey = (r) => r.id || `url:${r.url}`;

/** 参考图缩略图：× 只是从本次待提交里移除，图库里的文件由后端按需回收 */
function renderGenRefs() {
  const list = $('#genRefList');
  const sig = genState.refs.map(refKey).join(',');
  // 面板每 1.5 秒刷新一次，签名没变就别重建缩略图，免得图片一直闪
  if (list.dataset.sig !== sig) {
    list.dataset.sig = sig;
    list.textContent = '';
    for (const ref of genState.refs) {
      const key = refKey(ref);
      const item = document.createElement('div');
      item.className = 'gen-ref-item';
      item.dataset.refKey = key;

      // 缩略图单独一层：× 定位在它右上角，下方再挂「替换」按钮
      const thumb = document.createElement('div');
      thumb.className = 'gen-ref-thumb';
      const img = document.createElement('img');
      img.src = ref.localUrl || ref.url;
      img.alt = '';
      img.title = ref.url ? `外网链接：${ref.url}` : ref.originalName || ref.fileName || '';
      thumb.appendChild(img);
      const del = document.createElement('button');
      del.className = 'gen-ref-del';
      del.type = 'button';
      del.dataset.act = 'gen-ref-remove';
      del.dataset.refKey = key;
      del.textContent = '×';
      del.title = '移除这张参考图';
      thumb.appendChild(del);
      item.appendChild(thumb);

      const swap = document.createElement('button');
      swap.className = 'gen-ref-swap';
      swap.type = 'button';
      swap.dataset.act = 'gen-ref-swap';
      swap.dataset.refKey = key;
      swap.textContent = '替换';
      swap.title = '挑一张新图换掉这张（旧图仍留在图库里）';
      item.appendChild(swap);

      list.appendChild(item);
    }
  }

  const limit = maxRefs();
  const hint = $('#genRefHint');
  if (!genState.refs.length) {
    hint.className = 'hint';
    hint.textContent = `还没添加参考图（最多 ${limit} 张）`;
  } else {
    hint.className = 'hint';
    hint.textContent = `已选 ${genState.refs.length} / ${limit} 张，也可点生成结果上的「用作参考图」`;
  }
  $('#genRefAdd').disabled = genState.refs.length >= limit;
}

/** 把一条生成记录当时的参数填回表单，便于在原图基础上再改一版 */
function applyGenReuse(task, refs) {
  const select = $('#genModel');
  const model = enabledImageModels().find((m) => m.id === task.modelId);
  if (model) {
    select.value = model.id;
    // 标记已同步，避免下一次刷新把尺寸又覆盖回模型默认值
    select.dataset.modelId = model.id;
    renderGenRatios();
  }
  genState.refs = refs;
  $('#genMode').value = refs.length ? 'image' : 'text';
  $('#genCount').value = task.count || 1;
  $('#genPrompt').value = task.prompt || '';
  $('#genSize').value = task.size || '';
  // 记录里的尺寸反查回比例，尺寸候选也跟着收窄
  $('#genRatio').value = ratioOfSize(genRatioMap(), task.size);
  renderSizeDatalist();
  syncGenMode();
  renderGenRefs();
  syncGenStatus();
}

/**
 * 把参考图追加进图生图表单并切到图生图模式，返回真正加进去的张数（超上限的丢掉）。
 * 生成结果上的「用作参考图」和素材模块的「添加到图片生成」都走这里。
 * 调用方负责先检查上限，避免白跑一次上传/下载。
 */
function addGenRefs(refs, { stay = false } = {}) {
  const room = Math.max(0, maxRefs() - genState.refs.length);
  const added = refs.slice(0, room);
  if (!added.length) return 0;
  genState.refs.push(...added);
  // 直接用起来更顺手：切到图生图并展开参考图区
  $('#genMode').value = 'image';
  syncGenMode();
  renderGenRefs();
  syncGenStatus();
  // stay：素材里连截几张图时别每截一张就把界面跳去图片生成
  if (!stay) {
    switchModule('gen');
    // 切到新模块的「高级 · 模型」子页并展开参考图区，否则投送完看不到表单
    window.RHGen?.showForm?.();
  }
  return added.length;
}

// 素材模块（materials.js）拿不到本文件的闭包，参考图投送由这里接管
window.RHUtil.addRefs = addGenRefs;

/** 尺寸候选按协议切换：方舟用 1K/2K…，智谱只认「宽x高」枚举，即梦用推荐宽高，
 *  万相由原图或平台决定（无候选），混元用「宽:高」，MiniMax 用比例 */
function sizePresetsFor(protocol) {
  if (protocol === 'zhipu') return state.imageDefaults.zhipuSizePresets || [];
  if (protocol === 'jimeng') return state.imageDefaults.jimengSizePresets || [];
  if (protocol === 'hunyuan') return state.imageDefaults.hunyuanSizePresets || [];
  if (protocol === 'minimax') return state.imageDefaults.minimaxSizePresets || [];
  if (protocol === 'cloudflare') return state.imageDefaults.cloudflareSizePresets || [];
  if (protocol === 'wanxiang') return [];
  return state.imageDefaults.sizePresets || [];
}

/** 当前选中的图片模型 */
function currentGenModel() {
  return state.imageModels.find((m) => m.id === $('#genModel').value) || null;
}

/** 「比例 → 尺寸」对照表；万相只有文生图（t2i）模型能指定尺寸，图生图跟随原图 */
function genRatioMap() {
  const model = currentGenModel();
  const map = (state.imageDefaults.ratioPresets || {})[model?.protocol] || {};
  if (model?.protocol === 'wanxiang' && !/t2i/i.test(String(model.model || ''))) return {};
  return map;
}

/** 尺寸属于哪个比例：模型管理里存的默认尺寸要反查回比例，选中比例才不会跟尺寸脱节 */
function ratioOfSize(map, size) {
  const want = String(size || '').trim();
  if (!want) return '';
  for (const [ratio, list] of Object.entries(map)) if (list.includes(want)) return ratio;
  return '';
}

/** 比例下拉：只列当前协议真能表达的比例（比如智谱没有 3:2 就不出现） */
function renderGenRatios() {
  const ratios = Object.keys(genRatioMap());
  const select = $('#genRatio');
  const sig = `${currentGenModel()?.id || ''}|${ratios.join('|')}`;
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    const auto = document.createElement('option');
    auto.value = '';
    auto.textContent = ratios.length ? '自动（按尺寸 / 平台默认）' : '该模型不支持指定比例';
    select.appendChild(auto);
    for (const ratio of ratios) {
      const option = document.createElement('option');
      option.value = ratio;
      option.textContent = ratio;
      select.appendChild(option);
    }
  }
  select.disabled = !ratios.length;
  if (!ratios.includes(select.value)) select.value = '';
}

/** 尺寸候选写进 datalist；内容签名没变就不重建。selector 默认给生成面板那份 */
function fillSizeDatalist(sizes, sig, selector = '#genSizeList') {
  const sizeList = $(selector);
  if (!sizeList) return;
  const full = `${sig}|${sizes.join('|')}`;
  if (sizeList.dataset.sig === full) return;
  sizeList.dataset.sig = full;
  sizeList.textContent = '';
  for (const size of sizes) {
    const option = document.createElement('option');
    option.value = size;
    sizeList.appendChild(option);
  }
}

/** 尺寸候选：选了比例就只给这个比例的档位，没选就是该协议的全量候选 */
function renderSizeDatalist() {
  const model = currentGenModel();
  const ratio = $('#genRatio').value;
  const map = genRatioMap();
  const sizes = ratio && map[ratio] ? map[ratio] : sizePresetsFor(model?.protocol);
  fillSizeDatalist(sizes, `gen|${model?.protocol || ''}|${ratio}`);
}

/** 尺寸与模型 ID 候选，只在候选清单变化时重建 */
function renderGenDatalists() {
  const models = [
    ...(state.imageDefaults.arkModels || []),
    ...(state.imageDefaults.zhipuModels || []),
    ...(state.imageDefaults.jimengModels || []),
    ...(state.imageDefaults.wanxiangModels || []),
    ...(state.imageDefaults.hunyuanModels || []),
    ...(state.imageDefaults.minimaxModels || []),
    ...(state.imageDefaults.cloudflareModels || []),
  ];
  const arkList = $('#arkModelList');
  const modelSig = models.join('|');
  if (arkList.dataset.sig !== modelSig) {
    arkList.dataset.sig = modelSig;
    arkList.textContent = '';
    for (const id of models) {
      const option = document.createElement('option');
      option.value = id;
      arkList.appendChild(option);
    }
  }
}

const enabledImageModels = () => state.imageModels.filter((m) => m.enabled);

/** 模型下拉只列启用中的模型；重建后尽量保留用户当前选择 */
function renderGenModels() {
  const select = $('#genModel');
  const models = enabledImageModels();
  const sig = models.map((m) => `${m.id}:${m.name}:${m.hasKey}:${m.size}`).join('|');

  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    const previous = select.value;
    select.textContent = '';
    if (!models.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '（还没有可用的图片模型）';
      select.appendChild(option);
    } else {
      for (const model of models) {
        const option = document.createElement('option');
        option.value = model.id;
        option.textContent = model.hasKey ? model.name : `${model.name}（缺 Key）`;
        select.appendChild(option);
      }
    }
    select.value = models.some((m) => m.id === previous) ? previous : models[0]?.id || '';
  }

  // 选中模型发生变化（含自动选中）时，把尺寸切到该模型的默认尺寸，并按尺寸反查出比例
  const model = models.find((m) => m.id === select.value) || null;
  renderGenRatios();
  if (model && select.dataset.modelId !== model.id) {
    select.dataset.modelId = model.id;
    $('#genSize').value = model.size || '';
    $('#genRatio').value = ratioOfSize(genRatioMap(), model.size);
  } else if (!model) {
    select.dataset.modelId = '';
  }
  renderSizeDatalist();
}

/** 状态提示行 + 生成按钮可用性 */
function syncGenStatus() {
  const select = $('#genModel');
  const models = enabledImageModels();
  const model = models.find((m) => m.id === select.value) || null;
  const status = $('#genStatus');
  const imageMode = isImageMode();

  if (genState.submitting) return; // 提交中保持禁用，避免刷新把按钮恢复

  if (!models.length) {
    status.className = 'hint err';
    status.textContent = '还没有可用的图片模型，点「模型管理」创建';
    $('#genSubmit').disabled = true;
    return;
  }
  if (imageMode && !genState.refs.length) {
    status.className = 'hint err';
    status.textContent = '图生图需要先添加至少一张参考图';
    $('#genSubmit').disabled = true;
    return;
  }
  if (imageMode && model && !model.supportsImageInput) {
    status.className = 'hint err';
    status.textContent = '该模型未开启图生图，请在「模型管理」里勾选「支持图生图」或换一个模型';
    $('#genSubmit').disabled = true;
    return;
  }
  if (!imageMode && model && model.protocol === 'wanxiang') {
    status.className = 'hint err';
    status.textContent = '通义万相只能做图生图，请切到「图生图」模式并添加 1 张参考图';
    $('#genSubmit').disabled = true;
    return;
  }
  $('#genSubmit').disabled = false;
  if (model && !model.hasKey) {
    status.className = 'hint err';
    status.textContent = '该模型还没填 API Key，点「模型管理」补上';
  } else if (imageMode) {
    status.className = 'hint';
    status.textContent = `图生图 · ${genState.refs.length} 张参考图 · ${model?.model || '未填模型 ID'}`;
  } else {
    status.className = 'hint';
    status.textContent = model ? `就绪 · ${model.model || '未填模型 ID'}` : '';
  }
}

/** 收集可送入的目标：所有画布里 fieldType 为 IMAGE 的参数 */
function collectCanvasTargets() {
  const targets = [];
  for (const canvas of state.canvases) {
    for (const field of canvas.fields || []) {
      if (String(field.fieldType || '').toUpperCase() !== 'IMAGE') continue;
      targets.push({
        value: `${canvas.id}|${field.nodeId}::${field.fieldName}`,
        label: `${canvas.name} · ${field.description || field.fieldName}`,
      });
    }
  }
  return targets;
}

/** 收集可送入的目标：所有画布里 fieldType 为 VIDEO 的参数（素材模块「添加到画布」用） */
function videoTargets() {
  const targets = [];
  for (const canvas of state.canvases) {
    for (const field of canvas.fields || []) {
      if (String(field.fieldType || '').toUpperCase() !== 'VIDEO') continue;
      targets.push({
        value: `${canvas.id}|${field.nodeId}::${field.fieldName}`,
        label: `${canvas.name} · ${field.description || field.fieldName}`,
      });
    }
  }
  return targets;
}

// 素材模块（materials.js）拿不到本文件闭包里的 state，所以这两件事由这里提供
window.RHUtil.videoTargets = videoTargets;

/** 任务卡内容签名：只有这些数据变了才重建 DOM，避免刷新打断用户的下拉选择 */
function genTaskSig(task) {
  return JSON.stringify([
    task.status,
    task.error || '',
    task.modelName || '',
    task.prompt || '',
    task.size || '',
    task.count || 0,
    task.attempts || 0,
    (task.refs || []).map((r) => r.id).join(','),
    (task.images || []).map((i) => [
      i.id,
      i.size,
      i.usedBy ? `${i.usedBy.canvasId}:${i.usedBy.fieldKey}` : '',
    ]),
  ]);
}

function syncGenResults() {
  const wrap = $('#genResults');
  const tasks = state.imageTasks;

  if (!tasks.length) {
    if (!wrap.querySelector('.gen-empty')) {
      wrap.textContent = '';
      const hint = document.createElement('div');
      hint.className = 'gen-empty hint';
      hint.textContent = '还没有生成记录：填好提示词，点「生成图片」';
      wrap.appendChild(hint);
    }
    return;
  }
  wrap.querySelector('.gen-empty')?.remove();

  const existing = new Map();
  for (const child of wrap.querySelectorAll('.gen-task')) existing.set(child.dataset.taskId, child);

  const targets = collectCanvasTargets();
  const targetsSig = targets.map((t) => t.value).join('|');

  let sawNewTask = false; // 这次刷新有没有冒出新的任务卡

  tasks.forEach((task, index) => {
    let card = existing.get(task.id);
    if (!card) {
      card = document.createElement('article');
      card.className = 'gen-task';
      card.dataset.taskId = task.id;
      fillGenTask(card, task, targets, targetsSig);
      sawNewTask = true;
    } else if (card.dataset.sig !== genTaskSig(task) || card.dataset.targetsSig !== targetsSig) {
      fillGenTask(card, task, targets, targetsSig);
    }
    if (wrap.children[index] !== card) wrap.insertBefore(card, wrap.children[index] || null);
  });

  for (const child of [...wrap.children]) {
    if (child.classList.contains('gen-task') && !tasks.some((t) => t.id === child.dataset.taskId)) child.remove();
  }

  // 新任务永远插在最左边，而这条结果栏是横向滚动的：不滚回去，新任务就藏在左边界外，
  // 每次生成都得手动划一下才看得见。出现新任务时自动滑到最左，让「当前这条」直接露出来。
  if (sawNewTask) wrap.scrollTo({ left: 0, behavior: 'smooth' });
}

function fillGenTask(card, task, targets, targetsSig) {
  // 记录当前下拉选择，重建后原样恢复
  const previousChoice = {};
  for (const select of card.querySelectorAll('select[data-role="target"]')) {
    previousChoice[select.dataset.imageId] = select.value;
  }
  card.textContent = '';

  const head = document.createElement('div');
  head.className = 'gen-task-head';

  const badge = document.createElement('span');
  badge.className = `badge ${task.status}`;
  badge.textContent = IMAGE_STATUS_LABEL[task.status] || task.status;
  head.appendChild(badge);

  const modelName = document.createElement('span');
  modelName.className = 'gen-task-model';
  modelName.textContent = task.modelName || '（模型已删除）';
  modelName.title = task.modelName || '';
  head.appendChild(modelName);

  const meta = document.createElement('span');
  meta.className = 'gen-task-time';
  meta.textContent = `${task.refs?.length ? `图生图 ${task.refs.length}张 · ` : ''}${task.size || ''}${task.count ? ` · ${task.count}张` : ''}`;
  head.appendChild(meta);

  const time = document.createElement('span');
  time.className = 'gen-task-time';
  time.textContent = new Date(task.createdAt).toLocaleString();
  head.appendChild(time);

  const reuse = document.createElement('button');
  reuse.className = 'btn tiny';
  reuse.dataset.act = 'gen-reuse';
  reuse.textContent = '复用';
  reuse.title = '把这次的提示词、尺寸、数量、参考图填回左边的表单，改完直接再生成一版';
  head.appendChild(reuse);

  const del = document.createElement('button');
  del.className = 'btn tiny danger gen-del';
  del.dataset.act = 'gen-delete';
  del.textContent = '删除记录';
  head.appendChild(del);
  card.appendChild(head);

  const prompt = document.createElement('div');
  prompt.className = task.prompt ? 'gen-task-prompt copyable' : 'gen-task-prompt';
  prompt.textContent = task.prompt || '';
  prompt.title = task.prompt ? '点击复制提示词' : '';
  if (task.prompt) prompt.dataset.act = 'gen-copy-prompt';
  card.appendChild(prompt);

  // 参考图只在记录里展示：证明这一版是照着哪几张图画出来的
  if (task.refs?.length) {
    const refs = document.createElement('div');
    refs.className = 'gen-task-refs';
    for (const ref of task.refs) {
      const item = document.createElement('div');
      item.className = 'gen-ref-item';
      const thumb = document.createElement('div');
      thumb.className = 'gen-ref-thumb';
      const img = document.createElement('img');
      img.src = ref.localUrl;
      img.alt = '';
      img.loading = 'lazy';
      img.title = ref.originalName || ref.fileName || '';
      thumb.appendChild(img);
      item.appendChild(thumb);
      refs.appendChild(item);
    }
    card.appendChild(refs);
  }

  const body = document.createElement('div');
  body.className = 'gen-task-body';

  if (task.status === 'success' && (task.images || []).length) {
    const images = document.createElement('div');
    images.className = 'gen-images';
    for (const image of task.images) images.appendChild(buildGenImage(image, targets));
    body.appendChild(images);
  } else if (task.status === 'failed') {
    const error = document.createElement('div');
    error.className = 'error-box';
    error.textContent = task.error || '生成失败';
    body.appendChild(error);
    const retry = document.createElement('button');
    retry.className = 'btn tiny';
    retry.dataset.act = 'gen-retry';
    retry.textContent = '重新生成';
    body.appendChild(retry);
  } else if (task.status === 'queued') {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = '排队中，等待前一个任务完成…';
    body.appendChild(hint);
    const cancel = document.createElement('button');
    cancel.className = 'btn tiny';
    cancel.dataset.act = 'gen-cancel';
    cancel.textContent = '取消';
    body.appendChild(cancel);
  } else if (task.status === 'running') {
    const progress = document.createElement('div');
    progress.className = 'progress';
    const bar = document.createElement('div');
    bar.className = 'progress-bar';
    bar.appendChild(document.createElement('span'));
    progress.appendChild(bar);
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.style.marginTop = '7px';
    hint.textContent = '生成中…（请求已发出，无法取消）';
    progress.appendChild(hint);
    body.appendChild(progress);
  } else if (task.status === 'cancelled') {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = '任务已取消';
    body.appendChild(hint);
  } else {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = '（没有图片）';
    body.appendChild(hint);
  }
  card.appendChild(body);

  for (const select of card.querySelectorAll('select[data-role="target"]')) {
    if (previousChoice[select.dataset.imageId]) select.value = previousChoice[select.dataset.imageId];
  }
  card.dataset.sig = genTaskSig(task);
  card.dataset.targetsSig = targetsSig;
}

function buildGenImage(image, targets) {
  const box = document.createElement('div');
  box.className = 'gen-image';
  box.dataset.imageId = image.id;

  const link = document.createElement('a');
  link.href = image.localUrl;
  link.target = '_blank';
  link.rel = 'noreferrer';
  const img = document.createElement('img');
  img.src = image.localUrl;
  img.alt = '';
  img.loading = 'lazy';
  link.appendChild(img);
  box.appendChild(link);

  const meta = document.createElement('div');
  meta.className = 'gen-image-meta';
  const sizeTag = document.createElement('span');
  sizeTag.textContent = fmtSize(image.size);
  meta.appendChild(sizeTag);
  if (image.usedBy) {
    const used = document.createElement('span');
    used.className = 'used-tag';
    used.textContent = `已送入 ${image.usedBy.canvasName || '画布'}`;
    used.title = `已送入「${image.usedBy.canvasName || ''}」的 ${image.usedBy.fieldKey || ''}`;
    meta.appendChild(used);
  }
  if (image.sentToVideo) {
    const sent = document.createElement('span');
    sent.className = 'used-tag';
    sent.textContent = '已送入视频模块';
    sent.title = `已送去视频生成模块（${new Date(image.sentToVideo.at).toLocaleString()}）`;
    meta.appendChild(sent);
  }
  box.appendChild(meta);

  const actions = document.createElement('div');
  actions.className = 'gen-image-actions';

  const download = document.createElement('a');
  download.className = 'btn tiny';
  download.href = image.localUrl;
  download.download = image.fileName || '';
  download.textContent = '下载';
  actions.appendChild(download);

  const asRef = document.createElement('button');
  asRef.className = 'btn tiny';
  asRef.dataset.act = 'use-ref';
  asRef.textContent = '用作参考图';
  asRef.title = '把这张图加进图生图的参考图里，可以再改一版';
  actions.appendChild(asRef);

  const toVideo = document.createElement('button');
  toVideo.className = 'btn tiny';
  toVideo.dataset.act = 'send-video';
  toVideo.textContent = '送去视频';
  toVideo.title = '把这张图送进视频生成模块当输入图（会复制一份，删掉这里的记录也不影响）';
  actions.appendChild(toVideo);

  const select = document.createElement('select');
  select.dataset.role = 'target';
  select.dataset.imageId = image.id;
  if (!targets.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '先在画布上点「读取参数」';
    select.appendChild(option);
    select.disabled = true;
  } else {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '选择目标画布参数…';
    select.appendChild(placeholder);
    for (const target of targets) {
      const option = document.createElement('option');
      option.value = target.value;
      option.textContent = target.label;
      select.appendChild(option);
    }
  }
  actions.appendChild(select);

  const send = document.createElement('button');
  send.className = 'btn tiny primary';
  send.dataset.act = 'send-canvas';
  send.textContent = '送入';
  // 不置灰：没选目标时点它会弹提示，置灰了点了完全没反应
  actions.appendChild(send);

  box.appendChild(actions);
  return box;
}

$('#genToggle').addEventListener('click', () => {
  const panel = $('#genPanel');
  panel.classList.toggle('collapsed');
  try {
    localStorage.setItem(GEN_COLLAPSED_KEY, panel.classList.contains('collapsed') ? '1' : '0');
  } catch (err) {
    /* 隐私模式下 localStorage 可能不可用，忽略即可 */
  }
});

$('#genMode').addEventListener('change', () => {
  syncGenMode();
  renderGenRefs();
  syncGenStatus();
});

/* ---------------------------------- 参考图 --------------------------------- */

/** 上传单张参考图：走原始二进制，和其他上传接口保持一致 */
async function uploadRefFile(file) {
  const res = await fetch('/api/image-refs', {
    method: 'POST',
    headers: {
      'Content-Type': file.type || 'application/octet-stream',
      'x-filename': encodeURIComponent(file.name),
    },
    body: file,
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new Error(json?.error || `上传失败（HTTP ${res.status}）`);
  return json;
}

// 素材模块的视频截图走这里：canvas 出来的 blob 当普通图片文件走同一个上传接口
window.RHUtil.uploadRefBlob = (blob, name) =>
  uploadRefFile(new File([blob], name || 'snapshot.jpg', { type: blob.type || 'image/jpeg' }));

$('#genRefAdd').addEventListener('click', () => $('#genRefInput').click());

// 外网链接参考图：即梦这类只收公网 URL 的协议用，不落本地图库
function addRefLink(link) {
  const value = String(link || '').trim();
  if (!value) return;
  if (!/^https?:\/\/\S+$/i.test(value)) return toast('链接必须是 http(s) 开头的公网地址', true);
  genState.refs.push({ id: '', url: value, localUrl: value, originalName: value.slice(0, 60) });
  renderGenRefs();
  syncGenStatus();
}

/**
 * 行内输入框代替 window.prompt()。
 * Electron 的渲染进程不支持 prompt()，调用会直接抛错 —— 按钮看起来就像坏了一样点了没反应。
 */
function openRefLinkInput() {
  const anchor = $('#genRefAddLink');
  const existing = anchor.parentElement.querySelector('.gen-ref-link');
  if (existing) return existing.focus();

  const input = document.createElement('input');
  input.className = 'gen-ref-link';
  input.placeholder = '粘贴公网图片链接（http/https）后回车';
  input.autocomplete = 'off';
  const close = () => input.remove();
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const value = input.value;
      close();
      addRefLink(value);
    } else if (event.key === 'Escape') {
      close();
    }
  });
  input.addEventListener('blur', close);
  anchor.after(input);
  input.focus();
}

$('#genRefAddLink').addEventListener('click', () => {
  const limit = maxRefs();
  if (genState.refs.length >= limit) return toast(`参考图最多 ${limit} 张`, true);
  openRefLinkInput();
});

$('#genRefInput').addEventListener('change', async (event) => {
  const input = event.target;
  const files = [...(input.files || [])];
  input.value = '';
  if (!files.length) return;

  const limit = maxRefs();
  const room = limit - genState.refs.length;
  if (room <= 0) return toast(`参考图最多 ${limit} 张`, true);

  const failed = [];
  for (const file of files.slice(0, room)) {
    try {
      genState.refs.push(await uploadRefFile(file));
    } catch (err) {
      failed.push(`${file.name}：${err.message}`);
    }
  }
  renderGenRefs();
  syncGenStatus();
  if (failed.length) toast(failed[0], true);
  else if (files.length > room) toast(`已加满 ${limit} 张，多余的没有添加`);
});

// 「替换」：记下是哪张，弹出文件选择，选完在 change 里原位换掉
$('#genRefSwapInput').addEventListener('change', async (event) => {
  const input = event.target;
  const file = (input.files || [])[0];
  input.value = '';
  const key = genState.swapKey;
  genState.swapKey = '';
  if (!file || !key) return;

  const index = genState.refs.findIndex((r) => refKey(r) === key);
  if (index < 0) return;
  try {
    // 和 × 一样只动本次待提交的数组：旧图库记录留着，不调 DELETE
    genState.refs[index] = await uploadRefFile(file);
  } catch (err) {
    return toast(err.message, true);
  }
  renderGenRefs();
  syncGenStatus();
  toast('已替换这张参考图');
});

$('#genRefList').addEventListener('click', (event) => {
  const swap = event.target.closest('[data-act="gen-ref-swap"]');
  if (swap) {
    genState.swapKey = swap.dataset.refKey;
    $('#genRefSwapInput').click();
    return;
  }
  const del = event.target.closest('[data-act="gen-ref-remove"]');
  if (!del) return;
  genState.refs = genState.refs.filter((r) => refKey(r) !== del.dataset.refKey);
  renderGenRefs();
  syncGenStatus();
});

$('#genRatio').addEventListener('change', () => {
  // 选了比例就把尺寸带成这个比例的默认档，同时把尺寸候选收窄到这个比例
  const list = genRatioMap()[$('#genRatio').value];
  if (list?.length) $('#genSize').value = list[0];
  renderSizeDatalist();
  syncGenStatus();
});

$('#genModel').addEventListener('change', (event) => {
  const model = state.imageModels.find((m) => m.id === event.target.value);
  if (model) $('#genSize').value = model.size || '';
  event.target.dataset.modelId = event.target.value; // 标记已同步，刷新时不再覆盖
  renderGenRatios();
  $('#genRatio').value = ratioOfSize(genRatioMap(), $('#genSize').value);
  renderSizeDatalist();
  syncGenStatus();
});

$('#genForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const submit = $('#genSubmit');
  const modelId = form.elements.namedItem('model').value;
  const prompt = form.elements.namedItem('prompt').value.trim();
  if (!modelId) return toast('请先选择图片模型', true);
  if (!prompt) return toast('请填写提示词', true);

  genState.submitting = true;
  submit.disabled = true;
  submit.textContent = '提交中…';
  try {
    await api('/api/image-tasks', {
      method: 'POST',
      body: {
        modelId,
        prompt,
        size: form.elements.namedItem('size').value.trim(),
        count: Number(form.elements.namedItem('count').value) || 1,
        // 文生图不带参考图；图生图把本机图库 id 和外网链接图一起交给后端
        refIds: isImageMode() ? genState.refs.map((r) => (r.url ? { url: r.url } : r.id)) : [],
      },
    });
    form.elements.namedItem('prompt').value = '';
    toast('已加入生成队列');
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    genState.submitting = false;
    submit.textContent = '生成图片';
    syncGenStatus();
  }
});

$('#genResults').addEventListener('click', async (event) => {
  const action = event.target.closest('[data-act]')?.dataset.act;
  if (!action) return;
  const taskId = event.target.closest('.gen-task')?.dataset.taskId;
  if (!taskId) return;

  try {
    if (action === 'send-canvas') {
      const imageBox = event.target.closest('.gen-image');
      const value = imageBox?.querySelector('select[data-role="target"]')?.value || '';
      const [canvasId, fieldKey] = value.split('|');
      if (!canvasId || !fieldKey) {
        // 顺手聚焦下拉框：只弹一句提示时，用户容易以为「点了没反应」
        imageBox?.querySelector('select[data-role="target"]')?.focus();
        return toast(
          collectCanvasTargets().length ? '先在下拉里选一个目标画布参数' : '还没有可送入的参数：先在画布上点「读取参数」',
          true,
        );
      }
      const button = event.target;
      button.disabled = true;
      button.textContent = '送入中…';
      try {
        const result = await api(`/api/image-tasks/${taskId}/to-canvas`, {
          method: 'POST',
          body: { canvasId, fieldKey, imageId: imageBox.dataset.imageId },
        });
        toast(`已送入「${result.canvasName}」的 ${result.field}`);
        await refresh();
      } finally {
        button.disabled = false;
        button.textContent = '送入';
      }
    } else if (action === 'send-video') {
      const imageBox = event.target.closest('.gen-image');
      const imageId = imageBox?.dataset.imageId || '';
      const button = event.target;
      button.disabled = true;
      try {
        await window.RHVideo.receiveImage(taskId, imageId);
        toast('已送去视频模块，输入图已就位');
        switchModule('video');
      } finally {
        button.disabled = false;
      }
    } else if (action === 'use-ref') {
      const imageBox = event.target.closest('.gen-image');
      const limit = maxRefs();
      if (genState.refs.length >= limit) return toast(`参考图最多 ${limit} 张`, true);
      const imageId = imageBox?.dataset.imageId || '';
      const button = event.target;
      button.disabled = true;
      try {
        addGenRefs([await api(`/api/image-tasks/${taskId}/ref`, {
          method: 'POST',
          body: { imageId },
        })]);
        toast(`已加入参考图（${genState.refs.length} 张），改好提示词再点「生成图片」`);
      } finally {
        button.disabled = false;
      }
    } else if (action === 'gen-copy-prompt') {
      // 用户正在拖选文字时不抢，让他安心选中自己想复制的片段
      if (String(window.getSelection?.() || '')) return;
      const task = state.imageTasks.find((t) => t.id === taskId);
      if (!task?.prompt) return;
      if (await copyText(task.prompt)) toast('提示词已复制');
      else toast('复制失败，请手动选中提示词再按 Ctrl+C', true);
    } else if (action === 'gen-reuse') {
      const task = state.imageTasks.find((t) => t.id === taskId);
      if (!task) return toast('这条记录刚被刷新掉，请稍后再试', true);
      // 参考图统一由后端取回：图库登记被回收的会就地复原，外网链接图原样还原
      const refs = task.refs?.length
        ? (await api(`/api/image-tasks/${taskId}/reuse`, { method: 'POST', body: {} })).refs || []
        : [];
      applyGenReuse(task, refs);
      toast(`已填回「${task.modelName}」那次的提示词${refs.length ? `和 ${refs.length} 张参考图` : ''}，改完点「生成图片」即可`);
    } else if (action === 'gen-retry') {
      await api(`/api/image-tasks/${taskId}/retry`, { method: 'POST', body: {} });
      toast('已重新排队');
      await refresh();
    } else if (action === 'gen-cancel') {
      await api(`/api/image-tasks/${taskId}/cancel`, { method: 'POST', body: {} });
      toast('已取消');
      await refresh();
    } else if (action === 'gen-delete') {
      if (!(await confirmDialog('删除这条生成记录？本地图片文件也会一起删除。'))) return;
      await api(`/api/image-tasks/${taskId}`, { method: 'DELETE' });
      toast('已删除记录');
      await refresh();
    }
  } catch (err) {
    toast(err.message, true);
  }
});

/* ------------------------------- 图片模型管理 ------------------------------- */

const IMAGE_MODEL_FIELDS = ['name', 'baseUrl', 'model', 'size', 'accountId'];

/** 各协议「去哪拿 Key」的页面，填 Key 时显示在输入框下方 */
const IMAGE_KEY_SRC = {
  ark: {
    url: 'https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey',
    label: '去火山方舟控制台创建 API Key',
  },
  zhipu: {
    url: 'https://bigmodel.cn/usercenter/proj-mgmt/apikeys',
    label: '去智谱开放平台创建 API Key（和视频模块的智谱 Key 通用）',
  },
  jimeng: {
    url: 'https://console.volcengine.com/iam/KeyManage/newViews',
    label: '去火山引擎控制台新建访问密钥（AK/SK），并先开通「即梦AI-图片生成4.0」服务',
  },
  openai: {
    url: 'https://platform.openai.com/api-keys',
    label: '去 OpenAI 创建 API Key（兼容接口填服务商给的 Key）',
  },
  wanxiang: {
    url: 'https://bailian.console.aliyun.com/?apiKey=1#/api-key',
    label: '去阿里云百炼控制台创建 API Key（和视频模块的百炼 Key 通用，新用户免费 500 张）',
  },
  hunyuan: {
    label: '腾讯混元生图调用指南（拿 Key）',
    url: 'https://cloud.tencent.com/document/product/1823/135745',
  },
  minimax: {
    label: 'MiniMax 图片生成文档（拿 Key）',
    url: 'https://platform.minimaxi.com/docs/guides/image-generation',
  },
  cloudflare: {
    label: '去 Cloudflare 创建 API Token（权限勾「Workers AI · Read」）',
    url: 'https://dash.cloudflare.com/profile/api-tokens',
  },
};

/** API Key 输入框下方标注该协议取 Key 的页面，点一下跳到浏览器 */
function renderImageKeySrc(protocol) {
  const box = $('#imKeySrc');
  const sig = protocol || '';
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.textContent = '';
  const src = IMAGE_KEY_SRC[protocol];
  if (!src) return;
  const link = document.createElement('a');
  link.href = src.url;
  link.target = '_blank';
  link.rel = 'noreferrer';
  link.textContent = `${src.label} ↗`;
  link.title = src.url;
  box.appendChild(link);
}

/** 添加表单的默认值：协议 ark、地址取方舟、尺寸 2K、勾选去水印 */
function applyImageModelFormDefaults() {
  const form = $('#imageModelForm');
  if (form.dataset.ready === '1') return;
  form.dataset.ready = '1';
  form.elements.namedItem('protocol').value = 'ark';
  form.elements.namedItem('baseUrl').value = state.imageDefaults.arkBaseUrl || '';
  form.elements.namedItem('size').value = '2K';
  form.elements.namedItem('noWatermark').checked = true;
  renderImageKeySrc('ark');
}

function buildSwitch(labelText, role, checked, className) {
  const label = document.createElement('label');
  label.className = `switch ${className || ''}`.trim();
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.dataset.role = role;
  input.checked = checked;
  label.appendChild(input);
  label.appendChild(document.createTextNode(labelText));
  return label;
}

/** 一行模型：全部用 createElement 构建，名称/模型 ID 等用户输入不进 innerHTML */
// 豆包桥的检测状态按模型 id 缓存在这里：列表每次 refresh 都重建行，
// 状态文案不能只存在 DOM 里，否则「检测连接」的结果一秒后就被抹掉
const doubaoStatus = new Map();

function buildImageModelRow(model) {
  const row = document.createElement('div');
  row.className = 'image-model-row';
  row.dataset.modelId = model.id;

  const name = document.createElement('input');
  name.className = 'im-name';
  name.dataset.role = 'name';
  name.value = model.name;
  name.title = '模型名称';
  name.placeholder = '模型名称';
  name.autocomplete = 'off';

  const protocol = document.createElement('select');
  protocol.className = 'im-protocol';
  protocol.dataset.role = 'protocol';
  protocol.title = '协议';
  for (const [value, label] of [
    ['ark', '火山方舟豆包'],
    ['zhipu', '智谱 CogView（免费）'],
    ['jimeng', '即梦图片 4.0（AK/SK）'],
    ['wanxiang', '通义万相图生图（百炼）'],
    ['hunyuan', '腾讯混元生图（TokenHub）'],
    ['minimax', 'MiniMax 海螺'],
    ['cloudflare', 'Cloudflare FLUX.2（免费）'],
    ['doubao', '豆包客户端（本机免 Key）'],
    ['openai', 'OpenAI 兼容'],
  ]) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    protocol.appendChild(option);
  }
  protocol.value = model.protocol;

  const baseUrl = document.createElement('input');
  baseUrl.className = 'im-url';
  baseUrl.dataset.role = 'baseUrl';
  baseUrl.value = model.baseUrl;
  baseUrl.title = '接口地址';
  baseUrl.placeholder = '接口地址';
  baseUrl.autocomplete = 'off';

  const isJimeng = model.protocol === 'jimeng';
  const isCloudflare = model.protocol === 'cloudflare';
  const isDoubao = model.protocol === 'doubao';
  const apiKey = document.createElement('input');
  apiKey.className = 'im-key';
  apiKey.dataset.role = 'apiKey';
  apiKey.type = 'password';
  apiKey.value = '';
  apiKey.placeholder = isDoubao
    ? '不需要 API Key'
    : model.hasKey
      ? '已配置，留空不修改'
      : isJimeng ? '填 AccessKeyID' : isCloudflare ? '填 API Token' : '填 API Key';
  apiKey.title = isDoubao
    ? '豆包桥不需要 API Key'
    : model.hasKey
      ? `当前：${model.apiKey}（留空表示不修改）`
      : isJimeng ? 'AccessKeyID' : isCloudflare ? 'Cloudflare API Token' : 'API Key';
  apiKey.autocomplete = 'off';
  if (isDoubao) apiKey.disabled = true;

  const akSecret = document.createElement('input');
  akSecret.className = 'im-secret';
  akSecret.dataset.role = 'akSecret';
  akSecret.type = 'password';
  akSecret.value = '';
  akSecret.placeholder = model.hasSecret ? 'Secret 已配置，留空不修改' : '填 AccessKeySecret';
  akSecret.title = model.hasSecret
    ? `当前：${model.akSecret}（留空表示不修改）`
    : 'AccessKeySecret（火山引擎控制台访问密钥）';
  akSecret.autocomplete = 'off';
  akSecret.hidden = !isJimeng;

  const accountId = document.createElement('input');
  accountId.className = 'im-account';
  accountId.dataset.role = 'accountId';
  accountId.value = model.accountId || '';
  accountId.placeholder = 'Cloudflare 账号 ID';
  accountId.title = 'Cloudflare Account ID（仅 cloudflare 协议需要，在控制台首页右下角可复制）';
  accountId.autocomplete = 'off';
  accountId.hidden = !isCloudflare;

  const modelId = document.createElement('input');
  modelId.className = 'im-model';
  modelId.dataset.role = 'model';
  modelId.setAttribute('list', 'arkModelList');
  modelId.value = model.model;
  modelId.title = '模型 ID';
  modelId.placeholder = '模型 ID';
  modelId.autocomplete = 'off';

  const size = document.createElement('input');
  size.className = 'im-size';
  size.dataset.role = 'size';
  // 用模型弹窗专属的 datalist，别和生成面板抢同一份（见 index.html 里的说明）
  size.setAttribute('list', 'imageModelSizeList');
  size.value = model.size;
  size.title = '默认尺寸';
  size.placeholder = '尺寸';
  size.autocomplete = 'off';

  const noWatermark = buildSwitch('去水印', 'noWatermark', Boolean(model.noWatermark));
  const img2img = buildSwitch(
    model.protocol === 'jimeng' ? '图生图(链接)' : '图生图',
    'supportsImageInput',
    Boolean(model.supportsImageInput),
  );
  img2img.title =
    model.protocol === 'jimeng'
      ? '即梦带参考图时会自动切「图生图 3.0」；参考图优先用公网图片链接，本机上传的图走 base64'
      : model.protocol === 'zhipu'
        ? 'CogView 接口没有参考图字段，勾了也不生效'
        : model.protocol === 'wanxiang'
          ? '通义万相的图像编辑模型必须勾选并带参考图；文生图模型（wan2.2-t2i 等）勾不勾都行'
          : model.protocol === 'cloudflare'
            ? 'FLUX.2 支持参考图编辑（最多 4 张）；超 512×512 的参考图会自动等比压缩后上传，直接选图做图生图即可'
            : model.protocol === 'doubao'
              ? '豆包桥会把参考图直接塞进豆包的输入框，所以图生图是支持的'
              : '勾选后这个模型才能带参考图做图生图';
  const enabled = buildSwitch('启用', 'enabled', Boolean(model.enabled), 'im-enabled');

  const del = document.createElement('button');
  del.className = 'btn tiny danger';
  del.dataset.act = 'delete-model';
  del.textContent = '删除';

  row.append(name, protocol, baseUrl, apiKey, akSecret, accountId, modelId, size, noWatermark, img2img, enabled, del);

  const arkHint = document.createElement('span');
  arkHint.className = 'hint im-ark-hint';
  arkHint.textContent = '方舟 watermark 默认开启，程序会自动传 watermark=false，因此产出无水印';
  arkHint.hidden = model.protocol !== 'ark';
  row.appendChild(arkHint);

  const jimengHint = document.createElement('span');
  jimengHint.className = 'hint im-jimeng-hint';
  jimengHint.textContent = '即梦 4.0 走火山引擎 AK/SK 签名；无参考图走文生图 4.0，带参考图自动切「图生图 3.0」（链接或本机图都行）；需先开通「即梦AI-图片生成4.0」服务';
  jimengHint.hidden = model.protocol !== 'jimeng';
  row.appendChild(jimengHint);

  const wanxiangHint = document.createElement('span');
  wanxiangHint.className = 'hint im-wanxiang-hint';
  wanxiangHint.textContent = '通义万相：图像编辑模型（wanx2.1-imageedit）必须带至少 1 张参考图；文生图模型（wan2.2-t2i-flash 等）不要参考图；wan2.6/2.7 支持多图，新用户免费 500 张';
  wanxiangHint.hidden = model.protocol !== 'wanxiang';
  row.appendChild(wanxiangHint);

  const hunyuanHint = document.createElement('span');
  hunyuanHint.className = 'hint im-hunyuan-hint';
  hunyuanHint.textContent = '腾讯混元生图：走 TokenHub 新域名，只用填一个 API Key；尺寸填「宽:高」（如 1024:1024）；参考图最多 3 张。';
  hunyuanHint.hidden = model.protocol !== 'hunyuan';
  row.appendChild(hunyuanHint);

  const minimaxHint = document.createElement('span');
  minimaxHint.className = 'hint im-minimax-hint';
  minimaxHint.textContent = 'MiniMax 海螺：只用填一个 API Key；尺寸可填比例（如 16:9）或「宽x高」；参考图只取第 1 张。';
  minimaxHint.hidden = model.protocol !== 'minimax';
  row.appendChild(minimaxHint);

  const cloudflareHint = document.createElement('span');
  cloudflareHint.className = 'hint im-cloudflare-hint';
  cloudflareHint.textContent =
    'Cloudflare Workers AI：填 Account ID + API Token（Token 权限勾「Workers AI · Read」）；免费档每天 10,000 Neurons（00:00 UTC 重置），klein-4b 最省额度；尺寸用「宽x高」（256–1920）；图生图参考图最多 4 张，超 512×512 的会自动等比压缩后上传（仅收本机上传的图，链接图不支持）。';
  cloudflareHint.hidden = model.protocol !== 'cloudflare';
  row.appendChild(cloudflareHint);

  const doubaoHint = document.createElement('span');
  doubaoHint.className = 'hint im-doubao-hint';
  doubaoHint.textContent =
    '本机豆包客户端：不用 API Key，直接驱动你桌面上登录着的豆包出图。前提是豆包以调试端口启动'
    + '（点下面的「重启豆包」即可）；接口地址填 CDP 地址，默认 http://127.0.0.1:9555。'
    + '出图带「豆包 AI 生成」水印，一轮约 30–60 秒。';
  doubaoHint.hidden = model.protocol !== 'doubao';
  row.appendChild(doubaoHint);

  // 豆包桥的状态与「一键重启」：桥要能连上才算可用，这一步用户自己判断不了
  const doubaoActs = document.createElement('div');
  doubaoActs.className = 'hint im-doubao-acts';
  doubaoActs.hidden = model.protocol !== 'doubao';
  const doubaoState = document.createElement('span');
  doubaoState.className = 'im-doubao-state';
  // 状态存在缓存里：模型列表每轮 refresh 都会重建行，写进 DOM 的文案会被冲掉
  doubaoState.textContent = doubaoStatus.get(model.id) || '状态：未检测';
  const doubaoCheck = document.createElement('button');
  doubaoCheck.type = 'button';
  doubaoCheck.className = 'btn tiny';
  doubaoCheck.dataset.act = 'doubao-check';
  doubaoCheck.textContent = '检测连接';
  const doubaoRestart = document.createElement('button');
  doubaoRestart.type = 'button';
  doubaoRestart.className = 'btn tiny';
  doubaoRestart.dataset.act = 'doubao-restart';
  doubaoRestart.title = '完全退出豆包，再用调试端口重新打开（会短暂关闭豆包窗口）';
  doubaoRestart.textContent = '重启豆包';
  doubaoActs.append(doubaoState, doubaoCheck, doubaoRestart);
  row.appendChild(doubaoActs);

  return row;
}

function renderImageModelList() {
  const wrap = $('#imageModelList');
  wrap.textContent = '';
  if (!state.imageModels.length) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '还没有图片模型，可在上面直接添加一个（内置的豆包模型被删掉后不会自动恢复）。';
    wrap.appendChild(hint);
    return;
  }
  for (const model of state.imageModels) wrap.appendChild(buildImageModelRow(model));
}

const imageSaveTimers = new Map();
const imageSavePending = new Map();

/** 与画布字段一致的防抖 PATCH：编辑中的多次改动合并成一次请求 */
function queueImageModelSave(id, patch) {
  clearTimeout(imageSaveTimers.get(id));
  imageSavePending.set(id, { ...(imageSavePending.get(id) || {}), ...patch });
  imageSaveTimers.set(
    id,
    setTimeout(async () => {
      const body = imageSavePending.get(id);
      imageSaveTimers.delete(id);
      imageSavePending.delete(id);
      if (!body || !Object.keys(body).length) return;
      try {
        await api(`/api/image-models/${id}`, { method: 'PATCH', body });
        await refresh();
      } catch (err) {
        toast(err.message, true);
      }
    }, 450),
  );
}

$('#genModelManage').addEventListener('click', () => {
  applyImageModelFormDefaults();
  $('#modalImageModels').hidden = false;
  renderImageModelList();
});

$('#modalImageModels').addEventListener('click', (event) => {
  if (event.target.id === 'modalImageModels' || event.target.closest('[data-close]')) {
    $('#modalImageModels').hidden = true;
  }
});

// 换协议：换「去哪拿 Key」链接，并把接口地址 / 默认尺寸切到该协议的常用值
$('#imageModelForm').addEventListener('change', (event) => {
  if (event.target.name !== 'protocol') return;
  const protocol = event.target.value;
  renderImageKeySrc(protocol);
  const form = event.target.form;
  const urlInput = form.elements.namedItem('baseUrl');
  const sizeInput = form.elements.namedItem('size');
  const secretInput = form.elements.namedItem('akSecret');
  if (secretInput) secretInput.hidden = protocol !== 'jimeng';
  const accountInput = form.elements.namedItem('accountId');
  if (accountInput) accountInput.hidden = protocol !== 'cloudflare';
  const keyInput = form.elements.namedItem('apiKey');
  if (keyInput) {
    keyInput.placeholder = protocol === 'jimeng' ? 'AccessKeyID' : protocol === 'cloudflare' ? 'API Token' : protocol === 'doubao' ? '不需要 API Key' : 'API Key';
  }
  if (protocol === 'zhipu') {
    urlInput.value = state.imageDefaults.zhipuBaseUrl || 'https://open.bigmodel.cn/api/paas/v4';
    sizeInput.value = '1024x1024';
  } else if (protocol === 'jimeng') {
    urlInput.value = state.imageDefaults.jimengBaseUrl || 'https://visual.volcengineapi.com';
    sizeInput.value = '2048x2048';
  } else if (protocol === 'ark') {
    urlInput.value = state.imageDefaults.arkBaseUrl || '';
    sizeInput.value = '2K';
  } else if (protocol === 'wanxiang') {
    urlInput.value = state.imageDefaults.wanxiangBaseUrl || 'https://dashscope.aliyuncs.com';
    sizeInput.value = '';
  } else if (protocol === 'hunyuan') {
    urlInput.value = state.imageDefaults.hunyuanBaseUrl || 'https://tokenhub.tencentmaas.com';
    sizeInput.value = '1024:1024';
  } else if (protocol === 'minimax') {
    urlInput.value = state.imageDefaults.minimaxBaseUrl || 'https://api.minimax.cn';
    sizeInput.value = '1:1';
  } else if (protocol === 'cloudflare') {
    urlInput.value = state.imageDefaults.cloudflareBaseUrl || 'https://api.cloudflare.com';
    sizeInput.value = '1024x1024';
  } else if (protocol === 'doubao') {
    urlInput.value = 'http://127.0.0.1:9555';
    sizeInput.value = '';
  }
  // 按弹窗里选的协议给候选；写进弹窗自己那份 datalist，不碰生成面板的
  fillSizeDatalist(sizePresetsFor(protocol), `form|${protocol}`, '#imageModelSizeList');
});

$('#imageModelForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const value = (role) => form.elements.namedItem(role).value.trim();
  try {
    await api('/api/image-models', {
      method: 'POST',
      body: {
        name: value('name'),
        protocol: form.elements.namedItem('protocol').value,
        baseUrl: value('baseUrl'),
        apiKey: value('apiKey'),
        akSecret: value('akSecret'),
        accountId: value('accountId'),
        model: value('model'),
        size: value('size'),
        noWatermark: form.elements.namedItem('noWatermark').checked,
        supportsImageInput: form.elements.namedItem('supportsImageInput').checked,
        extraParams: value('extraParams'),
      },
    });
    form.reset();
    form.dataset.ready = '';
    applyImageModelFormDefaults();
    toast('模型已添加');
    await refresh();
    renderImageModelList();
  } catch (err) {
    toast(err.message, true);
  }
});

$('#imageModelList').addEventListener('input', (event) => {
  const row = event.target.closest('[data-model-id]');
  if (!row) return;
  const role = event.target.dataset.role;
  if (role === 'apiKey' || role === 'akSecret') {
    const value = event.target.value.trim();
    if (value) queueImageModelSave(row.dataset.modelId, { [role]: value }); // 留空 = 不修改
    return;
  }
  if (IMAGE_MODEL_FIELDS.includes(role)) {
    queueImageModelSave(row.dataset.modelId, { [role]: event.target.value });
  }
});

$('#imageModelList').addEventListener('change', (event) => {
  const row = event.target.closest('[data-model-id]');
  if (!row) return;
  const role = event.target.dataset.role;
  if (role === 'protocol') {
    queueImageModelSave(row.dataset.modelId, { protocol: event.target.value });
    const hint = row.querySelector('.im-ark-hint');
    if (hint) hint.hidden = event.target.value !== 'ark';
    const jhint = row.querySelector('.im-jimeng-hint');
    if (jhint) jhint.hidden = event.target.value !== 'jimeng';
    const whint = row.querySelector('.im-wanxiang-hint');
    if (whint) whint.hidden = event.target.value !== 'wanxiang';
    const hhint = row.querySelector('.im-hunyuan-hint');
    if (hhint) hhint.hidden = event.target.value !== 'hunyuan';
    const mhint = row.querySelector('.im-minimax-hint');
    if (mhint) mhint.hidden = event.target.value !== 'minimax';
    const cfhint = row.querySelector('.im-cloudflare-hint');
    if (cfhint) cfhint.hidden = event.target.value !== 'cloudflare';
    const dbhint = row.querySelector('.im-doubao-hint');
    if (dbhint) dbhint.hidden = event.target.value !== 'doubao';
    const dbacts = row.querySelector('.im-doubao-acts');
    if (dbacts) dbacts.hidden = event.target.value !== 'doubao';
    const secret = row.querySelector('.im-secret');
    if (secret) secret.hidden = event.target.value !== 'jimeng';
    const account = row.querySelector('.im-account');
    if (account) account.hidden = event.target.value !== 'cloudflare';
    const key = row.querySelector('.im-key');
    if (key) {
      // 豆包桥免 Key：把 Key 输入框直接禁掉，避免用户在这里白填
      key.disabled = event.target.value === 'doubao';
      key.placeholder =
        event.target.value === 'jimeng'
          ? '填 AccessKeyID'
          : event.target.value === 'cloudflare'
            ? '填 API Token'
            : event.target.value === 'doubao'
              ? '不需要 API Key'
              : '填 API Key';
    }
  } else if (role === 'noWatermark' || role === 'enabled' || role === 'supportsImageInput') {
    queueImageModelSave(row.dataset.modelId, { [role]: event.target.checked });
  }
});

$('#imageModelList').addEventListener('click', async (event) => {
  const act = event.target.closest('[data-act]')?.dataset.act;
  const row = event.target.closest('[data-model-id]');
  if (!row) return;

  if (act === 'doubao-check' || act === 'doubao-restart') {
    const stateEl = row.querySelector('.im-doubao-state');
    const endpointInput = row.querySelector('[data-role="baseUrl"]');
    const endpoint = endpointInput?.value.trim() || 'http://127.0.0.1:9555';
    const modelId = row.dataset.modelId;
    const buttons = row.querySelectorAll('[data-act^="doubao-"]');
    buttons.forEach((b) => { b.disabled = true; });
    const say = (text) => {
      doubaoStatus.set(modelId, text);
      if (stateEl) stateEl.textContent = text;
    };
    say(act === 'doubao-restart' ? '状态：正在重启豆包…' : '状态：检测中…');
    try {
      if (act === 'doubao-restart') {
        const out = await api('/api/doubao/restart', { method: 'POST', body: { endpoint } });
        say(out.already ? `状态：已连上（${out.browser}）` : `状态：已重启并连上（${out.browser}）`);
        toast(out.already ? '豆包已经在调试模式，无需重启' : '豆包已重启，现在可以出图了');
      } else {
        const out = await api(`/api/doubao/status?endpoint=${encodeURIComponent(endpoint)}`);
        say(out.ok
          ? `状态：已连上（${out.browser}）`
          : `状态：连不上 —— ${out.detail || '豆包没开调试端口'}`);
      }
    } catch (err) {
      say(`状态：${err.message}`);
      toast(err.message, true);
    } finally {
      buttons.forEach((b) => { b.disabled = false; });
    }
    return;
  }

  if (act !== 'delete-model') return;
  if (!(await confirmDialog('删除该图片模型？已生成的图片不受影响。'))) return;
  try {
    await api(`/api/image-models/${row.dataset.modelId}`, { method: 'DELETE' });
    toast('模型已删除');
    await refresh();
    renderImageModelList();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---------------------------------- 顶栏操作 --------------------------------- */

$('#btnAddCanvas').addEventListener('click', async () => {
  try {
    await api('/api/canvases', { method: 'POST', body: {} });
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

// 兜底：界面看着卡死、按钮点了没反应时，整页重新加载（会重新拉 /api/state 重建所有 DOM）
$('#btnReloadUI').addEventListener('click', () => {
  location.reload();
});

/* ------------------------------ 设置 · 存储位置 ------------------------------ */

/** 每个位置一行：名称 + 说明、当前目录、操作按钮 */
function renderStorageTable() {
  const rows = $('#storageRows');
  if (!rows) return;
  const items = state.storage || [];
  $('#storageDataDir').textContent = state.dataDir || '';
  if (!items.length) {
    rows.innerHTML = '<tr><td class="storage-empty" colspan="3">暂时拿不到存储位置</td></tr>';
    return;
  }
  rows.innerHTML = items
    .map(
      (item) => `
      <tr>
        <td>
          <span class="storage-name">${esc(item.label)}</span>
          <span class="storage-desc">${esc(item.desc)}</span>
        </td>
        <td>
          <span class="storage-path" title="${esc(item.path)}">${esc(item.path)}${
            item.isDefault ? '<span class="badge">默认</span>' : '<span class="badge">已改</span>'
          }</span>
        </td>
        <td>
          <div class="storage-ops">
            <button class="btn tiny" type="button" data-storage-act="change" data-key="${esc(item.key)}">更改</button>
            <button class="btn tiny" type="button" data-storage-act="open" data-key="${esc(item.key)}">打开</button>
            <button class="btn tiny" type="button" data-storage-act="reset" data-key="${esc(item.key)}" ${
              item.isDefault ? 'disabled' : ''
            }>恢复默认</button>
          </div>
        </td>
      </tr>`,
    )
    .join('');
}

/** 改完一个位置后统一刷新界面与提示 */
async function applyStorageChange(key, body, label) {
  try {
    const result = await api('/api/storage', { method: 'POST', body: { key, ...body }, timeoutMs: 120000 });
    state.storage = result.items || [];
    renderStorageTable();
    const action = body.reset ? '已恢复默认位置' : '已更改';
    const tail = result.noMove
      ? '（历史文件保留在原位置，新成品写到新目录）'
      : result.moved
        ? `，搬过去 ${result.moved} 个文件`
        : '';
    toast(`${label || result.label || '存储位置'}${action}${tail}`);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
}

/* --------------------------------- 外观主题 ---------------------------------
   三档：浅色 / 夜间 / 跟随系统。存 localStorage('app.theme')，'auto' 是默认值。
   真正在 DOM 上生效的只有 html[data-theme="light"|"dark"] —— 「跟随系统」在这里
   解析掉，并监听系统切换；首帧由 index.html <head> 里的内联脚本先定一次，
   否则 defer 的 app.js 跑起来之前会闪一下白。 */
const THEME_KEY = 'app.theme';
let themePref = localStorage.getItem(THEME_KEY) || 'auto';

function resolveTheme(pref) {
  if (pref === 'dark' || pref === 'light') return pref;
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(pref) {
  themePref = pref || 'auto';
  if (themePref === 'auto') localStorage.removeItem(THEME_KEY);
  else localStorage.setItem(THEME_KEY, themePref);
  document.documentElement.dataset.theme = resolveTheme(themePref);
  syncThemeUI();
  // 画在 canvas 上的东西（小地图）吃不到 CSS 变量，通知它们自己重画
  document.dispatchEvent(new CustomEvent('rh:theme'));
}

function syncThemeUI() {
  const dark = document.documentElement.dataset.theme === 'dark';
  for (const btn of document.querySelectorAll('#themeSeg button[data-theme]')) {
    btn.classList.toggle('active', btn.dataset.theme === themePref);
  }
  const quick = $('#btnTheme');
  if (quick) {
    quick.textContent = dark ? '浅色模式' : '夜间模式';
    quick.title = dark ? '切回浅色模式' : '切到夜间模式（设置里可改成跟随系统）';
  }
}

$('#themeSeg')?.addEventListener('click', (event) => {
  const btn = event.target.closest('button[data-theme]');
  if (btn) applyTheme(btn.dataset.theme);
});

$('#btnTheme')?.addEventListener('click', () => {
  applyTheme(resolveTheme(themePref) === 'dark' ? 'light' : 'dark');
});

// 系统自己切成深色/浅色时跟着变（只在「跟随系统」这一档生效）
window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
  if (themePref === 'auto') applyTheme('auto');
});

// 首帧同步一次 UI 文案（data-theme 头部脚本已经写好，这里只补按钮状态）
applyTheme(themePref);

$('#btnSettings').addEventListener('click', async () => {
  $('#modalSettings').hidden = false;
  renderStorageTable();
  // 进来先拉一次最新状态，避免显示的是上一次打开时的旧路径
  await refresh().catch(() => {});
  renderStorageTable();
});

$('#modalSettings').addEventListener('click', async (event) => {
  if (event.target.id === 'modalSettings' || event.target.closest('[data-close]')) {
    $('#modalSettings').hidden = true;
    return;
  }
  const btn = event.target.closest('[data-storage-act]');
  if (!btn) return;
  const item = (state.storage || []).find((s) => s.key === btn.dataset.key);
  const act = btn.dataset.storageAct;

  if (act === 'open') {
    if (desktop?.storage) {
      const err = await desktop.storage.open(item?.path || '');
      if (err) toast(err, true);
    } else {
      toast('网页版没有「打开文件夹」功能，桌面版才有');
    }
    return;
  }

  if (act === 'reset') {
    applyStorageChange(btn.dataset.key, { reset: true }, item?.label);
    return;
  }

  // 更改：桌面版弹系统原生选文件夹窗口；网页版没有这个能力，退化成手动填路径
  let dir = '';
  if (desktop?.storage) {
    dir = await desktop.storage.pick(item?.path || '');
    if (!dir) return; // 用户取消了
  } else {
    dir = window.prompt(`把「${item?.label || ''}」换到哪个目录？（留空 = 恢复默认）`, item?.path || '') || '';
    if (!dir) return;
  }
  applyStorageChange(btn.dataset.key, { dir }, item?.label);
});

/* ------------------------------ 账号 API 任务 ------------------------------ */

/**
 * 各账号提交到 RunningHub 的任务与结果。
 * 列表按需从 /api/tasks 拉（最多 400 条 = db 的保留上限），不塞进每 1.5 秒的 refresh()。
 * 「重新查询结果」只在平台侧查一下、把结果下载回来，绝不重新提交（不会重复扣费）。
 */

// 重查拿到的「原始返回 JSON」只留本次会话，不落库（免得 db.json 越滚越大）
const apiTaskRaw = new Map();
let apiTaskOpenId = '';

/** 状态 → 徽标 class：进行中的都按排队/生成两种颜色走，和画布卡片保持一致 */
function apiTaskBadgeClass(status) {
  if (!PENDING.has(status)) return status || '';
  return status === 'running' ? 'running' : 'queued';
}

/** 状态筛选：pending=进行中 / success=成功 / failed=失败与取消 */
function apiTaskMatchesStatus(task, filter) {
  if (!filter) return true;
  if (filter === 'pending') return PENDING.has(task.status);
  if (filter === 'success') return task.status === 'success';
  return task.status === 'failed' || task.status === 'canceled';
}

function apiTaskFiltered() {
  const status = $('#apiTaskStatus').value;
  const keyword = $('#apiTaskSearch').value.trim().toLowerCase();
  return (state.apiTasks || []).filter((task) => {
    if (!apiTaskMatchesStatus(task, status)) return false;
    if (!keyword) return true;
    const hay = `${task.canvasName || ''} ${task.remoteTaskId || ''}`.toLowerCase();
    return hay.includes(keyword);
  });
}

/** 表格里的结果缩略图：取第一个有文件的结果，视频取首帧、图片直接显示 */
function apiTaskThumb(task) {
  const o = (task.outputs || []).find((x) => x.localUrl || x.fileUrl);
  if (!o) return '<span class="api-task-thumb is-empty">—</span>';
  const src = o.localUrl || o.fileUrl;
  const type = String(o.fileType || '').toLowerCase();
  const isVideo = ['mp4', 'webm', 'mov', 'mkv', 'avi'].includes(type) || /\.(mp4|webm|mov|mkv)$/i.test(src);
  const inner = isVideo
    // preload="none" + #t=0.5：只在真正要显示时拉首帧。原来用 metadata，
    // 一屏 400 条就是 400 个并发媒体请求 + 400 个解码器，是渲染进程 OOM 的直接推手
    ? `<video src="${esc(src)}#t=0.5" muted preload="none"></video>`
    : `<img src="${esc(src)}" alt="" loading="lazy" />`;
  return `<span class="api-task-thumb">${inner}</span>`;
}

function renderApiTaskTable() {
  const rows = $('#apiTaskRows');
  if (!rows) return;
  const list = apiTaskFiltered();
  $('#apiTaskHint').textContent = list.length
    ? `共 ${list.length} 条${list.length < (state.apiTasks || []).length ? `（已筛选，全部 ${state.apiTasks.length} 条）` : ''}`
    : '没有符合条件的任务';

  // 签名守卫：内容没变就别重建。这个表是唯一一个没做守卫的列表，
  // 而搜索框每敲一个字都会调它 —— 一次输入就重建最多 400 行（含 400 个媒体元素）。
  const sig = [
    list.map((t) => `${t.id}|${t.status}|${t.outputs?.length || 0}|${t.remoteTaskId || ''}|${t.accountName || ''}|${t.canvasName || ''}`).join(','),
    apiTaskOpenId || '',
    $('#apiTaskAccount')?.value || '',
    $('#apiTaskStatus')?.value || '',
    $('#apiTaskSearch')?.value || '',
  ].join('#');
  if (rows.dataset.sig === sig) return;
  rows.dataset.sig = sig;

  if (!list.length) {
    rows.innerHTML = '<tr><td class="api-task-empty" colspan="8">暂无任务记录。任务在画布上开始生成后会出现在这里</td></tr>';
    return;
  }

  rows.innerHTML = list
    .map((task) => {
      const badgeClass = apiTaskBadgeClass(task.status);
      const outputs = task.outputs?.length || 0;
      const canRefresh = Boolean(task.remoteTaskId);
      return `<tr data-task-id="${esc(task.id)}" class="${task.id === apiTaskOpenId ? 'is-open' : ''}">
        <td class="api-task-cellthumb">${apiTaskThumb(task)}</td>
        <td class="api-task-time">${new Date(task.createdAt).toLocaleString()}</td>
        <td>${esc(task.accountName || '—')}</td>
        <td>${esc(task.canvasName || '—')}</td>
        <td><span class="badge ${badgeClass}">${esc(STATUS_LABEL[task.status] || task.status || '—')}</span></td>
        <td class="api-task-remote">${esc(task.remoteTaskId || '—')}</td>
        <td>${outputs ? `${outputs} 个` : '—'}</td>
        <td><button class="btn tiny" type="button" data-api-refresh="${esc(task.id)}" ${
          canRefresh ? '' : 'disabled'
        } title="${canRefresh ? '按远端任务号向平台重查最新状态与结果' : '没有远端任务号（可能从未提交成功），无法重查'}">重查</button></td>
      </tr>`;
    })
    .join('');
}

function renderApiTaskDetail(task) {
  const wrap = $('#apiTaskDetail');
  if (!task) {
    wrap.hidden = true;
    wrap.innerHTML = '';
    return;
  }
  wrap.hidden = false;

  const bits = [
    `账号 ${task.accountName || '—'}`,
    `画布 ${task.canvasName || '—'}`,
    `远端任务号 ${task.remoteTaskId || '—'}`,
    `创建 ${new Date(task.createdAt).toLocaleString()}`,
  ];
  if (task.startedAt && task.finishedAt) bits.push(`耗时 ${fmtDuration(task.finishedAt - task.startedAt)}`);
  if ((task.attempts || 0) > 1) bits.push(`第 ${task.attempts} 次尝试`);

  let body = `<div class="api-task-detail-head">
    <span class="api-task-detail-title">任务详情</span>
    <button class="btn tiny primary" type="button" data-api-refresh="${esc(task.id)}" ${
      task.remoteTaskId ? '' : 'disabled'
    }${task.remoteTaskId ? '' : ' title="没有远端任务号，无法重查"'}>重新查询结果</button>
  </div>
  <div class="hint">${esc(bits.join(' · '))}</div>`;

  if (task.status === 'success' && task.outputs?.length) {
    body += `<div class="result-box api-task-result">
      <div class="result-media">${task.outputs.map(mediaHtml).join('')}</div>
      <div class="result-actions">
        ${task.outputs
          .map(
            (o, i) =>
              o.localUrl
                ? `<a href="${o.localUrl}" download>下载结果 ${i + 1}${o.polished ? '（已去水印）' : ''}</a>`
                : `<a href="${esc(o.fileUrl)}" target="_blank" rel="noreferrer">远端文件 ${i + 1}</a>`,
          )
          .join('')}
      </div>
    </div>`;
  } else if (task.status === 'failed') {
    body += `<div class="error-box">${esc(task.error || task.message || '任务失败')}</div>`;
  } else if (task.status === 'canceled') {
    body += '<div class="hint">任务已取消</div>';
  } else {
    body += `<div class="hint">${esc(task.message || '处理中')}</div>`;
  }

  const lines = task.log || [];
  body += `<div class="hint" style="margin-top:10px">运行日志</div>
    <pre class="api-task-log">${esc(
      lines.length ? lines.map((l) => `[${new Date(l.t).toLocaleTimeString()}] ${l.text}`).join('\n') : '（暂无日志）',
    )}</pre>`;

  const raw = apiTaskRaw.get(task.id);
  if (raw) {
    body += `<details class="api-task-raw"><summary>原始返回（JSON）</summary><pre>${esc(
      JSON.stringify(raw, null, 2),
    )}</pre></details>`;
  }

  wrap.innerHTML = body;
}

function openApiTaskDetail(taskId) {
  apiTaskOpenId = apiTaskOpenId === taskId ? '' : taskId;
  const task = (state.apiTasks || []).find((t) => t.id === apiTaskOpenId);
  renderApiTaskTable();
  renderApiTaskDetail(task);
}

async function openApiTasks() {
  const select = $('#apiTaskAccount');
  const current = select.value;
  select.innerHTML =
    '<option value="">全部账号</option>' +
    state.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  select.value = state.accounts.some((a) => a.id === current) ? current : '';
  apiTaskOpenId = '';
  renderApiTaskDetail(null);
  $('#modalApiTasks').hidden = false;
  await loadApiTasks();
}

async function loadApiTasks() {
  try {
    const accountId = $('#apiTaskAccount').value;
    const query = new URLSearchParams({ limit: '400' });
    if (accountId) query.set('accountId', accountId);
    const list = await api(`/api/tasks?${query.toString()}`, { timeoutMs: 15000 });
    state.apiTasks = Array.isArray(list) ? list : [];
    renderApiTaskTable();
    // 详情开着的话，列表刷新后把最新数据重新渲染一遍
    if (apiTaskOpenId) renderApiTaskDetail(state.apiTasks.find((t) => t.id === apiTaskOpenId) || null);
  } catch (err) {
    toast(`任务列表加载失败：${err.message}`, true);
  }
}

/** 手动重查一条任务的结果，只查不重发 */
async function refreshApiTask(taskId) {
  const btn = document.querySelector(`[data-api-refresh="${taskId}"]`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '查询中…';
  }
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/refresh-result`, {
      method: 'POST',
      body: {},
      timeoutMs: 120000,
    });
    if (result.raw) apiTaskRaw.set(taskId, result.raw);
    const index = state.apiTasks.findIndex((t) => t.id === taskId);
    if (index >= 0) state.apiTasks[index] = result.task;
    const message = { success: '已取回结果', running: '平台仍在生成中', queued: '平台排队中', failed: '平台判定失败' }[result.state];
    toast(`${message || '已查询'}${result.error ? `：${result.error}` : ''}`, result.state === 'failed');
    if (apiTaskOpenId === taskId) {
      renderApiTaskDetail(result.task);
      renderApiTaskTable();
    } else {
      openApiTaskDetail(taskId);
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    // 无论成功失败都按最新数据重画，按钮状态跟着恢复
    renderApiTaskTable();
  }
}

/**
 * 按「远端任务号」从平台取回一条任务（本地没有记录也能补回来）。
 * 场景：旧版等超时后重发过任务，把那一版的远端任务号丢了，平台上其实已经出片。
 * 和重查一样只查不重发，不会重复扣费。
 */
async function importApiTask() {
  const input = $('#apiTaskRemoteId');
  const remoteTaskId = input.value.trim();
  if (!remoteTaskId) {
    toast('请先粘贴平台上的任务号', true);
    input.focus();
    return;
  }
  const accountId = $('#apiTaskAccount').value;
  if (!accountId) {
    toast('请先在账号下拉里选定用哪个账号去取回', true);
    $('#apiTaskAccount').focus();
    return;
  }
  const btn = $('#apiTaskImport');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '取回中…';
  try {
    const result = await api('/api/tasks/import', {
      method: 'POST',
      body: { remoteTaskId, accountId },
      timeoutMs: 180000,
    });
    const task = result.task;
    if (result.raw) apiTaskRaw.set(task.id, result.raw);
    const index = (state.apiTasks || []).findIndex((t) => t.id === task.id);
    if (index >= 0) state.apiTasks[index] = task;
    else state.apiTasks = [task, ...(state.apiTasks || [])];
    input.value = '';
    const message = {
      success: result.reused ? '已取回结果（复用已有记录）' : '已取回结果',
      running: '平台仍在生成中，过会儿再点「取回结果」',
      queued: '平台排队中，过会儿再点「取回结果」',
      failed: '平台判定这个任务失败',
    }[result.state];
    toast(`${message || '已查询'}${result.error ? `：${result.error}` : ''}`, result.state === 'failed');
    // 直接把这条展开，用户马上能看到结果和下载按钮
    apiTaskOpenId = task.id;
    renderApiTaskTable();
    renderApiTaskDetail(task);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

/**
 * 一键取回所有「本地结果文件已丢失」的任务：按远端任务号重新查一遍并把结果下下来，
 * 存到 输出目录/<当天日期>/ 里。只查不重发，不会重复扣费。
 */
async function fetchAllApiTasks() {
  const btn = $('#apiTaskFetchAll');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '取回中…';
  try {
    const result = await api('/api/tasks/fetch-missing', { method: 'POST', body: {}, timeoutMs: 600000 });
    await loadApiTasks();
    if (!result.total) {
      toast('没有发现「本地文件已丢失」的任务，都还在');
      return;
    }
    const parts = [`取回成功 ${result.recovered} 条`];
    if (result.stillRunning) parts.push(`平台还在跑 ${result.stillRunning} 条`);
    if (result.failed) parts.push(`失败 ${result.failed} 条`);
    if (result.skipped) parts.push(`跳过 ${result.skipped} 条`);
    toast(`${parts.join('，')}；已存到 输出目录\\${result.folder}\\`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

$('#btnApiTasks').addEventListener('click', () => {
  openApiTasks();
});

$('#modalApiTasks').addEventListener('click', (event) => {
  if (event.target.id === 'modalApiTasks' || event.target.closest('[data-close]')) {
    $('#modalApiTasks').hidden = true;
    return;
  }
  const refreshBtn = event.target.closest('[data-api-refresh]');
  if (refreshBtn) {
    if (!refreshBtn.disabled) refreshApiTask(refreshBtn.dataset.apiRefresh);
    return;
  }
  const row = event.target.closest('tr[data-task-id]');
  if (row) openApiTaskDetail(row.dataset.taskId);
});

$('#apiTaskAccount').addEventListener('change', () => {
  loadApiTasks();
});
$('#apiTaskStatus').addEventListener('change', renderApiTaskTable);
// 输入防抖：边打字边重建整表的话，连着敲就是连续的几百个媒体请求
$('#apiTaskSearch').addEventListener('input', debounce(renderApiTaskTable, 200));
$('#apiTaskReload').addEventListener('click', () => {
  loadApiTasks();
});
$('#apiTaskFetchAll').addEventListener('click', () => {
  fetchAllApiTasks();
});
$('#apiTaskImport').addEventListener('click', () => {
  importApiTask();
});
$('#apiTaskRemoteId').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    importApiTask();
  }
});

/* ------------------------------ Topaz 提升画质 ------------------------------ */

// 弹窗 UI 状态：候选列表（待提升 / 已提升）、勾选集合、选的档位
const topazUI = {
  tier: 'std',
  picked: new Set(), // "taskId|index"
  data: { pending: [], done: [] },
  loading: false,
};
// 记录已经处理过终态的 job，避免每轮刷新重复拉列表
const topazHandled = new Set();

// 候选条目的唯一键：生成结果是「来源 + 任务 id + 结果序号」（视频生成模块一条任务只有一个结果，序号恒为 0）；
// 本地文件夹里的视频没有任务 id，只能用路径当键 —— 否则同一个 index 会互相撞键、勾错。
const topazKey = (p) =>
  p?.source === 'local'
    ? `local|${p.localPath || p.fileName}`
    : `${p?.source === 'video' ? 'video' : 'task'}|${p?.taskId}|${p?.index || 0}`;

/** 正在跑 / 排队的提升任务（进度条用） */
const topazRunningJobs = () =>
  (state.topaz?.jobs || []).filter((j) => j.status === 'queued' || j.status === 'running');

/** 失败 / 被取消的提升任务（以前这些在弹窗里完全不显示，跑挂了就像凭空消失） */
const topazFailedJobs = () =>
  (state.topaz?.jobs || []).filter((j) => j.status === 'failed' || j.status === 'canceled').slice(0, 8);

/** 每轮刷新都跟着走：弹窗开着就重画；有任务跑到终态，就重拉一次候选列表 */
function syncTopaz() {
  if ($('#modalTopaz').hidden) return;
  renderTopazModal();
  for (const job of state.topaz?.jobs || []) {
    if (['success', 'failed', 'canceled'].includes(job.status) && !topazHandled.has(job.id)) {
      topazHandled.add(job.id);
      loadTopazCandidates().then(renderTopazModal);
      if (!$('#modalApiTasks').hidden) loadApiTasks();
      break;
    }
  }
}

/**
 * 一行候选 / 已提升的展示：缩略图 + 文件名 + 来源，方便分辨是哪条视频。
 * 候选项现在有两个来源（自用模块的画布结果 / 视频生成模块的成品），
 * 所以 key 里带上 source —— 免得两边 id 万一撞车时勾错。
 */
function topazItemHtml(p, isDone) {
  const key = topazKey(p);
  const isLocal = p.source === 'local';
  const srcLabel = isLocal ? '本地' : p.source === 'video' ? '视频生成' : p.canvasName || '自用';
  const srcClass = isLocal ? 'is-local' : p.source === 'video' ? 'is-video' : '';
  const meta = [
    p.accountName,
    p.size ? fmtSize(p.size) : '',
    p.createdAt ? new Date(p.createdAt).toLocaleString() : '',
  ]
    .filter(Boolean)
    .join(' · ');
  const doneTip = isDone
    ? `已提升：${p.tierLabel || ''}${p.outWidth ? ` → ${p.outWidth}×${p.outHeight}` : ''}`
    : '';
  // 本地文件没有「原片替换」这回事（原文件从头到尾没动过），所以不给「还原原片」，
  // 改成标出成品的完整路径，方便自己去找。
  const outTip = isDone && isLocal && p.outPath ? `成品：${p.outPath}` : '';
  return `<label class="topaz-item ${isDone ? 'is-done' : ''}">
    ${
      isDone
        ? '<span class="topaz-item-mark">✔</span>'
        : `<input type="checkbox" data-topaz-pick="${esc(key)}" ${topazUI.picked.has(key) ? 'checked' : ''} />`
    }
    <span class="topaz-src ${srcClass}">${esc(srcLabel)}</span>
    <span class="topaz-thumb"><video data-src="${esc(p.url)}#t=0.5" muted preload="metadata"></video></span>
    <span class="topaz-item-main">
      <span class="topaz-item-name" title="${esc(p.fileName)}">${esc(p.fileName)}</span>
      <span class="topaz-item-meta">${esc(meta)}</span>
      ${p.desc ? `<span class="topaz-item-meta" title="${esc(p.desc)}">${esc(p.desc)}</span>` : ''}
      ${isDone ? `<span class="topaz-item-meta is-ok">${esc(doneTip)}</span>` : ''}
      ${outTip ? `<span class="topaz-item-meta is-ok" title="${esc(outTip)}">${esc(outTip)}</span>` : ''}
    </span>
    ${isDone && !isLocal ? `<button class="btn tiny" type="button" data-topaz-restore="${esc(key)}">还原原片</button>` : ''}
  </label>`;
}

function topazBodyHtml() {
  const tz = state.topaz || {};

  if (!tz.found) {
    return `<div class="topaz-missing">
      <b>增强引擎的文件缺失</b>：随软件内置的 <code>ffmpeg.exe</code> 没找到
      （可能被安全软件拦截 / 清掉，或安装包不完整）。<br />
      恢复后回到这里重试；也可以把本机 Topaz 的安装目录填在下面。
      <div style="display:flex;gap:8px;margin-top:10px">
        <input id="topazDirInput" placeholder="例如 D:\\Topaz Video AI v6.0.2" style="flex:1;height:30px;padding:0 8px;border:1px solid var(--line-input);border-radius:6px" />
        <button class="btn tiny" type="button" id="topazDirSave">保存目录</button>
      </div>
    </div>`;
  }

  const { pending, done } = topazUI.data;
  const running = topazRunningJobs();
  const failed = topazFailedJobs();

  if (topazUI.loading && !pending.length && !done.length) {
    return '<div class="hint">正在读取列表…</div>';
  }

  const tiers = (tz.tiers || []).length
    ? tz.tiers
    : [
        { id: 'enhance', label: '快速增强（不放大）', desc: '' },
        { id: 'x2', label: '高清 2×', desc: '' },
        { id: 'max', label: '最高画质（本机上限 · 推荐）', desc: '' },
      ];
  let html = `<div class="topaz-tiers">${tiers
    .map(
      (t) => `<button type="button" class="topaz-tier ${t.id === topazUI.tier ? 'is-on' : ''}" data-tier="${esc(t.id)}">
        <span class="topaz-tier-body">
          <span class="topaz-tier-title">${esc(t.label)}</span>
          <span class="topaz-tier-desc">${esc(t.desc || '')}</span>
        </span>
      </button>`,
    )
    .join('')}</div>`;

  // 质感：决定提升后的整体观感。默认取服务端列表第一项（现在是「不增亮」：
  // 只压皮肤高光那一档、不动暗部中调，避免越提升人脸越白）。
  const looks = tz.looks || [];
  const lookId = tz.look || (looks[0] && looks[0].id) || 'natural';
  if (looks.length) {
    const cur = looks.find((l) => l.id === lookId) || looks[0];
    html += `<div class="topaz-looks">${looks
      .map(
        (l) =>
          `<button type="button" class="topaz-look ${l.id === lookId ? 'is-on' : ''}" data-look="${esc(l.id)}" title="${esc(l.desc || '')}">${esc(l.label)}</button>`,
      )
      .join('')}</div>
      <div class="hint" style="margin:6px 0 0">${esc(cur.desc || '')}</div>`;
  }

  // 本地文件夹：贴一个路径点「读取」，里面的视频会以「本地」来源出现在下面的候选里。
  // 路径存在后端（db 里），重启后还在；这里只负责展示与触发。
  const localDir = tz.localDir || '';
  html += `<div style="display:flex;gap:8px;margin:10px 0 0">
    <input id="topazLocalDir" type="text" spellcheck="false" autocomplete="off" value="${esc(localDir)}"
      placeholder="本地视频文件夹：粘贴路径后点「读取」，例如 D:\\视频"
      style="flex:1;min-width:0;height:30px;padding:0 8px;border:1px solid var(--line-input);border-radius:6px" />
    <button class="btn tiny primary" type="button" id="topazLocalLoad">读取</button>
    ${localDir ? '<button class="btn tiny" type="button" id="topazLocalClear">收起</button>' : ''}
  </div>`;
  if (localDir) {
    html += `<div class="hint" style="margin:4px 0 0">本地文件夹：${esc(localDir)}${
      tz.localLimit ? `（含子目录，最多列 ${tz.localLimit} 条，新的排前面）` : ''
    }</div>`;
  }

  // 引擎来源：用户看到的是「内置」，不再需要自己确认有没有装 Topaz
  html += `<div class="hint" style="margin:8px 0 0">引擎：${tz.bundled ? '内置（随软件安装）' : '本机 Topaz'}</div>`;

  const picked = topazUI.picked.size;
  html += `<div class="topaz-toolbar">
    <label class="topaz-check"><input type="checkbox" id="topazAll" /> 全选</label>
    <span class="hint" id="topazPickedCount">待提升 ${pending.length} 条 · 已选 ${picked} 条</span>
    <span class="topaz-spacer"></span>
    <button class="btn tiny" type="button" id="topazReload">刷新列表</button>
    <button class="btn primary" type="button" id="topazStart" ${picked ? '' : 'disabled'}>一键提升（${picked}）</button>
  </div>`;

  if (running.length) {
    html += `<div class="topaz-running">${running
      .map((j) => {
        const pct = Math.round((j.progress || 0) * 100);
        return `<div class="topaz-run-row" data-job-bar>
          <div class="topaz-run-name">${esc(j.fileName || '')} <span class="hint">${esc(j.tierLabel || '')}</span>
            <button class="btn tiny" type="button" data-topaz-cancel="${esc(j.id)}">取消</button>
          </div>
          <div class="progress-bar"><span class="is-run" style="--p:${pct}%"></span></div>
          <div class="hint progress-hint">${pct}%${j.speed ? ` · ${j.speed}x` : ''} · ${esc(j.message || '正在提升…')}</div>
        </div>`;
      })
      .join('')}</div>`;
  }

  if (failed.length) {
    html += `<div class="topaz-section-title">没成功的（${failed.length}）
      <button class="btn tiny" type="button" id="topazClearFailed" title="只清掉这些失败记录，不影响成品和正在跑的任务">清空记录</button>
    </div>
      <div class="topaz-running">${failed
        .map(
          (j) => `<div class="topaz-run-row">
          <div class="topaz-run-name">${esc(j.fileName || '')} <span class="hint">${esc(j.tierLabel || '')}</span>
            <span class="hint" style="color:var(--err)">${j.status === 'canceled' ? '已取消' : '失败'}</span>
          </div>
          <div class="hint" style="color:var(--err)">${esc(j.error || j.message || '')}</div>
        </div>`,
        )
        .join('')}</div>`;
  }

  if (!pending.length) {
    html += '<div class="hint" style="margin:12px 0">没有待提升的视频了（已提升的在下面）。</div>';
  } else {
    html += `<div class="topaz-list">${pending.map((p) => topazItemHtml(p, false)).join('')}</div>`;
  }

  if (done.length) {
    html += `<div class="topaz-section-title">已提升（${done.length}）</div>
      <div class="topaz-list topaz-list-done">${done.map((p) => topazItemHtml(p, true)).join('')}</div>`;
  }

  return html;
}

/** 重画弹窗；内容没变就不动它，免得列表里的缩略图（video）被反复重建闪个不停 */
function renderTopazModal() {
  const wrap = $('#topazBody');
  if (!wrap) return;
  const tz = state.topaz || {};
  const { pending, done } = topazUI.data;
  const sig = [
    tz.found ? 1 : 0,
    topazUI.loading ? 1 : 0,
    tz.look || '',
    pending.map((p) => topazKey(p)).join(','),
    done.map((p) => `${topazKey(p)}@${p.at}`).join(','),
    // 只看「有哪几条在跑」，不看进度百分比 —— 百分比每 1.5 秒都在变，
    // 算进签名的话跑任务期间整个弹窗（连同所有缩略图）每 1.5 秒重建一次，卡死
    topazRunningJobs()
      .map((j) => j.id)
      .join(','),
    topazFailedJobs()
      .map((j) => `${j.id}:${j.status}:${j.error || ''}`)
      .join(','),
  ].join('|');
  if (wrap.dataset.sig === sig) {
    updateTopazProgress();
    return;
  }
  // 用户正在「本地文件夹」输入框里打字时别重建，不然光标位置和刚敲进去的内容会被抹掉
  const ae = document.activeElement;
  if (ae && ae.id === 'topazLocalDir') return;
  wrap.dataset.sig = sig;
  wrap.innerHTML = topazBodyHtml();
  syncTopazSelection();
  initTopazThumbs(wrap);
  updateTopazProgress();
}

/** 进度条原地更新：跑任务时每 1.5 秒只改数字和进度条宽度，不重建 DOM（缩略图不重载） */
function updateTopazProgress() {
  const wrap = $('#topazBody');
  if (!wrap) return;
  const running = topazRunningJobs();
  const rows = wrap.querySelectorAll('.topaz-running [data-job-bar]');
  // 行数对不上说明刚重绘过 / 重绘被跳过，等下一轮 sig 变化兜底
  if (rows.length !== running.length) return;
  running.forEach((j, i) => {
    const row = rows[i];
    const pct = Math.round((j.progress || 0) * 100);
    const bar = row.querySelector('.progress-bar > span');
    if (bar) bar.style.setProperty('--p', `${pct}%`);
    const hint = row.querySelector('.progress-hint');
    // textContent 赋值不走 HTML 转义，安全且快
    if (hint) hint.textContent = `${pct}%${j.speed ? ` · ${j.speed}x` : ''} · ${j.message || '正在提升…'}`;
  });
}

/**
 * 缩略图懒加载：列表里动辄上百条 <video>，如果一打开就把 src 全填上，
 * 浏览器会同时给上百个视频开流、解首帧 —— 打开弹窗直接卡死几秒。
 * 改成进可视区才赋 src（data-src 里存着真地址），滚到哪加载到哪。
 */
let topazThumbIO = null;
function initTopazThumbs(root) {
  const vids = root.querySelectorAll('video[data-src]');
  if (!('IntersectionObserver' in window)) {
    for (const v of vids) v.src = v.dataset.src;
    return;
  }
  // 每次 重画 都重建观察：disconnect 顺便释放旧节点引用，避免累积
  if (topazThumbIO) topazThumbIO.disconnect();
  else {
    topazThumbIO = new IntersectionObserver(
      (entries) => {
        for (const en of entries) {
          if (!en.isIntersecting) continue;
          const v = en.target;
          if (v.dataset.src) {
            v.src = v.dataset.src;
            delete v.dataset.src;
          }
          topazThumbIO.unobserve(v);
        }
      },
      // 以弹窗的滚动容器为根，提前 300px 预载，滚动时缩略图基本已经就位
      { root: document.querySelector('#modalTopaz .modal-body'), rootMargin: '300px' },
    );
  }
  for (const v of vids) topazThumbIO.observe(v);
}

/** 只更新勾选相关的 DOM（不整段重画，缩略图不重载） */
function syncTopazSelection() {
  const total = topazUI.data.pending.length;
  const picked = topazUI.picked.size;
  for (const box of document.querySelectorAll('#topazBody [data-topaz-pick]')) {
    box.checked = topazUI.picked.has(box.dataset.topazPick);
  }
  const all = $('#topazAll');
  if (all) {
    all.checked = total > 0 && picked === total;
    all.indeterminate = picked > 0 && picked < total;
  }
  const cnt = $('#topazPickedCount');
  if (cnt) cnt.textContent = `待提升 ${total} 条 · 已选 ${picked} 条`;
  const start = $('#topazStart');
  if (start) {
    start.disabled = picked === 0;
    start.textContent = `一键提升（${picked}）`;
  }
}

/** 拉候选清单（待提升 / 已提升），并清掉已经不存在的勾选 */
async function loadTopazCandidates() {
  try {
    const data = await api('/api/topaz/candidates', { timeoutMs: 30000 });
    topazUI.data = { pending: data.pending || [], done: data.done || [] };
    const valid = new Set(topazUI.data.pending.map((p) => topazKey(p)));
    for (const key of [...topazUI.picked]) if (!valid.has(key)) topazUI.picked.delete(key);
  } catch (err) {
    toast(err.message, true);
  }
}

async function openTopazModal() {
  $('#modalTopaz').hidden = false;
  topazUI.loading = true;
  $('#topazBody').dataset.sig = '';
  renderTopazModal();
  await refresh();
  await loadTopazCandidates();
  topazUI.loading = false;
  renderTopazModal();
}

/** 一键提升：把勾选的条目按当前档位一次性排队 */
async function startTopazBatch() {
  const items = topazUI.data.pending
    .filter((p) => topazUI.picked.has(topazKey(p)))
    .map((p) => ({
      // 三种来源：自用模块的 task / 视频生成模块的 video / 本地文件夹里的 local
      source: p.source === 'local' ? 'local' : p.source === 'video' ? 'video' : 'task',
      taskId: p.taskId,
      index: p.index || 0,
      // 本地文件没有任务 id，得把绝对路径带上，后端才知道要提升哪个文件
      localPath: p.localPath || '',
    }));
  if (!items.length) return toast('先勾选要提升的视频', true);

  const btn = $('#topazStart');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '提交中…';
  }
  try {
    const res = await api('/api/topaz/enhance-batch', {
      method: 'POST',
      body: { items, tier: topazUI.tier },
      timeoutMs: 30000,
    });
    if (res.started) toast(`已排队 ${res.started} 条，开始提升`);
    if (res.errors?.length) toast(`${res.errors.length} 条没能排队：${res.errors[0].message}`, true);
    topazUI.picked.clear();
    await refresh();
    await loadTopazCandidates();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
    syncTopazSelection();
  }
}

async function cancelTopazEnhance(jobId) {
  try {
    await api('/api/topaz/cancel', { method: 'POST', body: { jobId }, timeoutMs: 20000 });
    await refresh();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  }
}

/** 清空「没成功的」记录：后端只删这份记录，成品和在跑的任务都不受影响 */
async function clearTopazFailed() {
  try {
    const res = await api('/api/topaz/clear-failed', { method: 'POST', body: {}, timeoutMs: 15000 });
    toast(res.removed ? `已清掉 ${res.removed} 条失败记录` : '没有可清的记录');
    await refresh();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  }
}

/** 「读取」：把输入框里的文件夹路径交给后端扫一遍，里面的视频会作为「本地」来源列进候选 */
async function readLocalDir() {
  const dir = ($('#topazLocalDir')?.value || '').trim();
  if (!dir) return toast('先粘贴一个文件夹路径', true);

  const btn = $('#topazLocalLoad');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '读取中…';
  }
  try {
    const res = await api('/api/topaz/local', { method: 'POST', body: { dir }, timeoutMs: 30000 });
    if (res.error) toast(`读取失败：${res.error}`, true);
    else toast(`读到 ${(res.items || []).length} 条本地视频`);
    await refresh();
    await loadTopazCandidates();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '读取';
    }
  }
}

/** 「收起」：清掉记住的本地文件夹（只清这条记录，磁盘上的文件一个都不动） */
async function clearLocalDir() {
  try {
    await api('/api/topaz/local', { method: 'POST', body: { dir: '' }, timeoutMs: 30000 });
    await refresh();
    await loadTopazCandidates();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  }
}

async function restoreTopazOutput(key) {
  // key 现在是 "source|taskId|index" 三段
  const [source, taskId, index] = String(key).split('|');
  try {
    await api('/api/topaz/restore', {
      method: 'POST',
      body: { source: source === 'video' ? 'video' : 'task', taskId, index: Number(index) || 0 },
      timeoutMs: 20000,
    });
    toast('已还原为原片');
    await refresh();
    await loadTopazCandidates();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  }
}

/** 切换质感档（自拍室内 / 原样 / 明亮通透）：存库，对之后提交的任务生效 */
async function setTopazLook(id) {
  try {
    await api('/api/topaz/look', { method: 'POST', body: { look: id }, timeoutMs: 15000 });
    await refresh();
    $('#topazBody').dataset.sig = ''; // 质感变了要重画选择行，不能靠上面的签名复用
    renderTopazModal();
    toast('质感已切换（对之后提交的任务生效）');
  } catch (err) {
    toast(err.message, true);
  }
}

/** 探测不到 Topaz 时，手动保存安装目录 */
async function saveTopazDir() {
  const input = $('#topazDirInput');
  const dir = (input?.value || '').trim();
  try {
    const probe = await api('/api/topaz/config', { method: 'POST', body: { dir }, timeoutMs: 20000 });
    toast(probe.found ? `已找到：${probe.ffmpeg}` : '这个目录里没找到 ffmpeg.exe，请确认路径', !probe.found);
    await refresh();
    await loadTopazCandidates();
    renderTopazModal();
  } catch (err) {
    toast(err.message, true);
  }
}

$('#modalTopaz').addEventListener('click', (event) => {
  if (event.target.id === 'modalTopaz' || event.target.closest('[data-close]')) {
    $('#modalTopaz').hidden = true;
    return;
  }
  const tier = event.target.closest('#topazBody [data-tier]');
  if (tier) {
    topazUI.tier = tier.dataset.tier;
    for (const el of document.querySelectorAll('#topazBody .topaz-tier')) {
      el.classList.toggle('is-on', el === tier);
    }
    return;
  }
  const look = event.target.closest('#topazBody [data-look]');
  if (look) {
    setTopazLook(look.dataset.look);
    return;
  }
  if (event.target.closest('#topazStart')) return startTopazBatch();
  if (event.target.closest('#topazReload')) return loadTopazCandidates().then(renderTopazModal);
  if (event.target.closest('#topazClearFailed')) return clearTopazFailed();
  if (event.target.closest('#topazLocalLoad')) return readLocalDir();
  if (event.target.closest('#topazLocalClear')) return clearLocalDir();
  const restore = event.target.closest('[data-topaz-restore]');
  if (restore) return restoreTopazOutput(restore.dataset.topazRestore);
  const cancel = event.target.closest('[data-topaz-cancel]');
  if (cancel) return cancelTopazEnhance(cancel.dataset.topazCancel);
  if (event.target.closest('#topazDirSave')) return saveTopazDir();
});

$('#modalTopaz').addEventListener('change', (event) => {
  const box = event.target.closest('[data-topaz-pick]');
  if (box) {
    if (box.checked) topazUI.picked.add(box.dataset.topazPick);
    else topazUI.picked.delete(box.dataset.topazPick);
    syncTopazSelection();
    return;
  }
  if (event.target.id === 'topazAll') {
    topazUI.picked.clear();
    if (event.target.checked) {
      for (const p of topazUI.data.pending) topazUI.picked.add(topazKey(p));
    }
    syncTopazSelection();
  }
});

$('#modalTopaz').addEventListener('keydown', (event) => {
  // 在本地路径输入框里直接按回车 = 点「读取」
  if (event.target.id === 'topazLocalDir' && event.key === 'Enter') {
    event.preventDefault();
    readLocalDir();
  }
});

$('#btnTopaz').addEventListener('click', () => openTopazModal());

$('#btnRunAll').addEventListener('click', async () => {
  try {
    const result = await api('/api/canvases/run-all', { method: 'POST', body: {} });
    if (!result.created) {
      toast(result.errors[0] || '没有可提交的画布', true);
    } else {
      toast(`已提交 ${result.created} 个任务${result.errors.length ? `，${result.errors.length} 个画布未就绪` : ''}`);
    }
    if (result.errors.length) console.warn('未提交的画布：', result.errors);
    await refresh();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ----------------------------- RH 币：刷新与领取 ---------------------------- */

/** 一键刷新全部账号的 RH 币余额 */
$('#btnRefreshCoins').addEventListener('click', async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '刷新中…';
  try {
    const out = await api('/api/accounts/refresh-balance', { method: 'POST', body: {} });
    if (!out.results.length) {
      toast('还没有可查询的账号（需要先填 API Key）', true);
    } else {
      const failed = out.results.filter((r) => r.error);
      toast(
        failed.length
          ? `${out.results.length} 个账号已刷新，${failed.length} 个失败：${failed[0].name} ${failed[0].error}`
          : `已刷新 ${out.results.length} 个账号的 RH 币余额`,
        failed.length > 0,
      );
    }
    await refresh();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

// 下面三个控件只有桌面端才有意义（网页版整条都隐藏着）
if (desktop) {
  $('#claimEnabled').addEventListener('change', async (event) => {
    const out = await desktop.claim.config({ enabled: event.target.checked });
    desktopState.enabled = out.enabled;
    toast(out.enabled ? '已开启每天自动领 RH 币' : '已关闭每天自动领 RH 币');
  });

  $('#claimTime').addEventListener('change', async (event) => {
    if (!event.target.value) return;
    const out = await desktop.claim.config({ time: event.target.value });
    desktopState.time = out.time;
    toast(`每天 ${out.time} 自动领 RH 币`);
  });

  $('#btnClaimNow').addEventListener('click', async () => {
    const hint = $('#claimHint');
    hint.textContent = '领取中：会依次打开各账号的页面，可能要一分钟…';
    const out = await desktop.claim.run();
    if (!out?.ok) {
      toast(out?.error || '领取失败', true);
      hint.textContent = '';
      return;
    }
    toast(out.results.map((r) => `${r.name}：${r.note}`).join('；') || '领取完成');
    await refresh();
  });

  $('#keepEnabled').addEventListener('change', async (event) => {
    const out = await desktop.keep.config({ enabled: event.target.checked });
    toast(out.enabled ? '已开启每天自动续登录态' : '已关闭每天自动续登录态');
  });

  $('#keepTime').addEventListener('change', async (event) => {
    if (!event.target.value) return;
    const out = await desktop.keep.config({ time: event.target.value });
    toast(`每天 ${out.time} 自动续登录态`);
  });

  $('#btnKeepNow').addEventListener('click', async () => {
    const hint = $('#keepHint');
    hint.textContent = '保活中：会依次用隐藏窗口访问各账号的页面，可能要一分钟…';
    const out = await desktop.keep.run();
    hint.textContent = '';
    if (!out?.ok) {
      toast(out?.error || '保活失败', true);
      return;
    }
    toast(out.results.map((r) => `${r.name}：${r.note}`).join('；') || '保活完成');
    await refresh();
  });
}

/* -------------------------------- 模块切换 -------------------------------- */

const MODULE_KEY = 'rh.module';
const MODULES = ['self', 'node', 'video', 'gen', 'material', 'publish', 'toolbox'];

/** 模块名 → 该模块挂在 window 上的命名空间（onShow / onHide 都从这里找） */
const MODULE_API = {
  node: 'RHNode',
  video: 'RHVideo',
  gen: 'RHGen',
  material: 'RHMaterial',
  publish: 'RHPublish',
  toolbox: 'RHToolbox',
};

/**
 * 六个模块物理隔离：只切 body 上的 data-module，显隐全交给 CSS。
 *
 * 切走时必须通知上一个模块「你被藏起来了」：内嵌页面的轮询、hls 分片下载、
 * 翻译补翻这些后台动作不会因为 CSS 隐藏而停下，不显式收掉就一直跑，
 * 内存和网络只涨不落（长时间挂着最终会把渲染进程拖到 OOM）。
 */
function switchModule(name) {
  const next = MODULES.includes(name) ? name : 'self';
  const prev = document.body.dataset.module;
  if (prev && prev !== next) {
    try {
      window[MODULE_API[prev]]?.onHide?.();
    } catch (err) {
      console.error(`[module] ${prev} 的 onHide 出错`, err);
    }
  }
  document.body.dataset.module = next;
  for (const item of document.querySelectorAll('#sidebarNav .sidebar-item')) {
    item.classList.toggle('active', item.dataset.module === next);
  }
  try {
    localStorage.setItem(MODULE_KEY, next);
  } catch (err) {
    /* 隐私模式下 localStorage 可能不可用，忽略即可 */
  }
  if (next === 'node') window.RHNode?.onShow();
  if (next === 'video') window.RHVideo?.onShow();
  if (next === 'gen') window.RHGen?.onShow();
  if (next === 'material') window.RHMaterial?.onShow();
  if (next === 'publish') window.RHPublish?.onShow();
  if (next === 'toolbox') window.RHToolbox?.onShow();
}

// 模块切换能力挂出去给其它模块用（发布页要跳回素材等场景）
window.RHUtil.switchModule = switchModule;

$('#sidebarNav').addEventListener('click', (event) => {
  const item = event.target.closest('.sidebar-item');
  if (item?.dataset.module) switchModule(item.dataset.module);
});

/* -------------------------------- 快捷键 / 小工具 -------------------------------- */

/** 输入框、下拉、可编辑区域里不抢按键：用户正在打字时快捷键全让路 */
function isTypingTarget(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/** 只要有弹窗开着，除 Esc 之外的快捷键都先让路，避免误触底下的界面 */
function anyModalOpen() {
  return [...document.querySelectorAll('.modal')].some((m) => !m.hidden);
}

/** 复制文本：桌面端 navigator.clipboard 偶发不可用，退回 execCommand 兜底 */
async function copyText(text) {
  const value = String(text ?? '');
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (err) {
    /* 落到下面的兜底 */
  }
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch (err) {
    return false;
  }
}

document.addEventListener('keydown', (event) => {
  // Esc：关掉当前弹窗（各弹窗都有自己的关闭按钮，这里统一兜一份）
  if (event.key === 'Escape') {
    for (const id of [
      '#modalAccounts', '#modalImageModels', '#modalVideoModels', '#modalMaterialSources',
      '#modalApiTasks', '#modalTopaz', '#modalSettings', '#modalShortcuts', '#modalStoryModel',
    ]) {
      const el = $(id);
      if (el && !el.hidden) el.hidden = true;
    }
    return;
  }

  // Ctrl / Cmd + Enter：光标在画布卡片里 → 直接开始这张画布；否则提交所在表单
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    const card = document.activeElement?.closest?.('.canvas');
    if (card) {
      event.preventDefault();
      card.querySelector('[data-act="run"]')?.click();
      return;
    }
    const form = document.activeElement?.closest?.('form');
    if (form) {
      event.preventDefault();
      form.requestSubmit();
      return;
    }
    // 素材搜索没包在 <form> 里，单独照顾一下
    const id = document.activeElement?.id;
    if (id === 'matSearchInput') { event.preventDefault(); $('#matSearchGo')?.click(); return; }
    return;
  }

  // 数字键 1–7：切到对应模块（不带修饰键、不在输入框里、没有弹窗开着时才生效）
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (isTypingTarget(event.target) || anyModalOpen()) return;
  const idx = ['1', '2', '3', '4', '5', '6', '7'].indexOf(event.key);
  if (idx >= 0) {
    event.preventDefault();
    switchModule(MODULES[idx]);
  }
});

/* 快捷键说明弹窗：列表在这里维护，界面上只负责渲染 */
const SHORTCUTS = [
  { keys: ['1', '2', '3', '4', '5', '6', '7'], desc: '依次切到「自用 / 节点画布 / 视频生成 / 图片生成 / 素材 / 发布抖音 / AI 工具箱」七个模块' },
  { keys: ['Ctrl', 'Enter'], desc: '提交当前表单：在图片生成 / 视频生成的提示词里按，直接开始生成' },
  { keys: ['Ctrl', 'Enter'], desc: '光标在某个画布卡片里时，立刻开始这张画布的生成' },
  { keys: ['←', '→'], desc: '素材模块里翻上一条 / 下一条（也可以点卡片两侧的圆形箭头）' },
  { keys: ['Esc'], desc: '关闭当前弹窗，或放弃正在编辑的行内输入（如换 Key）' },
  { keys: ['Ctrl', 'R'], desc: '重新加载界面，等同顶栏「刷新界面」' },
];

function renderShortcuts() {
  const grid = $('#shortcutGrid');
  if (!grid) return;
  grid.innerHTML = SHORTCUTS.map(
    (s) => `<div class="shortcut-row">
      <span class="shortcut-keys">${s.keys.map((k) => `<kbd>${esc(k)}</kbd>`).join('')}</span>
      <span class="shortcut-desc">${esc(s.desc)}</span>
    </div>`,
  ).join('');
}

$('#btnShortcuts')?.addEventListener('click', () => {
  renderShortcuts();
  $('#modalShortcuts').hidden = false;
});

$('#modalShortcuts')?.addEventListener('click', (event) => {
  if (event.target.id === 'modalShortcuts' || event.target.closest('[data-close]')) {
    $('#modalShortcuts').hidden = true;
  }
});

/* 画布参数：一键收起 / 展开全部，偏好记在 localStorage，新建的画布也照这个来 */
const CANVAS_PARAMS_KEY = 'rh.canvasParams.collapsed';

function canvasParamsCollapsed() {
  try {
    return localStorage.getItem(CANVAS_PARAMS_KEY) !== '0';
  } catch (err) {
    return true; // localStorage 不可用时沿用默认（收起）
  }
}

function setCanvasParamsCollapsed(collapsed) {
  try {
    localStorage.setItem(CANVAS_PARAMS_KEY, collapsed ? '1' : '0');
  } catch (err) {
    /* 忽略 */
  }
  for (const card of document.querySelectorAll('#canvasGrid .canvas')) {
    card.classList.toggle('params-collapsed', collapsed);
  }
  syncParamsToggleBtn();
}

/** 按钮文案跟着实际状态走：只要还有展开的卡，按钮就是「收起参数」 */
function syncParamsToggleBtn() {
  const btn = $('#btnToggleParams');
  if (!btn) return;
  const anyExpanded = [...document.querySelectorAll('#canvasGrid .canvas')].some(
    (c) => !c.classList.contains('params-collapsed'),
  );
  const text = anyExpanded ? '收起参数' : '展开参数';
  if (btn.textContent !== text) btn.textContent = text;
  btn.title = anyExpanded
    ? '一键收起所有画布的「其他参数」'
    : '一键展开所有画布的「其他参数」';
}

$('#btnToggleParams')?.addEventListener('click', () => {
  const anyExpanded = [...document.querySelectorAll('#canvasGrid .canvas')].some(
    (c) => !c.classList.contains('params-collapsed'),
  );
  setCanvasParamsCollapsed(anyExpanded); // 有展开的 → 全收起；全收起 → 全展开
});

/* 一键在资源管理器里打开「自用模块生成结果」目录 */
$('#btnOpenOutput')?.addEventListener('click', async () => {
  if (!desktop?.storage) return toast('网页版没有「打开文件夹」功能，桌面版才有', true);
  let item = (state.storage || []).find((s) => s.key === 'outputs');
  if (!item?.path) {
    await refresh().catch(() => {});
    item = (state.storage || []).find((s) => s.key === 'outputs');
  }
  if (!item?.path) return toast('还没拿到输出目录，稍后再试', true);
  const err = await desktop.storage.open(item.path);
  if (err) toast(err, true);
});

/* 回到顶部：长页面（画布多、素材列表长）时露出来 */
(() => {
  const btn = $('#backToTop');
  if (!btn) return;
  const sync = () => {
    btn.hidden = (window.scrollY || document.documentElement.scrollTop || 0) < 400;
  };
  window.addEventListener('scroll', sync, { passive: true });
  btn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  sync();
})();

/* 折叠状态存 localStorage，刷新后保持一致（默认展开） */
try {
  if (localStorage.getItem(GEN_COLLAPSED_KEY) === '1') $('#genPanel').classList.add('collapsed');
} catch (err) {
  /* localStorage 不可用时忽略 */
}

/* 上次停在哪个模块也记着 */
try {
  switchModule(localStorage.getItem(MODULE_KEY) || 'self');
} catch (err) {
  switchModule('self');
}

refresh();
setInterval(refresh, 1500);