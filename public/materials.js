/**
 * 素材模块前端
 *
 * 从第三方素材接口拉图片/视频，支持下载到本机、以及投送到别的模块：
 *   - 视频 → 某张画布的「参考视频」字段
 *   - 图片 → 图片生成模块的参考图
 *
 * 只对外暴露三个方法：window.RHMaterial = { sync(state), onShow(), openManager() }
 * app.js 的 refresh() 每次拿到 /api/state 就调 sync()。
 *
 * 注意：sync() 只同步「分类配置」，绝不自动去拉远端列表 —— refresh() 每 1.5 秒跑一次，
 * 在这里发请求会把第三方接口打爆。列表只在用户切分类 / 点「刷新」时才请求。
 *
 * 工具函数由 app.js 通过 window.RHUtil 提供（本文件在 app.js 之前加载，
 * 所以只能在真正用到的时候再去取，不能在加载时缓存）。
 *
 * 整个文件包在 IIFE 里：本文件与 app.js 都是普通脚本、共用同一个全局作用域，
 * 而 app.js 里同样有 buildCard() 这样的顶层函数，它在本文件之后加载、会把同名函数
 * 覆盖掉（曾导致素材卡片被渲染成画布卡片）。收进闭包后只把 window.RHMaterial 露出去。
 */

(() => {

const RM = {
  sources: [],
  linePool: [], // 全局线路池基地址（所有线路池分类共用，实际线路 = 基地址/编号.m3u8）
  kind: 'video', // video | image，对应两个子 Tab
  sourceId: '',
  items: [],
  index: 0, // 轮播位置：一次只看一条，点「下一个」前进
  fetchedAt: 0,
  line: '', // 本次列表实际用上的线路地址（多线路时自动换线，这里显示用的是哪条）
  loading: false,
  error: '',
  sent: {}, // url → 提示文案：投送过的卡片打标记，避免重复点（纯内存，不落库）
  editing: '', // 接口管理里正在编辑的分类 id
  dyDone: 0, // 本次进入「抖音采集」后，已经成功加进画布几条
  loadedFor: '', // 已加载过的「分类+类型」组合，用于 onShow 时判断要不要自动拉一次
  hls: [], // 挂到卡片 <video> 上的 hls.js 实例，重建时逐个销毁
  // 关键词搜图（只在图片子 Tab 用）：关键词现搜，和上面的「随机素材分类」是两回事
  searchSources: [], // 图源列表，由后端 /api/state 下发（pixiv / booru / bing）
  rankModes: [], // 榜单列表，由后端 /api/state 下发（日榜 / 周榜 / 月榜 / 男性向 / 女性向）
  search: { tab: 'random', q: '', source: '', items: [], page: 0, busy: false, error: '' },
  // pixiv 官方排行榜：和关键词搜索共用同一片缩略图墙，靠 search.tab 切换
  rank: { mode: 'day', items: [], page: 0, busy: false, error: '' },
  // 随机二次元人物：lolicon 全图库随机一批，同样展示在这片墙上，带作品名/画师
  random: { items: [], page: 0, busy: false, error: '' },
};

const m$ = (sel) => document.querySelector(sel);
const mUtil = () => window.RHUtil || {};
const mApi = (path, options) => mUtil().api(path, options);
const mToast = (message, isError) => mUtil().toast(message, isError);

const KIND_LABEL = { video: '视频', image: '图片' };

/* --------------------------------- 分类下拉 --------------------------------- */

const sourcesOfKind = () => RM.sources.filter((s) => s.kind === RM.kind);

const currentSource = () => RM.sources.find((s) => s.id === RM.sourceId) || null;

/** 当前分类是不是「抖音采集」：它不走后端线路，由桌面版内置浏览器采集 */
const isDouyin = () => currentSource()?.mode === 'douyin';
const dyBridge = () => window.rhDesktop?.douyin || null;

const linesOf = (source) => (Array.isArray(source?.lines) ? source.lines : []);

/** 分类实际能用的线路：全局池拼出来的 + 自己填的（列表/提示按这个算，编辑表单只回填自己填的） */
const effectiveLinesOf = (source) => [...(Array.isArray(source?.builtLines) ? source.builtLines : []), ...linesOf(source)];

/** 卡片/投送标记按 id 认：m3u8 线路池一批几条共用同一个地址，只能用 id 区分 */
const keyOf = (item) => item.id || item.url;

const hostOf = (url) => {
  try {
    return new URL(url).host;
  } catch (err) {
    return url;
  }
};

/** 子 Tab 高亮 */
function renderSubtabs() {
  for (const tab of document.querySelectorAll('#matSubtabs .mat-subtab')) {
    tab.classList.toggle('active', tab.dataset.kind === RM.kind);
  }
}

/** 分类下拉：只列当前类型的分类，重建后仍保住已选项 */
function renderSourceOptions() {
  const list = sourcesOfKind();
  if (!list.some((s) => s.id === RM.sourceId)) {
    RM.sourceId = list[0]?.id || '';
    // 换分类要重新拉列表
    RM.items = [];
    RM.error = '';
    RM.loadedFor = '';
  }

  const select = m$('#matSource');
  const sig = `${RM.kind}|${list.map((s) => `${s.id}:${s.name}`).join(',')}`;
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    if (!list.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = `还没有${KIND_LABEL[RM.kind]}分类`;
      select.appendChild(option);
    }
    for (const source of list) {
      const option = document.createElement('option');
      option.value = source.id;
      option.textContent =
        source.mode === 'douyin'
          ? source.name
          : effectiveLinesOf(source).length
            ? source.name
            : `${source.name}（未填地址）`;
      select.appendChild(option);
    }
  }
  select.value = RM.sourceId;
  m$('#matReload').disabled = !RM.sourceId || RM.loading;
}

/* ---------------------------------- 栅格 ---------------------------------- */

/** 挂 hls.js：Chromium 自身不会播 m3u8，必须靠它把分片喂给 <video> */
function attachHls(video, url) {
  const Hls = window.Hls;
  if (!Hls || !Hls.isSupported()) {
    video.src = url; // Safari 这类原生支持 HLS 的浏览器直接给 src
    return;
  }
  const hls = new Hls({ enableWorker: true });
  RM.hls.push(hls);
  hls.loadSource(url);
  hls.attachMedia(video);
}

/**
 * 轮播一次只渲染一条，但渲染时就把 hls.js 挂上等于切一条打一轮请求，
 * 所以等到用户点播放（或点一下画面）再挂。
 */
function attachHlsOnDemand(video, url) {
  const start = () => {
    video.removeEventListener('pointerdown', start);
    video.removeEventListener('play', start);
    attachHls(video, url);
    video.addEventListener('canplay', () => video.play().catch(() => {}), { once: true });
  };
  video.addEventListener('pointerdown', start);
  video.addEventListener('play', start);
}

/** 重建栅格前先把旧的 hls.js 实例销毁，否则它们会一直在后台拉分片 */
function releaseHls() {
  for (const hls of RM.hls) {
    try {
      hls.destroy();
    } catch (err) {
      // 已经销毁过就算了
    }
  }
  RM.hls = [];
}

function buildCard(item) {
  const card = document.createElement('article');
  card.className = 'mat-card';
  card.dataset.key = item.id || item.url;

  const media = document.createElement('div');
  media.className = 'mat-media';
  if (RM.kind === 'image') {
    const img = document.createElement('img');
    img.src = item.url;
    img.alt = '';
    img.loading = 'lazy';
    media.appendChild(img);
  } else {
    const video = document.createElement('video');
    video.controls = true;
    // 轮播切条时也不预载，点一下才开始拉
    video.preload = 'none';
    video.playsInline = true;
    if (item.poster) video.poster = item.poster;
    if (item.hls) {
      // hls.js 走 MSE，video 的 src 是 blob:，取帧不受跨域限制
      attachHlsOnDemand(video, item.url);
    } else {
      // 要能截图，canvas 就得读得到像素：标上 crossOrigin。源站不放 CORS 会直接播不了，
      // 那种情况下去掉标记重试一次 —— 至少还能看，只是截不了图
      video.crossOrigin = 'anonymous';
      video.addEventListener('error', () => {
        // 只用一次：去掉跨域标记重试，避免和 error 事件互相触发死循环
        if (video.hasAttribute('crossorigin')) {
          video.removeAttribute('crossorigin');
          video.src = item.url;
        }
      });
      video.src = item.url;
    }
    media.appendChild(video);
  }
  card.appendChild(media);

  if (item.title) {
    const title = document.createElement('div');
    title.className = 'mat-title';
    title.textContent = item.title;
    title.title = item.title;
    card.appendChild(title);
  }

  const actions = document.createElement('div');
  actions.className = 'mat-actions';

  const download = document.createElement('button');
  download.className = 'btn tiny';
  download.dataset.act = 'mat-download';
  download.textContent = '下载';
  download.title = '下载到本机（接口给的就是无水印原片）';
  actions.appendChild(download);

  const sent = RM.sent[keyOf(item)];
  if (RM.kind === 'video') {
    const targets = mUtil().videoTargets ? mUtil().videoTargets() : [];
    const select = document.createElement('select');
    select.dataset.role = 'mat-target';
    if (!targets.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '先给画布点「读取参数」';
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

    const add = document.createElement('button');
    add.className = 'btn tiny primary';
    add.dataset.act = 'mat-to-canvas';
    add.textContent = '添加到画布';
    add.title = '把这个视频添加到某张画布，作为参考视频（会再复制一份到画布素材目录）';
    // 不置灰：没选好目标时点它会弹提示，置灰了点了完全没反应
    actions.appendChild(add);

    // 截图：暂停到想要的画面，把当前帧截成图片扔进图片生成的参考图
    const shot = document.createElement('button');
    shot.className = 'btn tiny';
    shot.dataset.act = 'mat-capture';
    shot.textContent = '截图';
    shot.title = '暂停到想要的画面，点这里把当前帧截成图片并加入图片生成的参考图';
    actions.appendChild(shot);
  } else {
    const add = document.createElement('button');
    add.className = 'btn tiny primary';
    add.dataset.act = 'mat-to-ref';
    add.textContent = '添加到图片生成';
    add.title = '把这张图加进图片生成模块的参考图里，切到图生图模式进行二创';
    actions.appendChild(add);
  }

  if (sent) {
    const badge = document.createElement('span');
    badge.className = 'used-tag';
    badge.textContent = sent;
    badge.title = sent;
    actions.appendChild(badge);
  }

  card.appendChild(actions);
  return card;
}

function renderGrid() {
  const grid = m$('#matGrid');
  const item = RM.items[RM.index] || null;
  // 画布目标列表也要进签名：先开素材卡、再去画布点「读取参数」时，卡片得跟着重建出可选目标
  const targets = RM.kind === 'video' && mUtil().videoTargets ? mUtil().videoTargets() : [];
  const sig = JSON.stringify([RM.kind, RM.index, item, RM.sent, targets.map((t) => t.value)]);
  if (grid.dataset.sig === sig) return;
  grid.dataset.sig = sig;
  releaseHls();
  // 重建前记住已选的目标，列表变了也不用用户白选一次
  const picked = grid.querySelector('select[data-role="mat-target"]')?.value || '';
  grid.textContent = '';

  if (!item) {
    const hint = document.createElement('p');
    hint.className = 'hint mat-empty';
    if (RM.loading) {
      hint.textContent = '正在拉取素材…';
    } else if (RM.error) {
      hint.textContent = RM.error;
    } else {
      hint.textContent = RM.sourceId ? '这个分类还没拉到素材，点「刷新」再试' : '先在「接口管理」里加一个素材分类';
    }
    grid.appendChild(hint);
    return;
  }
  grid.appendChild(buildCard(item));

  const select = grid.querySelector('select[data-role="mat-target"]');
  if (select && picked && [...select.options].some((o) => o.value === picked)) select.value = picked;
}

function renderNav() {
  const total = RM.items.length;
  // 抖音采集是「一次性采到的一批」，翻到最后就停住（再往下不会自动补新一批）
  const atEnd = isDouyin() && RM.index >= total - 1;
  m$('#matPrev').disabled = !total || RM.index <= 0 || RM.loading;
  m$('#matNext').disabled = !total || RM.loading || atEnd;
  m$('#matPos').textContent = total ? `第 ${RM.index + 1} / ${total} 条` : '';
}

function renderHint() {
  const hint = m$('#matHint');
  const source = currentSource();
  if (RM.loading) {
    hint.className = 'hint';
    hint.textContent = isDouyin() ? '正在从抖音采集…' : '正在拉取素材…';
    return;
  }
  if (RM.error) {
    hint.className = 'hint err';
    hint.textContent = RM.error;
    return;
  }
  hint.className = 'hint';
  if (isDouyin()) {
    hint.textContent = RM.dyDone
      ? `已添加 ${RM.dyDone} 条到画布 · ${new Date().toLocaleTimeString()}`
      : '在下面抖音页面里刷：视频右上角「＋」加画布、「图」截当前画面当参考图；没看到就用右下角那两个按钮';
    return;
  }
  const byLine = RM.line ? ` · 线路 ${hostOf(RM.line)}` : '';
  if (!source) {
    hint.textContent = `还没有${KIND_LABEL[RM.kind]}分类：点「接口管理」加一条（一个分类可配多条线路）`;
  } else if (!RM.items.length) {
    hint.textContent = effectiveLinesOf(source).length ? '点「刷新」拉取素材' : '这个分类还没线路：点「接口管理」填全局线路池 + 编号，或直接填完整地址';
  } else {
    hint.textContent = `共 ${RM.items.length} 条${byLine}${
      RM.fetchedAt ? ` · ${new Date(RM.fetchedAt).toLocaleTimeString()}` : ''
    }`;
  }
}

function render() {
  renderSubtabs();
  renderSourceOptions();
  renderWebMode();
  renderSearch();
  renderGrid();
  renderNav();
  renderHint();
}

/**
 * 「内嵌网页」类分类（抖音采集）：内容区换成内嵌网页，工具条只留抖音那一行。
 */
function renderWebMode() {
  const dy = isDouyin();
  const reload = m$('#matReload');
  if (reload) reload.hidden = dy;
  const view = m$('#matView');
  if (view) view.hidden = dy;
  // 抖音那一套
  const dvin = m$('#matDouyin');
  if (dvin) dvin.hidden = !dy;
  const dybar = m$('#matDyBar');
  if (dybar) dybar.hidden = !dy;
  const dyweb = m$('#matWeb');
  if (dyweb) dyweb.hidden = !dy;

  // 抖音模式给工作台挂个 class：CSS 靠它把整块撑满可视区，
  // 让内嵌网页吃掉剩余高度（见 style.css 里 .mat-web 那段注释）。
  // 用 JS 挂 class 而不是 CSS :has()，少依赖一个选择器特性。
  const bench = m$('.mat-workbench');
  if (bench) bench.classList.toggle('is-douyin', dy);
  // 不是抖音分类了，放大状态必须收回来（否则那个 class 会赖在别的页面上）
  if (!dy) resetDyBig();

  if (dy) {
    ensureDyView();
    renderDyTarget();
    renderDyHint();
    startDyWatch();
  }
}

/* ------------------------------ 抖音缩放 ------------------------------ */

/**
 * 抖音是竖屏站：在宽窗口里内容只占中间一条，两边全是空白，看着就「小」。
 * 容器已经撑到 93% 了，再挤容器没用 —— 得把**页面本身**放大。
 *
 * webview 有 setZoomFactor（等价于浏览器的 Ctrl+加号），放大后视频、文字、
 * 按钮一起变大。放大到 200% 时，竖屏视频基本能填满窗口高度。
 *
 * 缩放值存 localStorage：调好了下次进来还是这个大小，不用每次重调。
 */
const DY_ZOOM_KEY = 'rh.dyZoom';
const DY_ZOOM_MIN = 0.5;
const DY_ZOOM_MAX = 3;
const DY_ZOOM_STEP = 0.1;
let dyZoom = 1;

function loadDyZoom() {
  try {
    const v = Number(localStorage.getItem(DY_ZOOM_KEY));
    if (Number.isFinite(v) && v >= DY_ZOOM_MIN && v <= DY_ZOOM_MAX) return v;
  } catch { /* 隐私模式读不到就用默认 */ }
  return 1;
}

/** 把缩放应用到 webview，并刷新那个百分比按钮 */
function applyDyZoom(next, { persist = true } = {}) {
  const v = Math.max(DY_ZOOM_MIN, Math.min(DY_ZOOM_MAX, Math.round(next * 100) / 100));
  dyZoom = v;
  if (dyView && typeof dyView.setZoomFactor === 'function') {
    try {
      dyView.setZoomFactor(v);
    } catch { /* 页面还没就绪，下次 dom-ready 时会再应用一遍 */ }
  }
  const btn = m$('#matDyZoomVal');
  if (btn) btn.textContent = `${Math.round(v * 100)}%`;
  if (persist) {
    try {
      localStorage.setItem(DY_ZOOM_KEY, String(v));
    } catch { /* 记不住就算了，不影响本次使用 */ }
  }
}

/** 缩放到位：先把存下来的值读出来，等 webview 就绪后再应用一次 */
function initDyZoom() {
  dyZoom = loadDyZoom();
  const btn = m$('#matDyZoomVal');
  if (btn) btn.textContent = `${Math.round(dyZoom * 100)}%`;
  applyDyZoom(dyZoom, { persist: false });
}

/* ------------------------------ 抖音放大模式 ------------------------------ */

/**
 * 内嵌抖音的网页区域还是不够看：小窗口下上下都是我们自己的工具条，留给网页的
 * 只有六百来像素。这里给一个「放大」开关，把抖音铺满整个窗口 ——
 * 顶栏、分类行、以及别的模块全部让位，只留一条细工具栏 + 网页。
 *
 * 状态挂在 body 上（class `dy-big`），CSS 里一次压掉所有别的块。
 * Esc 也能还原：全屏看东西时按 Esc 是本能动作，不给它留个出口会很难受。
 */
let dyBig = false;

function setDyBig(on) {
  dyBig = Boolean(on);
  document.body.classList.toggle('dy-big', dyBig);
  const btn = m$('#matDyBig');
  if (btn) {
    btn.textContent = dyBig ? '⛶ 还原' : '⛶ 放大';
    btn.classList.toggle('primary', dyBig);
  }
  const bar = m$('#matDyBarHint');
  if (bar && dyBig) bar.textContent = '';
  // 顶栏高度变了（放大时藏起来了），通知 app.js 重算那个变量
  try {
    (window.RHUtil || {}).syncTopbar?.();
  } catch { /* 没有就算了 */ }
  if (!dyBig) renderDyHint();
}

/** 切走素材模块 / 切到别的分类时，把放大状态收回来，免得留在别的页面里 */
function resetDyBig() {
  if (dyBig) setDyBig(false);
}

/* ------------------------------- 内嵌抖音 ------------------------------- */

let dyView = null; // 内嵌的 <webview>，只在选中「抖音采集」时建一次，切走切回不重载
let dyViewPromise = null;
let dyWorking = false; // 正在把点到的视频加进画布
let dyHooksReady = false; // 页面钩子这一轮页面加载里注入过没有（省得每 2 秒重复注入）
let dyWatchTimer = null; // 后台盯着「现在认到的是哪条视频」，把状态显示在工具栏上
const dyQueue = []; // 连点几次「＋」时排队，一条条来

/**
 * 建（或复用）内嵌抖音网页。
 * webview 的 preload 路径只能问主进程要 —— 页面侧（本文件）拿不到 electron 目录。
 * 钩 fetch / XHR、挂「＋」按钮、把点到的视频递回来，都在 douyin-preload.cjs 里做。
 */
function ensureDyView() {
  if (dyView) return Promise.resolve(dyView);
  if (!dyViewPromise) dyViewPromise = createDyView().finally(() => { dyViewPromise = null; });
  return dyViewPromise;
}

async function createDyView() {
  const host = m$('#matWeb');
  const bridge = dyBridge();
  if (!host || !bridge || dyView) return dyView;
  const preload = await bridge.preloadUrl().catch(() => '');
  if (dyView) return dyView;
  const view = document.createElement('webview');
  view.setAttribute('partition', 'persist:douyin');
  if (preload) view.setAttribute('preload', preload);
  view.setAttribute('src', 'https://www.douyin.com/');
  view.addEventListener('ipc-message', onDyMessage);
  // 双保险：预载脚本如果因为打包进 asar 没加载成，这里再自己把钩子打一遍（幂等，重复注入会自己跳过）
  view.addEventListener('dom-ready', () => {
    dyHooksReady = false;
    ensureDyHooks();
    // 每次页面加载完都把缩放应用一遍：webview 导航会重置 zoomFactor，
    // 不重设的话刷新一次就缩回 100% 了
    applyDyZoom(dyZoom, { persist: false });
  });
  host.textContent = '';
  host.appendChild(view);
  dyView = view;
  // webview 一建好就把存下来的缩放应用上（页面还没加载完也没关系，
  // setZoomFactor 会记住，dom-ready 时再兜一次）
  initDyZoom();
  return view;
}

/** 抖音页面里的「＋」按下去后，从这条通道递过来 */
function onDyMessage(event) {
  if (event.channel === 'douyin:pick') enqueueDy(event.args?.[0]);
  else if (event.channel === 'douyin:pick-key') {
    const key = event.args?.[0] || {};
    const bridge = dyBridge();
    if (bridge?.copy && key.url) bridge.copy(key.url).catch(() => {});
    // 这是「连详情接口都没拿到」的最后兜底：至少把链接给你，不丢东西。
    // 提示语别再说「往下滚一点」—— 搜索页往下滚也没用，会误导。
    mToast(`这条的直链没取到（抖音没给可下载地址），已把链接复制到剪贴板：${key.url || ''}`, true);
  } else if (event.channel === 'douyin:pick-miss')
    mToast('没认准当前这条：把它点开放大播放，再用上方工具栏的「＋ 当前视频加画布」', true);
}

function enqueueDy(payload) {
  const item = payload?.item;
  if (!item || !item.id) return;
  dyQueue.push({ item, act: payload.act === 'ref' ? 'ref' : 'canvas' });
  runDyQueue();
}

async function runDyQueue() {
  const bridge = dyBridge();
  if (!bridge || dyWorking) return;
  dyWorking = true;
  try {
    while (dyQueue.length) {
      const job = dyQueue.shift();
      if (job.act === 'ref') await addDyToImageRef(job.item, bridge);
      else await addDyToCanvas(job.item, bridge);
    }
  } finally {
    dyWorking = false;
    renderDyHint();
  }
}

/** 内嵌抖音 <webview> 的 webContents id：主进程要借它自己的浏览器上下文下载（才带登录态/Referer） */
function dyGuestId() {
  try {
    return dyView && typeof dyView.getWebContentsId === 'function' ? dyView.getWebContentsId() : 0;
  } catch {
    // webContents 还没建好
    return 0;
  }
}

/** 复用普通卡片的投送通道（/api/materials/to-canvas）：先下成本地文件，再交给后端入画布 */
async function addDyToCanvas(item, bridge) {
  const value = m$('#matDyTarget')?.value || '';
  const [canvasId, fieldKey] = value.split('|');
  if (!canvasId || !fieldKey) {
    mToast('先在右上角选一个「目标画布参数」，再点视频上的「＋」', true);
    return;
  }
  setDyHint(`正在添加：${String(item.desc || item.id || '').slice(0, 24)}…`);
  try {
    const out = await bridge.download(item, dyGuestId());
    if (!out?.ok) throw new Error(out?.error || '没下载到可用的视频');
    // 这条视频的话题一起交给后端：后端会存进话题库，发布时就能挑到
    const topics = Array.isArray(item.tags) ? item.tags : [];
    const res = await mApi('/api/materials/to-canvas', {
      method: 'POST',
      body: {
        localFile: out.localPath,
        canvasId,
        fieldKey,
        sourceId: RM.sourceId,
        topics,
        sourceDesc: String(item.desc || '').slice(0, 80),
      },
      timeoutMs: 240000,
    });
    RM.dyDone += 1;
    const added = (res.topicsAdded || []).length;
    const existed = (res.topicsExisted || []).length;
    const topicTip = topics.length
      ? added
        ? `，并记下 ${added} 个话题${existed ? `（${existed} 个已在库）` : ''}`
        : `，${existed} 个话题已在库中`
      : '';
    mToast(`已添加到「${res.canvasName}」的 ${res.field}${topicTip}`);
    // 话题库变了，顺手让发布页那边刷新列表
    if (topics.length) mUtil().refresh?.();
  } catch (err) {
    mToast(err.message, true);
  }
}

/**
 * 「参考图」按钮：优先**截当前画面**（看到什么得到什么），截不到才退回封面。
 *
 * 以前直接用封面 —— 但抖音的封面是裁剪过的竖版缩略图，跟用户看到的画面差很远，
 * 所以会出现「图一看到全身、加进去只有半身」。现在反过来：截图为主、封面兜底。
 */
async function addDyToImageRef(item, bridge) {
  setDyHint(`正在截画面：${String(item.desc || item.id || '').slice(0, 24)}…`);
  try {
    // 主路：截当前画面（完整、所见即所得）
    try {
      const ref = await captureToImageRef();
      mToast('已把当前画面截图加入「图片生成」的参考图');
      return ref;
    } catch (shotErr) {
      // 退路：截不到（页面没就绪 / 没在播放）就用封面，并说明白用的是封面
      setDyHint('截画面没成功，改用封面…');
      const out = await bridge.downloadCover(item, dyGuestId());
      if (!out?.ok) throw new Error(out?.error || `没截到画面，封面也没下到：${shotErr.message}`);
      const ref = await mApi('/api/materials/to-image-ref', {
        method: 'POST',
        body: { localFile: out.localPath, sourceId: RM.sourceId },
        timeoutMs: 240000,
      });
      const addRefs = mUtil().addRefs;
      if (!addRefs) throw new Error('参考图接口还没就绪，请稍后重试');
      if (!addRefs([ref], { stay: true })) {
        throw new Error('图片生成模块的参考图已满，先在那边移除几张再试');
      }
      mToast('截不到当前画面，已改用这条视频的封面（封面可能被裁剪过）', true);
      return ref;
    }
  } catch (err) {
    mToast(err.message, true);
    return null;
  }
}

/**
 * 把「页面世界」的钩子打进抖音网页。这些钩子本来在 douyin-preload.cjs 里随预载跑，
 * 但打包进 asar 后预载不一定能被 <webview> 认到，所以宿主这边再补一次 —— 代码由主进
 * 程从同一个文件里抠出来给（见 electron/douyin.mjs 的 pageCode），注入幂等。
 */
async function ensureDyHooks() {
  const bridge = dyBridge();
  if (!dyView || !bridge?.pageCode || dyHooksReady) return;
  try {
    const code = await bridge.pageCode();
    if (code) {
      await dyView.executeJavaScript(code);
      dyHooksReady = true;
    }
  } catch {
    // 页面还没就绪就下次再试，不报错
  }
}

/**
 * 问抖音网页「现在看的是哪条」：判定逻辑（地址栏 / 正在播放的 <video> / 屏幕中心 / 最近的卡片）
 * 全在页面世界的 window.__dyCurrent 里，和网页内右下角浮动按钮共用同一套，省得两边写法不一致。
 */
const DY_CURRENT_JS = `(() => (typeof window.__dyCurrent === 'function' ? window.__dyCurrent() : null))()`;

/** 拿回 { id, url, item, known }；页面钩子还没打进去时返回 null */
async function dyCurrent() {
  await ensureDyHooks();
  if (!dyView) return null;
  try {
    return await dyView.executeJavaScript(DY_CURRENT_JS);
  } catch {
    return null;
  }
}

/** 工具栏「＋ 当前视频加画布」：认准当前这条 → 下单 → 入画布；抓不到直链就把链接复制到剪贴板兜底 */
async function onDyAddCurrent() {
  setDyHint('正在认当前这条视频…');
  const cur = await dyCurrent();
  const bridge = dyBridge();
  if (!cur || !cur.id) {
    mToast('没认准当前这条：把它点开放大播放（或点它上面的「＋」）再点这里', true);
    renderDyHint();
    return;
  }
  if (!cur.item) {
    if (bridge?.copy) await bridge.copy(cur.url).catch(() => {});
    mToast(`这条还没抓到直链，已把链接复制到剪贴板（${cur.url}）；往下滚一点再试一次`, true);
    renderDyHint();
    return;
  }
  enqueueDy({ item: cur.item, act: 'canvas' });
}

/**
 * 截内嵌抖音「当前画面」并存进图片生成的参考图。
 *
 * 为什么要用截图而不是封面：抖音的封面是**裁剪过的竖版缩略图**（只取画面中间一块），
 * 跟用户正在看的完整画面根本不是一回事 —— 之前卡片上的「参考图」按钮取的就是封面，
 * 结果「看到的是全身、加进去只有半身」。现在统一改成截当前画面：看到什么就得到什么。
 */
async function captureToImageRef() {
  const bridge = dyBridge();
  if (!bridge?.capture) throw new Error('截屏通道还没就绪，请稍后重试');
  if (!dyView) throw new Error('内嵌抖音还没准备好');

  // 只截正在播放的那块 <video>；量不到就整窗（capture 内部会兜）
  const rect = await dyView
    .executeJavaScript(
      `(() => {
        const v = document.querySelector('video');
        if (!v) return null;
        const r = v.getBoundingClientRect();
        if (r.width < 40 || r.height < 40) return null;
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      })()`,
    )
    .catch(() => null);

  const out = await bridge.capture(dyGuestId(), rect);
  if (!out?.ok) throw new Error(out?.error || '没截到画面');

  const ref = await mApi('/api/materials/to-image-ref', {
    method: 'POST',
    body: { localFile: out.localPath, sourceId: RM.sourceId },
    timeoutMs: 240000,
  });
  const addRefs = mUtil().addRefs;
  if (!addRefs) throw new Error('参考图接口还没就绪，请稍后重试');
  if (!addRefs([ref], { stay: true })) throw new Error('图片生成模块的参考图已满，先在那边移除几张再试');
  return ref;
}

/** 工具栏「截屏 → 参考图」：把抖音窗口当前画面截下来（有 <video> 就只截它），当图片生成的参考图 */
async function onDyShot() {
  setDyHint('正在截当前画面…');
  try {
    await captureToImageRef();
    mToast('已把当前画面截图加入「图片生成」的参考图');
  } catch (err) {
    mToast(err.message, true);
  } finally {
    renderDyHint();
  }
}

/** 工具栏「复制视频链接」：把当前这条的分享链接复制到剪贴板 */
async function onDyCopy() {
  const cur = await dyCurrent();
  const bridge = dyBridge();
  if (!cur || !cur.id) {
    mToast('没认准当前这条：把它点开放大播放再试', true);
    return;
  }
  try {
    await bridge.copy(cur.url);
    mToast(`已复制视频链接：${cur.url}`);
  } catch {
    mToast('复制失败，请重试', true);
  }
}

/**
 * 用系统里的真实 Chrome 登录抖音。
 * 内嵌浏览器的指纹过不了抖音的登录风控（二维码只出 logo、验证码发不出去），
 * 所以借真实 Chrome 登一次，主进程通过调试协议把 Cookie 搬进内嵌分区。
 */
async function onDyChromeLogin() {
  const bridge = dyBridge();
  if (!bridge?.loginChrome) {
    mToast('这个版本没有这个功能（需要桌面版）', true);
    return;
  }
  const btn = m$('#matDyChromeLogin');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '等浏览器里登录…';
  mToast('已打开 Chrome：在弹出的浏览器窗口里登录抖音，登好会自动把登录态搬过来（成功前别关那个窗口）');
  try {
    const r = await bridge.loginChrome();
    if (r?.ok) {
      mToast(`登录态已搬过来（${r.count} 个 Cookie），正在刷新抖音页…`);
      try {
        dyView?.reload();
      } catch {
        /* ignore */
      }
    } else {
      mToast('没有拿到登录态', true);
    }
  } catch (err) {
    mToast(`登录没完成：${err?.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/**
 * 后台每 2 秒问一次抖音网页「现在认到的是哪条」，把结果显示在工具栏那行提示上。
 * 目的很直白：让用户看得见这个功能是活的、认没认准，而不是点下去没反应干着急。
 */
function startDyWatch() {
  if (dyWatchTimer) return;
  dyWatchTimer = setInterval(async () => {
    const bar = m$('#matDyBarHint');
    if (!bar) return;
    // 已经切走这个模块了就别再折腾页面：CSS 只是隐藏，定时器不会自己停
    if (document.body.dataset.module !== 'material') return;
    if (!isDouyin() || !dyView) {
      bar.textContent = '';
      return;
    }
    const cur = await dyCurrent();
    if (!cur) {
      bar.textContent = '抖音页面还在加载…';
      return;
    }
    if (!cur.id) {
      bar.textContent = '还没认到当前视频：点开一条视频让它放大播放，再点上面「＋ 当前视频加画布」';
      return;
    }
    if (!cur.item) {
      // 把「已索引 N 条」显出来，方便判断钩子到底有没有在工作：
      // 推荐流下这个数字会一直涨；如果搜索页刷半天还是 0，就是钩子没抓到搜索接口。
      // 点「＋」时还会再主动查一次单条详情，所以这里不劝人「往下滚」了。
      bar.textContent = `已定位到 ${cur.id}，直链还没拿到（已索引 ${cur.known} 条）· 点「＋」会主动去查一次，或点「复制视频链接」`;
      return;
    }
    bar.textContent = `已认到视频 ${cur.id}（已索引 ${cur.known} 条）· 点上面「＋ 当前视频加画布」`;
  }, 2000);
}

/** 目标画布参数下拉：和普通卡片一样，取自所有画布的 VIDEO 参数 */
function renderDyTarget() {
  const select = m$('#matDyTarget');
  if (!select) return;
  const targets = mUtil().videoTargets ? mUtil().videoTargets() : [];
  const sig = targets.map((t) => `${t.value}:${t.label}`).join(',');
  if (select.dataset.sig === sig) return;
  select.dataset.sig = sig;
  const picked = select.value;
  select.textContent = '';
  if (!targets.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '先给画布点「读取参数」';
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
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
  if ([...select.options].some((o) => o.value === picked)) select.value = picked;
}

function setDyHint(text) {
  const hint = m$('#matDyHint');
  if (hint) hint.textContent = text;
}

function renderDyHint() {
  const hint = m$('#matDyHint');
  if (!hint) return;
  if (!dyBridge()) {
    hint.textContent = '内嵌抖音只在桌面版可用';
    return;
  }
  const hasTarget = Boolean(m$('#matDyTarget')?.value);
  const done = RM.dyDone ? `已添加 ${RM.dyDone} 条 · ` : '';
  hint.textContent = `${done}${hasTarget ? '点视频上的「＋」加画布、「图」截当前画面当参考图；右下角也有两个兜底按钮' : '先选右上角的目标画布参数'}`;
}

/* --------------------------------- 拉取列表 --------------------------------- */

async function loadItems({ refresh = false } = {}) {
  const source = currentSource();
  // 「抖音采集」不在后端拉：内容区直接换成内嵌抖音网页
  if (source?.mode === 'douyin') {
    ensureDyView();
    return;
  }
  if (!source || RM.loading) return;
  RM.loading = true;
  RM.error = '';
  RM.items = [];
  RM.index = 0;
  renderGrid();
  renderSourceOptions();
  renderNav();
  renderHint();
  try {
    const out = await mApi(`/api/material-sources/${source.id}/items${refresh ? '?refresh=1' : ''}`, {
      timeoutMs: 30000,
    });
    RM.items = out.items || [];
    RM.fetchedAt = out.fetchedAt || 0;
    RM.line = out.line || '';
    RM.loadedFor = `${RM.kind}:${source.id}`;
  } catch (err) {
    RM.error = err.message;
    RM.items = [];
    RM.line = '';
  } finally {
    RM.loading = false;
    renderGrid();
    renderSourceOptions();
    renderNav();
    renderHint();
  }
}

/* ------------------------------- 轮播 ------------------------------- */

/** 轮播：上一个 / 下一个。看完一批的最后一条再点「下一个」= 换一批新的随机内容 */
async function stepItem(delta) {
  if (RM.loading) return;
  if (!RM.items.length) return loadItems();
  const next = RM.index + delta;
  if (next < 0) return;
  if (next >= RM.items.length) {
    if (isDouyin()) return; // 抖音是采到的一批，翻到头就停住
    await loadItems({ refresh: true });
    autoplayCurrent();
    return;
  }
  RM.index = next;
  renderGrid();
  renderNav();
  autoplayCurrent();
}

/** 切条后直接播：点按钮就是用户手势，Chromium 允许带声播放；m3u8 的 play 事件会触发挂 hls.js */
function autoplayCurrent() {
  const video = m$('#matGrid .mat-media video');
  if (video) video.play().catch(() => {});
}

/* --------------------------------- 卡片动作 --------------------------------- */

/** 触发浏览器另存为：href 指向本地静态文件，避免第三方防盗链 */
function saveAs(localUrl, fileName) {
  const a = document.createElement('a');
  a.href = localUrl;
  a.download = fileName || '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function downloadItem(item) {
  const out = await mApi('/api/materials/download', {
    method: 'POST',
    body: { url: item.url, kind: RM.kind, sourceId: RM.sourceId, unique: Boolean(item.random) },
    // m3u8 要把分片挨个拉下来再合并，比直链慢
    timeoutMs: 180000,
  });
  saveAs(out.localUrl, out.fileName);
  mToast(item.hls ? '已把分片合并成一个文件，存到本机素材目录' : '已保存到本机素材目录，并开始下载');
}

async function toCanvas(item, card, button) {
  const value = card.querySelector('select[data-role="mat-target"]')?.value || '';
  const [canvasId, fieldKey] = value.split('|');
  if (!canvasId || !fieldKey) {
    const hasTargets = (mUtil().videoTargets ? mUtil().videoTargets() : []).length > 0;
    // 顺手把下拉框聚焦一下：只弹提示时用户容易以为「点了没反应」
    card.querySelector('select[data-role="mat-target"]')?.focus();
    return mToast(hasTargets ? '先在下拉里选一个目标画布参数' : '还没有可选目标：先去画布点「读取参数」', true);
  }
  button.disabled = true;
  // 投送要先把这个视频下到本地素材目录再复制给画布，大文件要等几秒，必须有进行中的提示
  button.textContent = '添加中…';
  try {
    const out = await mApi('/api/materials/to-canvas', {
      method: 'POST',
      body: { url: item.url, canvasId, fieldKey, sourceId: RM.sourceId, unique: Boolean(item.random) },
      timeoutMs: 240000,
    });
    RM.sent[keyOf(item)] = `已添加到 ${out.canvasName}`;
    renderGrid();
    mToast(`已添加到「${out.canvasName}」的 ${out.field}`);
  } finally {
    button.disabled = false;
    button.textContent = '添加到画布';
  }
}

async function toImageRef(item) {
  const ref = await mApi('/api/materials/to-image-ref', {
    method: 'POST',
    // 搜图来的结果带自己的来源页：图站/图床普遍校验 Referer，不带就 403
    body: { url: item.url, sourceId: RM.sourceId, referer: item.referer || '' },
    timeoutMs: 180000,
  });
  // 由 app.js 接管：塞进参考图列表、切成图生图、并切回「自用」模块
  if (!mUtil().addRefs) throw new Error('参考图接口还没就绪，请稍后重试');
  if (!mUtil().addRefs([ref])) {
    throw new Error('图片生成模块的参考图已满，先在那边移除几张再试');
  }
  RM.sent[keyOf(item)] = '已加入参考图';
  renderGrid();
  renderSearch();
}

/**
 * 截取视频当前帧 → 上传成图片 → 加入图片生成的参考图。
 * 跨域视频只有在源站放行 CORS 时 canvas 才读得到像素（buildCard 已标 crossOrigin），
 * 被污染的画布 toBlob 会抛 SecurityError，这里给出人话提示。
 */
async function captureFrame(item, card) {
  const video = card.querySelector('.mat-media video');
  if (!video || !video.videoWidth) {
    throw new Error('先把视频播起来，暂停到想要的画面再截图');
  }
  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);

  let blob;
  try {
    blob = await new Promise((resolve, reject) => {
      canvas.toBlob(
        (out) => (out ? resolve(out) : reject(new Error('截图失败，换个画面再试'))),
        'image/jpeg',
        0.92,
      );
    });
  } catch (err) {
    // 未标 crossOrigin 的跨域视频：画布被污染，读不出像素
    throw new Error('这个视频源不允许截图（服务器没放行跨域），换一条再试');
  }

  if (!mUtil().uploadRefBlob || !mUtil().addRefs) throw new Error('参考图接口还没就绪，请稍后重试');
  const stamp = new Date().toTimeString().slice(0, 8).replace(/:/g, '');
  const ref = await mUtil().uploadRefBlob(blob, `视频截图-${stamp}.jpg`);
  // stay：连截几张时别每截一张就把界面跳去图片生成
  if (!mUtil().addRefs([ref], { stay: true })) {
    throw new Error('图片生成模块的参考图已满，先在那边移除几张再试');
  }
  RM.sent[keyOf(item)] = '已截图加入参考图';
  renderGrid();
  mToast('已把当前画面截成图片并加入参考图');
}

/* -------------------------------- 关键词搜图 -------------------------------- */

/** 图源下拉：选项由后端下发，前端不写死（少一处两边走样的地方） */
function renderSearchSources() {
  const select = m$('#matSearchSource');
  const list = RM.searchSources;
  const sig = list.map((s) => `${s.id}:${s.name}`).join(',');
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    for (const source of list) {
      const option = document.createElement('option');
      option.value = source.id;
      option.textContent = source.name;
      select.appendChild(option);
    }
  }
  if (!RM.search.source) RM.search.source = list[0]?.id || '';
  select.value = RM.search.source;
}

/** 榜单下拉：同样由后端下发（日榜/周榜/月榜/男性向/女性向） */
function renderRankModes() {
  const select = m$('#matRankMode');
  const list = RM.rankModes;
  const sig = list.map((m) => `${m.id}:${m.name}`).join(',');
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    for (const mode of list) {
      const option = document.createElement('option');
      option.value = mode.id;
      option.textContent = mode.name;
      select.appendChild(option);
    }
  }
  if (!list.some((m) => m.id === RM.rank.mode)) RM.rank.mode = list[0]?.id || 'day';
  select.value = RM.rank.mode;
}

/** 当前这一片缩略图墙在展示哪个列表：关键词结果 or 排行榜结果 */
const activeSearchList = () => {
  if (RM.search.tab === 'rank') return RM.rank.items;
  if (RM.search.tab === 'random') return RM.random.items;
  return RM.search.items;
};

/** 搜索区只属于「图片」子 Tab：视频子 Tab 直接收起来。随机/关键词/排行榜三模式共用同一片缩略图墙 */
function renderSearch() {
  const box = m$('#matSearch');
  const visible = RM.kind === 'image';
  box.hidden = !visible;
  if (!visible) return;

  const tab = RM.search.tab;
  const isRank = tab === 'rank';
  const isKeyword = tab === 'keyword';
  const isRandom = tab === 'random';
  for (const t of document.querySelectorAll('#matSearchTabs .mat-search-tab')) {
    t.classList.toggle('active', t.dataset.tab === tab);
  }
  // 同一行工具条：哪个模式就只露哪个模式用得上的一组控件
  m$('#matRandomGo').hidden = !isRandom;
  m$('#matSearchInput').hidden = !isKeyword;
  m$('#matSearchSource').hidden = !isKeyword;
  m$('#matRankMode').hidden = !isRank;
  m$('#matSearchGo').textContent = isRank ? '刷新' : '搜索';
  m$('#matSearchGo').hidden = isRandom; // 随机模式用自带的「随机二次元人物」大按钮
  if (isRank) renderRankModes();
  else if (isKeyword) renderSearchSources();

  const view = isRank ? RM.rank : isRandom ? RM.random : RM.search;
  const hint = m$('#matSearchHint');
  if (view.busy) {
    hint.className = 'hint';
    hint.textContent = isRank ? '正在取榜单…' : isRandom ? '正在随机人物…' : '正在搜图…';
  } else if (view.error) {
    hint.className = 'hint err';
    hint.textContent = view.error;
  } else if (view.items.length) {
    hint.className = 'hint';
    hint.textContent = isRank
      ? `榜单前 ${view.items.length} 名 · 点缩略图直接加入参考图`
      : isRandom
        ? `随机 ${view.items.length} 位人物 · 点缩略图直接加入参考图`
        : `找到 ${view.items.length} 张 · 点缩略图直接加入参考图`;
  } else {
    hint.className = 'hint';
    hint.textContent = isRank
      ? 'pixiv 官方日榜/周榜/月榜，每天中午更新，选一个榜单看看'
      : isRandom
        ? '点「随机二次元人物」，从全图库随机一批人物图'
        : '输入关键词搜人物图，点一下就能加进参考图做二创';
  }

  m$('#matSearchGo').disabled = view.busy;
  m$('#matRandomGo').disabled = view.busy;
  const more = m$('#matSearchMore');
  more.hidden = !view.items.length;
  more.disabled = view.busy;

  const grid = m$('#matSearchGrid');
  const sig = JSON.stringify([RM.search.tab, view.busy, view.items.map((i) => [keyOf(i), RM.sent[keyOf(i)] || ''])]);
  if (grid.dataset.sig === sig) return;
  grid.dataset.sig = sig;
  grid.textContent = '';

  if (view.busy) {
    const loading = document.createElement('p');
    loading.className = 'hint mat-empty';
    loading.textContent = isRank ? '正在取榜单…' : isRandom ? '正在随机人物…' : '正在搜图…';
    grid.appendChild(loading);
    return;
  }

  for (const item of view.items) {
    const key = keyOf(item);
    const sent = RM.sent[key];
    const hit = document.createElement('div');
    hit.className = `mat-hit${sent ? ' sent' : ''}`;
    hit.dataset.key = key;
    hit.title = [item.title, item.author, item.width && item.height ? `${item.width}×${item.height}` : '', ...(item.tags || [])]
      .filter(Boolean)
      .join(' · ');

    const img = document.createElement('img');
    img.src = item.thumb;
    img.alt = item.title || '';
    img.loading = 'lazy';
    // 送我们自己的来源页会被图站当盗链拦掉，干脆不带 Referer
    img.referrerPolicy = 'no-referrer';
    hit.appendChild(img);

    // 底部信息条：作品名 · 画师，随机模式一眼能认出是谁；完整标签悬停 title 里看
    const info = document.createElement('div');
    info.className = 'mat-hit-info';
    info.textContent = [item.title, item.author].filter(Boolean).join(' · ') || '未命名';
    hit.appendChild(info);

    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'mat-hit-add';
    add.dataset.act = 'mat-search-add';
    add.textContent = sent || '加入参考图';
    hit.appendChild(add);

    grid.appendChild(hit);
  }
}

/** 搜图 / 换一批：同一关键词再点一次「换一批」才翻页，换词或换源都从第一页开始 */
async function runSearch({ next = false } = {}) {
  if (RM.search.tab === 'rank') return runRanking({ next });
  if (RM.search.tab === 'random') return runRandom({ next });
  const q = String(m$('#matSearchInput').value || '').trim();
  if (!q) return mToast('先输入要搜的关键词', true);
  const source = m$('#matSearchSource').value || RM.search.source || '';
  if (next && RM.search.q === q && RM.search.source === source) RM.search.page += 1;
  else RM.search.page = 0;
  RM.search.q = q;
  RM.search.source = source;
  RM.search.busy = true;
  RM.search.error = '';
  RM.search.items = [];
  renderSearch();
  try {
    const out = await mApi(
      `/api/materials/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent(source)}&page=${RM.search.page}&limit=24`,
      { timeoutMs: 40000 },
    );
    RM.search.items = out.items || [];
    if (!RM.search.items.length) RM.search.error = '这次没搜到，点「换一批」再试试';
  } catch (err) {
    RM.search.error = err.message;
  } finally {
    RM.search.busy = false;
    renderSearch();
  }
}

/** 排行榜：换榜单从第一页开始，同一榜单点「换一批」才往后翻 */
async function runRanking({ next = false } = {}) {
  const mode = m$('#matRankMode').value || RM.rank.mode || 'day';
  if (next && RM.rank.mode === mode) RM.rank.page += 1;
  else RM.rank.page = 0;
  RM.rank.mode = mode;
  RM.rank.busy = true;
  RM.rank.error = '';
  RM.rank.items = [];
  renderSearch();
  try {
    const out = await mApi(
      `/api/materials/ranking?mode=${encodeURIComponent(mode)}&page=${RM.rank.page}&limit=24`,
      { timeoutMs: 40000 },
    );
    RM.rank.items = out.items || [];
    if (!RM.rank.items.length) RM.rank.error = '这个榜单这一页没有数据，点「换一批」或换个榜单';
  } catch (err) {
    RM.rank.error = err.message;
  } finally {
    RM.rank.busy = false;
    renderSearch();
  }
}

/** 随机二次元人物：不指定关键词，lolicon 全图库随机一批；「换一批」= 再随机一批 */
async function runRandom({ next = false } = {}) {
  RM.random.busy = true;
  RM.random.error = '';
  RM.random.items = [];
  renderSearch();
  try {
    const out = await mApi('/api/materials/random?limit=16', { timeoutMs: 40000 });
    RM.random.items = out.items || [];
    if (!RM.random.items.length) RM.random.error = '没取到，点「换一批」再试试';
  } catch (err) {
    RM.random.error = err.message;
  } finally {
    RM.random.busy = false;
    renderSearch();
  }
}

/* -------------------------------- 接口管理弹窗 -------------------------------- */

// lines 是 textarea，单独处理（其余字段名与接口字段一一对应）
const SOURCE_FIELDS = [
  'name',
  'kind',
  'mode',
  'count',
  'clip',
  'apiKey',
  'apiKeyHeader',
  'apiKeyPrefix',
  'headers',
  'itemsPath',
  'urlField',
  'extraParams',
];
const KEY_PLACEHOLDER = 'API Key（可选，填了自动加进请求头）';

/** 表单控件只能用 elements[name] 取：form.name 是表单自己的 name 属性，会把同名输入框遮住 */
const mField = (form, name) => form.elements[name];

/**
 * 表单按接口类型联动显隐：
 *  - 线路池模式：编号 + 一次拉几条有用，「列表路径 / 地址字段 / 额外参数」是普通接口专属
 *  - 普通接口：反过来，编号和条数没意义
 * lines 文本框两种模式都要（线路池里它是兜底备用线路）
 */
function syncSourceFormFields(form) {
  const mode = mField(form, 'mode').value;
  const m3u8 = mode === 'm3u8';
  const redirect = mode === 'redirect';
  // 条数：随机类 = 一批拉几条；列表类 = 每次从清单里抽几条（不再一次端出几百条）
  mField(form, 'count').hidden = false;
  // 编号：只有 m3u8 走全局线路池拼接
  mField(form, 'clip').hidden = !m3u8;
  for (const name of ['itemsPath', 'urlField', 'extraParams']) mField(form, name).hidden = m3u8 || redirect;
}

function resetSourceForm() {
  const form = m$('#materialSourceForm');
  form.reset();
  mField(form, 'kind').value = RM.kind;
  mField(form, 'mode').value = 'api';
  mField(form, 'count').value = 8;
  mField(form, 'clip').value = '';
  mField(form, 'lines').value = '';
  mField(form, 'apiKeyHeader').value = 'Authorization';
  mField(form, 'apiKeyPrefix').value = 'Bearer ';
  mField(form, 'apiKey').placeholder = KEY_PLACEHOLDER;
  syncSourceFormFields(form);
  RM.editing = '';
  form.querySelector('[type="submit"]').textContent = '添加分类';
}

function fillSourceForm(source) {
  const form = m$('#materialSourceForm');
  form.reset();
  for (const field of SOURCE_FIELDS) {
    if (field === 'apiKey') continue;
    mField(form, field).value = source[field] ?? '';
  }
  mField(form, 'mode').value = source.mode === 'm3u8' || source.mode === 'redirect' ? source.mode : 'api';
  mField(form, 'count').value = source.count || 8;
  // 只回填分类自己配的线路：全局池拼出来的那条不该被写进它自己的 lines
  mField(form, 'lines').value = linesOf(source).join('\n');
  // Key 只回显掩码，编辑时留空 = 不修改，避免把掩码写回去
  mField(form, 'apiKey').value = '';
  mField(form, 'apiKey').placeholder = source.hasKey
    ? `已保存（${source.apiKey}），留空则不修改`
    : KEY_PLACEHOLDER;
  syncSourceFormFields(form);
  RM.editing = source.id;
  form.querySelector('[type="submit"]').textContent = '保存修改';
}

function renderSourceList() {
  const wrap = m$('#materialSourceList');
  wrap.textContent = '';
  if (!RM.sources.length) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '还没有素材分类，在上面加一个（名称 + 类型 + 线路地址）。';
    wrap.appendChild(hint);
    return;
  }
  for (const source of RM.sources) {
    const row = document.createElement('div');
    row.className = 'mat-source-row';
    row.dataset.sourceId = source.id;

    const main = document.createElement('div');
    main.className = 'mat-source-main';
    const name = document.createElement('div');
    name.className = 'mat-source-name';
    name.textContent = source.name;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = KIND_LABEL[source.kind] || source.kind;
    name.appendChild(badge);
    if (source.mode === 'm3u8') {
      const mode = document.createElement('span');
      mode.className = 'badge';
      mode.textContent = '线路池 m3u8';
      mode.title = '每个线路请求一次返回一段随机短片，下载会合并分片';
      name.appendChild(mode);
    }
    if (source.mode === 'redirect') {
      const mode = document.createElement('span');
      mode.className = 'badge';
      mode.textContent = '随机直链';
      mode.title = '请求后 302 跳到一段随机 mp4 / 一张随机图片，每刷新一次都是新内容';
      name.appendChild(mode);
    }
    if (source.mode === 'douyin') {
      const mode = document.createElement('span');
      mode.className = 'badge';
      mode.textContent = '内置采集';
      mode.title = '桌面版内置浏览器采集：打开抖音窗口登录一次，之后填关键词或贴链接就能拉视频';
      name.appendChild(mode);
    }
    if (source.hasKey) {
      const key = document.createElement('span');
      key.className = 'badge';
      key.textContent = '有 Key';
      name.appendChild(key);
    }
    main.appendChild(name);

    const lines = effectiveLinesOf(source);
    const url = document.createElement('div');
    url.className = 'hint mat-source-url';
    const ownCount = linesOf(source).length;
    if (source.mode === 'douyin') {
      url.textContent = '内置浏览器采集：打开抖音窗口登录一次，之后填关键词或贴链接就能拉视频（不占线路）';
    } else {
      url.textContent = lines.length
        ? `${lines.length} 条线路：${lines.join(' · ')}${
            source.mode === 'm3u8' && lines.length > ownCount ? `（其中 ${lines.length - ownCount} 条由全局池 × 编号 ${source.clip} 拼出）` : ''
          }`
        : '（未填线路：全局池 + 编号，或自己填完整地址）';
      if (source.lastGoodLine) url.textContent += `　｜　上次可用：${hostOf(source.lastGoodLine)}`;
    }
    url.title = lines.join('\n');
    main.appendChild(url);
    row.appendChild(main);

    // 内置采集分类不用配地址，也就没有「编辑 / 删除」——留着反而容易被误删
    if (source.mode === 'douyin') {
      wrap.appendChild(row);
      continue;
    }

    const edit = document.createElement('button');
    edit.className = 'btn tiny';
    edit.dataset.act = 'mat-source-edit';
    edit.textContent = '编辑';
    row.appendChild(edit);

    const del = document.createElement('button');
    del.className = 'btn tiny danger';
    del.dataset.act = 'mat-source-delete';
    del.textContent = '删除';
    row.appendChild(del);

    wrap.appendChild(row);
  }
}

function openManager() {
  resetSourceForm();
  renderLinePool();
  m$('#modalMaterialSources').hidden = false;
  renderSourceList();
}

/* -------------------------------- 全局线路池 -------------------------------- */

/** 把 RM.linePool 回填进 textarea（弹窗打开时）；用户没改就不覆盖正在编辑的内容 */
function renderLinePool() {
  const box = m$('#matLinePool');
  box.value = RM.linePool.join('\n');
  m$('#matPoolHint').textContent = RM.linePool.length
    ? `已配 ${RM.linePool.length} 条基地址`
    : '还没配：线路池分类要靠这里拼地址';
}

async function saveLinePool() {
  const hint = m$('#matPoolHint');
  try {
    const out = await mApi('/api/material-line-pool', {
      method: 'PUT',
      body: { lines: String(m$('#matLinePool').value || '') },
    });
    RM.linePool = out.lines || [];
    hint.textContent = RM.linePool.length ? `已保存 ${RM.linePool.length} 条基地址` : '已清空线路池';
    mToast('已保存全局线路池，所有线路池分类立刻生效');
    await mUtil().refresh();
    renderSourceList();
    render();
  } catch (err) {
    hint.textContent = err.message;
    mToast(err.message, true);
  }
}

async function submitSourceForm(event) {
  event.preventDefault();
  const form = event.target;
  const body = {};
  for (const field of SOURCE_FIELDS) body[field] = String(mField(form, field).value ?? '');
  // 线路池是 textarea：一行一条，交后端切分/去重
  body.lines = String(mField(form, 'lines').value || '');
  // 普通接口模式下编号没意义：清掉，免得切回线路池时残留旧编号
  if (body.mode !== 'm3u8') body.clip = '';
  // 编辑时 Key 留空 = 保持原样
  if (!body.apiKey.trim()) delete body.apiKey;
  if (!body.name.trim()) return mToast('请填写分类名称', true);
  try {
    if (RM.editing) {
      await mApi(`/api/material-sources/${RM.editing}`, { method: 'PATCH', body });
      mToast('已保存分类');
    } else {
      const created = await mApi('/api/material-sources', { method: 'POST', body });
      mToast(`已添加分类「${created.name}」`);
      // 新建的是当前类型的分类就顺手选中它，省得用户再去下拉里找
      if (created.kind === RM.kind) {
        RM.sourceId = created.id;
        RM.loadedFor = '';
      }
    }
    resetSourceForm();
    await mUtil().refresh();
    renderSourceList();
    // 配置变了立刻重拉一次，省得等 3 分钟缓存过期
    await loadItems({ refresh: true });
  } catch (err) {
    mToast(err.message, true);
  }
}

/* -------------------------------- 事件绑定 -------------------------------- */

m$('#matSubtabs').addEventListener('click', (event) => {
  const tab = event.target.closest('.mat-subtab');
  if (!tab || tab.dataset.kind === RM.kind) return;
  RM.kind = tab.dataset.kind;
  RM.items = [];
  RM.error = '';
  RM.line = '';
  RM.sent = {};
  render();
  loadItems();
});

m$('#matSource').addEventListener('change', (event) => {
  RM.sourceId = event.target.value;
  RM.items = [];
  RM.error = '';
  RM.line = '';
  RM.loadedFor = '';
  RM.dyDone = 0;
  render();
  loadItems();
});

m$('#matReload').addEventListener('click', () => loadItems({ refresh: true }));
// 内嵌抖音：换目标画布参数时刷新一下提示
m$('#matDyTarget').addEventListener('change', renderDyHint);
// 抖音工具栏：这三个按钮在本页面里，不靠抖音 DOM，点了一定有反应
m$('#matDyAdd').addEventListener('click', () => onDyAddCurrent());
m$('#matDyShot').addEventListener('click', () => onDyShot());
m$('#matDyCopy').addEventListener('click', () => onDyCopy());
// 抖音登录被风控拦死时的正路：开真实 Chrome 登一次，登录态自动搬进来
m$('#matDyChromeLogin').addEventListener('click', () => onDyChromeLogin());
// 放大 / 还原：小窗口下留给网页的地方太少，铺满整屏看着舒服
m$('#matDyBig').addEventListener('click', () => setDyBig(!dyBig));
// 页面缩放：抖音是竖屏站，宽窗口里内容只占中间一条，靠这个放大看
m$('#matDyZoomIn').addEventListener('click', () => applyDyZoom(dyZoom + DY_ZOOM_STEP));
m$('#matDyZoomOut').addEventListener('click', () => applyDyZoom(dyZoom - DY_ZOOM_STEP));
m$('#matDyZoomVal').addEventListener('click', () => applyDyZoom(1));
// Ctrl + 滚轮缩放：这是浏览器的通用习惯，不给会很不顺手
m$('#matWeb').addEventListener(
  'wheel',
  (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    applyDyZoom(dyZoom + (event.deltaY < 0 ? DY_ZOOM_STEP : -DY_ZOOM_STEP));
  },
  { passive: false },
);
m$('#matPrev').addEventListener('click', () => stepItem(-1));
m$('#matNext').addEventListener('click', () => stepItem(1));
// 键盘 ← / → 也是「上一条 / 下一条」：在素材模块里随时能翻，不用去够箭头
document.addEventListener('keydown', (event) => {
  if (document.body.dataset.module !== 'material') return;
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  // Esc：放大模式下还原。铺满屏幕看东西时按 Esc 是本能动作，
  // 必须给个出口 —— 而且要在下面「翻页」那条之前处理，别被它拦掉。
  if (event.key === 'Escape' && dyBig) {
    event.preventDefault();
    setDyBig(false);
    return;
  }
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
  const tag = (event.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || event.target?.isContentEditable) return;
  if (document.querySelector('.modal:not([hidden])')) return;
  event.preventDefault();
  stepItem(event.key === 'ArrowLeft' ? -1 : 1);
});
m$('#matManage').addEventListener('click', openManager);

m$('#materialSourceForm').addEventListener('submit', submitSourceForm);
m$('#materialSourceForm').addEventListener('change', (event) => {
  if (event.target.matches('select[name="mode"]')) syncSourceFormFields(event.target.form);
});
m$('#matPoolSave').addEventListener('click', () => saveLinePool());

m$('#matGrid').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const card = button.closest('.mat-card');
  const item = RM.items.find((i) => keyOf(i) === card?.dataset.key);
  if (!item) return;
  try {
    if (button.dataset.act === 'mat-download') {
      button.disabled = true;
      try {
        await downloadItem(item);
      } finally {
        button.disabled = false;
      }
    } else if (button.dataset.act === 'mat-to-canvas') {
      await toCanvas(item, card, button);
    } else if (button.dataset.act === 'mat-to-ref') {
      button.disabled = true;
      try {
        await toImageRef(item);
      } finally {
        button.disabled = false;
      }
    } else if (button.dataset.act === 'mat-capture') {
      button.disabled = true;
      try {
        await captureFrame(item, card);
      } finally {
        button.disabled = false;
      }
    }
  } catch (err) {
    // 随机源里偶尔有「列表时还活着、点下去已经没了」的片子（远端 404）。
    // 这种情况自动跳到下一条，省得用户自己再点一次。
    const stale = /已失效|HTTP\s*40\d|下载失败/.test(err.message);
    if (stale && RM.items.length > 1) {
      mToast(`${err.message} · 已自动跳到下一条`, true);
      stepItem(1);
    } else {
      mToast(err.message, true);
    }
  }
});

m$('#matRandomGo').addEventListener('click', () => runRandom());
m$('#matSearchGo').addEventListener('click', () => runSearch());
m$('#matSearchMore').addEventListener('click', () => runSearch({ next: true }));
m$('#matSearchInput').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  runSearch();
});
m$('#matSearchSource').addEventListener('change', (event) => {
  // 换图源不自动重搜：换个源又要等一次请求，等用户点「搜索」再发
  RM.search.source = event.target.value;
});
// 「关键词搜索 / 排行榜」切换：切到排行榜且还没拉过时自动取一次，省得用户再点一下
m$('#matSearchTabs').addEventListener('click', (event) => {
  const tab = event.target.closest('.mat-search-tab');
  if (!tab) return;
  RM.search.tab = tab.dataset.tab; // 'random' | 'keyword' | 'rank'
  renderSearch();
  // 排行榜 / 随机：切过去且还没数据时自动取一次，省得再点一下
  if (RM.search.tab === 'rank' && !RM.rank.items.length && !RM.rank.busy) runRanking();
  if (RM.search.tab === 'random' && !RM.random.items.length && !RM.random.busy) runRandom();
});
m$('#matRankMode').addEventListener('change', (event) => {
  RM.rank.mode = event.target.value;
  runRanking(); // 换榜单立刻重新取，榜单之间没有「再点一次」的说法
});
// 缩略图点一下（点图或点按钮都算）= 加入参考图
m$('#matSearchGrid').addEventListener('click', async (event) => {
  const hit = event.target.closest('.mat-hit');
  if (!hit) return;
  const item = activeSearchList().find((i) => keyOf(i) === hit.dataset.key);
  if (!item || RM.sent[keyOf(item)]) return;
  try {
    await toImageRef(item);
    mToast('已加入参考图，切到图片生成就能二创');
  } catch (err) {
    mToast(err.message, true);
  }
});

m$('#modalMaterialSources').addEventListener('click', async (event) => {
  if (event.target.id === 'modalMaterialSources' || event.target.closest('[data-close]')) {
    m$('#modalMaterialSources').hidden = true;
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const row = button.closest('.mat-source-row');
  const source = RM.sources.find((s) => s.id === row?.dataset.sourceId);
  if (!source) return;
  if (button.dataset.act === 'mat-source-edit') {
    fillSourceForm(source);
  } else if (button.dataset.act === 'mat-source-delete') {
    if (!(await confirmDialog(`删除分类「${source.name}」？只删配置，已下载到本机文件不受影响。`))) return;
    try {
      await mApi(`/api/material-sources/${source.id}`, { method: 'DELETE' });
      if (RM.editing === source.id) resetSourceForm();
      if (RM.sourceId === source.id) {
        RM.sourceId = '';
        RM.items = [];
        RM.loadedFor = '';
      }
      await mUtil().refresh();
      renderSourceList();
      render();
      mToast('已删除分类');
    } catch (err) {
      mToast(err.message, true);
    }
  }
});

/* -------------------------------- 对外接口 -------------------------------- */

window.RHMaterial = {
  /** app.js 的 refresh() 每次喂 /api/state 过来：只同步分类配置，不发任何远端请求 */
  sync(state) {
    RM.sources = state.materialSources || [];
    RM.linePool = state.materialLinePool || [];
    RM.searchSources = state.imageSearchSources || [];
    RM.rankModes = state.imageRankModes || [];
    render();
  },

  /** 切到素材模块时调一次：有分类但还没拉过列表就自动拉一次 */
  onShow() {
    render();
    const source = currentSource();
    // 内嵌抖音不用拉列表：把网页建出来 / 亮出来就行
    if (source?.mode === 'douyin') {
      ensureDyView();
      return;
    }
    // 切到非抖音分类时把放大状态收回（放大时顶栏是藏起来的，
    // 留在别的分类上会看着像界面坏了）
    resetDyBig();
    if (source && RM.loadedFor !== `${RM.kind}:${source.id}`) loadItems();
  },

  /**
   * 切走素材模块：把后台还在跑的东西收掉。
   * 只靠 CSS 隐藏的话，抖音观察的 2 秒轮询和 hls 的分片下载都会继续跑，
   * 内存和带宽只涨不落。回来时 onShow 会按需重新拉起来。
   */
  onHide() {
    if (dyWatchTimer) {
      clearInterval(dyWatchTimer);
      dyWatchTimer = null;
    }
    releaseHls();
  },

  openManager,
};

})();