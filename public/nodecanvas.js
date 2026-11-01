/**
 * 节点画布 + 本地素材库（独立模块）
 *
 * 数据只在服务端存一份：db.nodeCanvases / db.nodeAssets。
 * 画布上的 AI 生图、AI 视频不另建任务队列，直接调现有的
 * /api/image-tasks、/api/video-tasks，结果回填到节点里。
 *
 * 对外只暴露 window.RHNode = { sync(state), onShow() }，
 * 由 app.js 的状态轮询和模块切换调用。
 */
(() => {
  const util = () => window.RHUtil || {};
  const api = (p, opts) => util().api(p, opts);
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const toast = (msg, isError) => util().toast?.(msg, isError);
  const esc = (value) => util().esc?.(value) ?? String(value ?? '');
  const uid = (prefix) => `${prefix}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;

  const NODE_LABEL = {
    text: '文本',
    image: '图片',
    video: '视频',
    audio: '音频',
    imageGen: 'AI 生图',
    videoGen: 'AI 视频',
  };
  /**
   * 排版基准（对齐 MiniMax Design）：
   * - 卡片是「缩略图态」：画布上只看得到缩略图 + 类型标签，
   *   编辑（模型/提示词/参数）统一收进点选后底部滑出的编辑面板
   * - 所有卡片宽度收敛到同一个 CARD_W，画布水平节奏 = CARD_W + LAYOUT_GAP
   * - 高度按缩略图比例走，不追求等高
   */
  const CARD_W = 210;
  const LAYOUT_GAP = 100;
  /** 卡片头高度（类型标签行），缩略图高度 = 节点高 - HEAD_H */
  const HEAD_H = 26;
  /** 缩略图区的高度档位：文本/占位用小高，媒体按比例并夹在 [THUMB_MIN, THUMB_MAX] */
  const THUMB_MIN = 72;
  const THUMB_MAX = 320;
  const THUMB_DEFAULT = 118;
  const NODE_SIZE = {
    text: [CARD_W, HEAD_H + 110],
    image: [CARD_W, HEAD_H + THUMB_DEFAULT],
    video: [CARD_W, HEAD_H + THUMB_DEFAULT],
    audio: [CARD_W, HEAD_H + 84],
    imageGen: [CARD_W, HEAD_H + THUMB_DEFAULT],
    videoGen: [CARD_W, HEAD_H + THUMB_DEFAULT],
  };
  const ZOOM_MIN = 0.1;
  const ZOOM_MAX = 4;
  const ZOOM_STEP = 1.2;
  /** 端口圆心的纵向位置：卡片头 26px 的中线（与 .nc-port 的 top:13px + 7px 半径一致） */
  const PORT_Y = 20;

  /** 连线类型约束：无意义的素材流在交互层就堵死（Table 这类纯数据卡不给目标） */
  const ALLOWED_TARGET_TYPES = {
    text: ['text', 'audio', 'video', 'image', 'imageGen', 'videoGen'],
    image: ['image', 'video', 'text', 'imageGen', 'videoGen'],
    imageGen: ['image', 'video', 'text', 'imageGen', 'videoGen'],
    video: ['video', 'text', 'audio', 'videoGen'],
    videoGen: ['video', 'text', 'audio'],
    audio: ['text', 'video', 'audio'],
  };

  const PENDING = new Set(['queued', 'running', 'polling', 'preparing']);
  const STATUS_LABEL = {
    queued: '排队中',
    running: '生成中',
    polling: '生成中',
    preparing: '准备中',
    success: '已完成',
    failed: '失败',
    cancelled: '已取消',
    canceled: '已取消',
    idle: '',
  };

  const S = {
    inited: false,
    graphs: [],
    assets: [],
    imageModels: [],
    videoModels: [],
    videoPresets: {},
    imageDefaults: {},
    imageTasks: [],
    videoTasks: [],
    currentId: '',
    graph: null,
    selected: new Set(),
    selectedEdge: '',
    selectedEdges: [],
    assetView: 'all', // all | favorite | trash
    assetKind: 'all', // all | image | video | audio
    dirty: false,
    saving: false,
    saveTimer: null,
    modelSig: '',
    creating: false,
    // 是否已经从 /api/state 拿到过数据：页面刚打开、模块又是上次停留的「节点画布」时，
    // onShow 会先于第一次状态刷新执行，这时不能误判成「没有画布」去新建一张
    stateSeen: false,
    panning: false,
    connecting: null,
    // 底部编辑面板：当前打开面板的节点 id（'' = 收起）
    panelNode: '',
    // 参数/模型选择浮层（挂在 body 上的 fixed 元素）
    picker: null,
    // AI 编导：styles/info 从服务端取一次就缓存；agentRun 管批量生成的并发状态
    director: { styles: null, info: null },
    // 画布内的 AI 聊天窗：对话按画布存进 db（chats[] + chatId），内存里的是当前这段
    chat: { chatId: null, messages: [], busy: false, loaded: false },
    agentRun: null,
    runHidden: false,
    // 体验增强
    gridOn: localStorage.getItem('nc.grid') === '1',
    // 画布皮肤：底色（9 档，写在 html[data-skin]）+ 底纹（dots/grid/none）
    skin: localStorage.getItem('nc.skin') || 'default',
    pattern: localStorage.getItem('nc.pattern') || (localStorage.getItem('nc.grid') === '1' ? 'grid' : 'dots'),
    marquee: null,
    guides: null,
    menu: null,
    // 撤销 / 重做：每次改动前压一份 nodes+edges 的快照
    history: { undo: [], redo: [], limit: 50, silent: false },
    // 交互增强（对齐 MiniMax Design 的画布）
    snapOn: localStorage.getItem('nc.snap') !== '0',
    edgesMode: localStorage.getItem('nc.edgesMode') || 'all', // all | sel | none
    minimapOn: localStorage.getItem('nc.minimap') === '1',
    panMode: false,
    cutMode: false,
    // 按住空格 = 临时平移（MiniMax 的画布手势），松开恢复
    spaceDown: false,
    find: { open: false, ids: [], index: -1, kw: '' },
    clipboard: null,
    minimap: null,
    cmd: null,
  };

  const nodeById = (id) => (S.graph?.nodes || []).find((n) => n.id === id);

  /* ------------------------------ 视图（平移/缩放） ----------------------------- */

  function applyViewport() {
    const vp = S.graph?.viewport || { x: 80, y: 80, zoom: 1 };
    const world = $('#ncWorld');
    if (world) world.style.transform = `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})`;
    // 选中框/连线用这个变量做「缩放反补偿」：无论放大到 4 倍还是缩到 0.1 倍，
    // 屏幕上的描边恒为 1.5px，不会放大变粗、缩小看不见。
    $('#moduleNode')?.style.setProperty('--nc-zoom', String(vp.zoom));
    const zoomEl = $('#ncZoomLevel');
    if (zoomEl) zoomEl.textContent = `${Math.round(vp.zoom * 100)}%`;
    syncGrid();
    drawMinimap();
    // 浮动工具条跟着选区走：视口一动，它在屏幕上的位置就变了
    updateSelBar();
  }

  /** 网格贴图跟着视口走：位移 = 视口偏移，格子大小 = 基准间距 * 缩放 */
  function syncGrid() {
    const grid = $('.nc-grid');
    if (!grid) return;
    const vp = S.graph?.viewport || { x: 0, y: 0, zoom: 1 };
    const base = GRID_BASE[S.pattern] || GRID_BASE.dots;
    // 缩小时间距跟着变小，会挤成一片噪点（点阵尤其明显）；这里按 2 的幂放大基准间距，
    // 让屏幕上的间距恒定落在 10~24px，任何缩放级别下都能看清、又不会抢内容。
    let step = base * vp.zoom;
    while (step < 10) step *= 2;
    while (step > 24) step /= 2;
    grid.style.backgroundSize = `${step}px ${step}px`;
    grid.style.backgroundPosition = `${vp.x}px ${vp.y}px`;
    // flow 是纯氛围层（.nc-flow 自己画），网格必须让位；none 同样不画。
    grid.style.opacity = S.pattern === 'none' || S.pattern === 'flow' ? '0' : '1';
  }

  /** 网格开关 = 底纹显隐。图案种类归皮肤面板管，这里只负责「有 / 无」。
   *  关掉再打开要回到之前那种图案（用户可能用的是流光，不能一按 G 就退回点阵）。 */
  function setGridOn(on) {
    const last = S.pattern !== 'none' ? S.pattern : (localStorage.getItem('nc.lastPattern') || 'dots');
    const key = PATTERNS.some((p) => p.key === last) && last !== 'none' ? last : 'dots';
    setPattern(on ? key : 'none', true);
  }

  /* ------------------------------ 画布皮肤 ------------------------------ */

  /** 9 档底色，色值直接取自 MiniMax 的 --canvas-bg-*；key 用作 html[data-skin] */
  const TONES = [
    { key: 'default', label: '默认', color: '#fafafa' },
    { key: 'warm', label: '暖灰', color: '#f3f0ea' },
    { key: 'cool', label: '冷灰', color: '#eef2f5' },
    { key: 'paper', label: '纸感', color: '#f6f2e9' },
    { key: 'sage', label: '鼠尾草', color: '#eef3ed' },
    { key: 'mist', label: '雾蓝', color: '#edf3f7' },
    { key: 'lavender', label: '淡紫', color: '#f3eff8' },
    { key: 'blush', label: '藕粉', color: '#f7f0f0' },
    { key: 'sand', label: '沙色', color: '#f3ebdd' },
  ];
  const PATTERNS = [
    { key: 'dots', label: '点阵' },
    { key: 'grid', label: '线格' },
    { key: 'flow', label: '流光' },
    { key: 'none', label: '无' },
  ];
  /** 底纹基准间距：dots 10px / grid 12px（MiniMax 的预览就是这两档）。
   *  flow 是纯氛围层，没有网格间距，用 12 占位不参与贴图计算。 */
  const GRID_BASE = { dots: 10, grid: 12, flow: 12, none: 12 };

  /** 把皮肤写到 DOM 上：底色去 html[data-skin]，底纹去 #moduleNode[data-pattern]。
   *  这个函数只做「写属性 + 重算贴图」，不再回调 setSkin/setPattern（否则互相递归）。 */
  function applySkin() {
    const html = document.documentElement;
    if (S.skin && S.skin !== 'default') html.dataset.skin = S.skin;
    else delete html.dataset.skin;
    const mod = $('#moduleNode');
    if (mod) mod.dataset.pattern = S.pattern;
    syncGrid();
  }

  function setSkin(key, silent) {
    if (!TONES.some((t) => t.key === key)) key = 'default';
    S.skin = key;
    localStorage.setItem('nc.skin', key);
    applySkin();
    renderSkinPanel();
    if (!silent) toast(`底色：${TONES.find((t) => t.key === key).label}`);
  }

  function setPattern(key, silent) {
    if (!PATTERNS.some((p) => p.key === key)) key = 'dots';
    S.pattern = key;
    // 图案是唯一真相：选「无」就等于关掉底纹，选点阵/线格/流光就等于打开。
    S.gridOn = key !== 'none';
    localStorage.setItem('nc.pattern', key);
    localStorage.setItem('nc.grid', S.gridOn ? '1' : '0');
    // 记住最后一次真正生效的图案，G 键重新打开底纹时回到它（而不是永远退回点阵）
    if (key !== 'none') localStorage.setItem('nc.lastPattern', key);
    applySkin();
    renderSkinPanel();
    if (!silent) toast(`底纹：${PATTERNS.find((p) => p.key === key).label}`);
  }

  function renderSkinPanel() {
    // 夜间模式下画布纸面由主题接管：html[data-theme="dark"] 和 html[data-skin] 同特异性，
    // 主题那块写在后面，所以这 9 档近白色点了不会有任何变化 —— 说明一句，别让人白点。
    const dark = document.documentElement.dataset.theme === 'dark';
    const note = $('#ncSkinNote');
    if (note) {
      note.hidden = !dark;
      note.textContent = dark
        ? '夜间模式下画布底色固定为深色，这 9 档不生效。要换回浅色纸面：左下角「设置 · 外观 → 浅色」。'
        : '';
    }
    const tones = $('#ncSkinTones');
    if (tones) {
      tones.innerHTML = '';
      for (const tone of TONES) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `nc-skin-tone${tone.key === S.skin ? ' on' : ''}`;
        btn.style.setProperty('--tone', tone.color);
        btn.textContent = tone.label;
        btn.dataset.tone = tone.key;
        btn.disabled = dark;
        btn.addEventListener('click', () => setSkin(tone.key));
        tones.appendChild(btn);
      }
    }
    const pats = $('#ncSkinPatterns');
    if (pats) {
      pats.innerHTML = '';
      for (const pat of PATTERNS) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = pat.key === S.pattern ? 'on' : '';
        btn.textContent = pat.label;
        btn.addEventListener('click', () => setPattern(pat.key));
        pats.appendChild(btn);
      }
    }
  }

  function openSkinPanel() {
    const el = $('#ncSkin');
    if (!el) return;
    closeCmd?.();
    closeHelp?.();
    closeFind?.();
    renderSkinPanel();
    el.hidden = false;
  }

  function closeSkinPanel() {
    const el = $('#ncSkin');
    if (el) el.hidden = true;
  }

  function isSkinOpen() {
    return !$('#ncSkin')?.hidden;
  }

  function viewportSize() {
    const vp = $('#ncViewport');
    return { w: vp?.clientWidth || 800, h: vp?.clientHeight || 600 };
  }

  function screenToWorld(clientX, clientY) {
    const rect = $('#ncViewport').getBoundingClientRect();
    const vp = S.graph.viewport;
    return {
      x: (clientX - rect.left - vp.x) / vp.zoom,
      y: (clientY - rect.top - vp.y) / vp.zoom,
    };
  }

  function centerWorld() {
    const { w, h } = viewportSize();
    const vp = S.graph.viewport;
    return { x: (w / 2 - vp.x) / vp.zoom, y: (h / 2 - vp.y) / vp.zoom };
  }

  function fitView() {
    const nodes = S.graph?.nodes || [];
    if (!nodes.length) {
      S.graph.viewport = { x: 80, y: 80, zoom: 1 };
    } else {
      const minX = Math.min(...nodes.map((n) => n.x));
      const minY = Math.min(...nodes.map((n) => n.y));
      const maxX = Math.max(...nodes.map((n) => n.x + n.w));
      const maxY = Math.max(...nodes.map((n) => n.y + n.h));
      const { w, h } = viewportSize();
      // 初次适配夹在 0.2 ~ 1：宁可留白，也不让节点怼满屏
      const zoom = Math.max(0.2, Math.min(1, (w - 80) / Math.max(1, maxX - minX), (h - 80) / Math.max(1, maxY - minY)));
      S.graph.viewport = {
        zoom,
        x: (w - (maxX - minX) * zoom) / 2 - minX * zoom,
        y: (h - (maxY - minY) * zoom) / 2 - minY * zoom,
      };
    }
    applyViewport();
    markDirty();
  }

  function zoomAt(clientX, clientY, delta) {
    const vp = S.graph.viewport;
    const rect = $('#ncViewport').getBoundingClientRect();
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    const wx = (mx - vp.x) / vp.zoom;
    const wy = (my - vp.y) / vp.zoom;
    const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, vp.zoom * (delta > 0 ? 1 / ZOOM_STEP : ZOOM_STEP)));
    vp.zoom = next;
    vp.x = mx - wx * next;
    vp.y = my - wy * next;
    applyViewport();
    markDirty();
  }

  /** 以视口中心为基准步进缩放 */
  function zoomStep(factor) {
    if (!S.graph) return;
    const { w, h } = viewportSize();
    const vp = S.graph.viewport;
    const cx = w / 2, cy = h / 2;
    const wx = (cx - vp.x) / vp.zoom, wy = (cy - vp.y) / vp.zoom;
    vp.zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, vp.zoom * factor));
    vp.x = cx - wx * vp.zoom;
    vp.y = cy - wy * vp.zoom;
    applyViewport();
    markDirty();
  }
  function zoomReset() {
    if (!S.graph) return;
    const { w, h } = viewportSize();
    const vp = S.graph.viewport;
    const cx = w / 2, cy = h / 2;
    const wx = (cx - vp.x) / vp.zoom, wy = (cy - vp.y) / vp.zoom;
    vp.zoom = 1;
    vp.x = cx - wx;
    vp.y = cy - wy;
    applyViewport();
    markDirty();
  }

  /* ------------------------------ 撤销 / 重做 ------------------------------- */

  function histSnapshot() {
    if (!S.graph) return '';
    return JSON.stringify({ nodes: S.graph.nodes, edges: S.graph.edges });
  }

  function pushHistory() {
    if (!S.graph || S.history.silent) return;
    const snap = histSnapshot();
    if (!snap) return;
    const h = S.history;
    if (h.undo[h.undo.length - 1] === snap) return; // 相同不重复
    h.undo.push(snap);
    if (h.undo.length > h.limit) h.undo.shift();
    h.redo.length = 0;
    refreshHistoryButtons();
  }

  function restoreSnapshot(snap) {
    if (!S.graph || !snap) return;
    try {
      const data = JSON.parse(snap);
      S.history.silent = true;
      S.graph.nodes = data.nodes || [];
      S.graph.edges = data.edges || [];
      // 只保留仍存在的选中
      S.selected = new Set([...S.selected].filter((id) => nodeById(id)));
      if (S.selectedEdge && !S.graph.edges.some((e) => e.id === S.selectedEdge)) S.selectedEdge = '';
      renderNodes();
      renderEdges();
      // 面板里开着的节点可能被 undo 改回了旧值：强制重建面板表单
      //（syncPanel 对同节点有短路保护，这里绕开它）
      if (S.panelNode) {
        const pn = nodeById(S.panelNode);
        S.panelNode = '';
        if (pn) openPanel(pn);
      }
      markDirty();
      S.history.silent = false;
    } catch (err) {
      /* ignore corrupt snapshot */
    }
  }

  function undo() {
    const h = S.history;
    if (!S.graph || !h.undo.length) return;
    const cur = histSnapshot();
    h.redo.push(cur);
    let snap = h.undo.pop();
    // 跳过和当前完全相同的快照（避免肉眼看不到变化）
    while (snap === cur && h.undo.length) snap = h.undo.pop();
    if (snap === cur) {
      h.redo.push(snap);
      refreshHistoryButtons();
      return;
    }
    restoreSnapshot(snap);
    refreshHistoryButtons();
  }

  function redo() {
    const h = S.history;
    if (!S.graph || !h.redo.length) return;
    const cur = histSnapshot();
    h.undo.push(cur);
    let snap = h.redo.pop();
    while (snap === cur && h.redo.length) snap = h.redo.pop();
    if (snap === cur) {
      h.undo.push(snap);
      refreshHistoryButtons();
      return;
    }
    restoreSnapshot(snap);
    refreshHistoryButtons();
  }

  function refreshHistoryButtons() {
    const u = $('#ncUndo'), r = $('#ncRedo');
    if (u) u.disabled = !S.history.undo.length;
    if (r) r.disabled = !S.history.redo.length;
  }

  /* -------------------------------- 服务端读写 -------------------------------- */

  /**
   * 每次改动的序号。saveNow 发出请求前记一份，回来时比对：
   * 不一样就说明「保存期间又有新改动」，那这一轮的 dirty 不能清 ——
   * 否则最后一次编辑会被当成已保存，内容永久丢失（界面还谎报「已保存」）。
   */
  let dirtySeq = 0;

  /**
   * 连续保存失败的次数，成功一次就归零。
   * 之前失败后固定 300ms 重试，形成自激：磁盘写满这类持久错误会每 0.3 秒打一次全量 PATCH，
   * 还不停刷新提示条的时间戳，把真实故障盖住。改成逐次退避到最长 8 秒。
   */
  let saveFailures = 0;
  const SAVE_RETRY_DELAYS = [1000, 3000, 8000];

  /**
   * 本地刚建 / 刚导入、但服务端 state 快照还没带回来的画布 id。
   * 用来挡住「新画布被旧快照判成不存在、把用户踢回第一张」的竞态。
   */
  const justCreated = new Set();

  function markDirty() {
    S.dirty = true;
    dirtySeq += 1;
    setSaveHint('未保存');
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(saveNow, 600);
  }

  function setSaveHint(text, isError) {
    const el = $('#ncSaveHint');
    if (el) {
      el.textContent = text;
      el.classList.toggle('err', Boolean(isError));
    }
    // 左上角状态条跟工具条里的保存提示是同一份信息，但状态条上要能一眼看出
    // 「保存中 / 已保存 / 失败」，所以这里按文字归类上色
    const bar = $('#ncStatusSave');
    if (bar) {
      bar.textContent = text;
      bar.classList.toggle('saving', !isError && /保存中|未保存/.test(text));
      bar.classList.toggle('saved', !isError && /已保存/.test(text));
      bar.classList.toggle('err', Boolean(isError));
    }
  }

  /** 画布状态条：画布名 · 节点数 · 保存态 */
  function updateStatusBar() {
    const name = $('#ncStatusName');
    if (name) name.textContent = S.graph?.name || '画布';
    const count = $('#ncStatusCount');
    if (!count) return;
    const nodes = S.graph?.nodes?.length || 0;
    const edges = S.graph?.edges?.length || 0;
    const text = edges ? `${nodes} 节点 · ${edges} 连线` : `${nodes} 节点`;
    // renderEdges 在拖拽的每条 mousemove 都会跑到，这里用签名挡掉无意义的写入
    if (count.dataset.sig === text) return;
    count.dataset.sig = text;
    count.textContent = text;
  }

  async function saveNow() {
    clearTimeout(S.saveTimer);
    if (!S.graph || !S.dirty) return;
    // 上一次保存还没回来：等它结束再存，不能把这次改动丢掉
    if (S.saving) {
      S.saveTimer = setTimeout(saveNow, 300);
      return;
    }
    S.saving = true;
    setSaveHint('保存中…');
    const graph = S.graph;
    // 发出请求前的改动序号：回来时如果它变了，说明这轮保存期间用户又编辑过
    const seqAtSend = dirtySeq;
    try {
      await api(`/api/node-canvases/${graph.id}`, {
        method: 'PATCH',
        body: {
          name: graph.name,
          nodes: graph.nodes,
          edges: graph.edges,
          viewport: graph.viewport,
        },
      });
      // 保存期间又有改动：保持 dirty，下一轮再存（dirtySeq 变了就不能清）
      if (S.graph === graph && dirtySeq === seqAtSend) {
        S.dirty = false;
        saveFailures = 0;
        setSaveHint(`已保存 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`);
      }
    } catch (err) {
      setSaveHint(`保存失败：${err.message}`, true);
      // 失败后不能 300ms 无条件自激重试：磁盘写满这类持久错误会一直刷请求，
      // 还不停刷新「保存失败」的时间戳把真实故障盖住。改成最多重试 3 次、逐次拉长间隔。
      saveFailures = Math.min(saveFailures + 1, SAVE_RETRY_DELAYS.length);
    } finally {
      S.saving = false;
      if (S.dirty) {
        const delay = saveFailures ? SAVE_RETRY_DELAYS[saveFailures - 1] : 300;
        S.saveTimer = setTimeout(saveNow, delay);
      }
    }
  }

  async function ensureCanvas() {
    if (S.creating) return;
    if (!S.graphs.length) {
      S.creating = true;
      try {
        const graph = await api('/api/node-canvases', { method: 'POST', body: { name: '节点画布 1' } });
        S.graphs = [graph];
        loadGraph(graph.id);
      } catch (err) {
        toast(`新建节点画布失败：${err.message}`, true);
      } finally {
        S.creating = false;
      }
      return;
    }
    if (!S.currentId || !S.graphs.some((g) => g.id === S.currentId)) loadGraph(S.graphs[0].id);
  }

  /**
   * 旧画布迁移到「缩略图卡」：
   * - 宽度一律收敛到 CARD_W
   * - 高度按内容重算：媒体卡用已存的真实宽高比，文本/生成卡给默认档位；
   *   图片加载后还会按实际比例再精调一次（见 buildThumb）
   * - 旧数据里的 manualSize 是「手动拉过大表单卡」留下的，那些高度是按表单算的，
   *   迁移时认不出是不是用户真心想要的大小 —— 首次迁移（宽度还是旧 350）统一清掉；
   *   迁移后在缩略卡上重新拖过右下角的，尊重保留
   */
  function normalizeCardWidths(nodes) {
    let changed = false;
    for (const n of nodes) {
      if (!n || typeof n !== 'object') continue;
      const w = Number(n.w) || 0;
      // 旧版大表单卡宽 350：manualSize 是那时留下的，一律收敛；
      // 迁移后在缩略卡上手动拉过的尺寸（宽度不是旧 350）尊重保留
      const legacyWide = Math.abs(w - 350) < 0.5;
      if (n.manualSize && !legacyWide && w > 0 && Number(n.h) > 0) continue;
      const media = n.type === 'image' || n.type === 'video';
      let nextH = NODE_SIZE[n.type]?.[1] || (HEAD_H + THUMB_DEFAULT);
      if (media && n.mediaW && n.mediaH) {
        nextH = HEAD_H + Math.max(THUMB_MIN, Math.min(THUMB_MAX, Math.round((CARD_W * n.mediaH) / n.mediaW)));
      }
      const h = Number(n.h) || 0;
      if (Math.abs(w - CARD_W) < 0.5 && Math.abs(h - nextH) < 0.5) continue;
      n.w = CARD_W;
      n.h = nextH;
      delete n.manualSize;
      delete n.autoSized;
      changed = true;
    }
    return changed;
  }

  async function loadGraph(id) {
    const graph = S.graphs.find((g) => g.id === id);
    if (!graph) return;
    // 切画布前先把上一张的改动落盘，否则切换会把未保存的内容丢掉
    if (S.dirty) await saveNow();
    // 聊天记录同理：这张画布刚聊完就切走，别把最后一轮丢在内存里
    if (chatDirty) await flushChat();
    S.currentId = id;
    S.graph = {
      ...graph,
      nodes: (graph.nodes || []).map((n) => ({ ...n })),
      edges: (graph.edges || []).map((e) => ({ ...e })),
      viewport: { ...(graph.viewport || { x: 80, y: 80, zoom: 1 }) },
      // 对话列表要拷一份：历史面板会就地增删，别改到 S.graphs 里的那份快照
      chats: (graph.chats || []).map((c) => ({ ...c, messages: (c.messages || []).map((m) => ({ ...m })) })),
    };
    normalizeCardWidths(S.graph.nodes);
    // 老画布升级：提示词顺序（以前画风在最前，主体被挤掉了）
    const upgradedPrompts = upgradeShotPrompts(S.graph.nodes) > 0;
    // 老画布补救：这条规则上线前铺的分镜，提示词里还写着角色长相（有参考图也不该写）
    const repairedRefPrompts = repairRefPrompts(S.graph.nodes) > 0;
    // 老画布升级：视频提示词补上「首帧」那一头（以前只写结尾，模型会自己另起姿势）
    const upgradedVideo = upgradeVideoPrompts(S.graph.nodes) > 0;
    // 老画布补救：视频入边没标首帧/尾帧的，按画面顺序补上（以前存盘会把 role 洗掉）
    const backfilledRoles = backfillFrameRoles() > 0;
    // 老画布补救：镜头补上「地点」（以前根本没有这个字段，同场戏会一镜厨房一镜客厅）
    const backfilledPlaces = backfillPlaces(S.graph.nodes) > 0;
    // 老画布补救：剧本卡按内容重算高度（以前定死在 460，几十行文字只露出五六行）
    const fittedScripts = fitScriptCards(S.graph.nodes) > 0;
    S.selected.clear();
    S.selectedEdge = '';
    S.dirty = false;
    // 换画布：跑批条的「收起」只在当前这张画布有效（新画布可能有自己的分镜要跑）
    S.runHidden = false;
    // 聊天记录跟着画布走：每张画布带着自己那几段对话（存 db 里），切回来 / 重启应用都能接着聊。
    // 但也绝不能拿 A 画布的对话去问 B 画布 —— 模型会指着一堆不存在的镜头说要改。
    const chatLoaded = chat().loaded;
    S.chat = restoreChat(graph, chatLoaded);
    renderChatLog();
    renderChatHistory();
    // 聊天窗开着的时候切画布：那行小字要立刻改成「接着上次聊」，否则看着像没切过来
    if (isDirectorOpen()) setChatSub(chatSubText());
    // 新画布的历史栈清空，避免跨画布撤销
    S.history.undo.length = 0;
    S.history.redo.length = 0;
    setSaveHint('已保存');
    refreshHistoryButtons();
    renderAll();
    // 补救过就标脏：让这次修正自己存回 db，下次打开不用再算一遍
    if (repairedRefPrompts || upgradedPrompts || upgradedVideo || backfilledRoles || backfilledPlaces || fittedScripts) markDirty();
  }

  /* ---------------------------------- 渲染 ---------------------------------- */

  function renderAll() {
    renderCanvasSelect();
    renderNodes();
    renderEdges();
    applyViewport();
    syncEmpty();
    updateStatusBar();
  }

  function renderCanvasSelect() {
    const select = $('#ncCanvasSelect');
    if (!select) return;
    const sig = S.graphs.map((g) => `${g.id}:${g.name}`).join('|');
    if (select.dataset.sig !== sig) {
      select.dataset.sig = sig;
      select.textContent = '';
      for (const graph of S.graphs) {
        const option = document.createElement('option');
        option.value = graph.id;
        option.textContent = graph.name;
        select.appendChild(option);
      }
    }
    if (S.currentId) select.value = S.currentId;
  }

  function renderNodes() {
    const wrap = $('#ncNodes');
    if (!wrap) return;
    wrap.textContent = '';
    for (const node of S.graph?.nodes || []) {
      const el = buildNode(node);
      wrap.appendChild(el);
    }
    syncSelection();
    syncEmpty();
  }

  function buildNode(node) {
    const el = document.createElement('div');
    el.className = `nc-node nc-node-${node.type}`;
    el.dataset.id = node.id;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.style.width = `${node.w}px`;
    el.style.height = `${node.h}px`;

    const head = document.createElement('div');
    head.className = 'nc-node-head';
    // 图标用内联 SVG：⧉ / 🖼 这类字符在部分机器上缺字形会变豆腐块
    head.innerHTML = `
      <span class="nc-node-actions">
        <button type="button" data-act="dup" title="复制节点"><svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.2"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><path d="M8.5 3.5v-1a1.5 1.5 0 0 0-1.5-1.5H3A1.5 1.5 0 0 0 1.5 2.5v4A1.5 1.5 0 0 0 3 8h.5"/></svg></button>
        <button type="button" data-act="del" title="删除节点"><svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"/></svg></button>
      </span>`;
    el.appendChild(head);

    // 类型标签浮在卡片上方外侧（MiniMax 的「▶ 视频」），不占卡片内容高度。
    // 只给两个 AI 生成节点用：图片/视频/文本卡本身一眼就知道是什么，再挂个「图片」纯属噪音。
    if (node.type === 'imageGen' || node.type === 'videoGen') {
      const tag = document.createElement('span');
      tag.className = 'nc-node-tag';
      tag.innerHTML = `${nodeTagGlyph(node.type)}<span>${esc(NODE_LABEL[node.type] || node.type)}</span>`;
      el.appendChild(tag);
    }

    // 右侧 ⊕：两种用法（都从「输出」这一侧出发）
    //   点一下 → 弹「新建并连接」菜单（MiniMax 的 + 就是这么用的）
    //   按住拖 → 直接拉一根线，和输出端口完全同一套磁吸规则：
    //            吸到某个节点的端口/本体就只连线，落在空白处才弹菜单
    // 也就是说：卡片边缘那个小端口能做的事，这颗 ⊕ 也能做，而且更好按。
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'nc-node-add';
    addBtn.title = '点一下新建并连接；按住拖可以拉线连到已有节点';
    addBtn.innerHTML = '<svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M6 2.2v7.6M2.2 6h7.6"/></svg>';
    let addDragging = false;
    let addPressed = null;
    addBtn.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      addPressed = { x: event.clientX, y: event.clientY };
      addDragging = false;
      const move = (e) => {
        if (!addPressed) return;
        // 4px 死区：手抖不算拖，否则点一下也会拉出一根线
        if (!addDragging && Math.hypot(e.clientX - addPressed.x, e.clientY - addPressed.y) < 4) return;
        if (addDragging) return;
        addDragging = true;
        addBtn.classList.add('drag');
        startConnect(e, node, 'out');
      };
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        addPressed = null;
        addBtn.classList.remove('drag');
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    addBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      // 刚刚拖过线：这一下是松手带出来的 click，不能当成「点一下」
      if (addDragging) { addDragging = false; return; }
      const rect = addBtn.getBoundingClientRect();
      const world = screenToWorld(rect.right + 60, rect.top + rect.height / 2);
      offerSpawnConnect({ from: node.id, fromSide: 'out' }, world, event.clientX, event.clientY);
    });
    el.appendChild(addBtn);

    const body = document.createElement('div');
    body.className = 'nc-node-body';
    el.appendChild(body);
    buildThumb(node, body);

    const inPort = document.createElement('div');
    inPort.className = 'nc-port nc-port-in';
    inPort.dataset.port = 'in';
    inPort.title = '输入（把别的节点的输出拖到这里，或从这里往外拉）';
    inPort.setAttribute('aria-label', '输入端口');
    const outPort = document.createElement('div');
    outPort.className = 'nc-port nc-port-out';
    outPort.dataset.port = 'out';
    outPort.title = '输出（拖到另一个节点松手连线；拖到空白处可新建节点）';
    outPort.setAttribute('aria-label', '输出端口');
    el.append(inPort, outPort);

    const grip = document.createElement('div');
    grip.className = 'nc-resize';
    grip.dataset.role = 'resize';
    grip.title = '拖动调整缩略图大小';
    el.appendChild(grip);

    return el;
  }

  /* --------------------------- 卡片：缩略图态 --------------------------- */

  /**
   * 缩略图高度按内容比例精调（图片/视频加载完才知道比例）。
   * 用户拖过右下角（manualSize）就不再自动改。
   */
  function fitThumbHeight(node, ratio) {
    if (!node || node.manualSize || !ratio || !Number.isFinite(ratio)) return;
    const next = HEAD_H + Math.max(THUMB_MIN, Math.min(THUMB_MAX, Math.round(node.w * ratio)));
    if (Math.abs((node.h || 0) - next) < 4) return;
    node.h = next;
    const el = $(`.nc-node[data-id="${node.id}"]`);
    if (el) el.style.height = `${node.h}px`;
    markDirty();
    renderEdges();
    drawMinimap();
  }

  /** 卡片主体：只画缩略图 / 文本预览 / 生成占位，表单全部在底部编辑面板里 */
  function buildThumb(node, body) {
    body.textContent = '';
    const thumb = document.createElement('div');
    thumb.className = `nc-thumb nc-thumb-${node.type}`;

    if (node.type === 'text') {
      const text = String(node.text || '').trim();
      // 文本卡整段铺开：卡拉多大就显示多少，装不下再在卡内滚动。
      // 以前这里只切前 160 字 + CSS 截 6 行，于是 460×460 的剧本卡只露出五六行，
      // 剩下几十行明明在节点里却看不见（用户反馈「框这么大，就哪一点显示」）。
      thumb.innerHTML = text
        ? `<span class="nc-thumb-text nc-thumb-text-full">${esc(text)}</span>`
        : '<span class="nc-thumb-placeholder">文本 · 点一下编辑</span>';
      body.appendChild(thumb);
      return;
    }

    if (node.type === 'image' || node.type === 'video' || node.type === 'audio') {
      const url = node.localUrl || node.url;
      if (url && node.type === 'image') {
        const img = document.createElement('img');
        img.src = url;
        img.alt = '';
        img.draggable = false;
        img.addEventListener('load', () => {
          setDim(img.naturalWidth, img.naturalHeight);
          fitThumbHeight(node, img.naturalHeight / (img.naturalWidth || 1));
        });
        thumb.appendChild(img);
      } else if (url && node.type === 'video') {
        const video = document.createElement('video');
        video.src = url;
        video.muted = true;
        video.preload = 'metadata';
        video.addEventListener('loadedmetadata', () => {
          setDim(video.videoWidth, video.videoHeight);
          fitThumbHeight(node, video.videoHeight / (video.videoWidth || 1));
        });
        thumb.appendChild(video);
        const badge = document.createElement('span');
        badge.className = 'nc-thumb-badge';
        badge.innerHTML = '<svg viewBox="0 0 12 12" width="12" height="12"><path d="M3.5 2.2v7.6l6-3.8z" fill="currentColor"/></svg>';
        thumb.appendChild(badge);
      } else if (url && node.type === 'audio') {
        thumb.innerHTML = '<span class="nc-thumb-icon"><svg viewBox="0 0 20 20" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><path d="M7.5 13.5V4.8l7-1.8v9.2"/><circle cx="5.5" cy="13.5" r="2"/><circle cx="12.5" cy="12.2" r="2"/></svg></span>';
      } else {
        thumb.innerHTML = `<span class="nc-thumb-placeholder">${node.type === 'image' ? '图片' : node.type === 'video' ? '视频' : '音频'} · 点一下选择文件</span>`;
      }
      if (node.fileName) {
        const name = document.createElement('span');
        name.className = 'nc-thumb-name';
        name.textContent = node.fileName;
        name.title = node.fileName;
        thumb.appendChild(name);
      }
      body.appendChild(thumb);
      // 已量过尺寸的节点先按存档比例摆好，图片加载完再精调，免得闪一下
      if (node.mediaW && node.mediaH) setDim(node.mediaW, node.mediaH);
      return;
    }

    if (node.type === 'imageGen' || node.type === 'videoGen') {
      const gen = node.gen || {};
      const url = gen.resultUrl || '';
      if (url && node.type === 'imageGen') {
        const img = document.createElement('img');
        img.src = url;
        img.alt = '';
        img.draggable = false;
        img.addEventListener('load', () => fitThumbHeight(node, img.naturalHeight / (img.naturalWidth || 1)));
        thumb.appendChild(img);
      } else if (url && node.type === 'videoGen') {
        const video = document.createElement('video');
        video.src = url;
        video.muted = true;
        video.preload = 'metadata';
        video.addEventListener('loadedmetadata', () => fitThumbHeight(node, video.videoHeight / (video.videoWidth || 1)));
        thumb.appendChild(video);
        const badge = document.createElement('span');
        badge.className = 'nc-thumb-badge';
        badge.innerHTML = '<svg viewBox="0 0 12 12" width="12" height="12"><path d="M3.5 2.2v7.6l6-3.8z" fill="currentColor"/></svg>';
        thumb.appendChild(badge);
      } else {
        // 还没出结果：图标 + 提示词摘要 + 状态，一眼看出这张卡在干什么
        const models = node.type === 'imageGen' ? S.imageModels : S.videoModels;
        const modelName = models.find((m) => m.id === gen.modelId)?.name || '';
        const promptText = String(gen.prompt || '').trim();
        const status = gen.error
          || (PENDING.has(gen.status) ? (gen.progress || STATUS_LABEL[gen.status] || '生成中') : '')
          || (gen.status === 'failed' ? '失败' : '');
        // 图标用内联 SVG，不依赖 emoji 字形（部分机器上 🖼/🎬 会变豆腐块）
        const icon = node.type === 'imageGen'
          ? '<svg viewBox="0 0 20 20" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="3.5" width="16" height="13" rx="2"/><circle cx="7" cy="8" r="1.6"/><path d="M2 14.5l4.5-4 4 3.5 3.5-3 4 3.5"/></svg>'
          : '<svg viewBox="0 0 20 20" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="2" y="3.5" width="16" height="13" rx="2"/><path d="M8.5 7.5l4.5 2.5-4.5 2.5z" fill="currentColor" stroke="none"/></svg>';
        thumb.innerHTML = `
          <span class="nc-thumb-icon">${icon}</span>
          <span class="nc-thumb-sub">${esc(modelName || (node.type === 'imageGen' ? 'AI 生图' : 'AI 视频'))}</span>
          ${promptText ? `<span class="nc-thumb-text">${esc(promptText.slice(0, 80))}</span>` : ''}
          <span class="nc-thumb-status ${gen.status === 'failed' || gen.error ? 'err' : ''} ${status ? '' : 'dim'}">${esc(status || '点一下设置提示词')}</span>
        `;
      }
      // 生成状态角标：生成中 / 失败时哪怕有结果图也要看得见
      if (url && (PENDING.has(gen.status) || gen.status === 'failed')) {
        const st = document.createElement('span');
        st.className = `nc-thumb-status float ${gen.status === 'failed' ? 'err' : ''}`;
        st.textContent = gen.status === 'failed' ? '失败' : (gen.progress || '生成中');
        thumb.appendChild(st);
      }
      body.appendChild(thumb);
      return;
    }

    body.appendChild(thumb);

    function setDim(w, h) {
      if (!w || !h) return;
      node.mediaW = Math.round(w);
      node.mediaH = Math.round(h);
    }
  }

  /* --------------------------- 底部编辑面板 --------------------------- */

  /** 面板里的表单就是原来卡片里那套（buildBody），容器换成了面板 */
  function panelEl() { return $('#ncPanel'); }

  function panelTitleOf(node) {
    const base = NODE_LABEL[node.type] || node.type;
    if (node.type === 'imageGen' || node.type === 'videoGen') {
      const models = node.type === 'imageGen' ? S.imageModels : S.videoModels;
      const name = models.find((m) => m.id === node.gen?.modelId)?.name;
      return name ? `${base} · ${name}` : base;
    }
    if (node.fileName) return `${base} · ${node.fileName}`;
    return base;
  }

  function openPanel(node) {
    const panel = panelEl();
    const body = $('#ncPanelBody');
    if (!panel || !body || !node) return;
    S.panelNode = node.id;
    panel.dataset.node = node.id;
    const title = $('#ncPanelTitle');
    if (title) title.textContent = panelTitleOf(node);
    buildBody(node, body);
    body.scrollTop = 0;
    panel.hidden = false;
  }

  function closePanel() {
    const panel = panelEl();
    S.panelNode = '';
    closePicker();
    if (panel) {
      panel.hidden = true;
      delete panel.dataset.node;
    }
    const body = $('#ncPanelBody');
    if (body) body.textContent = '';
  }

  /** 选中态变化的总出口：单选一个节点 → 面板展开；多选/框选/选连线/空白 → 收起 */
  function syncPanel() {
    if (!S.graph) { closePanel(); return; }
    // 框选/连线拖动过程中不弹面板，免得拖一下跳一下
    if (S.marquee || S.connecting) return;
    const ids = [...S.selected];
    if (ids.length !== 1) { if (S.panelNode) closePanel(); return; }
    const node = nodeById(ids[0]);
    if (!node) { if (S.panelNode) closePanel(); return; }
    if (S.panelNode === node.id) return; // 已经开着这个节点，别重建（输入框会失焦）
    openPanel(node);
  }

  /** 生成类节点状态文案的落点：面板开着时在面板里，没开时更新卡片缩略图上的状态行 */
  function genStatusElOf(node) {
    if (S.panelNode === node.id) return $(`.nc-panel[data-node="${node.id}"] .nc-gen-status`);
    return $(`.nc-node[data-id="${node.id}"] .nc-thumb-status`);
  }

  /** 面板开着且焦点在面板输入控件里时，不重建表单（只补状态文案），否则打字被打断 */
  function panelBusy() {
    const body = $('#ncPanelBody');
    const ae = document.activeElement;
    return Boolean(body && ae && body.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName));
  }

  function refreshPanelTitle(node) {
    if (S.panelNode !== node.id) return;
    const title = $('#ncPanelTitle');
    if (title) title.textContent = panelTitleOf(node);
  }

  function rerenderPanel(node) {
    if (S.panelNode !== node.id) return;
    const body = $('#ncPanelBody');
    if (!body) return;
    refreshPanelTitle(node);
    if (panelBusy()) {
      // 正在打字：只补底栏胶囊与状态文案，绝不重建 DOM
      refreshComposerChips(node, node.type === 'videoGen' ? 'video' : 'image');
      return;
    }
    buildBody(node, body);
  }

  function buildBody(node, body) {
    body.textContent = '';
    if (node.type === 'text') {
      const area = document.createElement('textarea');
      area.className = 'nc-text';
      area.placeholder = '输入文本 / 提示词…（连到生图、视频节点可直接当提示词）';
      area.value = node.text || '';
      area.addEventListener('input', () => {
        node.text = area.value;
        markDirty();
      });
      body.appendChild(area);
      return;
    }

    if (node.type === 'image' || node.type === 'video' || node.type === 'audio') {
      renderMediaBody(node, body);
      return;
    }

    if (node.type === 'imageGen') {
      renderImageGenBody(node, body);
      return;
    }
    if (node.type === 'videoGen') {
      renderVideoGenBody(node, body);
      return;
    }
  }

  function renderMediaBody(node, body) {
    const url = node.localUrl || node.url;
    const media = document.createElement('div');
    media.className = 'nc-media';

    if (url && node.type === 'image') {
      const img = document.createElement('img');
      img.src = url;
      img.alt = '';
      img.draggable = false;
      // 节点尺寸由卡片缩略图（buildThumb / fitThumbHeight）负责，面板里只量尺寸存档
      media.appendChild(img);
      img.addEventListener('load', () => setDim(img.naturalWidth, img.naturalHeight));
    } else if (url && node.type === 'video') {
      const video = document.createElement('video');
      video.src = url;
      video.controls = true;
      video.preload = 'metadata';
      video.addEventListener('loadedmetadata', () => setDim(video.videoWidth, video.videoHeight));
      media.appendChild(video);
    } else if (url && node.type === 'audio') {
      const audio = document.createElement('audio');
      audio.src = url;
      audio.controls = true;
      audio.addEventListener('loadedmetadata', () => {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          const min = Math.floor(audio.duration / 60);
          const sec = Math.round(audio.duration % 60);
          dim.textContent = `${min}:${String(sec).padStart(2, '0')}`;
        }
      });
      media.appendChild(audio);
    } else {
      const empty = document.createElement('div');
      empty.className = 'nc-media-empty';
      empty.innerHTML = `<span>${node.type === 'image' ? '拖素材到这里，或点下方按钮' : '还没有文件'}</span>`;
      media.appendChild(empty);
    }
    body.appendChild(media);

    const bar = document.createElement('div');
    bar.className = 'nc-media-bar';
    // 文件名 + 分辨率放同一行（MiniMax 的「图片 7 / 2720 × 1520」）。
    // 分辨率是判断这份素材还能不能用的第一信息，藏在 hover 里等于没有。
    const meta = document.createElement('div');
    meta.className = 'nc-media-meta';
    const hint = document.createElement('span');
    hint.className = 'nc-media-name';
    hint.textContent = node.fileName || '';
    hint.title = node.fileName || '';
    const dim = document.createElement('span');
    dim.className = 'nc-media-dim';
    const setDim = (w, h) => {
      if (!w || !h) { dim.textContent = ''; return; }
      dim.textContent = `${Math.round(w)} × ${Math.round(h)}`;
      node.mediaW = Math.round(w);
      node.mediaH = Math.round(h);
    };
    meta.append(hint, dim);
    bar.appendChild(meta);
    // 已经量过尺寸的节点（同一会话里重渲染）先显示上一次的值，免得闪一下空白
    if (node.mediaW && node.mediaH) setDim(node.mediaW, node.mediaH);

    const pick = document.createElement('button');
    pick.type = 'button';
    pick.className = 'btn tiny';
    pick.dataset.act = 'pick';
    pick.textContent = url ? '更换' : '选择文件';
    if (node.type === 'image') {
      pick.addEventListener('click', () => pickNodeFile(node, 'image/*'));
    } else if (node.type === 'video') {
      pick.addEventListener('click', () => pickNodeFile(node, 'video/*'));
    } else {
      pick.addEventListener('click', () => pickNodeFile(node, 'audio/*'));
    }
    bar.appendChild(pick);

    if (url) {
      const addLib = document.createElement('button');
      addLib.type = 'button';
      addLib.className = 'btn tiny';
      addLib.textContent = '存入素材库';
      addLib.title = '把这份文件登记到本地素材库（同一个文件，不复制）';
      addLib.addEventListener('click', () => saveNodeToLibrary(node));
      bar.appendChild(addLib);
    }
    body.appendChild(bar);
  }

  /** 可被 @ 引用的上游图片节点，顺序即发给模型时的 refIds 顺序 */
  function refCandidates(node) {
    return incomingNodes(node).filter((n) => ['image', 'imageGen'].includes(n.type) && mediaUrlOf(n));
  }

  /** 引用编号：@ 插入的 [图N] 和下面的缩略图用同一套序号 */
  function refLabel(index) {
    return `图${index + 1}`;
  }

  function refSig(node) {
    return refCandidates(node).map((n) => n.id).join(',');
  }

  /**
   * 在生图 / 视频节点里画一排引用缩略图。
   * 用户反馈：@ 之后只看到一个文件名，根本分不清引用的是哪张图 ——
   * 这里把已连线的图片按顺序显示成缩略图，点一下就把 [图N] 插到光标处。
   */
  function renderRefChips(host, node, gen, textarea) {
    host.textContent = '';
    const list = refCandidates(node);
    host.hidden = !list.length;
    // 连线一变，提示词里能对上的旧式 [图:文件名] 也跟着换成 [图N]
    migrateRefTokens(node, gen, textarea);
    if (!list.length) return;
    for (const [index, cand] of list.entries()) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'nc-ref-chip';
      chip.title = `${cand.fileName || cand.title || '图片'}（点击插入 ${refLabel(index)}）`;
      const img = document.createElement('img');
      img.src = mediaUrlOf(cand);
      img.alt = '';
      img.draggable = false;
      const name = document.createElement('span');
      name.textContent = refLabel(index);
      const del = document.createElement('i');
      del.className = 'nc-ref-x';
      del.textContent = '×';
      del.title = '断开这个引用（删除连线）';
      chip.append(img, name, del);

      del.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        pushHistory();
        S.graph.edges = S.graph.edges.filter((e) => !(e.from === cand.id && e.to === node.id));
        markDirty();
        renderEdges();
        rerenderNode(node);
      });
      chip.addEventListener('click', (event) => {
        event.preventDefault();
        insertRefToken(textarea, gen, index);
      });
      host.appendChild(chip);
    }
  }

  /**
   * 老版本 @ 插入的是纯文本 `[图:文件名.jpg]`，提示词里只看得见文件名。
   * 这里把能对上当前连线图片的旧标记就地换成 `[图N]`，对不上的原样保留。
   */
  function migrateRefTokens(node, gen, textarea) {
    if (!textarea || !gen || typeof gen.prompt !== 'string') return;
    if (!gen.prompt.includes('[图:')) return;
    const list = refCandidates(node);
    if (!list.length) return;
    const byName = new Map();
    list.forEach((cand, index) => {
      const key = String(cand.fileName || '').trim();
      if (key) byName.set(key, index);
    });
    let touched = false;
    const next = gen.prompt.replace(/\[图:([^\]]+)\]/g, (whole, raw) => {
      const index = byName.get(String(raw).trim());
      if (index === undefined) return whole;
      touched = true;
      return `[${refLabel(index)}]`;
    });
    if (!touched) return;
    gen.prompt = next;
    textarea.value = next;
    markDirty();
  }

  /** 把 [图N] 插到光标处，替换掉光标前正在输入的 @ */
  function insertRefToken(textarea, gen, index) {
    const text = textarea.value;
    const caret = textarea.selectionStart ?? text.length;
    let at = caret - 1;
    while (at >= 0 && /[\w一-龥-]/.test(text[at])) at -= 1;
    const head = at >= 0 && text[at] === '@' ? at : caret;
    const token = `[${refLabel(index)}]`;
    textarea.value = `${text.slice(0, head)}${token}${text.slice(caret).replace(/^\s*/, ' ')}`;
    gen.prompt = textarea.value;
    markDirty();
    const pos = head + token.length;
    textarea.focus();
    textarea.setSelectionRange(pos, pos);
  }

  /** 连线变化后刷新所有生图节点的引用缩略图（不重建输入框，避免打断打字） */
  function syncRefChips() {
    if (!S.graph) return;
    // 参考图 chips 只活在底部编辑面板里（卡片折叠后没有表单）
    const hosts = $$('.nc-panel .nc-refs');
    if (!hosts.length) return;
    // 先把「谁指向谁」算一遍，避免每个节点都扫一遍全表（拖拽时每条 mousemove 都会走到这）
    const incoming = new Map();
    for (const e of S.graph.edges || []) {
      if (!incoming.has(e.to)) incoming.set(e.to, []);
      incoming.get(e.to).push(e.from);
    }
    for (const host of hosts) {
      const node = nodeById(host.closest('.nc-panel')?.dataset.node);
      if (!node) continue;
      const sig = (incoming.get(node.id) || []).join(',');
      if (host.dataset.sig === sig) continue;
      host.dataset.sig = sig;
      const textarea = host.closest('.nc-composer')?.querySelector('textarea[data-role="prompt"]');
      const gen = node.gen || (node.gen = {});
      renderRefChips(host, node, gen, textarea);
    }
  }

  /** 为提示词 textarea 绑定 @ 引用：候选只显示已连线的图片节点 */
  function bindPromptMention(textarea, node, gen) {
    let mentionEl = null;

    function close() {
      if (mentionEl) { mentionEl.remove(); mentionEl = null; }
    }

    function open() {
      close();
      const text = textarea.value;
      const caret = textarea.selectionStart ?? text.length;
      // 光标前扫到 @
      let at = caret - 1;
      while (at >= 0 && /[\w一-龥-]/.test(text[at])) at -= 1;
      if (at < 0 || text[at] !== '@') return;

      const query = text.slice(at + 1, caret).toLowerCase();
      const all = refCandidates(node);
      const candidates = all
        .map((n, index) => ({ node: n, index }))
        .filter(({ node: cand }) => {
          if (!query) return true;
          const label = `${cand.fileName || ''} ${cand.title || ''} ${cand.type}`.toLowerCase();
          return label.includes(query);
        });
      if (!candidates.length) return;

      mentionEl = document.createElement('div');
      mentionEl.className = 'nc-node-mention';
      for (const { node: cand, index } of candidates.slice(0, 8)) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'nc-node-mention-item';
        const thumbUrl = mediaUrlOf(cand);
        if (thumbUrl) {
          const img = document.createElement('img');
          img.src = thumbUrl;
          img.alt = '';
          btn.appendChild(img);
        }
        const name = document.createElement('span');
        name.textContent = cand.fileName || cand.title || NODE_LABEL[cand.type];
        btn.appendChild(name);
        const badge = document.createElement('b');
        badge.className = 'nc-node-mention-badge';
        badge.textContent = refLabel(index);
        btn.appendChild(badge);
        btn.addEventListener('click', () => {
          insertRefToken(textarea, gen, index);
          close();
        });
        mentionEl.appendChild(btn);
      }
      // 挂到 body 并用 fixed 定位，避免被 .nc-node-body 的 overflow:hidden 裁剪
      document.body.appendChild(mentionEl);
      const rect = textarea.getBoundingClientRect();
      mentionEl.style.left = `${rect.left}px`;
      // 下方空间不够就翻到上方，避免浮层被视口裁掉
      const below = rect.bottom + 4;
      const space = window.innerHeight - below;
      if (space < 180 && rect.top > 200) {
        mentionEl.style.top = '';
        mentionEl.style.bottom = `${window.innerHeight - rect.top + 4}px`;
      } else {
        mentionEl.style.bottom = '';
        mentionEl.style.top = `${below}px`;
      }
      mentionEl.style.width = `${rect.width}px`;
    }

    textarea.addEventListener('input', () => {
      const text = textarea.value;
      const caret = textarea.selectionStart ?? text.length;
      let at = caret - 1;
      while (at >= 0 && /[\w一-龥-]/.test(text[at])) at -= 1;
      if (at >= 0 && text[at] === '@') open(); else close();
    });
    textarea.addEventListener('blur', () => setTimeout(close, 150));
    textarea.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  }

  /* ------------------ 生成节点编辑区（MiniMax 式 composer） ------------------ */

  /**
   * 对齐 MiniMax Design 的编辑区形态：
   *   参考图缩略条
   *   大输入框（提示词，@ 可引上游图片）
   *   [模型胶囊] [参数胶囊] [×数量] [状态…] [生成 ↑]
   * 模型 / 参数 / 数量都是点开浮层选，不在面板里铺一堆下拉框。
   */
  const PROTOCOL_LABEL = {
    ark: '火山方舟',
    openai: 'OpenAI 兼容',
    zhipu: '智谱 CogView',
    jimeng: '即梦',
    wanxiang: '通义万相',
    hunyuan: '腾讯混元',
    minimax: 'MiniMax 海螺',
    cloudflare: 'Cloudflare',
    doubao: '本机豆包桥',
  };
  /** 没有预设比例表时的兜底（图片） */
  const FALLBACK_RATIOS = ['1:1', '4:3', '3:4', '16:9', '9:16'];

  function modelsOf(kind) {
    return kind === 'video' ? S.videoModels : S.imageModels;
  }

  /** 视频模型的参数预设（时长档位 / 分辨率档 / 比例表），按 provider 查服务端下发的表 */
  function videoPresetOf(model) {
    return S.videoPresets?.[model?.provider] || null;
  }

  function modelOf(node, kind) {
    const gen = node.gen || {};
    return modelsOf(kind).find((m) => m.id === gen.modelId) || null;
  }

  /** 图片模型可选的「比例 → 尺寸」表（按协议查，服务端下发） */
  function ratioMapOf(node) {
    const model = modelOf(node, 'image');
    const map = S.imageDefaults?.ratioPresets?.[model?.protocol];
    return map && typeof map === 'object' ? map : null;
  }

  /**
   * 视频参数的**生效值**：卡上没设过就落到模型默认。
   *
   * 服务端 submitVideo 就是这么兜的（duration ?? model.defaultDuration），
   * 所以卡上显示的必须也是这一组，不然「默认 10 秒横屏」改完了用户还是看不见 ——
   * 新建的视频节点 gen 是空的，胶囊只会写「参数」两个字。
   */
  function videoEffectiveOf(node) {
    const gen = node.gen || {};
    const model = modelOf(node, 'video');
    return {
      ratio: gen.ratio || model?.defaultRatio || '',
      resolution: gen.resolution || model?.defaultResolution || '',
      duration: gen.duration || model?.defaultDuration || '',
    };
  }

  /** 底栏第二个胶囊的文案：视频「16:9 · 768p · 15s」，图片「1024x1024」 */
  function paramSummary(node, kind) {
    const gen = node.gen || {};
    if (kind === 'video') {
      const preset = videoPresetOf(modelOf(node, 'video'));
      const eff = videoEffectiveOf(node);
      const bits = [];
      if (eff.ratio) bits.push(eff.ratio);
      // 智谱这类「宽x高」档位的 sizeLabels 就是比例本身（1920x1080 → 16:9），
      // 直接拼会显示成「16:9 · 16:9 · 10s」；标签和比例重复时只留一个
      const resLabel = eff.resolution ? preset?.sizeLabels?.[eff.resolution] || eff.resolution : '';
      if (resLabel && resLabel !== eff.ratio) bits.push(resLabel);
      if (eff.duration) bits.push(`${eff.duration}s`);
      return bits.join(' · ');
    }
    if (gen.size) return String(gen.size);
    // 没显式设过尺寸：显示当前模型默认尺寸，让用户知道「不填会走哪个」
    return modelOf(node, 'image')?.size || '';
  }

  /* ------------------------------ 浮层选择器 ------------------------------ */

  function closePicker() {
    if (S.picker) {
      S.picker.remove();
      S.picker = null;
    }
    document.removeEventListener('pointerdown', onPickerOutside, true);
  }

  function onPickerOutside(event) {
    if (!S.picker) return;
    if (S.picker.contains(event.target)) return;
    // 点胶囊本身交给胶囊自己的 click 切换，这里不管，否则会被关掉又立刻打开
    if (event.target.closest?.('[data-chip]')) return;
    closePicker();
  }

  /** 在锚点上方弹出浮层（上方放不下就翻到下方），点外部 / Esc 收起 */
  function openPicker(anchor, build) {
    closePicker();
    const el = document.createElement('div');
    el.className = 'nc-picker';
    build(el);
    document.body.appendChild(el);
    const rect = anchor.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    let left = rect.left;
    let top = rect.top - box.height - 8;
    if (top < 8) top = Math.min(window.innerHeight - box.height - 8, rect.bottom + 8);
    left = Math.max(8, Math.min(window.innerWidth - box.width - 8, left));
    el.style.left = `${left}px`;
    el.style.top = `${Math.max(8, top)}px`;
    S.picker = el;
    setTimeout(() => document.addEventListener('pointerdown', onPickerOutside, true), 0);
  }

  function pickerSection(el, title) {
    const head = document.createElement('div');
    head.className = 'nc-picker-title';
    head.textContent = title;
    el.appendChild(head);
  }

  /** 比例示意小方块：按 16:9 / 3:4 之类画一个正比矩形 */
  function ratioGlyph(ratio) {
    const [w, h] = String(ratio).split(':').map((n) => Number(n) || 1);
    const long = Math.max(w, h);
    const box = document.createElement('span');
    box.className = 'nc-ratio-box';
    box.style.width = `${Math.round(20 * (w / long))}px`;
    box.style.height = `${Math.round(20 * (h / long))}px`;
    return box;
  }

  /* ------------------------------ 模型浮层 ------------------------------ */

  function openModelPicker(anchor, node, kind) {
    const list = modelsOf(kind).filter((m) => m.enabled);
    openPicker(anchor, (el) => {
      pickerSection(el, kind === 'video' ? '视频模型' : '图片模型');
      if (!list.length) {
        const empty = document.createElement('div');
        empty.className = 'nc-picker-empty';
        empty.textContent = '还没有可用的模型，先去「设置 → 模型」里添加';
        el.appendChild(empty);
        return;
      }
      const gen = node.gen || {};
      for (const m of list) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = `nc-picker-item${m.id === gen.modelId ? ' on' : ''}`;
        const proto = kind === 'video' ? m.provider : m.protocol;
        const badge = proto === 'doubao' ? '<em class="nc-badge">本机免 Key</em>' : '';
        const sub = [
          PROTOCOL_LABEL[proto] || proto || '',
          kind === 'image' && m.supportsImageInput ? '支持参考图' : '',
        ].filter(Boolean).join(' · ');
        item.innerHTML = `
          <span class="nc-picker-icon">${modelGlyph(kind)}</span>
          <span class="nc-picker-main">
            <span class="nc-picker-name">${esc(m.name || m.id)}${badge}</span>
            ${sub ? `<span class="nc-picker-sub">${esc(sub)}</span>` : ''}
          </span>
          ${m.id === gen.modelId ? '<span class="nc-picker-check">✓</span>' : ''}`;
        item.addEventListener('click', () => {
          closePicker();
          const before = (node.gen ||= {}).modelId;
          // 视频节点：换模型可能换掉「吃不吃尾帧」，提示词里首/尾帧的说法得跟着换。
          // 只改没被人手改过的 —— 旧模型下自动拼出来的那串一模一样才动，否则保留原文
          if (kind === 'video' && node.agent?.role === 'video' && before && before !== m.id) {
            const source = videoPromptSource(node);
            if (String(node.gen.prompt || '') === videoPromptOf(source, { modelId: before })) {
              node.gen.prompt = videoPromptOf(source, { modelId: m.id });
            }
          }
          node.gen.modelId = m.id;
          markDirty();
          rerenderCard(node);
          // 换模型后参数预设可能整套变（时长档位/分辨率），整个编辑区重建一次
          rerenderPanel(node);
        });
        el.appendChild(item);
      }
    });
  }

  function modelGlyph(kind) {
    return kind === 'video'
      ? '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="4" width="16" height="12" rx="2"/><path d="M8.5 7.5l4 2.5-4 2.5z" fill="currentColor" stroke="none"/></svg>'
      : '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="3.5" width="16" height="13" rx="2"/><circle cx="7" cy="8" r="1.5"/><path d="M2 14l4.5-4 4 3.5 3.5-3 4 3.5"/></svg>';
  }

  /** 卡片上方类型标签的小图标（MiniMax 的「▶ 视频」） */
  function nodeTagGlyph(type) {
    const attrs = 'width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.4"';
    if (type === 'text') {
      return `<svg viewBox="0 0 12 12" ${attrs} stroke-linecap="round"><path d="M2.5 3h7M2.5 6h7M2.5 9h4.5"/></svg>`;
    }
    if (type === 'audio') {
      return `<svg viewBox="0 0 12 12" ${attrs} stroke-linecap="round"><path d="M4.4 8.6V3.6l4.2-1v5"/><circle cx="3.2" cy="8.7" r="1.2"/><circle cx="7.4" cy="7.7" r="1.2"/></svg>`;
    }
    if (type === 'video' || type === 'videoGen') {
      return `<svg viewBox="0 0 12 12" ${attrs}><rect x="1.5" y="2" width="9" height="8" rx="1.5"/><path d="M5 4.3l3 1.7-3 1.7z" fill="currentColor" stroke="none"/></svg>`;
    }
    return `<svg viewBox="0 0 12 12" ${attrs}><rect x="1.5" y="2" width="9" height="8" rx="1.5"/><circle cx="4.4" cy="4.8" r="1"/><path d="M1.5 8.5l2.8-2.4 2.4 2 1.9-1.6 2.4 2"/></svg>`;
  }

  /* ------------------------------ 参数浮层 ------------------------------ */

  function openParamPicker(anchor, node, kind) {
    openPicker(anchor, (el) => {
      if (kind === 'video') buildVideoParams(el, node);
      else buildImageParams(el, node);
    });
  }

  function buildImageParams(el, node) {
    const gen = (node.gen ||= {});
    const map = ratioMapOf(node);
    const ratios = map ? Object.keys(map) : FALLBACK_RATIOS;
    pickerSection(el, '比例');
    const grid = document.createElement('div');
    grid.className = 'nc-ratio-grid';
    const current = gen.size || '';
    for (const ratio of ratios) {
      const value = map?.[ratio]?.[0] || ratio;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `nc-ratio${value === current || ratio === current ? ' on' : ''}`;
      btn.append(ratioGlyph(ratio));
      const label = document.createElement('span');
      label.textContent = ratio;
      btn.appendChild(label);
      btn.addEventListener('click', () => {
        gen.size = value;
        markDirty();
        for (const other of grid.querySelectorAll('.nc-ratio')) other.classList.remove('on');
        btn.classList.add('on');
        refreshComposerChips(node, 'image');
      });
      grid.appendChild(btn);
    }
    el.appendChild(grid);

    pickerSection(el, '尺寸（留空按模型默认）');
    const sizeInput = document.createElement('input');
    sizeInput.type = 'text';
    sizeInput.className = 'nc-picker-input';
    sizeInput.placeholder = modelOf(node, 'image')?.size || '例如 1024x1024';
    sizeInput.value = gen.size || '';
    sizeInput.addEventListener('input', () => {
      gen.size = sizeInput.value;
      markDirty();
      refreshComposerChips(node, 'image');
    });
    el.appendChild(sizeInput);
  }

  function buildVideoParams(el, node) {
    const gen = (node.gen ||= {});
    const preset = videoPresetOf(modelOf(node, 'video'));
    if (!preset || (!preset.ratios?.length && !preset.resolutions?.length && !preset.durations?.length)) {
      const empty = document.createElement('div');
      empty.className = 'nc-picker-empty';
      empty.textContent = '这个模型没有参数预设，走服务端默认值';
      el.appendChild(empty);
      return;
    }
    // 没设过参数时高亮的是「模型默认」那一档（默认 10 秒 / 16:9），
    // 否则打开浮层一个都没选中，用户不知道实际会按什么生成
    const eff = videoEffectiveOf(node);

    if (preset.ratios?.length) {
      pickerSection(el, '比例');
      const grid = document.createElement('div');
      grid.className = 'nc-ratio-grid';
      for (const ratio of preset.ratios) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `nc-ratio${eff.ratio === ratio ? ' on' : ''}`;
        btn.append(ratioGlyph(ratio));
        const label = document.createElement('span');
        label.textContent = ratio;
        btn.appendChild(label);
        btn.addEventListener('click', () => {
          gen.ratio = ratio;
          markDirty();
          for (const other of grid.querySelectorAll('.nc-ratio')) other.classList.remove('on');
          btn.classList.add('on');
          refreshComposerChips(node, 'video');
        });
        grid.appendChild(btn);
      }
      el.appendChild(grid);
    }

    if (preset.resolutions?.length) {
      pickerSection(el, '清晰度');
      const seg = document.createElement('div');
      seg.className = 'nc-seg';
      for (const res of preset.resolutions) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `nc-seg-item${eff.resolution === res ? ' on' : ''}`;
        btn.textContent = preset.sizeLabels?.[res] || res;
        btn.addEventListener('click', () => {
          gen.resolution = res;
          markDirty();
          for (const other of seg.querySelectorAll('.nc-seg-item')) other.classList.remove('on');
          btn.classList.add('on');
          refreshComposerChips(node, 'video');
        });
        seg.appendChild(btn);
      }
      el.appendChild(seg);
    }

    if (preset.tiers?.length) {
      // 豆包内部的模型档（2.5 / 2.0 / Fast / Mini）：倍率差好几倍，放在参数里让用户自己选，
      // 别让他每次都去豆包窗口里点。选中后会写进任务参数，桥负责切档
      pickerSection(el, '豆包档位');
      const seg = document.createElement('div');
      seg.className = 'nc-seg';
      for (const tier of preset.tiers) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `nc-seg-item${(gen.doubaoTier || '') === tier.id ? ' on' : ''}`;
        btn.textContent = tier.label;
        btn.title = tier.hint || '';
        btn.addEventListener('click', () => {
          gen.doubaoTier = tier.id;
          markDirty();
          for (const other of seg.querySelectorAll('.nc-seg-item')) other.classList.remove('on');
          btn.classList.add('on');
          refreshComposerChips(node, 'video');
        });
        seg.appendChild(btn);
      }
      el.appendChild(seg);
      if (!gen.doubaoTier) {
        const note = document.createElement('div');
        note.className = 'nc-picker-empty';
        note.textContent = '不选就跟随豆包窗口里当前的档位';
        el.appendChild(note);
      }
    }

    if (preset.durations?.length) {
      pickerSection(el, '时长');
      const values = preset.durations.map(Number).filter((n) => n > 0).sort((a, b) => a - b);
      if (values.length) {
        const row = document.createElement('div');
        row.className = 'nc-dur';
        const minus = document.createElement('button');
        minus.type = 'button';
        minus.className = 'nc-dur-step';
        minus.textContent = '−';
        const out = document.createElement('span');
        out.className = 'nc-dur-val';
        const plus = document.createElement('button');
        plus.type = 'button';
        plus.className = 'nc-dur-step';
        plus.textContent = '+';
        // 档位下标要跟着 −/+ 和滑块一起动，所以必须是 let（以前写成 const，
        // 点一下就在严格模式下抛 "Assignment to constant variable"，整排按钮全哑）
        let idx = Math.max(0, values.indexOf(Number(eff.duration) || values[0]));
        let range = null;
        const show = () => {
          out.textContent = `${values[idx] ?? values[0]}s`;
          // 到头了就把那侧按钮置灰：让用户一眼看出「已经是最短档了」，而不是以为点坏了
          minus.disabled = idx <= 0;
          plus.disabled = idx >= values.length - 1;
        };
        const apply = () => {
          idx = Math.max(0, Math.min(values.length - 1, idx));
          gen.duration = values[idx];
          markDirty();
          show();
          if (range) range.value = String(idx);
          refreshComposerChips(node, 'video');
        };
        minus.addEventListener('click', () => { if (idx > 0) { idx -= 1; apply(); } });
        plus.addEventListener('click', () => { if (idx < values.length - 1) { idx += 1; apply(); } });
        show();
        row.append(minus, out, plus);
        el.appendChild(row);

        // 滑块：档位是离散的，滑块按档位下标走
        range = document.createElement('input');
        range.type = 'range';
        range.className = 'nc-dur-range';
        range.min = '0';
        range.max = String(values.length - 1);
        range.step = '1';
        range.value = String(idx);
        range.addEventListener('input', () => {
          idx = Number(range.value);
          apply();
        });
        el.appendChild(range);
      }
    }
  }

  /* ------------------------------ 数量浮层 ------------------------------ */

  function openCountPicker(anchor, node) {
    const gen = (node.gen ||= {});
    openPicker(anchor, (el) => {
      pickerSection(el, '生成数量');
      for (const n of [1, 2, 3, 4]) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = `nc-picker-item${Number(gen.count || 1) === n ? ' on' : ''}`;
        item.innerHTML = `<span class="nc-picker-main"><span class="nc-picker-name">× ${n}</span></span>${Number(gen.count || 1) === n ? '<span class="nc-picker-check">✓</span>' : ''}`;
        item.addEventListener('click', () => {
          closePicker();
          gen.count = n;
          markDirty();
          refreshComposerChips(node, 'image');
        });
        el.appendChild(item);
      }
    });
  }

  /* ------------------------------ 豆包客户端 ------------------------------ */

  /**
   * 豆包这条路不靠 API Key，靠本机客户端开着调试端口 —— 客户端没以
   * `--remote-debugging-port` 起来时，生成一定失败。以前只在设置页里有「重启豆包」，
   * 在画布上点生成就只看到一句「连不上豆包」，所以这里直接把开关放到编辑条里。
   */
  const DOUBAO_DEFAULT_ENDPOINT = 'http://127.0.0.1:9555';

  function doubaoEndpointOf(node, kind) {
    const model = modelOf(node, kind);
    if (model?.protocol !== 'doubao') return '';
    return model.endpoint || model.baseUrl || DOUBAO_DEFAULT_ENDPOINT;
  }

  function mountDoubaoHint(wrap, node, kind) {
    const endpoint = doubaoEndpointOf(node, kind);
    if (!endpoint) return;

    const hint = document.createElement('div');
    hint.className = 'nc-hint';
    hint.hidden = true;
    const text = document.createElement('span');
    text.className = 'nc-hint-text';
    const act = document.createElement('button');
    act.type = 'button';
    act.className = 'nc-hint-act';
    act.textContent = '一键启动豆包';
    hint.append(text, act);

    const show = (msg, withButton) => {
      hint.hidden = false;
      text.textContent = msg;
      act.hidden = !withButton;
      act.disabled = false;
    };

    act.addEventListener('click', async () => {
      act.disabled = true;
      act.textContent = '正在启动…';
      text.textContent = `正在把豆包切到调试端口 ${endpoint}，大约 20 秒…`;
      try {
        const res = await fetch('/api/doubao/restart', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint }),
        });
        const out = await res.json().catch(() => ({}));
        if (!res.ok || !out.ok) throw new Error(out.error || out.detail || `HTTP ${res.status}`);
        hint.hidden = true;
        toast('豆包已启动，现在可以点生成');
      } catch (err) {
        show(`启动失败：${err.message}`, true);
        act.textContent = '一键启动豆包';
      }
    });

    // 只探一次：面板每次打开探一下够了，别做成轮询（轮询会把手机热点/CPU 拖起来）
    (async () => {
      try {
        const res = await fetch(`/api/doubao/status?endpoint=${encodeURIComponent(endpoint)}`);
        const out = await res.json();
        if (out.ok) return;
        show(`豆包客户端没在调试模式运行（连不上 ${endpoint.split('//')[1]}），生成会失败`, true);
      } catch {
        /* 服务端都没起来时忽略，让生成流程自己报错 */
      }
    })();

    wrap.insertBefore(hint, wrap.querySelector('.nc-composer-bar'));
  }

  /* ------------------------------ 编辑区主体 ------------------------------ */

  function renderComposer(node, body, kind) {
    const gen = (node.gen ||= {});
    const isVideo = kind === 'video';
    // 还没选过模型（新建节点 / 旧数据）：默认落到第一个可用模型，
    // 否则点「生成」会立刻报「请先选择模型」
    const enabled = modelsOf(kind).filter((m) => m.enabled);
    if (enabled.length && !enabled.some((m) => m.id === gen.modelId)) gen.modelId = enabled[0].id;
    const wrap = document.createElement('div');
    wrap.className = 'nc-composer';
    wrap.dataset.node = node.id;
    wrap.dataset.kind = kind;

    // 参考图缩略条（上游连进来的图片；首帧图对视频就是它）
    const refs = document.createElement('div');
    refs.className = 'nc-refs';
    refs.dataset.sig = refSig(node);
    wrap.appendChild(refs);

    // 视频节点：首帧 / 尾帧到底用的哪两张图，写在面板最上面 ——
    // 一段视频吃两张图，光看连线分不清谁先谁后，这里直接点名
    if (isVideo) wrap.appendChild(buildFrameBar(node));

    // 提示词：面板里唯一的大输入区
    const prompt = document.createElement('textarea');
    prompt.className = 'nc-composer-input';
    prompt.dataset.role = 'prompt';
    prompt.placeholder = isVideo
      ? '描述你想要的镜头，点击 @ 添加参考内容'
      : '描述你想要生成的内容，点击 @ 添加参考内容';
    prompt.rows = isVideo ? 4 : 5;
    prompt.value = gen.prompt || '';
    prompt.addEventListener('input', () => {
      gen.prompt = prompt.value;
      markDirty();
    });
    bindPromptMention(prompt, node, gen);
    wrap.appendChild(prompt);
    renderRefChips(refs, node, gen, prompt);
    migrateRefTokens(node, gen, prompt);

    // 结果预览（图片带「变成图片节点」，方便继续往下游连）
    if (gen.resultUrl) wrap.appendChild(buildResultPreview(node, kind));

    // 底栏：模型 / 参数 / 数量 … 状态 / 生成
    const bar = document.createElement('div');
    bar.className = 'nc-composer-bar';

    const modelBtn = document.createElement('button');
    modelBtn.type = 'button';
    modelBtn.className = 'nc-chip nc-chip-model';
    modelBtn.dataset.chip = 'model';
    modelBtn.title = '选择模型';
    modelBtn.innerHTML = `<span class="nc-chip-dot">${modelGlyph(kind)}</span><span class="nc-chip-text">${esc(modelOf(node, kind)?.name || '选模型')}</span>`;
    modelBtn.addEventListener('click', () => openModelPicker(modelBtn, node, kind));
    bar.appendChild(modelBtn);

    const paramBtn = document.createElement('button');
    paramBtn.type = 'button';
    paramBtn.className = 'nc-chip';
    paramBtn.dataset.chip = 'params';
    paramBtn.title = isVideo ? '比例 / 清晰度 / 时长' : '比例 / 尺寸';
    paramBtn.innerHTML = `<span class="nc-chip-text">${esc(paramSummary(node, kind) || (isVideo ? '参数' : '尺寸'))}</span>`;
    paramBtn.addEventListener('click', () => openParamPicker(paramBtn, node, kind));
    bar.appendChild(paramBtn);

    if (!isVideo) {
      const countBtn = document.createElement('button');
      countBtn.type = 'button';
      countBtn.className = 'nc-chip';
      countBtn.dataset.chip = 'count';
      countBtn.title = '生成数量';
      countBtn.innerHTML = `<span class="nc-chip-text">× ${Number(gen.count || 1)}</span>`;
      countBtn.addEventListener('click', () => openCountPicker(countBtn, node));
      bar.appendChild(countBtn);
    }

    const status = document.createElement('span');
    status.className = `nc-gen-status ${gen.status === 'failed' ? 'err' : gen.status === 'success' ? 'ok' : ''}`;
    status.textContent =
      gen.error
      || (PENDING.has(gen.status) && gen.progress ? gen.progress : '')
      || STATUS_LABEL[gen.status]
      || '';
    bar.appendChild(status);

    // 已经有结果了，这一下就是「重新生成」—— 文案和图标都得跟着变，
    // 否则用户看不出那个 ↑ 还能再点一次
    const done = Boolean(gen.resultUrl);
    if (gen.revise) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'nc-chip nc-chip-revise';
      chip.title = `这次的要求：${gen.revise}`;
      chip.innerHTML = `<span class="nc-chip-text">改：${esc(gen.revise)}</span>`;
      chip.addEventListener('click', () => regenerateNode(node));
      bar.appendChild(chip);
    }

    const run = document.createElement('button');
    run.type = 'button';
    run.className = 'nc-send';
    run.dataset.act = 'run';
    run.title = PENDING.has(gen.status)
      ? '生成中…'
      : done
        ? '重新生成（可以顺便说说这次想改什么）'
        : isVideo ? '生成视频' : '生成图片';
    run.disabled = PENDING.has(gen.status);
    run.innerHTML = done
      ? '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M13 8a5 5 0 1 1-1.6-3.7"/><path d="M13 2.5V5h-2.5"/></svg>'
      : '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 12.5V3.5M4 7l4-3.5L12 7"/></svg>';
    // 有结果时点它走「先问改什么」的入口；没结果还是直接开跑，别多一步
    run.addEventListener('click', () => (done ? regenerateNode(node) : isVideo ? runVideoGen(node) : runImageGen(node)));
    bar.appendChild(run);

    wrap.appendChild(bar);

    // 豆包模型：客户端没开调试端口时，在生成按钮上方给一条能直接点的提示
    mountDoubaoHint(wrap, node, kind);

    body.appendChild(wrap);
  }

  /**
   * 视频节点的「首帧 / 尾帧」状态条。
   *
   * 一段视频从上一张图走到下一张图，所以这里要点名是哪两张：
   * 没连首帧直接说缺（点了生成也就一句「需要首帧图」），
   * 尾帧连了但模型不吃时如实说「写进提示词了」，别让人以为尾帧生效了。
   */
  function buildFrameBar(node) {
    const { start, end } = frameNodes(node);
    const takes = modelTakesLastFrame(node.gen?.modelId);
    const bar = document.createElement('div');
    bar.className = 'nc-frames';

    const row = (role, src, fallback) => {
      const line = document.createElement('div');
      line.className = `nc-frame-row${src ? '' : ' miss'}`;
      const tag = document.createElement('span');
      tag.className = `nc-frame-tag ${role === 'start' ? 'edge-start' : 'edge-end'}`;
      tag.textContent = EDGE_ROLE_LABEL[role];
      const name = document.createElement('span');
      name.className = 'nc-frame-name';
      // 连上了但还没出图：说清楚是「还没生成」，别让人以为连线丢了
      name.textContent = src
        ? `${src.title || src.fileName || '图片'}${mediaUrlOf(src) ? '' : '（还没生成）'}`
        : fallback;
      line.append(tag, name);
      if (mediaUrlOf(src)) {
        const thumb = document.createElement('img');
        thumb.src = mediaUrlOf(src);
        thumb.alt = '';
        thumb.draggable = false;
        line.appendChild(thumb);
      }
      return line;
    };

    // 首帧真正在用的是哪张：上一段的最后一帧（接力）> 本镜的镜头图
    const takesTwo = modelTakesSecondImage(node.gen?.modelId);
    const relay = relayFrameOf(node);
    if (relay?.url) {
      const line = document.createElement('div');
      line.className = 'nc-frame-row';
      const tag = document.createElement('span');
      tag.className = 'nc-frame-tag edge-start';
      tag.textContent = EDGE_ROLE_LABEL.start;
      const name = document.createElement('span');
      name.className = 'nc-frame-name';
      name.textContent = '上一段视频的最后一帧（接力：接缝处同一个画面）';
      line.append(tag, name);
      const thumb = document.createElement('img');
      thumb.src = relay.url;
      thumb.alt = '';
      thumb.draggable = false;
      line.appendChild(thumb);
      bar.appendChild(line);
    } else {
      bar.appendChild(row('start', start, '还没连首帧图'));
      if (relay) {
        const note = document.createElement('div');
        note.className = 'nc-frame-note';
        note.textContent = '上一段视频出片后，这一段的首帧会自动换成它的最后一帧（现在先用镜头图）';
        bar.appendChild(note);
      }
    }
    // 第二张图不是必须的：只连一张图就是普通的单图图生视频
    bar.appendChild(row(
      'end',
      end,
      takesTwo ? '再连一张图当目标画面（可选）' : '这个模型不收第二张图，落点已写进提示词',
    ));
    if (end && !takes) {
      const note = document.createElement('div');
      note.className = 'nc-frame-note';
      note.textContent = takesTwo
        ? '目标画面已连：会和首帧图一起塞给模型，当「这段结束时要走到哪」的引导'
        : '尾帧已连，但这个模型只认首帧 —— 终点靠提示词里那句「最后一帧要落到…」';
      bar.appendChild(note);
    }
    return bar;
  }

  function buildResultPreview(node, kind) {
    const gen = node.gen || {};
    const preview = document.createElement('div');
    preview.className = 'nc-gen-result';
    if (kind === 'video') {
      const video = document.createElement('video');
      video.src = gen.resultUrl;
      video.controls = true;
      video.preload = 'metadata';
      preview.appendChild(video);
      return preview;
    }
    const img = document.createElement('img');
    img.src = gen.resultUrl;
    img.alt = '';
    img.draggable = false;
    preview.appendChild(img);
    const bar = document.createElement('div');
    bar.className = 'nc-gen-result-bar';
    const toNode = document.createElement('button');
    toNode.type = 'button';
    toNode.className = 'btn tiny';
    toNode.textContent = '变成图片节点';
    toNode.title = '在右侧生成一个图片节点，方便继续连线';
    toNode.addEventListener('click', () => spawnFromResult(node));
    bar.appendChild(toNode);
    preview.appendChild(bar);
    return preview;
  }

  /** 只更新底栏胶囊文案（改参数时不重建 DOM，免得输入框失焦） */
  function refreshComposerChips(node, kind) {
    const wrap = $(`.nc-composer[data-node="${node.id}"]`);
    if (!wrap) return;
    const gen = node.gen || {};
    const modelText = wrap.querySelector('[data-chip="model"] .nc-chip-text');
    if (modelText) modelText.textContent = modelOf(node, kind)?.name || '选模型';
    const paramText = wrap.querySelector('[data-chip="params"] .nc-chip-text');
    if (paramText) paramText.textContent = paramSummary(node, kind) || (kind === 'video' ? '参数' : '尺寸');
    const countText = wrap.querySelector('[data-chip="count"] .nc-chip-text');
    if (countText) countText.textContent = `× ${Number(gen.count || 1)}`;
    const status = wrap.querySelector('.nc-gen-status');
    if (status) {
      status.textContent = gen.error
        || (PENDING.has(gen.status) && gen.progress ? gen.progress : '')
        || STATUS_LABEL[gen.status]
        || '';
      status.className = `nc-gen-status ${gen.status === 'failed' ? 'err' : gen.status === 'success' ? 'ok' : ''}`;
    }
  }

  function renderImageGenBody(node, body) {
    renderComposer(node, body, 'image');
  }

  function renderVideoGenBody(node, body) {
    renderComposer(node, body, 'video');
  }

  /* --------------------------------- 连线渲染 -------------------------------- */

  /** 端口圆心：横向贴节点左右边，纵向对齐卡片头（.nc-port 的 top:13px + 半径 7px = 20px） */
  function portPoint(node, side) {
    return {
      x: side === 'out' ? node.x + node.w : node.x,
      y: node.y + PORT_Y,
    };
  }

  /**
   * 连线路径。
   * 正向（目标在起点右边）走标准 S 形。
   * 反向（目标在起点左边，比如把下游节点拖到了上游左侧）如果还沿用正向公式，
   * 两个控制点会各往外甩出半个画布宽度，曲线绕一大圈还压在节点上，
   * 视觉上就像「这条线没画出来」。这里改成从右侧出、绕到下方再折回来。
   */
  function edgePath(from, to) {
    const a = portPoint(from, 'out');
    const b = portPoint(to, 'in');
    const span = b.x - a.x;
    // 近垂直（含完全对齐）也走 S 形，避免为了一点点负偏移画个大圈
    if (span >= -20) {
      const dx = Math.max(40, Math.abs(span) * 0.45);
      return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
    }
    // 反向：两边各往外让出一段，再按垂直方向绕过去，避免控制点跨出画布
    const outX = 64;
    const below = b.y >= a.y;
    const detour = Math.min(160, Math.max(56, Math.abs(b.y - a.y) * 0.22));
    const dir = below ? 1 : -1;
    return `M ${a.x} ${a.y} C ${a.x + outX} ${a.y + dir * detour}, ${b.x - outX} ${b.y + dir * detour}, ${b.x} ${b.y}`;
  }

  function renderEdges() {
    const svg = $('#ncEdges');
    if (!svg || !S.graph) return;
    svg.textContent = '';
    for (const edge of S.graph.edges) {
      const from = nodeById(edge.from);
      const to = nodeById(edge.to);
      if (!from || !to) continue;
      if (!edgeVisible(edge)) continue;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', edgePath(from, to));
      const classes = ['nc-edge'];
      // 首帧 / 尾帧：一段视频吃两张图，连线得能看出哪条是起点、哪条是终点
      if (edge.role === 'start' || edge.role === 'end') classes.push(edge.role === 'start' ? 'edge-start' : 'edge-end');
      if (S.selectedEdge === edge.id || S.selectedEdges?.includes(edge.id)) classes.push('selected');
      // 生成中的连线画成流动虚线：一眼看出这条素材正在被消费
      const running = PENDING.has(to.gen?.status);
      if (running) classes.push('running');
      path.setAttribute('class', classes.join(' '));
      // 连线 5 色：只有用户显式选过才写 data-color，默认仍是灰（见 nodecanvas.css）
      if (edge.color) path.dataset.color = edge.color;
      if (edge.role) path.dataset.role = edge.role;
      // 线宽按屏幕像素算，放大画布时连线不会跟着变粗
      path.setAttribute('vector-effect', 'non-scaling-stroke');
      path.dataset.id = edge.id;
      // 生成中的连线下面垫一层虚化光带（#C996FF），让它在满屏卡片里"亮"出来。
      // 光带只做视觉，所以整层 pointer-events:none，命中判定的活仍归 .nc-edge-hit。
      if (running) {
        const glow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        glow.setAttribute('d', path.getAttribute('d'));
        glow.setAttribute('class', 'nc-edge-glow');
        glow.setAttribute('vector-effect', 'non-scaling-stroke');
        svg.appendChild(glow);
      }
      const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      hit.setAttribute('d', path.getAttribute('d'));
      hit.setAttribute('class', 'nc-edge-hit');
      hit.setAttribute('vector-effect', 'non-scaling-stroke');
      hit.dataset.id = edge.id;
      svg.append(path, hit);
      // 首帧 / 尾帧的文字标签：只标视频那两条，普通参考图连线不标（不然满屏都是字）
      if (edge.role === 'start' || edge.role === 'end') {
        const a = portPoint(from, 'out');
        const b = portPoint(to, 'in');
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('class', `nc-edge-label ${edge.role === 'start' ? 'edge-start' : 'edge-end'}`);
        label.setAttribute('x', (a.x + b.x) / 2);
        label.setAttribute('y', (a.y + b.y) / 2 - 8);
        // 画布缩放时文字不跟着放大缩小到看不见：按缩放反补偿
        label.setAttribute('font-size', String(11 / (S.graph?.viewport?.zoom || 1)));
        label.textContent = EDGE_ROLE_LABEL[edge.role];
        svg.appendChild(label);
      }
    }
    if (S.connecting?.tempPath) svg.appendChild(S.connecting.tempPath);
    // 连线变了，生图节点上的引用缩略图要跟着变
    syncRefChips();
    updateStatusBar();
    // 拖拽节点时位置在变，多选浮动条要跟着选区漂
    updateSelBar();
  }

  function updateNodePosition(node) {
    const el = $(`.nc-node[data-id="${node.id}"]`);
    if (!el) return;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.style.width = `${node.w}px`;
    el.style.height = `${node.h}px`;
  }

  /* ------------------------------ 框选 / 对齐 / 打组 ----------------------------- */

  /** 框选矩形：屏幕坐标转世界坐标，取与节点相交的 */
  function marqueeSelect(x1, y1, x2, y2) {
    const a = screenToWorld(x1, y1);
    const b = screenToWorld(x2, y2);
    const rx = Math.min(a.x, b.x), ry = Math.min(a.y, b.y);
    const rw = Math.abs(a.x - b.x), rh = Math.abs(a.y - b.y);
    S.selected.clear();
    for (const node of S.graph?.nodes || []) {
      if (node.x < rx + rw && node.x + node.w > rx && node.y < ry + rh && node.y + node.h > ry) {
        S.selected.add(node.id);
      }
    }
    expandSelectionByGroup();
    // 框选连线：检查连线中点是否在选框内
    const marqueeEdges = [];
    for (const edge of S.graph?.edges || []) {
      const from = nodeById(edge.from);
      const to = nodeById(edge.to);
      if (!from || !to) continue;
      const a = portPoint(from, 'out');
      const b = portPoint(to, 'in');
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      if (mx >= rx && mx <= rx + rw && my >= ry && my <= ry + rh) {
        marqueeEdges.push(edge.id);
      }
    }
    if (marqueeEdges.length === 1) S.selectedEdge = marqueeEdges[0];
    else if (marqueeEdges.length > 1) S.selectedEdges = marqueeEdges;
    else { S.selectedEdge = ''; S.selectedEdges = []; }
    syncSelection();
  }

  function showMarquee(sx, sy, ex, ey) {
    const el = $('#ncMarquee');
    if (!el) return;
    el.hidden = false;
    const rect = $('#ncViewport').getBoundingClientRect();
    el.style.left = `${Math.min(sx, ex) - rect.left}px`;
    el.style.top = `${Math.min(sy, ey) - rect.top}px`;
    el.style.width = `${Math.abs(ex - sx)}px`;
    el.style.height = `${Math.abs(ey - sy)}px`;
  }

  function hideMarquee() {
    const el = $('#ncMarquee');
    if (el) el.hidden = true;
  }

  /** 拖拽时贴齐：边缘 / 中心对齐，阈值 6px（屏幕像素） */
  function showGuides(movingIds) {
    const nodes = S.graph?.nodes || [];
    if (!S.snapOn) { hideGuides(); return; }
    const others = nodes.filter((n) => !movingIds.has(n.id));
    const guidesEl = $('#ncGuides');
    const gv = $('#ncGuideV');
    const gh = $('#ncGuideH');
    if (!others.length || !guidesEl || !gv || !gh) {
      hideGuides();
      return;
    }
    const moved = nodes.filter((n) => movingIds.has(n.id));
    if (!moved.length) { hideGuides(); return; }

    const T = 6 / (S.graph.viewport.zoom || 1); // 世界坐标下的阈值
    const movedBox = {
      x1: Math.min(...moved.map((n) => n.x)),
      y1: Math.min(...moved.map((n) => n.y)),
      x2: Math.max(...moved.map((n) => n.x + n.w)),
      y2: Math.max(...moved.map((n) => n.y + n.h)),
      cx: 0, cy: 0,
    };
    movedBox.cx = (movedBox.x1 + movedBox.x2) / 2;
    movedBox.cy = (movedBox.y1 + movedBox.y2) / 2;

    let bestV = null, bestH = null;
    for (const n of others) {
      const bx1 = n.x, by1 = n.y, bx2 = n.x + n.w, by2 = n.y + n.h;
      const bcx = (bx1 + bx2) / 2, bcy = (by1 + by2) / 2;
      const tryV = [
        [movedBox.x1, bx1], [movedBox.x1, bx2], [movedBox.cx, bcx],
        [movedBox.x2, bx1], [movedBox.x2, bx2],
      ];
      for (const [mv, bv] of tryV) {
        const d = Math.abs(mv - bv);
        if (d < T && (!bestV || d < bestV.d)) bestV = { d, from: mv, to: bv, delta: bv - mv };
      }
      const tryH = [
        [movedBox.y1, by1], [movedBox.y1, by2], [movedBox.cy, bcy],
        [movedBox.y2, by1], [movedBox.y2, by2],
      ];
      for (const [mv, bv] of tryH) {
        const d = Math.abs(mv - bv);
        if (d < T && (!bestH || d < bestH.d)) bestH = { d, from: mv, to: bv, delta: bv - mv };
      }
    }

    if (bestV) {
      const vp = S.graph.viewport;
      const screenX = bestV.to * vp.zoom + vp.x;
      gv.style.left = `${screenX}px`;
      gv.hidden = false;
    } else gv.hidden = true;

    if (bestH) {
      const vp = S.graph.viewport;
      const screenY = bestH.to * vp.zoom + vp.y;
      gh.style.top = `${screenY}px`;
      gh.hidden = false;
    } else gh.hidden = true;

    guidesEl.hidden = !(bestV || bestH);
    S.guides = { v: bestV, h: bestH };
  }

  function hideGuides() {
    const el = $('#ncGuides');
    if (el) el.hidden = true;
    S.guides = null;
  }

  /** 应用对齐吸附：移动结束后根据辅助线微调位置 */
  function snapByGuides(movingIds) {
    if (!S.snapOn || !S.guides) return;
    const { v, h } = S.guides;
    if (!v && !h) return;
    const dx = v?.delta || 0, dy = h?.delta || 0;
    if (!dx && !dy) return;
    for (const id of movingIds) {
      const n = nodeById(id);
      if (!n) continue;
      n.x += dx;
      n.y += dy;
      updateNodePosition(n);
    }
    renderEdges();
    markDirty();
  }

  /** 把选中的节点打包成一个组：移动一个 = 整组一起动 */
  function groupSelected() {
    const ids = [...S.selected];
    if (ids.length < 2) return toast('至少选中 2 个节点才能打组', true);
    const gid = uid('grp');
    for (const id of ids) {
      const n = nodeById(id);
      if (n) n.groupId = gid;
    }
    markDirty();
    toast(`已打组 ${ids.length} 个节点`);
  }

  function ungroupSelected() {
    const ids = [...S.selected];
    let count = 0;
    for (const id of ids) {
      const n = nodeById(id);
      if (n?.groupId) {
        delete n.groupId;
        count += 1;
      }
    }
    if (count) {
      markDirty();
      toast(`已取消 ${count} 个节点的分组`);
    } else {
      toast('选中的节点不在任何组里', true);
    }
  }

  /** 点中一个带组节点时，把整组都选进来 */
  function expandSelectionByGroup() {
    const extra = new Set();
    for (const id of S.selected) {
      const n = nodeById(id);
      if (!n?.groupId) continue;
      for (const m of S.graph.nodes) if (m.groupId === n.groupId) extra.add(m.id);
    }
    for (const id of extra) S.selected.add(id);
  }

  /* ------------------------------- 右键菜单 -------------------------------- */

  function closeMenu() {
    const el = $('#ncMenu');
    if (el) el.hidden = true;
    S.menu = null;
  }

  function menuItem(label, handler, danger = false, hint = '') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `nc-menu-item${danger ? ' danger' : ''}`;
    const text = document.createElement('span');
    text.className = 'nc-menu-label';
    text.textContent = label;
    btn.appendChild(text);
    if (hint) {
      const k = document.createElement('kbd');
      k.className = 'nc-menu-hint';
      k.textContent = hint;
      btn.appendChild(k);
    }
    btn.addEventListener('click', () => {
      closeMenu();
      handler();
    });
    return btn;
  }

  function openMenu(items, x, y) {
    closeMenu();
    const el = $('#ncMenu');
    if (!el) return;
    el.textContent = '';
    for (const item of items) {
      if (item === '-') {
        const sep = document.createElement('div');
        sep.className = 'nc-menu-sep';
        el.appendChild(sep);
      } else if (item.label) {
        const btn = menuItem(item.label, item.handler, item.danger, item.hint);
        if (item.disabled) btn.disabled = true;
        el.appendChild(btn);
      }
    }
    el.hidden = false;
    const rect = $('#ncViewport').getBoundingClientRect();
    const menuW = el.offsetWidth || 160;
    const menuH = el.offsetHeight || 200;
    let left = x - rect.left;
    let top = y - rect.top;
    if (left + menuW > rect.width) left = rect.width - menuW - 4;
    if (top + menuH > rect.height) top = rect.height - menuH - 4;
    el.style.left = `${Math.max(4, left)}px`;
    el.style.top = `${Math.max(4, top)}px`;
    S.menu = true;
  }

  function nodeMenu(node, clientX, clientY) {
    const inGroup = Boolean(node.groupId);
    const multi = S.selected.size > 1;
    const items = [
      { label: '创建副本', hint: 'Ctrl+D', handler: () => duplicateNode(node.id) },
      { label: '拷贝', hint: 'Ctrl+C', handler: () => copySelection() },
      { label: '粘贴', hint: 'Ctrl+V', handler: () => pasteClipboard(), disabled: !S.clipboard },
    ];
    // 生图 / 生视频节点：重新生成放最上面。这是最常用的动作，
    // 之前只有底栏那个 ↑ 能重跑，图标看不出是「重跑」，等于藏起来了
    if (node.type === 'imageGen' || node.type === 'videoGen') {
      const pending = PENDING.has(node.gen?.status);
      items.unshift('-');
      items.unshift({
        label: pending ? '重新生成（生成中…）' : '重新生成…',
        hint: '说说这次想改什么',
        handler: () => regenerateNode(node),
        disabled: pending,
      });
    }
    if (inGroup) {
      items.push('-');
      items.push({ label: '取消打组', hint: 'Ctrl+Shift+G', handler: () => ungroupSelected() });
    } else if (S.selected.size >= 2) {
      items.push('-');
      items.push({ label: `打组（${S.selected.size} 个）`, hint: 'Ctrl+G', handler: () => groupSelected() });
    }
    items.push('-');
    items.push({ label: '选中上游', hint: 'Alt+↑', handler: () => selectLinked('up') });
    items.push({ label: '选中下游', hint: 'Alt+↓', handler: () => selectLinked('down') });
    items.push('-');
    items.push({ label: multi ? '缩放到选中' : '缩放到此节点', hint: 'Shift+F', handler: () => (multi ? zoomToSelection() : focusNode(node.id)) });
    items.push({ label: '适应视图', hint: 'F', handler: () => fitView() });
    items.push('-');
    items.push({ label: '删除', hint: 'Del', handler: () => deleteNode(node.id), danger: true });
    openMenu(items, clientX, clientY);
  }

  function canvasMenu(clientX, clientY) {
    const world = screenToWorld(clientX, clientY);
    const empty = !S.graph.nodes.length;
    openMenu([
      { label: '新建文本节点', handler: () => addNode('text', { at: world }) },
      { label: '新建图片节点', handler: () => addNode('image', { at: world }) },
      { label: '新建视频节点', handler: () => addNode('video', { at: world }) },
      { label: '新建 AI 生图节点', handler: () => addNode('imageGen', { at: world }) },
      { label: '新建 AI 生视频节点', handler: () => addNode('videoGen', { at: world }) },
      '-',
      { label: '粘贴', hint: 'Ctrl+V', handler: () => pasteClipboard(), disabled: !S.clipboard },
      { label: '全选节点', hint: 'Ctrl+A', handler: () => { S.selected = new Set(S.graph.nodes.map((n) => n.id)); S.selectedEdge = ''; syncSelection(); }, disabled: empty },
      '-',
      { label: '一键整理', hint: 'T', handler: () => tidyLayout('grid'), disabled: empty },
      { label: '整理成单行', handler: () => tidyLayout('row'), disabled: empty },
      { label: '整理成单列', handler: () => tidyLayout('col'), disabled: empty },
      { label: '按连线层级排版', handler: () => tidyLayout('layer'), disabled: empty },
      '-',
      { label: '适应视图', hint: 'F', handler: () => fitView() },
      { label: `连线：${EDGE_MODE_LABEL[S.edgesMode]}`, hint: 'L', handler: () => setEdgesMode(EDGE_MODE_NEXT[S.edgesMode]) },
      { label: S.snapOn ? '关闭对齐吸附' : '开启对齐吸附', hint: 'S', handler: () => setSnapOn(!S.snapOn) },
      { label: S.gridOn ? '隐藏底纹' : '显示底纹', hint: 'G', handler: () => setGridOn(!S.gridOn) },
      { label: S.minimapOn ? '关闭小地图' : '打开小地图', hint: 'M', handler: () => setMinimapOn(!S.minimapOn) },
      { label: '画布皮肤…', handler: () => openSkinPanel() },
      '-',
      { label: '命令面板', hint: 'Ctrl+K', handler: () => openCmd() },
      { label: '快捷键一览', hint: '?', handler: () => openHelp() },
    ], clientX, clientY);
  }

  /* --------------------------------- 选择/操作 -------------------------------- */

  function syncSelection() {
    for (const el of $$('.nc-node')) {
      el.classList.toggle('selected', S.selected.has(el.dataset.id));
    }
    renderEdges();
    updateSelBar();
    syncPanel();
  }

  /* --------------------------- 多选浮动工具条 / 对齐 --------------------------- */

  /** 换算：世界坐标 → 视口内屏幕坐标（#ncSelBar 是 .nc-viewport 的绝对定位子元素） */
  function worldToLocal(wx, wy) {
    const vp = S.graph?.viewport || { x: 0, y: 0, zoom: 1 };
    return { x: vp.x + wx * vp.zoom, y: vp.y + wy * vp.zoom };
  }

  function selectedNodes() {
    return [...S.selected].map(nodeById).filter(Boolean);
  }

  /**
   * 选中 ≥2 个节点时，把浮动工具条浮在选区正上方（对齐 MiniMax 的「N 节点 | 布局 | …」）。
   * 上方放不下就翻到选区下方，左右夹在视口内 —— 多选经常发生在屏幕边缘。
   */
  function updateSelBar() {
    const bar = $('#ncSelBar');
    if (!bar) return;
    const nodes = selectedNodes();
    if (nodes.length < 2) {
      bar.hidden = true;
      closeSelMenu();
      return;
    }
    const x1 = Math.min(...nodes.map((n) => n.x));
    const y1 = Math.min(...nodes.map((n) => n.y));
    const x2 = Math.max(...nodes.map((n) => n.x + n.w));
    const y2 = Math.max(...nodes.map((n) => n.y + n.h));
    const tl = worldToLocal(x1, y1);
    const br = worldToLocal(x2, y2);
    const count = $('#ncSelBarCount');
    if (count) count.textContent = `${nodes.length} 节点`;
    bar.hidden = false;
    const { w } = viewportSize();
    const bw = bar.offsetWidth || 300;
    let left = Math.round((tl.x + br.x) / 2 - bw / 2);
    left = Math.max(8, Math.min(Math.max(8, w - bw - 8), left));
    const above = Math.round(tl.y - 42);
    const below = Math.round(br.y + 10);
    bar.style.left = `${left}px`;
    bar.style.top = `${above >= 8 ? above : below}px`;
  }

  function closeSelMenu() {
    const menu = $('#ncSelMenu');
    if (menu) menu.hidden = true;
    $$('#ncSelBar button').forEach((b) => b.classList.remove('on'));
  }

  function openSelMenu(anchor) {
    const menu = $('#ncSelMenu');
    const bar = $('#ncSelBar');
    if (!menu || !bar) return;
    menu.hidden = false;
    const barRect = bar.getBoundingClientRect();
    const hostRect = $('#ncViewport').getBoundingClientRect();
    const mw = menu.offsetWidth || 118;
    let left = barRect.left - hostRect.left + (anchor.offsetLeft || 0);
    left = Math.max(8, Math.min(hostRect.width - mw - 8, left));
    menu.style.left = `${left}px`;
    menu.style.top = `${barRect.bottom - hostRect.top + 6}px`;
    anchor.classList.add('on');
  }

  /**
   * 对齐 / 等距：参照物是「选中集合的包围盒」——这是多选操作最符合直觉的语义
   * （相对画布或相对某个"主节点"都要用户先指定谁是基准，多一步就没人用了）。
   */
  function alignSelected(mode) {
    const nodes = selectedNodes();
    if (nodes.length < 2) return;
    pushHistory();
    const x1 = Math.min(...nodes.map((n) => n.x));
    const y1 = Math.min(...nodes.map((n) => n.y));
    const x2 = Math.max(...nodes.map((n) => n.x + n.w));
    const y2 = Math.max(...nodes.map((n) => n.y + n.h));
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;

    if (mode === 'hdist' || mode === 'vdist') {
      const horizontal = mode === 'hdist';
      const sorted = [...nodes].sort((a, b) => (horizontal ? a.x - b.x : a.y - b.y));
      const total = horizontal ? x2 - x1 : y2 - y1;
      const used = sorted.reduce((sum, n) => sum + (horizontal ? n.w : n.h), 0);
      const gap = (total - used) / (sorted.length - 1);
      let cursor = horizontal ? x1 : y1;
      for (const n of sorted) {
        if (horizontal) { n.x = Math.round(cursor); cursor += n.w + gap; }
        else { n.y = Math.round(cursor); cursor += n.h + gap; }
      }
    } else {
      for (const n of nodes) {
        if (mode === 'left') n.x = Math.round(x1);
        else if (mode === 'right') n.x = Math.round(x2 - n.w);
        else if (mode === 'hcenter') n.x = Math.round(cx - n.w / 2);
        else if (mode === 'top') n.y = Math.round(y1);
        else if (mode === 'bottom') n.y = Math.round(y2 - n.h);
        else if (mode === 'vcenter') n.y = Math.round(cy - n.h / 2);
      }
    }
    for (const n of nodes) updateNodePosition(n);
    markDirty();
    renderEdges();
    drawMinimap();
    updateSelBar();
  }

  function bindSelBar() {
    const bar = $('#ncSelBar');
    const menu = $('#ncSelMenu');
    if (!bar || bar.dataset.bound) return;
    bar.dataset.bound = '1';
    bar.addEventListener('click', (event) => {
      const btn = event.target.closest('button[data-selact]');
      if (!btn) return;
      event.stopPropagation();
      switch (btn.dataset.selact) {
        case 'layout': tidyLayout('grid'); break;
        case 'align':
          if (menu && !menu.hidden) { closeSelMenu(); break; }
          openSelMenu(btn);
          break;
        case 'group': groupSelected(); break;
        case 'copy': copySelection(); break;
        case 'dup': {
          const ids = [...S.selected];
          pushHistory();
          for (const id of ids) duplicateNode(id);
          markDirty();
          break;
        }
        case 'del': deleteSelected(); break;
        default: break;
      }
    });
    // 浮动条本身不该被画布的 pointerdown 当成空白
    bar.addEventListener('pointerdown', (event) => event.stopPropagation());
    menu?.addEventListener('pointerdown', (event) => event.stopPropagation());
    menu?.addEventListener('click', (event) => {
      const btn = event.target.closest('button[data-align]');
      if (!btn) return;
      alignSelected(btn.dataset.align);
      closeSelMenu();
      // 对齐后选区包围盒变了，浮动条要跟着挪（但保持菜单关闭）
    });
  }

  function selectNode(id, additive) {
    if (additive) {
      if (S.selected.has(id)) S.selected.delete(id);
      else S.selected.add(id);
    } else {
      S.selected.clear();
      S.selected.add(id);
    }
    expandSelectionByGroup();
    S.selectedEdge = '';
    syncSelection();
  }

  /**
   * 找一个不压住已有节点的位置。
   * 优先在当前视口里按「一卡宽 + 100」的格子从中心往外找空位 —— 新卡出现在眼前，
   * 画布不会自己乱跑；视口里实在排不下，才接在现有内容最右侧（MiniMax 的派生放置）。
   */
  function freeSpot(size, at) {
    const nodes = S.graph?.nodes || [];
    if (at) return { x: Math.round(at.x - size[0] / 2), y: Math.round(at.y - size[1] / 2) };
    const taken = (xx, yy) =>
      nodes.some(
        (n) =>
          xx < n.x + n.w + 16 &&
          xx + size[0] + 16 > n.x &&
          yy < n.y + n.h + 16 &&
          yy + size[1] + 16 > n.y,
      );
    const center = centerWorld();
    const centerSpot = { x: Math.round(center.x - size[0] / 2), y: Math.round(center.y - size[1] / 2) };
    if (!nodes.length || !taken(centerSpot.x, centerSpot.y)) return centerSpot;

    // 视口里按 450 模数扫格子：列间距 CARD_W + LAYOUT_GAP，行间距 = 卡高 + LAYOUT_GAP
    const stepX = CARD_W + LAYOUT_GAP;
    const stepY = Math.round((size[1] || 300) + LAYOUT_GAP);
    const vp = S.graph.viewport;
    const { w, h } = viewportSize();
    const padT = 72; // 让开顶部缩放条
    const padB = 86; // 让开底部工具条
    const worldL = (24 - vp.x) / vp.zoom;
    const worldT = (padT - vp.y) / vp.zoom;
    const worldR = (w - 24 - vp.x) / vp.zoom;
    const worldB = (h - padB - vp.y) / vp.zoom;
    const cols = Math.floor((worldR - worldL - size[0]) / stepX);
    const rows = Math.floor((worldB - worldT - size[1]) / stepY);
    if (cols >= 0 && rows >= 0) {
      const spots = [];
      for (let row = 0; row <= rows; row += 1) {
        for (let col = 0; col <= cols; col += 1) {
          const x = Math.round(worldL + col * stepX);
          const y = Math.round(worldT + row * stepY);
          const dist = Math.hypot(x + size[0] / 2 - center.x, y + size[1] / 2 - center.y);
          spots.push({ x, y, dist });
        }
      }
      spots.sort((a, b) => a.dist - b.dist);
      for (const spot of spots) if (!taken(spot.x, spot.y)) return { x: spot.x, y: spot.y };
    }

    const x = Math.round(Math.max(...nodes.map((n) => n.x + n.w)) + LAYOUT_GAP);
    const minY = Math.round(Math.min(...nodes.map((n) => n.y)));
    let y = minY;
    for (let i = 0; i < 40 && taken(x, y); i += 1) y += stepY;
    return { x, y };
  }

  /**
   * 新卡片可能落在视口外、或被浮层挡住（画布大了以后很容易），这时把视口挪最小的距离
   * 把它带进可视区。只做刚好够用的位移，不重设缩放 —— 用户当前的视距不该被新建操作改掉。
   */
  function ensureNodeVisible(node) {
    if (!S.graph || !node) return;
    const vp = S.graph.viewport;
    const { w, h } = viewportSize();
    // 四边留白不对称：右上角有缩放条（高 36 + 上边距 10），底部有浮动工具条（高 46 + 下边距 12）。
    // 都留够就不必再为「卡片被浮层盖住」做特例判断。
    const padL = 24;
    const padR = 24;
    const padT = 62;
    const padB = 72;
    const left = node.x * vp.zoom + vp.x;
    const top = node.y * vp.zoom + vp.y;
    const right = (node.x + node.w) * vp.zoom + vp.x;
    const bottom = (node.y + node.h) * vp.zoom + vp.y;
    let dx = 0;
    let dy = 0;
    if (right > w - padR) dx = w - padR - right;
    if (left + dx < padL) dx = padL - left;
    if (bottom > h - padB) dy = h - padB - bottom;
    if (top + dy < padT) dy = padT - top;
    if (!dx && !dy) return;
    vp.x += Math.round(dx);
    vp.y += Math.round(dy);
    applyViewport();
    markDirty();
  }

  function addNode(type, opts = {}) {
    if (!S.graph) return null;
    pushHistory();
    const size = NODE_SIZE[type] || [CARD_W, 240];
    // 素材拖进画布时落点就是鼠标位置；点按钮新建才自动找一个不重叠的空位
    const spot = opts.at
      ? { x: Math.round(opts.at.x - size[0] / 2), y: Math.round(opts.at.y - size[1] / 2) }
      : freeSpot(size);
    const node = {
      id: uid('nd'),
      type,
      x: spot.x,
      y: spot.y,
      w: size[0],
      h: size[1],
      title: NODE_LABEL[type],
      text: '',
      url: '',
      localUrl: '',
      localPath: '',
      fileName: '',
      assetId: '',
      gen: {},
      ...(opts.asset
        ? {
            url: opts.asset.localUrl,
            localUrl: opts.asset.localUrl,
            localPath: opts.asset.localPath,
            fileName: opts.asset.originalName,
            assetId: opts.asset.id,
          }
        : {}),
    };
    S.graph.nodes.push(node);
    markDirty();
    const el = buildNode(node);
    $('#ncNodes').appendChild(el);
    S.selected = new Set([node.id]);
    syncSelection();
    syncEmpty();
    // 点按钮新建时落点可能已经在视口外（画布变大了以后很常见），把视口挪回来一点
    if (!opts.at) ensureNodeVisible(node);
    drawMinimap();
    return node;
  }

  function deleteNode(id, skipHistory = false) {
    const index = S.graph.nodes.findIndex((n) => n.id === id);
    if (index < 0) return;
    if (!skipHistory) pushHistory();
    S.graph.nodes.splice(index, 1);
    S.graph.edges = S.graph.edges.filter((e) => e.from !== id && e.to !== id);
    S.selected.delete(id);
    // 删的正是面板里开着的节点：面板一起收
    if (S.panelNode === id) closePanel();
    markDirty();
    $(`.nc-node[data-id="${id}"]`)?.remove();
    renderEdges();
    syncEmpty();
    drawMinimap();
  }

  function duplicateNode(id) {
    const node = nodeById(id);
    if (!node) return;
    pushHistory();
    const copy = JSON.parse(JSON.stringify(node));
    copy.id = uid('nd');
    copy.x += 32;
    copy.y += 32;
    delete copy.groupId; // 复制出来的新节点不继承组，避免意外粘连
    if (copy.gen && PENDING.has(copy.gen.status)) {
      copy.gen.status = '';
      copy.gen.taskId = '';
      copy.gen.resultUrl = '';
    }
    S.graph.nodes.push(copy);
    markDirty();
    $('#ncNodes').appendChild(buildNode(copy));
    S.selected = new Set([copy.id]);
    syncSelection();
    drawMinimap();
  }

  function deleteSelected() {
    if (!S.graph) return;
    if (S.selectedEdges?.length) {
      pushHistory();
      const edgeSet = new Set(S.selectedEdges);
      S.graph.edges = S.graph.edges.filter((e) => !edgeSet.has(e.id));
      S.selectedEdge = '';
      S.selectedEdges = [];
      markDirty();
      renderEdges();
      drawMinimap();
      return;
    }
    if (S.selectedEdge) {
      pushHistory();
      S.graph.edges = S.graph.edges.filter((e) => e.id !== S.selectedEdge);
      S.selectedEdge = '';
      markDirty();
      renderEdges();
      drawMinimap();
      return;
    }
    if (!S.selected.size) return;
    pushHistory();
    for (const id of [...S.selected]) deleteNode(id, true);
    drawMinimap();
  }

  function syncEmpty() {
    const el = $('#ncEmpty');
    if (!el) return;
    el.hidden = Boolean(S.graph?.nodes?.length);
  }

  /* -------------------------------- 生成任务 -------------------------------- */

  function incomingNodes(node) {
    const ids = S.graph.edges.filter((e) => e.to === node.id).map((e) => e.from);
    return ids.map(nodeById).filter(Boolean);
  }

  /* ------------------------------ 首帧 / 尾帧 ------------------------------ */

  const EDGE_ROLE_LABEL = { start: '首帧', end: '尾帧', ref: '参考图' };

  /**
   * 能不能当一帧：只看节点类型。
   * 有没有真的出图是另一回事（镜头图铺出来时还没跑，mediaUrlOf 是空的）——
   * 那种情况要在界面上说「还没生成」，而不是当成没连线。
   */
  const isImageNode = (node) => Boolean(node) && ['image', 'imageGen'].includes(node.type);

  /**
   * 一个视频节点的两张入图：首帧（这一段从哪开始）+ 尾帧（这一段停在哪）。
   *
   * 成片是「图 1 → 视频 → 图 2 → 视频 → 图 3 …」串起来的，所以一段视频要吃两张图，
   * 连线必须能分清谁当首帧、谁当尾帧 —— 这就是 edge.role。
   * 老画布（只连了一张图的）没有 role：按顺序补位，第一条当首帧，第二条当尾帧。
   */
  function frameNodes(node) {
    const edges = (S.graph?.edges || []).filter((e) => e.to === node?.id);
    const byRole = (role) => {
      for (const edge of edges) {
        if ((edge.role || '') !== role) continue;
        const candidate = nodeById(edge.from);
        if (isImageNode(candidate)) return candidate;
      }
      return null;
    };
    let start = byRole('start');
    let end = byRole('end');
    // 没标过角色的连线按连线顺序补位（先补首帧，再补尾帧）
    const rest = edges.filter((e) => !e.role).map((e) => nodeById(e.from)).filter(isImageNode);
    let cursor = 0;
    if (!start && rest[cursor]) start = rest[cursor++];
    if (!end && rest[cursor]) end = rest[cursor++];
    return { start: start || null, end: end || null };
  }

  /** 新连一条线时它该当什么角色：视频节点的入边第一条是首帧、第二条是尾帧，再多算普通参考图 */
  function edgeRoleFor(fromId, toId) {
    const target = nodeById(toId);
    const source = nodeById(fromId);
    if (target?.type !== 'videoGen' || !isImageNode(source)) return '';
    const { start, end } = frameNodes(target);
    if (!start) return 'start';
    if (!end) return 'end';
    return 'ref';
  }

  /** 视频模型吃不吃尾帧（后端按模型 ID 判，前端拿快照里的标记） */
  function modelTakesLastFrame(modelId) {
    return Boolean(S.videoModels.find((m) => m.id === modelId)?.supportsLastFrame);
  }

  /** 这个模型值不值得喂第二张图：真吃尾帧的算，豆包这种「两张一起塞」的也算 */
  function modelTakesSecondImage(modelId) {
    return Boolean(S.videoModels.find((m) => m.id === modelId)?.takesSecondImage);
  }

  /**
   * 末帧接力：上一段视频出片后抽出来的最后一帧。
   *
   * 免费链路（豆包 / 智谱）都没有尾帧参数，一段视频的结尾是模型自己发挥的，
   * 于是「上一段的最后一帧」和「下一段的第一帧」永远对不上，成片在每个接缝处都跳一下。
   * 与其猜结尾，不如让下一段真的从上一帧接着走 —— 接缝两边是同一个画面，像素级连续。
   *
   * 上一段还没出片（或没抽到末帧）时返回 { url: '' }：调用方退回用镜头图，
   * 等上一段出片后重新打开这一段就会自动换成接力帧。
   */
  function relayFrameOf(node) {
    const from = Number(node?.agent?.fromShot || node?.agent?.shotIndex) || 0;
    if (!from) return null;
    const prev = agentNodes().find((n) => n.agent?.role === 'video' && Number(n.agent?.toShot) === from);
    if (!prev) return null;
    const task = (S.videoTasks || []).find((t) => t.id === prev.gen?.taskId);
    const frame = task?.lastFrame;
    if (!frame?.localUrl) return { from: prev, url: '', fileName: '' };
    return { from: prev, url: frame.localUrl, fileName: frame.fileName || `${frame.id}.png` };
  }

  /** 新建连线时附带的角色字段：普通连线不带，视频节点的入边才分首帧 / 尾帧 */
  function edgeRoleAttr(fromId, toId) {
    const role = edgeRoleFor(fromId, toId);
    return role ? { role } : {};
  }

  /**
   * 改一条连线的角色（首帧 / 尾帧 / 普通参考图）。
   * 同一段视频的首尾不能是同一条：改成首帧时，原来那条首帧自动让位成尾帧（反之亦然），
   * 这样两根线换一下就调头了，不用删了重连。
   */
  function setEdgeRole(edgeId, role) {
    const edge = (S.graph?.edges || []).find((e) => e.id === edgeId);
    if (!edge) return;
    pushHistory();
    // 同一段视频只能有一条首帧、一条尾帧：占着这个位置的那条自动让到另一头
    const other = (S.graph.edges || []).find((e) => e.to === edge.to && e.id !== edgeId
      && (e.role === 'start' || e.role === 'end' || !e.role));
    if (other && isImageNode(nodeById(other.from))) {
      other.role = role === 'start' ? 'end' : 'start';
    }
    edge.role = role;
    markDirty();
    renderEdges();
    drawMinimap();
    const node = nodeById(edge.to);
    if (node) rerenderNode(node);
    toast(`这条连线现在是${EDGE_ROLE_LABEL[role]}`);
  }

  function mediaUrlOf(node) {
    return node?.localUrl || node?.gen?.resultUrl || node?.url || '';
  }

  /** 上游文本节点的内容拼接成提示词；没有就返回空 */
  function upstreamPrompt(node) {
    return incomingNodes(node)
      .filter((n) => n.type === 'text' && String(n.text || '').trim())
      .map((n) => n.text.trim())
      .join('\n');
  }

  const MIME_EXT = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/webp': '.webp',
    'image/gif': '.gif',
    'video/mp4': '.mp4',
    'video/webm': '.webm',
    'audio/mpeg': '.mp3',
    'audio/wav': '.wav',
  };

  async function uploadBlobTo(route, blob, name) {
    // 生成结果节点的文件名可能没有扩展名，后端按扩展名判类型，这里补一个
    let fileName = String(name || 'file');
    const ext = MIME_EXT[blob.type] || '';
    if (ext && !/\.[a-z0-9]{2,5}$/i.test(fileName)) fileName += ext;
    const res = await fetch(route, {
      method: 'POST',
      headers: {
        'Content-Type': blob.type || 'application/octet-stream',
        'x-filename': encodeURIComponent(fileName),
        'x-mime': blob.type || '',
      },
      body: blob,
    });
    const text = await res.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch (err) {
      json = null;
    }
    if (!res.ok) throw new Error(json?.error || `上传失败（HTTP ${res.status}）`);
    return json;
  }

  async function uploadAssetFile(file) {
    return uploadBlobTo('/api/node-assets', file, file.name);
  }

  async function asImageRef(node) {
    const url = mediaUrlOf(node);
    if (!url) return '';
    const res = await fetch(url);
    if (!res.ok) throw new Error('上游图片读取失败，可能文件已被删除');
    const blob = await res.blob();
    const ref = await uploadBlobTo('/api/image-refs', blob, node.fileName || `${node.id}.png`);
    return ref?.id || '';
  }

  /**
   * 生成中的分步文案。
   * 一轮生成要经过「准备参考图 → 提交任务 → 服务端排队/生成 → 下载结果」，
   * 光写「生成中」看不出卡在哪一步（豆包桥那种一轮几十秒的尤其明显）。
   * 这里同时给出「当前步骤」和「第几步 / 共几步」，和 MiniMax 的
   * 「正在核对参数 / 正在上传参考图 (2/4)」是同一类信息。
   */
  const GEN_STEPS = {
    refs: { label: '准备参考图', total: 4 },
    submit: { label: '提交任务', total: 4 },
    run: { label: '生成中', total: 4 },
    download: { label: '下载结果', total: 4 },
  };

  function genStepText(step, detail) {
    const info = GEN_STEPS[step];
    if (!info) return String(detail || '');
    const index = Object.keys(GEN_STEPS).indexOf(step) + 1;
    const tail = detail ? ` · ${detail}` : '';
    return `${info.label}（第 ${index}/${info.total} 步）${tail}`;
  }

  async function runImageGen(node) {
    const gen = node.gen || (node.gen = {});
    if (!gen.modelId) return toast('请先在节点里选择图片模型', true);
    // 「这次的要求」单独存在 gen.revise 里，不改写 gen.prompt：
    // 提示词框里始终是用户自己写的原文，改要求就改那一条，不会越堆越长
    const base = String(gen.prompt || '').trim() || upstreamPrompt(node);
    // 提交前把角色名换成「图N」：模型只认第几张图，认不出名字（见 withRefNames）
    // 再补一句「房间以图N为准」：场景板连过来只是一张参考图，不点名模型不会当回事（见 withSceneRef）
    const prompt = withSceneRef(withRefNames(withRevise(base, gen.revise), node), node);
    if (!prompt) return toast('请填写提示词，或把文本节点连到生图节点的左侧', true);

    gen.status = 'queued';
    gen.error = '';
    gen.taskId = '';
    gen.resultUrl = '';
    gen.progress = genStepText('refs', '');
    markDirty();
    rerenderNode(node);

    try {
      // 顺序必须和提示词里的 [图N]、下面的缩略图一致：都用 refCandidates
      const refIds = [];
      const sources = refCandidates(node);
      for (let i = 0; i < sources.length; i += 1) {
        const src = sources[i];
        if (sources.length > 1) {
          // 这一步可能要传好几张大图，逐步报数，用户才知道不是卡死
          gen.progress = genStepText('refs', `已上传 ${i}/${sources.length}`);
          const statusEl = genStatusElOf(node);
          if (statusEl) statusEl.textContent = gen.progress;
        }
        const id = await asImageRef(src);
        if (id) refIds.push(id);
        if (refIds.length >= 10) break;
      }
      gen.progress = genStepText('submit', '');
      const statusEl = genStatusElOf(node);
      if (statusEl) statusEl.textContent = gen.progress;
      const task = await api('/api/image-tasks', {
        method: 'POST',
        body: {
          modelId: gen.modelId,
          prompt,
          size: gen.size || '',
          count: Math.max(1, Math.min(4, Number(gen.count) || 1)),
          refIds,
        },
      });
      gen.status = task.status || 'queued';
      gen.taskId = task.id;
      gen.progress = genProgressText(task) || genStepText('run', '');
      markDirty();
      rerenderNode(node);
    } catch (err) {
      gen.status = 'failed';
      gen.progress = '';
      gen.error = err.message;
      markDirty();
      rerenderNode(node);
      toast(`生图提交失败：${err.message}`, true);
    }
  }

  /** 服务端任务状态 → 卡片上的进度文案（服务端给了 progress 就优先用它） */
  function genProgressText(task) {
    if (!task) return '';
    if (task.progress) return task.progress;
    if (task.status === 'running') return genStepText('run', '');
    if (task.status === 'queued') return '排队中（前面还有任务）';
    return '';
  }

  async function runVideoGen(node) {
    const gen = node.gen || (node.gen = {});
    if (!gen.modelId) return toast('请先在节点里选择视频模型', true);
    // 首帧换成了接力帧（或反过来），提示词里的「开头」口径得跟着换 ——
    // 只改没被人手改过的：还等于另一种口径下自动拼出来的那句才动
    const promptSource = videoPromptSource(node);
    if (String(gen.prompt || '').trim()) {
      const flipped = videoPromptOf({ ...promptSource, relay: !promptSource.relay }, { modelId: gen.modelId });
      if (String(gen.prompt).trim() === flipped) gen.prompt = videoPromptOf(promptSource, { modelId: gen.modelId });
    }
    // 提交前把角色名换成「图N」：模型只认第几张图，认不出名字（见 withRefNames）
    const prompt = withRefNames(withRevise(String(gen.prompt || '').trim() || upstreamPrompt(node), gen.revise), node);
    if (!prompt) return toast('请填写镜头描述，或把文本节点连到视频节点的左侧', true);

    // 一段视频吃两张图：前一张当首帧，后一张当尾帧（成片是「图 → 视频 → 图」串起来的）。
    // 首帧优先用「上一段视频的最后一帧」（末帧接力）：接缝两边是同一个画面，成片才不会跳。
    // 上一段还没出片时退回用本镜的镜头图 —— 等它出片后重新提交这一段就会自动接上
    const { start: shotStart, end: endSrc } = frameNodes(node);
    const relay = relayFrameOf(node);
    const startUrl = relay?.url || (shotStart ? mediaUrlOf(shotStart) : '');
    const startName = relay?.fileName || shotStart?.fileName || `${shotStart?.id || node.id}.png`;
    if (!shotStart && !relay) return toast('视频需要一张首帧图：把一个图片节点连到视频节点的左侧', true);
    // 连上了但那张图还没生成出来（上游那波失败 / 还没跑）：等它出图，别报「缺少首帧图」
    if (!startUrl) return toast('首帧图还没生成出来：先跑一下上一行的镜头图', true);
    // 第二张图：真吃尾帧的按尾帧传；豆包这类没有尾帧参数、但收多张图的一起塞进去，
    // 当「这段结束时要走到哪」的目标画面。都不支持的模型靠提示词里那句「落到…」兜方向
    const useEnd = Boolean(endSrc) && Boolean(mediaUrlOf(endSrc)) && modelTakesSecondImage(gen.modelId);

    gen.status = 'queued';
    gen.error = '';
    gen.taskId = '';
    gen.resultUrl = '';
    gen.progress = genStepText('refs', relay?.url ? '读取上一段的最后一帧' : '读取首帧图');
    markDirty();
    rerenderNode(node);

    try {
      const res = await fetch(startUrl);
      if (!res.ok) throw new Error('首帧图读取失败，可能文件已被删除');
      const blob = await res.blob();
      gen.progress = genStepText('submit', useEnd ? '上传两张图（首帧 + 目标画面）' : '上传首帧图');
      let statusEl = genStatusElOf(node);
      if (statusEl) statusEl.textContent = gen.progress;
      const input = await uploadBlobTo('/api/video-inputs', blob, startName);
      let endInput = null;
      if (useEnd) {
        gen.progress = genStepText('submit', '上传尾帧图');
        statusEl = genStatusElOf(node);
        if (statusEl) statusEl.textContent = gen.progress;
        const endRes = await fetch(mediaUrlOf(endSrc));
        if (!endRes.ok) throw new Error('尾帧图读取失败，可能文件已被删除');
        endInput = await uploadBlobTo('/api/video-inputs', await endRes.blob(), endSrc.fileName || `${endSrc.id}.png`);
      }
      gen.progress = genStepText('submit', '');
      const task = await api('/api/video-tasks', {
        method: 'POST',
        body: {
          modelId: gen.modelId,
          prompt,
          duration: gen.duration || '',
          ratio: gen.ratio || '',
          resolution: gen.resolution || '',
          // 豆包内部档位（别的厂商忽略这个字段）
          ...(gen.doubaoTier ? { tier: gen.doubaoTier } : {}),
          seed: gen.seed || '',
          inputId: input.id,
          ...(endInput?.id ? { endInputId: endInput.id } : {}),
        },
      });
      gen.status = task.status || 'queued';
      gen.taskId = task.id;
      // 视频一轮可能好几分钟，进度文案必须撑住整个等待期
      gen.progress = task.progress || genStepText('run', '视频比图片慢，请稍等');
      markDirty();
      rerenderNode(node);
    } catch (err) {
      gen.status = 'failed';
      gen.progress = '';
      gen.error = err.message;
      markDirty();
      rerenderNode(node);
      toast(`视频提交失败：${err.message}`, true);
    }
  }

  /**
   * 重新生成一个生图 / 生视频节点。
   *
   * 和直接点 ↑ 的区别只有一个：先问一句「这次想改什么」，填了就把要求带进提示词。
   * 留空 = 原样重跑，行为跟点 ↑ 完全一样，所以不必强迫用户填。
   *
   * 已有的修改要求会预填进输入框，方便在上一版基础上再调；
   * 清空输入框 = 把这条要求删掉（下次就不带了）。
   */
  async function regenerateNode(node) {
    const isVideo = node.type === 'videoGen';
    const gen = node.gen || (node.gen = {});
    if (PENDING.has(gen.status)) return toast('这个节点正在生成，等它跑完再重来', true);
    const advice = await reviseDialog(
      isVideo ? '重新生成这段视频' : '重新生成这张图',
      '这次想改什么（留空 = 原样重跑）',
      gen.revise || '',
      '填了会作为「这次的要求」追加进提示词；原来的镜头描述保留不动。Ctrl+Enter 提交。',
    );
    if (advice === null) return; // 点了取消
    const next = String(advice || '').trim();
    if (next !== String(gen.revise || '')) {
      gen.revise = next;
      markDirty();
    }
    return isVideo ? runVideoGen(node) : runImageGen(node);
  }

  /** 只重建卡片缩略图（不动面板，避免打断输入） */
  function rerenderCard(node) {
    const el = $(`.nc-node[data-id="${node.id}"]`);
    if (!el) return;
    const body = el.querySelector('.nc-node-body');
    if (body) buildThumb(node, body);
  }

  /** 只重建一个节点，避免整张图重绘导致输入框失焦 */
  function rerenderNode(node) {
    rerenderCard(node);
    // 底部编辑面板正开着这个节点的话，面板里的编辑区同步刷新
    //（正在打字时只补状态文案，不打断输入）
    rerenderPanel(node);
  }

  /** 从生图结果生成一个图片节点，方便继续连线到视频 */
  function spawnFromResult(node) {
    const url = node.gen?.resultUrl;
    if (!url) return;
    const created = addNode('image', { asset: null, at: null });
    if (!created) return;
    created.url = url;
    created.localUrl = url;
    created.fileName = `${node.title || 'AI 生图'} 结果`;
    // 派生节点落在源节点右侧「一个卡宽 + 100」处；被占了就继续往下找空位
    const spot = derivedSpot(node, created.w, created.h);
    created.x = spot.x;
    created.y = spot.y;
    markDirty();
    rerenderNode(created);
    updateNodePosition(created);
    renderEdges();
    drawMinimap();
  }

  /**
   * 派生节点落位（对齐 MiniMax 的 computeDerivedNodePosition）：
   * 默认贴源节点右侧一个卡宽 + 一个间距，垂直居中；被占用时按同一列继续下探。
   */
  function derivedSpot(source, w, h) {
    const nodes = S.graph?.nodes || [];
    let x = Math.round(source.x + CARD_W + LAYOUT_GAP);
    let y = Math.round(source.y + (source.h - h) / 2);
    const hits = (px, py) =>
      nodes.some(
        (n) =>
          n.id !== source.id &&
          px < n.x + n.w + 16 &&
          px + w + 16 > n.x &&
          py < n.y + n.h + 16 &&
          py + h + 16 > n.y,
      );
    for (let i = 0; i < 40 && hits(x, y); i += 1) {
      y += h + LAYOUT_GAP;
    }
    return { x, y };
  }

  /** 把画布节点上的文件登记进素材库（引用同一份文件，不复制） */
  async function saveNodeToLibrary(node) {
    if (node.assetId) return toast('这份文件已经在素材库里了');
    // 生成结果节点只有 localUrl（结果回填的是 /files/... 的 URL，没有本地绝对路径），
    // 所以这里不能只认 localPath —— 否则「把 AI 生成结果存进素材库」永远走不通。
    if (!node.localPath && !node.localUrl) return toast('这个节点没有可登记的本地文件', true);
    const kind = node.type === 'video' ? 'video' : node.type === 'audio' ? 'audio' : 'image';
    try {
      const res = await fetch('/api/node-assets/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          localPath: node.localPath,
          localUrl: node.localUrl,
          fileName: node.fileName,
          kind,
        }),
        // 没有 localPath 时（AI 生成结果），后端按 localUrl 反推它真实所在的目录
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json?.error || '登记失败');
      node.assetId = json.id;
      markDirty();
      renderAssets();
      toast('已加入本地素材库');
    } catch (err) {
      toast(`加入素材库失败：${err.message}`, true);
    }
  }

  /* --------------------------------- 素材库 --------------------------------- */

  function renderAssets() {
    const list = $('#ncAssetList');
    if (!list) return;
    const items = S.assets
      .filter((a) => (S.assetView === 'trash' ? a.deletedAt : !a.deletedAt))
      .filter((a) => (S.assetView === 'favorite' ? a.favorite : true))
      .filter((a) => (S.assetKind === 'all' ? true : a.kind === S.assetKind))
      .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));

    // 每 1.5 秒的状态轮询都会走这里，列表没变就不重建 ——
    // 否则视频缩略图会被反复销毁重载，一直在闪
    const sig = [
      S.assetView,
      S.assetKind,
      items.map((a) => `${a.id}:${a.favorite ? 1 : 0}:${a.deletedAt ? 1 : 0}:${a.originalName}`).join(','),
    ].join('|');
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.textContent = '';

    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'nc-asset-empty';
      empty.textContent = S.assetView === 'trash' ? '回收站是空的' : '还没有素材，点右上角「+ 上传」或把文件拖进来';
      list.appendChild(empty);
    } else {
      for (const asset of items) list.appendChild(buildAssetCard(asset));
    }

    const foot = $('#ncAssetFoot');
    if (foot) {
      const active = S.assets.filter((a) => !a.deletedAt).length;
      const trash = S.assets.filter((a) => a.deletedAt).length;
      foot.textContent = `本地素材 ${active} 个 · 回收站 ${trash} 个`;
    }
  }

  function buildAssetCard(asset) {
    const card = document.createElement('div');
    card.className = 'nc-asset';
    card.draggable = !asset.deletedAt;
    card.dataset.id = asset.id;
    card.title = asset.deletedAt ? '' : '拖到右侧画布即可变成节点';

    const thumb = document.createElement('div');
    thumb.className = 'nc-asset-thumb';
    if (asset.kind === 'image') {
      const img = document.createElement('img');
      img.src = asset.localUrl;
      img.alt = '';
      img.loading = 'lazy';
      img.draggable = false;
      thumb.appendChild(img);
    } else if (asset.kind === 'video') {
      const video = document.createElement('video');
      video.src = asset.localUrl;
      video.muted = true;
      video.preload = 'metadata';
      thumb.appendChild(video);
      const badge = document.createElement('span');
      badge.className = 'nc-asset-badge';
      badge.textContent = '视频';
      thumb.appendChild(badge);
    } else {
      const badge = document.createElement('span');
      badge.className = 'nc-asset-badge audio';
      badge.textContent = '音频';
      thumb.appendChild(badge);
    }
    card.appendChild(thumb);

    const meta = document.createElement('div');
    meta.className = 'nc-asset-meta';
    const name = document.createElement('div');
    name.className = 'nc-asset-name';
    name.textContent = asset.originalName || asset.fileName;
    const size = document.createElement('div');
    size.className = 'nc-asset-size';
    size.textContent = util().fmtSize?.(asset.size) || '';
    meta.append(name, size);
    card.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'nc-asset-actions';
    if (asset.deletedAt) {
      actions.appendChild(assetBtn('恢复', () => restoreAsset(asset), false));
      actions.appendChild(assetBtn('永久删除', () => purgeAsset(asset), true));
    } else {
      actions.appendChild(assetBtn(asset.favorite ? '★' : '☆', () => toggleFavorite(asset), false, asset.favorite ? '取消收藏' : '收藏'));
      actions.appendChild(assetBtn('+ 画布', () => addAssetToCanvas(asset), false, '添加到当前画布'));
      actions.appendChild(assetBtn('删除', () => removeAsset(asset), true, '移入回收站'));
    }
    card.appendChild(actions);

    if (!asset.deletedAt) {
      card.addEventListener('dragstart', (event) => {
        event.dataTransfer.setData('text/nc-asset', asset.id);
        event.dataTransfer.effectAllowed = 'copy';
      });
      card.addEventListener('dblclick', () => addAssetToCanvas(asset));
    }
    return card;
  }

  function assetBtn(label, handler, danger, title) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `nc-asset-btn${danger ? ' danger' : ''}`;
    btn.textContent = label;
    if (title) btn.title = title;
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      handler();
    });
    return btn;
  }

  function addAssetToCanvas(asset, at) {
    if (!S.graph) return;
    const type = asset.kind === 'video' ? 'video' : asset.kind === 'audio' ? 'audio' : 'image';
    addNode(type, { asset, at });
  }

  async function reloadAssets() {
    try {
      const json = await api('/api/node-assets');
      S.assets = [...(json.items || []), ...(json.trash || [])];
      renderAssets();
    } catch (err) {
      toast(`素材库加载失败：${err.message}`, true);
    }
  }

  async function toggleFavorite(asset) {
    try {
      await api(`/api/node-assets/${asset.id}`, { method: 'PATCH', body: { favorite: !asset.favorite } });
      asset.favorite = !asset.favorite;
      renderAssets();
    } catch (err) {
      toast(`操作失败：${err.message}`, true);
    }
  }

  async function removeAsset(asset) {
    try {
      await api(`/api/node-assets/${asset.id}`, { method: 'DELETE' });
      asset.deletedAt = Date.now();
      renderAssets();
    } catch (err) {
      toast(`删除失败：${err.message}`, true);
    }
  }

  async function restoreAsset(asset) {
    try {
      await api(`/api/node-assets/${asset.id}/restore`, { method: 'POST' });
      asset.deletedAt = null;
      renderAssets();
    } catch (err) {
      toast(`恢复失败：${err.message}`, true);
    }
  }

  async function purgeAsset(asset) {
    const ok = await util().confirmDialog?.(`永久删除「${asset.originalName || asset.fileName}」？文件会从磁盘上删掉，无法恢复。`, {
      okText: '永久删除',
    });
    if (ok === false) return;
    try {
      await api(`/api/node-assets/${asset.id}/purge`, { method: 'DELETE' });
      S.assets = S.assets.filter((a) => a.id !== asset.id);
      renderAssets();
      toast('已永久删除');
    } catch (err) {
      toast(`删除失败：${err.message}`, true);
    }
  }

  async function uploadFiles(files) {
    const list = [...files];
    if (!list.length) return;
    let ok = 0;
    for (const file of list) {
      try {
        const asset = await uploadAssetFile(file);
        S.assets.unshift(asset);
        ok += 1;
      } catch (err) {
        toast(`${file.name} 上传失败：${err.message}`, true);
      }
    }
    renderAssets();
    if (ok) toast(`已导入 ${ok} 个素材`);
  }

  /* -------------------------------- 节点上传文件 ------------------------------- */

  function pickNodeFile(node, accept) {
    const input = $('#ncNodeFileInput');
    input.accept = accept;
    input.value = '';
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        const asset = await uploadAssetFile(file);
        S.assets.unshift(asset);
        node.localUrl = asset.localUrl;
        node.url = asset.localUrl;
        node.localPath = asset.localPath;
        node.fileName = asset.originalName;
        node.assetId = asset.id;
        markDirty();
        rerenderNode(node);
        renderAssets();
      } catch (err) {
        toast(`上传失败：${err.message}`, true);
      }
    };
    input.click();
  }

  /* --------------------------------- 交互绑定 -------------------------------- */

  function bindViewport() {
    const viewport = $('#ncViewport');

    // 按住空格时，无论在节点上还是空白处，拖动都是平移画布。
    // 用捕获阶段先一步拦下，避免节点自己的拖动/选择逻辑接手。
    viewport.addEventListener('pointerdown', (event) => {
      if (!S.spaceDown || event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      S.panning = { sx: event.clientX, sy: event.clientY, x: S.graph.viewport.x, y: S.graph.viewport.y };
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add('panning');
    }, true);

    viewport.addEventListener('pointerdown', (event) => {
      // 点击菜单、缩放条、节点、连线、悬浮工具时不触发画布操作
      if (event.target.closest('.nc-menu, .nc-zoombar, .nc-node, .nc-edge, .nc-edge-hit, .nc-vtools, .nc-minimap, .nc-find, .nc-cmd, .nc-help, .nc-skin, .nc-selbar, .nc-selmenu, .nc-status, .nc-panel')) return;
      closeMenu();
      // 右键：打开画布菜单，不平移
      if (event.button === 2) return;

      const wantPan = event.button === 1 || event.shiftKey || S.spaceDown || (S.panMode && event.button === 0);
      if (wantPan) {
        S.panning = { sx: event.clientX, sy: event.clientY, x: S.graph.viewport.x, y: S.graph.viewport.y };
        viewport.setPointerCapture(event.pointerId);
        viewport.classList.add('panning');
        return;
      }

      // 左键空白：清空选择并开始框选
      if (event.button === 0) {
        if (!event.shiftKey) {
          S.selected.clear();
          S.selectedEdge = '';
          syncSelection();
        }
        S.marquee = { sx: event.clientX, sy: event.clientY };
        viewport.setPointerCapture(event.pointerId);
      }
    });

    viewport.addEventListener('pointermove', (event) => {
      if (S.panning) {
        S.graph.viewport.x = S.panning.x + (event.clientX - S.panning.sx);
        S.graph.viewport.y = S.panning.y + (event.clientY - S.panning.sy);
        applyViewport();
      }
      if (S.marquee) {
        showMarquee(S.marquee.sx, S.marquee.sy, event.clientX, event.clientY);
        marqueeSelect(S.marquee.sx, S.marquee.sy, event.clientX, event.clientY);
      }
      // 连线拖拽的临时线统一由 startConnect 里挂在 window 上的 move 画（带端口磁吸），
      // 这里不重复画，免得两套逻辑各画各的
    });

    viewport.addEventListener('pointerup', (event) => {
      if (S.panning) {
        S.panning = false;
        viewport.classList.remove('panning');
        markDirty();
      }
      if (S.marquee) {
        S.marquee = null;
        hideMarquee();
      }
      if (S.connecting) {
        finishConnect(event);
      }
    });

    // 右键菜单：节点上 = 节点菜单，连线上 = 连线菜单，空白 = 画布菜单
    viewport.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      closeMenu();
      const nodeEl = event.target.closest('.nc-node');
      if (nodeEl) {
        const node = nodeById(nodeEl.dataset.id);
        if (!node) return;
        // 右键未选中的节点时，先把它选进来
        if (!S.selected.has(node.id)) {
          S.selected.clear();
          S.selected.add(node.id);
          expandSelectionByGroup();
          S.selectedEdge = '';
          syncSelection();
        }
        nodeMenu(node, event.clientX, event.clientY);
        return;
      }
      const edgeEl = event.target.closest('.nc-edge-hit, .nc-edge');
      if (edgeEl?.dataset.id && S.graph) {
        const edgeId = edgeEl.dataset.id;
        const edge = S.graph.edges.find((e) => e.id === edgeId);
        const setColor = (color) => {
          if (!edge) return;
          pushHistory();
          if (color) edge.color = color;
          else delete edge.color;
          markDirty();
          renderEdges();
          drawMinimap();
        };
        const colorItem = (label, color) => ({
          label: `${(!edge?.color && !color) || edge?.color === color ? '● ' : '○ '}${label}`,
          handler: () => setColor(color),
        });
        // 视频节点的入边：一条是首帧、一条是尾帧，连错了得能改回来
        const target = nodeById(edge?.to);
        const source = nodeById(edge?.from);
        const frameItems = target?.type === 'videoGen' && isImageNode(source)
          ? [
            '-',
            {
              label: `${edge?.role === 'start' ? '● ' : '○ '}当首帧（这一段从这张图开始）`,
              handler: () => setEdgeRole(edgeId, 'start'),
            },
            {
              label: `${edge?.role === 'end' ? '● ' : '○ '}当尾帧（这一段停在这张图）`,
              handler: () => setEdgeRole(edgeId, 'end'),
            },
          ]
          : [];
        openMenu([
          colorItem('默认灰', ''),
          colorItem('冰川青', 'teal'),
          colorItem('星玫紫', 'violet'),
          colorItem('流金', 'gold'),
          colorItem('蔷薇', 'rose'),
          colorItem('墨黑', 'slate'),
          ...frameItems,
          '-',
          { label: '删除连线', danger: true, handler: () => {
            pushHistory();
            S.graph.edges = S.graph.edges.filter((e) => e.id !== edgeId);
            if (S.selectedEdge === edgeId) S.selectedEdge = '';
            markDirty();
            renderEdges();
            drawMinimap();
            updateStatusBar();
          } },
        ], event.clientX, event.clientY);
        return;
      }
      if (S.graph) canvasMenu(event.clientX, event.clientY);
    });

    // 点击别处 / 滚轮 / Esc 关闭右键菜单
    document.addEventListener('pointerdown', (e) => {
      if (S.menu && !e.target.closest('.nc-menu')) closeMenu();
    }, true);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && S.menu) closeMenu();
    });

    // 按住空格 = 临时平移（MiniMax 的空画布引导里写的就是「按住 Space 拖拽」）。
    // 焦点在输入控件里时不管，否则打字打不出空格。
    const isTypingTarget = (el) => Boolean(el?.closest?.('input, textarea, select, [contenteditable="true"]'));
    document.addEventListener('keydown', (e) => {
      if (e.code !== 'Space' || e.repeat || isTypingTarget(e.target)) return;
      if (document.body.dataset.module !== 'node') return;
      if (isCmdOpen() || !$('#ncHelp')?.hidden) return;
      e.preventDefault();
      if (S.spaceDown) return;
      S.spaceDown = true;
      viewport.classList.add('space-pan');
    });
    document.addEventListener('keyup', (e) => {
      if (e.code !== 'Space') return;
      S.spaceDown = false;
      viewport.classList.remove('space-pan');
    });
    // 失焦（切窗口回来时）可能漏掉 keyup，这里兜底清掉
    window.addEventListener('blur', () => {
      S.spaceDown = false;
      viewport.classList.remove('space-pan');
    });

    /** 从事件目标往上找第一个「真的能竖向滚」的元素；找不到返回 null */
    function findScrollableAncestor(target) {
      let el = target instanceof Element ? target : target?.parentElement;
      while (el && el !== viewport) {
        if (el.scrollHeight > el.clientHeight + 1) {
          const tag = el.tagName;
          const style = getComputedStyle(el).overflowY;
          // textarea/select 天生可滚；其余必须显式声明了 auto/scroll 才算
          if (tag === 'TEXTAREA' || tag === 'SELECT' || style === 'auto' || style === 'scroll') return el;
        }
        el = el.parentElement;
      }
      return null;
    }

    viewport.addEventListener('wheel', (event) => {
      if (!S.graph) return;
      // 指针落在节点里的输入控件上时，滚轮只归这个控件，绝不缩放画布。
      // 之前只判断「能不能滚」，于是内容没溢出的提示词框滚一下就把画布缩了 ——
      // 正在写字的人会觉得画布在乱动。
      if (event.target.closest?.('textarea, input, select, [contenteditable="true"]')) return;
      // 节点内部其它能滚的地方（长文本等）先让给它，
      // 注意只认「真的能滚」的元素：.nc-node-body 是 overflow:hidden（内容被裁掉），
      // 让它接手的话滚轮就变成既没滚动也没缩放的死区。
      const scrollable = findScrollableAncestor(event.target);
      if (scrollable) {
        const atTop = scrollable.scrollTop <= 0;
        const atBottom = scrollable.scrollTop + scrollable.clientHeight >= scrollable.scrollHeight - 1;
        // 还能往这个方向滚就交给它；滚到头了才把剩下的交给画布缩放
        if (event.deltaY < 0 ? !atTop : !atBottom) return;
      }
      event.preventDefault();
      zoomAt(event.clientX, event.clientY, event.deltaY);
    }, { passive: false });

    viewport.addEventListener('dragover', (event) => {
      if (event.dataTransfer.types.includes('text/nc-asset')) {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      } else if ([...event.dataTransfer.types].includes('Files')) {
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      }
    });

    viewport.addEventListener('drop', (event) => {
      event.preventDefault();
      const assetId = event.dataTransfer.getData('text/nc-asset');
      const at = screenToWorld(event.clientX, event.clientY);
      if (assetId) {
        const asset = S.assets.find((a) => a.id === assetId);
        if (asset) addAssetToCanvas(asset, at);
        return;
      }
      const files = [...(event.dataTransfer.files || [])];
      if (files.length) {
        uploadFiles(files).then(() => {
          // 上传完直接把它们摆到落点，省一次拖拽
          const latest = S.assets.slice(0, files.length);
          latest.forEach((asset, index) => addAssetToCanvas(asset, { x: at.x + index * 24, y: at.y + index * 24 }));
        });
      }
    });

    // 双击空白处新建文本节点
    viewport.addEventListener('dblclick', (event) => {
      if (event.target.closest('.nc-node')) return;
      addNode('text', { at: screenToWorld(event.clientX, event.clientY) });
    });
  }

  /* -------------------------------- AI 编导 -------------------------------- */

  /**
   * 剧本 → 分镜 → 一键出图出视频
   *
   * 分三层：
   *   1. 服务端只把剧本拆成文字结构（server/canvas-director.js），不建任务；
   *   2. 这里把结构**铺成真实节点**：角色定妆照 → 镜头图 → 镜头视频，连线即参考图关系；
   *   3. 「一键生成」按 定妆照 → 镜头图 → 镜头视频 分波次跑，每波等落地再开下一波 ——
   *      镜头图必须拿定妆照当参考图，跳过顺序就会每张一个长相。
   *
   * 铺完之后这些节点和手动建的完全一样：提示词、模型、连线都能改，
   * 编导只是「替你先摆一遍」，不是另一套数据。
   */
  const AGENT_ROLE_LABEL = { cast: '定妆照', scene: '场景板', shot: '镜头图', video: '镜头视频' };
  const DIRECTOR_RATIOS = [
    { id: '16:9', label: '16:9 横屏' },
    { id: '9:16', label: '9:16 竖屏' },
    { id: '1:1', label: '1:1 方形' },
  ];
  /** 默认画幅：横屏。竖屏是短剧的主要形态，但这一版主用横屏，需要竖屏在面板里改 */
  const DEFAULT_DIRECTOR_RATIO = '16:9';
  /** 默认单镜时长（秒）：豆包 5 秒出片太快、动作演不完，10 秒是这条链路的平衡点 */
  const DEFAULT_SHOT_DURATION = 10;
  /** 编导铺出来的三行之间的纵向间距（卡高 144 + 留白） */
  const AGENT_ROW_GAP = 250;
  /** 剧本卡的高度区间：矮了看不见首帧提示词，高了铺满整屏（超出的部分框内滚动） */
  const SCRIPT_CARD_MIN_H = 220;
  // 上限从 460 抬到 760：5 镜的剧本大约 1300 字、要 660 高才铺得下，
  // 460 的卡只露出三分之一，剩下的明明在节点里却看不见（用户反馈「框这么大就哪一点显示」）。
  // 再长仍然框内滚动，右下角也能自己拉大。
  const SCRIPT_CARD_MAX_H = 760;
  const DIRECTOR_PREFS_KEY = 'nc.director';
  /**
   * 偏好结构的版本。
   * 升到 2：默认的出图 / 视频模型改成**本机豆包桥**（不用 Key、不占额度）。
   *   老用户存过的是 v1（记着「即梦 / 智谱」），照旧读出来就会一直停在旧模型上，
   *   所以碰到旧结构时把两个模型 id 丢掉，让它重新挑一次默认值。
   * 升到 3：默认画幅从 9:16 竖屏改成 16:9 横屏。老用户存过的 ratio 是 9:16，
   *   不丢掉的话「默认横屏」对他们永远不生效 —— 只清 ratio，模型选择不动。
   */
  const DIRECTOR_PREFS_VERSION = 3;

  function readDirectorPrefs() {
    let prefs = {};
    try {
      prefs = JSON.parse(localStorage.getItem(DIRECTOR_PREFS_KEY) || '{}') || {};
    } catch (err) {
      prefs = {};
    }
    const version = Number(prefs.v || 0);
    if (version >= DIRECTOR_PREFS_VERSION) return prefs;
    const migrated = { ...prefs, v: DIRECTOR_PREFS_VERSION };
    // v1 → v2：模型 id 是旧的（即梦 / 智谱），丢掉重新挑默认
    if (version < 2) {
      delete migrated.imageModelId;
      delete migrated.videoModelId;
    }
    // v2 → v3：画幅默认值变了，清掉存着的旧画幅
    delete migrated.ratio;
    return migrated;
  }

  function writeDirectorPrefs(patch) {
    try {
      localStorage.setItem(DIRECTOR_PREFS_KEY, JSON.stringify({ ...readDirectorPrefs(), ...patch }));
    } catch (err) {
      /* 隐私模式下写不了就算了，不影响功能 */
    }
  }

  /* ------------------------------ 聊天窗状态 ------------------------------ */

  const CHAT_OPEN_KEY = 'nc.chat.open';
  // 服务端 /api/canvas/chat 内部给自己留的总预算是 280 秒（见 server/canvas-chat.js
  // 的 CHAT_BUDGET_MS）。前端必须比它更宽，否则服务端还在补镜、前端先断了，
  // 用户看到的是「signal timed out」而不是「这次拆得慢，分镜给你了」。
  // 所以这里给 330 秒 = 服务端预算 + 50 秒网络/序列化余量。
  const CHAT_TIMEOUT_MS = 330000;
  // chatId = 正在聊的那段对话在 graph.chats 里的 id（null = 刚点过「新对话」，还没开口）
  const chat = () => S.chat || (S.chat = { chatId: null, messages: [], busy: false, loaded: false });

  const isDirectorOpen = () => !$('#ncChat')?.hidden;

  const CHAT_SUB_IDLE = '贴剧本，我铺分镜';

  /** 对话标题：第一句用户发言，历史列表靠它认是哪一段 */
  function chatTitleOf(messages) {
    const first = (messages || []).find((m) => m.role === 'user' && String(m.text || '').trim());
    if (!first) return '';
    const line = String(first.text).replace(/\s+/g, ' ').trim();
    return line.length > 32 ? `${line.slice(0, 32)}…` : line;
  }

  /** 历史列表里的时间：今天只写时分，往前的带月日 */
  function fmtChatTime(ts) {
    const at = Number(ts) || 0;
    if (!at) return '';
    const date = new Date(at);
    const pad = (n) => String(n).padStart(2, '0');
    const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
    const today = new Date();
    if (date.toDateString() === today.toDateString()) return `今天 ${hm}`;
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}`;
  }

  /**
   * 从存档里恢复这个画布的对话：优先 chatId 指的那段，没有就挑最近改过的那段。
   * 老存档（还只有一个 graph.chat 的）也能读出来，服务端开机时会顺手迁移。
   */
  function restoreChat(graph, loaded) {
    const sessions = Array.isArray(graph.chats) ? graph.chats : [];
    const legacy = Array.isArray(graph.chat) && graph.chat.length
      ? [{ id: '', title: chatTitleOf(graph.chat), messages: graph.chat, updatedAt: 0 }]
      : [];
    const all = sessions.length ? sessions : legacy;
    const current = all.find((c) => c.id && c.id === graph.chatId)
      || all.slice().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0]
      || null;
    return {
      chatId: current?.id || null,
      messages: (current?.messages || []).map((m) => ({ ...m })),
      busy: false,
      photos: [],
      loaded,
    };
  }

  /**
   * 把当前这段对话写回本地缓存的那张画布（S.graph 与 S.graphs 各一份）。
   * 第一次开口时服务端才分配 chatId，这里顺手补一条本地记录，
   * 不然「刚聊了两句就切走」再回来会找不到这段。
   */
  function cacheChat(graphId, chatId, messages) {
    for (const holder of [S.graph, S.graphs.find((g) => g.id === graphId)]) {
      if (!holder || holder.id !== graphId) continue;
      const list = Array.isArray(holder.chats) ? holder.chats : (holder.chats = []);
      let session = chatId ? list.find((c) => c.id === chatId) : null;
      if (!session && chatId && messages.length) {
        session = { id: chatId, title: chatTitleOf(messages), messages: [], createdAt: Date.now() };
        list.push(session);
      }
      if (session) {
        session.messages = messages;
        session.title = chatTitleOf(messages) || session.title;
        session.updatedAt = Date.now();
      }
      // 空 chatId 不代表「没有对话」：只是这一段还没拿到服务端 id，别在这儿把它清掉
      if (chatId) holder.chatId = chatId;
    }
  }

  /* ------------------------- 聊天记录落盘（复用） ------------------------- */

  let chatSaveTimer = null;
  let chatSaving = false;
  let chatSaveFails = 0;
  let chatDirty = false;

  /** 只挑渲染气泡用得上的字段：聊天窗里的临时态（正在想 / 待发参考图）不进库 */
  function chatPayload() {
    return chat().messages.map((m) => ({
      role: m.role,
      text: m.text || '',
      ...(m.photos?.length ? { photos: m.photos.map((p) => ({ id: p.id, name: p.name, localUrl: p.localUrl })) } : {}),
      ...(m.action === 'run' ? { action: 'run' } : {}),
      ...(m.at ? { at: m.at } : {}),
    }));
  }

  /**
   * 「聊天复用」：每张画布带着自己的对话，而且不止一段 ——
   * 「新对话」把上一段留在历史里，随时能翻回去接着聊。
   *
   * 一段对话可能几十条，边聊边整段提交太浪费，所以停手 500ms 再存一次；
   * 同时在本地 S.graphs 里先记一份 —— 切走再切回来走的是内存，不用等网络回来。
   */
  function persistChat(delay = 500) {
    clearTimeout(chatSaveTimer);
    if (!S.graph) return;
    cacheChat(S.graph.id, chat().chatId, chatPayload());
    chatDirty = true;
    chatSaveTimer = setTimeout(flushChat, delay);
  }

  async function flushChat() {
    clearTimeout(chatSaveTimer);
    if (!S.graph || !chatDirty) return;
    if (chatSaving) {
      chatSaveTimer = setTimeout(flushChat, 300);
      return;
    }
    const graphId = S.graph.id;
    const payload = chatPayload();
    const chatId = chat().chatId || '';
    chatSaving = true;
    try {
      const res = await api(`/api/node-canvases/${graphId}/chat`, {
        method: 'POST',
        body: { chatId: chatId || null, messages: payload },
      });
      chatDirty = false;
      chatSaveFails = 0;
      // 第一次开口：服务端才分配会话 id，记住它，否则下一句会又开一段新的
      if (res?.chatId && res.chatId !== chat().chatId) chat().chatId = res.chatId;
      cacheChat(graphId, chat().chatId, payload);
    } catch (err) {
      // 存不下不影响这一轮对话，但要重试；最多 3 次，别在原地刷请求
      chatSaveFails = Math.min(chatSaveFails + 1, 3);
      if (chatSaveFails < 3) chatSaveTimer = setTimeout(flushChat, 1200 * chatSaveFails);
    } finally {
      chatSaving = false;
    }
  }

  /** 标题下面那行小字：平时是引导语，等模型的时候当状态用 */
  function setChatSub(text) {
    const el = $('#ncChatSub');
    if (el) el.textContent = text || CHAT_SUB_IDLE;
  }

  /** 标题下那行小字该写什么：在想 → 状态；有旧对话 → 说明在复用；否则留引导语 */
  function chatSubText() {
    const st = chat();
    if (st.busy) return '想一想…';
    if (st.messages.length) return `接着上次聊 · 已 ${st.messages.length} 条`;
    return '';
  }

  /** 复制纯文本：桌面端 navigator.clipboard 偶尔不可用（没焦点 / 被拦），退回 execCommand */
  async function copyPlain(text) {
    const value = String(text ?? '');
    if (!value.trim()) return false;
    const helper = util().copyText;
    if (typeof helper === 'function') {
      try {
        if (await helper(value)) return true;
      } catch (err) {
        /* 落到下面的兜底 */
      }
    }
    try {
      const area = document.createElement('textarea');
      area.value = value;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select?.();
      const ok = document.execCommand?.('copy');
      area.remove?.();
      return Boolean(ok);
    } catch (err) {
      return false;
    }
  }

  function chatBubble(msg) {
    const row = document.createElement('div');
    row.className = `nc-chat-msg ${msg.role}`;
    const bubble = document.createElement('div');
    bubble.className = 'nc-chat-bubble';
    // 带出去的参考图在气泡里留一份缩略图：事后回看才知道当时发了什么图
    if (msg.photos?.length) {
      const strip = document.createElement('div');
      strip.className = 'nc-chat-bubble-photos';
      for (const photo of msg.photos) {
        const img = document.createElement('img');
        img.src = photo.localUrl || '';
        img.alt = photo.name || '参考图';
        strip.appendChild(img);
      }
      bubble.appendChild(strip);
    }
    const body = document.createElement('div');
    body.textContent = msg.text || '';
    bubble.appendChild(body);
    // 气泡里的文字本来就能选中复制（.nc-chat 放开了 user-select），
    // 这里再给一个一键复制的按钮：分镜回复动辄几十行，拖着选很烦
    if (msg.text && msg.role !== 'error') {
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'nc-chat-copy';
      copy.title = '复制这条';
      copy.innerHTML =
        '<svg viewBox="0 0 14 14" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round">'
        + '<rect x="4.6" y="4.6" width="7.4" height="7.4" rx="1.4"/>'
        + '<path d="M9.4 4.6V3.2A1.2 1.2 0 0 0 8.2 2H3.2A1.2 1.2 0 0 0 2 3.2v5A1.2 1.2 0 0 0 3.2 9.4h1.4"/></svg>';
      copy.addEventListener('click', async (event) => {
        event.stopPropagation();
        const ok = await copyPlain(msg.text);
        copy.classList.toggle('ok', ok);
        if (!ok) {
          toast('复制失败，直接选中文字复制吧', true);
          return;
        }
        toast('已复制这条');
        setTimeout(() => copy.classList.remove('ok'), 900);
      });
      bubble.appendChild(copy);
    }
    row.appendChild(bubble);
    return row;
  }

  /** 空对话时的引导 + 三个能直接点的例子（点一下填进输入框，不会直接发） */
  function chatWelcome() {
    const box = document.createElement('div');
    box.className = 'nc-chat-welcome';
    const tip = document.createElement('p');
    tip.className = 'nc-chat-welcome-tip';
    tip.textContent = '贴一段剧本，或者说一句题材，我拆成分镜铺到画布上。铺好之后还能继续说「第 3 镜改成雨夜」。';
    box.appendChild(tip);

    const examples = [
      '一个女孩在雨夜的地铁站，等一班永远不来的末班车',
      '把画布上这份分镜拆成 6 个镜头',
      '第 3 镜改成清晨，运镜慢一点',
    ];
    const list = document.createElement('div');
    list.className = 'nc-chat-examples';
    for (const sample of examples) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nc-chat-example';
      btn.textContent = sample;
      btn.addEventListener('click', () => {
        const input = $('#ncChatInput');
        if (!input) return;
        input.value = sample;
        autoGrowChatInput();
        input.focus?.();
      });
      list.appendChild(btn);
    }
    box.appendChild(list);

    // 空对话但画布上有历史：直接把「复用」摆在眼前，
    // 不然刚点完「新对话」只看到空白，会以为记录被清掉了
    const others = chatSessions()
      .filter((c) => (c.messages || []).length && c.id !== chat().chatId)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))
      .slice(0, 3);
    if (others.length) {
      const reuse = document.createElement('div');
      reuse.className = 'nc-chat-reuse';
      const label = document.createElement('span');
      label.className = 'nc-chat-reuse-label';
      label.textContent = '接着之前聊：';
      reuse.appendChild(label);
      for (const session of others) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'nc-chat-example';
        btn.textContent = `${session.title || chatTitleOf(session.messages)} · ${(session.messages || []).length} 条`;
        btn.addEventListener('click', () => loadChatSession(session));
        reuse.appendChild(btn);
      }
      box.appendChild(reuse);
    }
    return box;
  }

  function chatTyping() {
    const row = document.createElement('div');
    row.className = 'nc-chat-msg assistant';
    const bubble = document.createElement('div');
    bubble.className = 'nc-chat-bubble nc-chat-typing';
    for (let i = 0; i < 3; i += 1) bubble.appendChild(document.createElement('i'));
    row.appendChild(bubble);
    return row;
  }

  function renderChatLog() {
    const log = $('#ncChatLog');
    if (!log) return;
    const st = chat();
    log.textContent = '';
    if (!st.messages.length) {
      log.appendChild(chatWelcome());
    } else {
      for (const msg of st.messages) {
        log.appendChild(chatBubble(msg));
        if (msg.role === 'assistant' && msg.action === 'run') {
          const row = document.createElement('div');
          row.className = 'nc-chat-act';
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = '一键生成';
          btn.addEventListener('click', () => runAgentPlan());
          row.appendChild(btn);
          log.appendChild(row);
        }
      }
    }
    if (st.busy) log.appendChild(chatTyping());
    // 新消息总在最下面：不滚到底用户会以为没反应
    log.scrollTop = log.scrollHeight;
  }

  function autoGrowChatInput() {
    const input = $('#ncChatInput');
    if (!input) return;
    input.style.height = 'auto';
    const height = Math.min(132, Math.max(38, input.scrollHeight || 38));
    input.style.height = `${height}px`;
  }

  /** 输入框还能不能发：没字、太短、或者上一句还没回，都不发 */
  function canSendChat(raw, photoCount = 0) {
    const st = chat();
    if (st.busy) {
      toast('还在想上一条，等一下再发', true);
      return false;
    }
    // 没写字但有图也算：只贴图就是「按这几个人物拍」
    if (String(raw || '').trim().length < 2 && !photoCount) return false;
    if (!S.director.info?.hasModel && S.director.info) {
      toast('还没配置写剧本的对话模型 —— 点「参数」里的「写剧本的模型」设置', true);
      return false;
    }
    return true;
  }

  /* ------------------------------ 参考图 ------------------------------ */

  /** 一次最多带几张人物图：多了模型认不准，定妆照也排不下 */
  const MAX_CHAT_PHOTOS = 4;

  const chatPhotos = () => (chat().photos ||= []);

  function renderChatPhotos() {
    const box = $('#ncChatPhotos');
    if (!box) return;
    const list = chatPhotos();
    box.textContent = '';
    box.hidden = !list.length;
    for (const photo of list) {
      const item = document.createElement('span');
      item.className = 'nc-chat-photo';
      const img = document.createElement('img');
      img.src = photo.localUrl || '';
      img.alt = photo.name || '参考图';
      item.appendChild(img);
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'nc-chat-photo-x';
      del.title = '撤掉这张';
      del.textContent = '×';
      del.addEventListener('click', () => {
        const next = chatPhotos().filter((p) => p.id !== photo.id);
        chat().photos = next;
        renderChatPhotos();
      });
      item.appendChild(del);
      box.appendChild(item);
    }
  }

  /**
   * 收图：点按钮选、Ctrl+V 粘贴、直接拖进聊天窗，三种入口都走这里。
   * 先上传到图库（/api/image-refs）拿到 id，发送时只带 id ——
   * 聊天接口是 JSON，塞不下图片二进制。
   */
  async function addChatPhotos(files) {
    const list = [...(files || [])].filter((f) => f && String(f.type || '').startsWith('image/'));
    if (!list.length) return;
    const room = MAX_CHAT_PHOTOS - chatPhotos().length;
    if (room <= 0) {
      toast(`一次最多带 ${MAX_CHAT_PHOTOS} 张参考图`, true);
      return;
    }
    for (const file of list.slice(0, room)) {
      try {
        const ref = await uploadBlobTo('/api/image-refs', file, file.name || 'photo.png');
        if (!ref?.id) throw new Error('上传没返回图片 id');
        chatPhotos().push({ id: ref.id, localUrl: ref.localUrl || '', name: file.name || '参考图' });
      } catch (err) {
        toast(`「${file.name || '这张图'}」上传失败：${err.message}`, true);
      }
    }
    if (list.length > room) toast(`一次最多带 ${MAX_CHAT_PHOTOS} 张，后面的没带上`, true);
    renderChatPhotos();
  }

  /* ------------------- 素材库 → 参考图（优先复用，别重传） ------------------- */

  /** 还能再带几张 */
  const libPickRoom = () => Math.max(0, MAX_CHAT_PHOTOS - chatPhotos().length);

  /** 素材库里能当参考图的：只要图片，回收站里的不算 */
  const libImages = () => (S.assets || [])
    .filter((a) => a.kind === 'image' && !a.deletedAt)
    .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));

  let libPickSel = [];
  let libPickBusy = false;

  function openLibPick() {
    const box = $('#ncLibPick');
    if (!box) return;
    libPickSel = [];
    box.hidden = false;
    const room = libPickRoom();
    const hint = $('#ncLibPickHint');
    if (hint) {
      hint.textContent = room
        ? `最多再选 ${room} 张 · 素材库里已有的不用重新上传`
        : `已经带满 ${MAX_CHAT_PHOTOS} 张，先撤掉几张再选`;
    }
    renderLibPick();
  }

  function closeLibPick() {
    const box = $('#ncLibPick');
    if (box) box.hidden = true;
    libPickSel = [];
  }

  function renderLibPick() {
    const grid = $('#ncLibPickGrid');
    if (!grid) return;
    const images = libImages();
    const room = libPickRoom();
    const sig = `${images.map((a) => `${a.id}:${a.originalName || ''}`).join(',')}|${libPickSel.join(',')}|${room}`;
    if (grid.dataset.sig === sig) return;
    grid.dataset.sig = sig;
    grid.textContent = '';

    if (!images.length) {
      const empty = document.createElement('div');
      empty.className = 'nc-libpick-empty';
      empty.textContent = '素材库里还没有图片。点下面的「上传新图」传一张，下次就能直接选它。';
      grid.appendChild(empty);
      updateLibPickOk();
      return;
    }

    for (const asset of images) {
      const picked = libPickSel.includes(asset.id);
      const item = document.createElement('button');
      item.type = 'button';
      item.className = `nc-libpick-item${picked ? ' on' : ''}`;
      item.title = asset.originalName || asset.fileName || '';
      item.disabled = !picked && libPickSel.length >= room;
      const img = document.createElement('img');
      img.src = asset.localUrl || '';
      img.alt = '';
      img.loading = 'lazy';
      const name = document.createElement('span');
      name.textContent = asset.originalName || asset.fileName || '素材';
      item.append(img, name);
      item.addEventListener('click', () => {
        if (libPickSel.includes(asset.id)) libPickSel = libPickSel.filter((id) => id !== asset.id);
        else if (libPickSel.length < room) libPickSel.push(asset.id);
        else {
          toast(`最多再带 ${room} 张`, true);
          return;
        }
        // 选中态变了要重画：sig 里带着选择，直接重跑就行
        grid.dataset.sig = '';
        renderLibPick();
      });
      grid.appendChild(item);
    }
    updateLibPickOk();
  }

  function updateLibPickOk() {
    const btn = $('#ncLibPickOk');
    if (!btn) return;
    const n = libPickSel.length;
    btn.textContent = n ? `加入（${n}）` : '加入';
    btn.disabled = libPickBusy || !n;
  }

  /** 选完确认：素材 → 参考图（服务端直接引用素材文件，不复制），再挂到待发列表 */
  async function confirmLibPick() {
    if (libPickBusy) return;
    const ids = [...libPickSel];
    if (!ids.length) {
      closeLibPick();
      return;
    }
    libPickBusy = true;
    updateLibPickOk();
    let added = 0;
    for (const id of ids) {
      const asset = (S.assets || []).find((a) => a.id === id);
      if (!asset) continue;
      try {
        const ref = await api(`/api/node-assets/${id}/to-image-ref`, { method: 'POST', body: {} });
        if (!ref?.id) throw new Error('没拿到参考图 id');
        chatPhotos().push({
          id: ref.id,
          localUrl: ref.localUrl || asset.localUrl || '',
          name: asset.originalName || '参考图',
        });
        added += 1;
      } catch (err) {
        toast(`「${asset.originalName || '这张图'}」加不上：${err.message}`, true);
      }
    }
    libPickBusy = false;
    renderChatPhotos();
    closeLibPick();
    if (added) toast(`从素材库带了 ${added} 张参考图`);
  }

  /* ------------------------------ 画布 ↔ 聊天 ------------------------------ */

  /** 当前参数，给聊天模型参考（它要用这些来拆镜头、拼提示词） */
  function chatPrefs() {
    return {
      shotCount: Number(readSelect('#ncDirShots')) || 6,
      style: readSelect('#ncDirStyle') || '',
      ratio: readSelect('#ncDirRatio', DEFAULT_DIRECTOR_RATIO) || DEFAULT_DIRECTOR_RATIO,
      imageModelId: readSelect('#ncDirImageModel'),
      videoModelId: readSelect('#ncDirVideoModel'),
      wantVideo: Boolean(readSelect('#ncDirVideoModel')),
    };
  }

  /**
   * 把画布上的分镜压缩成一段结构，交给服务端去拼 prompt。
   * 只给「模型需要知道的」：第几镜、画什么、怎么动、说什么、出没出图。
   */
  function sceneSummary() {
    const list = agentNodes();
    if (!list.length) return null;
    const prefs = readDirectorPrefs();
    const shots = list
      .filter((n) => n.agent.role === 'shot')
      .sort((a, b) => a.agent.shotIndex - b.agent.shotIndex)
      .map((n) => ({
        index: n.agent.shotIndex,
        scene: n.agent.scene || '',
        // 收尾画面：这一镜动作做完时画面停在哪（下一段视频的尾帧就是它）
        endState: n.agent.endState || '',
        motion: n.agent.motion || '',
        dialogue: n.agent.dialogue || '',
        duration: Number(n.gen?.duration) || DEFAULT_SHOT_DURATION,
        characters: n.agent.character || '',
        hasImage: Boolean(n.gen?.resultUrl),
      }));
    const characters = list
      .filter((n) => n.agent.role === 'cast')
      .map((n) => ({ name: n.agent.character, desc: n.agent.desc || '' }));
    return {
      planTitle: list[0].agent.planTitle || '',
      ratio: prefs.ratio || DEFAULT_DIRECTOR_RATIO,
      style: prefs.style || '',
      characters,
      shots,
      hasVideo: list.some((n) => n.agent.role === 'video'),
    };
  }

  /**
   * 把「改某几镜」套到节点上。
   *
   * 三种改法的影响面不一样，别一把清空：
   *   · 改画面 → 提示词变了，旧图作废，清掉结果等重出；
   *   · 改运镜 → 出图不受影响，但对应视频节点的提示词要跟着换，清掉视频结果；
   *   · 改台词 / 时长 → 只动文本和参数，图和视频都还能用。
   */
  function applyChatEdits(edits) {
    const list = agentNodes();
    if (!list.length) return 0;
    let touched = 0;
    pushHistory();
    for (const edit of edits || []) {
      const index = Number(edit?.shotIndex);
      if (!Number.isFinite(index)) continue;
      const shots = list.filter((n) => n.agent.role === 'shot' && n.agent.shotIndex === index);
      for (const node of shots) {
        const agent = node.agent;
        resetEditTarget(node);
        if (edit.scene) {
          agent.scene = edit.scene;
          // 新画面里自己写了景别就以它为准（「改成特写」这种改法很常见），
          // 否则还按原来那档，拼提示词时也不会重复补
          const sizeHit = ['特写', '近景', '中景', '全景', '远景'].find((s) => edit.scene.includes(s));
          if (sizeHit) agent.shotSize = sizeHit;
          // 新画面里提到了别的地点（「改成她在阳台」）→ 地点跟着换；
          // 一句地点都没提就保留原地点，别因为改一句台词就把它清空
          const placeHit = detectPlaceOf(edit.scene);
          if (placeHit) agent.place = placeHit;
          node.gen = node.gen || {};
          // 重拼也走同一个 shotPromptOf：有人物参考图的镜头不能把「长相按参考图」那句丢了
          node.gen.prompt = shotPromptOf(agent, edit.scene);
          // 画面改了，之前那张图就不是这一镜了
          dropGenResult(node);
          // 画面改了：这一镜同时是上一段视频的尾帧、又可能是下一段视频的首帧，
          // 两段都得重拼（尾帧写的是「停在哪」，首帧写的是「从哪开始」）
          for (const video of list.filter((n) => n.agent.role === 'video' && n.agent.toShot === index)) {
            video.agent = video.agent || {};
            video.agent.endState = edit.scene;
            video.gen = video.gen || {};
            rebuiltVideoPrompt(video);
            dropGenResult(video);
            rerenderNode(video);
          }
          for (const video of list.filter((n) => n.agent.role === 'video' && (n.agent.fromShot || n.agent.shotIndex) === index)) {
            rebuiltVideoPrompt(video);
            dropGenResult(video);
            rerenderNode(video);
          }
        }
        // 改收尾：这一镜的落点变了，以它为尾帧的那段视频提示词要跟着换
        if (edit.endState) {
          agent.endState = edit.endState;
          for (const video of list.filter((n) => n.agent.role === 'video' && (n.agent.fromShot || n.agent.shotIndex) === index)) {
            video.agent.endState = edit.endState;
            video.gen = video.gen || {};
            rebuiltVideoPrompt(video);
            dropGenResult(video);
            rerenderNode(video);
          }
        }
        if (edit.motion) agent.motion = edit.motion;
        if (edit.dialogue !== undefined) agent.dialogue = edit.dialogue;
        if (edit.duration) {
          node.gen = node.gen || {};
          const preset = videoPresetOf(S.videoModels.find((m) => m.id === node.gen.modelId));
          const durations = (preset?.durations || []).map(Number).filter((n) => n > 0);
          const target = Number(edit.duration) || DEFAULT_SHOT_DURATION;
          node.gen.duration = String(
            durations.length
              ? durations.reduce((best, cur) => (Math.abs(cur - target) < Math.abs(best - target) ? cur : best), durations[0])
              : target,
          );
        }
        rerenderNode(node);
        touched += 1;

        // 对应的视频节点：提示词是「从首帧怎么动到尾帧」，跟着改；运镜变了视频就得重出
        if (edit.motion) {
          // 视频一段跨两个镜头，起点是 fromShot（老画布只有 shotIndex，认它）
          const videos = list.filter((n) => n.agent.role === 'video' && (n.agent.fromShot || n.agent.shotIndex) === index);
          for (const video of videos) {
            video.agent.motion = edit.motion;
            video.gen = video.gen || {};
            rebuiltVideoPrompt(video);
            dropGenResult(video);
            rerenderNode(video);
          }
        }
      }
    }
    if (touched) {
      markDirty();
      updateRunBar();
      drawMinimap();
    }
    return touched;
  }

  /** 改之前把「正在生成」这件事停掉，不然回来的结果会把新提示词的图覆盖成旧画面 */
  function resetEditTarget(node) {
    if (!PENDING.has(node.gen?.status)) return;
    node.gen.status = '';
    node.gen.progress = '';
  }

  /** 清掉一张已出的图/视频，让它重新进「待生成」 */
  function dropGenResult(node) {
    if (!node.gen) return;
    node.gen.resultUrl = '';
    node.gen.status = '';
    node.gen.error = '';
    node.gen.taskId = '';
    node.gen.progress = '';
  }

  /** 发一句话，按返回的意图决定要不要铺节点 / 改节点 / 开跑 */
  async function sendChat(rawText) {
    const st = chat();
    const input = $('#ncChatInput');
    const photos = chatPhotos();
    let raw = String(rawText ?? input?.value ?? '').trim();
    // 只贴图没写字：给一句明确的指令，别让服务端收到空消息
    if (!raw && photos.length) raw = `按这 ${photos.length} 张人物参考图拍一段分镜`;
    if (!canSendChat(raw, photos.length)) return;

    if (input) {
      input.value = '';
      autoGrowChatInput();
    }
    const photoIds = photos.map((p) => p.id);
    // 先落一条用户消息：万一这一轮超时 / 应用被关掉，回头还能看见自己说过什么
    st.messages.push({ role: 'user', text: raw, photos: photoIds.length ? photos.slice() : undefined, at: Date.now() });
    chat().photos = [];
    renderChatPhotos();
    st.busy = true;
    setChatSub('想一想…');
    renderChatLog();
    persistChat();

    try {
      const result = await api('/api/canvas/chat', {
        method: 'POST',
        body: {
          messages: st.messages.map((m) => ({ role: m.role, content: m.text })),
          scene: sceneSummary(),
          prefs: chatPrefs(),
          photoIds,
        },
        timeoutMs: CHAT_TIMEOUT_MS,
      });

      const msg = { role: 'assistant', text: result.reply || '好。', at: Date.now() };
      st.messages.push(msg);

      if (result.intent === 'plan' && result.plan) {
        const prefs = chatPrefs();
        const scriptNode = layOutPlan(result.plan, {
          ratio: prefs.ratio,
          imageModelId: prefs.imageModelId,
          videoModelId: prefs.videoModelId,
        });
        if (scriptNode) {
          msg.action = 'run';
          toast(`已铺 ${result.plan.shots.length} 个镜头${prefs.videoModelId ? ' + 视频节点' : ''}`);
        }
      } else if (result.intent === 'revise') {
        const changed = applyChatEdits(result.edits);
        if (changed) {
          msg.action = 'run';
        } else {
          msg.text = `${msg.text}\n（画布上没找到对应的镜头，先铺一份分镜再来改？）`;
        }
      } else if (result.intent === 'run') {
        msg.action = 'run';
        // 用户明说要跑，就别让他再点一次；失败了气泡里还能再点
        setTimeout(() => runAgentPlan(), 260);
      }
    } catch (err) {
      st.messages.push({ role: 'error', text: err.message || '这一句没跑通', at: Date.now() });
    } finally {
      st.busy = false;
      setChatSub('');
      renderChatLog();
      // 含 action / 报错气泡一起存：重开画布时「一键生成」按钮还得在原位
      persistChat(0);
    }
  }

  /**
   * 新对话：只把「当前指针」挪开，上一段留在历史里。
   * 想复用就点右上角那个历史按钮翻回去，不想要了在那儿删。
   */
  function resetChat() {
    const st = chat();
    if (st.busy) {
      toast('还在想上一条，等一下再清', true);
      return;
    }
    st.messages = [];
    st.chatId = null;
    st.photos = [];
    // 本地的 chatId 也得清：不然切走再切回来，loadGraph 又把上一段恢复出来
    if (S.graph) S.graph.chatId = null;
    const local = S.graphs.find((g) => g.id === S.graph?.id);
    if (local) local.chatId = null;
    renderChatPhotos();
    renderChatLog();
    renderChatHistory();
    setChatSub('');
    persistChat(0);
    toast('开了个新对话，上一段留在「历史对话」里');
  }

  /* ------------------------------ 历史对话（复用） ------------------------------ */

  const chatSessions = () => (Array.isArray(S.graph?.chats) ? S.graph.chats : []);

  /** 历史列表：最近改过的排最前，点一段就在这儿接着聊 */
  function renderChatHistory() {
    const list = $('#ncChatHistoryList');
    if (!list) return;
    const sessions = chatSessions()
      .filter((c) => (c.messages || []).length)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    const count = $('#ncChatHistoryCount');
    if (count) count.textContent = sessions.length ? `${sessions.length} 段` : '';

    const sig = [
      chat().chatId || '',
      sessions.map((c) => `${c.id}:${(c.messages || []).length}:${c.updatedAt || 0}`).join(','),
    ].join('|');
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.textContent = '';

    if (!sessions.length) {
      const empty = document.createElement('div');
      empty.className = 'nc-chat-history-empty';
      empty.textContent = '还没有别的对话。点「新对话」后，现在这段会留在这里，随时能翻回来接着聊。';
      list.appendChild(empty);
      return;
    }

    for (const session of sessions) {
      const row = document.createElement('div');
      row.className = 'nc-chat-history-row';
      if (session.id && session.id === chat().chatId) row.classList.add('on');

      const main = document.createElement('button');
      main.type = 'button';
      main.className = 'nc-chat-history-main';
      const title = document.createElement('span');
      title.className = 'nc-chat-history-title';
      title.textContent = session.title || chatTitleOf(session.messages) || '（未命名对话）';
      const meta = document.createElement('span');
      meta.className = 'nc-chat-history-meta';
      meta.textContent = `${(session.messages || []).length} 条 · ${fmtChatTime(session.updatedAt)}`;
      main.append(title, meta);
      main.addEventListener('click', () => loadChatSession(session));

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'nc-chat-history-del';
      del.title = '删掉这段对话（画布上的节点不动）';
      del.textContent = '✕';
      del.addEventListener('click', (event) => {
        event.stopPropagation();
        deleteChatSession(session);
      });

      row.append(main, del);
      list.appendChild(row);
    }
  }

  /** 切到某段历史对话：把它装回聊天窗，「接着上次」的气泡原样回来 */
  function loadChatSession(session) {
    const st = chat();
    if (st.busy) {
      toast('还在想上一条，等一下再切', true);
      return;
    }
    st.chatId = session.id || null;
    st.messages = (session.messages || []).map((m) => ({ ...m }));
    st.photos = [];
    if (S.graph) S.graph.chatId = st.chatId;
    const local = S.graphs.find((g) => g.id === S.graph?.id);
    if (local) local.chatId = st.chatId;
    renderChatPhotos();
    renderChatLog();
    renderChatHistory();
    setChatSub(chatSubText());
    // 顺手把「现在用的是这段」存回去，下次开画布直接落在这段
    if (st.chatId) persistChat(0);
    toast(`接着「${session.title || '这段对话'}」聊`);
  }

  async function deleteChatSession(session) {
    const st = chat();
    if (!S.graph || !session.id) return;
    if (st.busy) {
      toast('还在想上一条，等一下再删', true);
      return;
    }
    try {
      await api(`/api/node-canvases/${S.graph.id}/chat/${session.id}`, { method: 'DELETE' });
    } catch (err) {
      toast(err.message || '这段对话删不掉', true);
      return;
    }
    for (const holder of [S.graph, S.graphs.find((g) => g.id === S.graph?.id)]) {
      if (Array.isArray(holder?.chats)) holder.chats = holder.chats.filter((c) => c.id !== session.id);
    }
    if (st.chatId === session.id) {
      st.chatId = null;
      st.messages = [];
      if (S.graph) S.graph.chatId = null;
      renderChatLog();
      setChatSub('');
    }
    renderChatHistory();
    toast('删掉了这段对话，画布上的节点没动');
  }

  /** 快捷条：有分镜才出现「一键生成」和「统一尺寸」 */
  function updateChatQuick() {
    const list = agentNodes();
    const todo = list.filter(needRun).length;
    const run = $('#ncChatQuick [data-quick="run"]');
    if (run) {
      run.hidden = !list.length;
      run.textContent = todo ? `一键生成（${todo}）` : '一键生成';
      run.disabled = Boolean(S.agentRun?.busy) || !todo;
    }
    // 统一尺寸：一个镜头都没有时没意义（新铺分镜时参数面板里选过就带着走了）
    const ratio = $('#ncChatQuick [data-quick="ratio"]');
    if (ratio) ratio.hidden = !list.length;
  }


  /** 下拉框填充：value 对得上就选中（linkedom 里 select.value 赋值不生效，所以同时标 selected） */
  function fillSelect(sel, items, value) {
    if (!sel) return;
    sel.textContent = '';
    for (const item of items) {
      const opt = document.createElement('option');
      opt.value = item.value;
      opt.textContent = item.label;
      if (item.value === value) {
        opt.selected = true;
        // 属性也写一份：有些环境读不到 select.value，只能靠这个找当前项
        opt.setAttribute('selected', '');
      }
      sel.appendChild(opt);
    }
    if (value != null) {
      try {
        sel.value = value;
      } catch (err) {
        /* 个别环境不认就算了，靠 selected 兜住 */
      }
    }
  }

  /** 读下拉框当前值：优先 select.value，拿不到就退回带 selected 属性的 option */
  function readSelect(sel, fallback = '') {
    const el = typeof sel === 'string' ? $(sel) : sel;
    if (!el) return fallback;
    const value = el.value;
    if (value) return value;
    const chosen = [...(el.querySelectorAll?.('option') || [])].find((opt) => opt.hasAttribute?.('selected'));
    return chosen?.value ?? fallback;
  }

  /**
   * 默认出图模型：**本机豆包桥优先** —— 不用 Key、不占额度，参考图直接走 CDP 传本机文件，
   * 比任何一家 API 都省事；没有豆包时才退回「能图生图的第一个」（分镜要靠参考图锁人物）。
   */
  function pickDefaultImageModel() {
    const enabled = S.imageModels.filter((m) => m.enabled !== false);
    return enabled.find((m) => m.protocol === 'doubao')
      || enabled.find((m) => m.supportsImageInput)
      || enabled[0]
      || null;
  }

  /** 同上：视频也默认本机豆包桥（免 Key，首帧图在本机不外传），没有就退回第一个 */
  function pickDefaultVideoModel() {
    const enabled = S.videoModels.filter((m) => m.enabled !== false);
    return enabled.find((m) => m.provider === 'doubao') || enabled[0] || null;
  }

  function renderDirectorForm() {
    const info = S.director.info || {};
    const prefs = readDirectorPrefs();

    const shotItems = [];
    for (let n = info.minShots || 2; n <= (info.maxShots || 12); n += 1) {
      shotItems.push({ value: String(n), label: `${n} 个镜头` });
    }
    fillSelect($('#ncDirShots'), shotItems, String(prefs.shotCount || info.defaultShots || 6));

    const styleItems = (S.director.styles || []).map((s) => ({ value: s.prompt, label: s.label }));
    if (!styleItems.length && info.defaultStyle) styleItems.push({ value: info.defaultStyle, label: '默认' });
    fillSelect($('#ncDirStyle'), styleItems, prefs.style || info.defaultStyle || styleItems[0]?.value);

    fillSelect($('#ncDirRatio'), DIRECTOR_RATIOS.map((r) => ({ value: r.id, label: r.label })), prefs.ratio || DEFAULT_DIRECTOR_RATIO);

    fillSelect(
      $('#ncDirImageModel'),
      S.imageModels.filter((m) => m.enabled !== false).map((m) => ({ value: m.id, label: m.name })),
      prefs.imageModelId || pickDefaultImageModel()?.id,
    );

    const videoDefault = prefs.videoModelId === undefined ? pickDefaultVideoModel()?.id || '' : prefs.videoModelId;
    fillSelect(
      $('#ncDirVideoModel'),
      [{ value: '', label: '关（只铺图片）' }, ...S.videoModels.filter((m) => m.enabled !== false).map((m) => ({ value: m.id, label: m.name }))],
      videoDefault,
    );

    updateDirectorHint();
  }

  function updateDirectorHint() {
    const el = $('#ncDirHint');
    if (!el) return;
    updateDirectorModelBtn();
    const info = S.director.info || {};
    const imageModel = S.imageModels.find((m) => m.id === readSelect('#ncDirImageModel')) || null;
    const tips = [];
    if (info.hasModel === false) {
      tips.push('还没配置写剧本的模型，编导跑不了 —— 点下面的「写剧本的模型」填一个 OpenAI 兼容的对话模型。');
    } else if (info.modelIsVision) {
      tips.push(`写剧本用的是多模态模型 ${info.modelName}，让它照着你给的故事拆经常自己另编一个 —— 换成纯文本模型（如 glm-4.5-flash）会稳很多。`);
    }
    if (imageModel && imageModel.supportsImageInput === false) {
      tips.push('这个出图模型没开图生图，跨镜头的人物会长得不一样；建议换成带图生图的模型。');
    }
    if (!readSelect('#ncDirVideoModel')) tips.push('视频关了：只铺镜头图，不建视频节点。');
    // 豆包桥没有尺寸接口：比例交给它（会去界面上点），点不中就在抓图时按比例裁
    if (imageModel && S.imageDefaults?.ratioPresets?.[imageModel.protocol] === undefined) {
      tips.push('本机豆包默认出方图：这里选的画幅会去豆包界面上切，切不到就按这个比例居中裁剪（会裁掉一点边）。');
    }
    el.hidden = !tips.length;
    el.textContent = tips.join(' ');
  }

  /**
   * 「写剧本的模型」按钮：显示当前模型名，点一下开「剧本模型」弹窗（复用分镜那边的）。
   * 没配模型时按钮标红文案，省得用户一直猜编导为什么不跑。
   */
  function updateDirectorModelBtn() {
    const btn = $('#ncDirModelBtn');
    if (!btn) return;
    const info = S.director.info || {};
    const name = info.modelName || '';
    btn.textContent = info.hasModel ? (name || '已配置') : '未配置 · 点此设置';
    btn.title = info.hasModel
      ? `当前写剧本用的是 ${name}，点此更换`
      : '还没配编导模型，AI 编导跑不起来，点此设置';
    btn.classList.toggle('danger', !info.hasModel);
    btn.classList.toggle('warn', Boolean(info.hasModel && info.modelIsVision));
  }

  function openDirectorModelModal() {
    const story = window.RHStoryboard;
    if (story?.openModelModal) {
      story.openModelModal();
      return;
    }
    // 分镜模块没加载（理论不会发生，storyboard.js 在 nodecanvas.js 之前引入）：
    // 退化成把用户送去「视频生成 → 分镜」
    if (window.RHUtil?.switchModule) window.RHUtil.switchModule('video');
    window.RHUtil?.toast?.('在「视频生成 → 分镜」里点「剧本模型」设置', true);
  }

  function saveDirectorPrefsFromForm() {
    writeDirectorPrefs({
      shotCount: Number(readSelect('#ncDirShots')) || 6,
      style: readSelect('#ncDirStyle'),
      ratio: readSelect('#ncDirRatio', DEFAULT_DIRECTOR_RATIO) || DEFAULT_DIRECTOR_RATIO,
      imageModelId: readSelect('#ncDirImageModel'),
      videoModelId: readSelect('#ncDirVideoModel'),
    });
  }

  function setChatOpenPref(open) {
    try {
      localStorage.setItem(CHAT_OPEN_KEY, open ? '1' : '0');
    } catch (err) {
      /* 隐私模式写不了无所谓，下次进画布还是默认打开 */
    }
  }

  async function openDirector({ focus = true } = {}) {
    const el = $('#ncChat');
    if (!el) return;
    closePicker();
    if (isCmdOpen()) closeCmd();
    if (!$('#ncHelp')?.hidden) closeHelp();
    if (isSkinOpen()) closeSkinPanel();
    if (S.find.open) closeFind();
    renderDirectorForm();
    el.hidden = false;
    // 编辑面板要给聊天窗让位：CSS 靠这个 class 把面板在剩下的空间里居中
    $('#ncViewport')?.classList.add('nc-chat-open');
    setChatOpenPref(true);
    renderChatLog();
    renderChatHistory();
    // 复用旧对话时给个明确信号，否则分不清这是刚聊的那段还是新开的
    setChatSub(chatSubText());
    // 画风列表在服务端（以后要加画风只改一处），第一次打开时取一次
    if (!S.director.styles && !chat().loaded) {
      chat().loaded = true;
      try {
        S.director.info = await api('/api/canvas/director');
        S.director.styles = S.director.info?.styles || [];
        renderDirectorForm();
      } catch (err) {
        chat().messages.push({ role: 'error', text: `读取编导配置失败：${err.message}` });
        renderChatLog();
      }
    }
    if (focus) $('#ncChatInput')?.focus?.();
  }

  function closeDirector() {
    const el = $('#ncChat');
    if (el) el.hidden = true;
    $('#ncViewport')?.classList.remove('nc-chat-open');
    setChatOpenPref(false);
  }

  /* ------------------------------ 铺节点 ------------------------------ */

  const stripTail = (value) => String(value || '').trim().replace(/[。，、；,;.]+$/, '');
  const joinCn = (parts) => parts.map(stripTail).filter(Boolean).join('，') + '。';

  /**
   * 把「这次想改什么」拼进提示词。
   *
   * 追加而不是替换：上一次的镜头描述通常是有效的，人想改的往往只是其中一两个细节
   * （「把窗帘改成拉开」「脸别那么亮」）。整段重写反而会丢掉已经调好的部分。
   *
   * 插在负面词之前 —— 负面词是「别画什么」的兜底，它排在最末才能压住画崩；
   * 新要求塞到它后面会被抢掉权重。
   */
  const REVISE_MARK = '不要出现：';
  function withRevise(prompt, advice) {
    const base = String(prompt || '').trim();
    const add = String(advice || '').trim();
    if (!add) return base;
    const at = base.indexOf(REVISE_MARK);
    const head = at >= 0 ? base.slice(0, at) : base;
    const tail = at >= 0 ? base.slice(at) : '';
    return joinCn([head, `这次的要求：${add}`, tail]);
  }

  /**
   * 角色名 → 「图N」的对照表。
   *
   * 参考图是按连线顺序（refCandidates）发给模型的，模型只认「第几张图」，
   * 认不出「稚圭」这三个字对应哪一张。所以第 i 张图上的角色，名字就是图(i+1)。
   * 只收 cast 节点：视频节点连的是首尾帧，不是人物图，不能拿来编号。
   */
  function refNameMap(node) {
    const map = new Map();
    const list = refCandidates(node);
    list.forEach((cand, index) => {
      // 只认定妆照节点：镜头图节点的 agent.character 是「稚圭、贺小凉」整串，
      // 拿它当名字会把两个人都换成同一个图号；视频节点连的是首尾帧，更不能编号
      if (cand.agent?.role !== 'cast') return;
      const name = String(cand.agent?.character || '').trim();
      if (!name || map.has(name)) return;
      map.set(name, refLabel(index));
    });
    return map;
  }

  /**
   * 纯替换：把 pairs 里的名字逐个换成图号。
   *
   * 长名字先换 —— 「贺小凉」要是排在「贺小」后面，就会被切一半换成「图1凉」。
   * 单独拆出来是为了能脱离画布数据直接测（withRefNames 还要读连线）。
   */
  function replaceRefNames(text, pairs) {
    let out = String(text || '');
    if (!out) return out;
    const list = (pairs || [])
      .filter(([name, label]) => name && label)
      .sort((a, b) => String(b[0]).length - String(a[0]).length);
    for (const [name, label] of list) out = out.split(String(name)).join(String(label));
    return out;
  }

  /**
   * 提交前把提示词里的角色名换成「图N」。
   *
   * 用户点出来的问题：节点里挂着两张参考图（图1、图2），提示词里却写「稚圭」「贺小凉」——
   * 模型没法把名字和图对上，只能猜，实测就是两张脸互相串。
   * 换成人人能对的序号，模型才知道「图1 是这个人、图2 是那个人」。
   *
   * 只在这一步换：提示词框里仍然是名字，人改起来才看得懂。
   */
  function withRefNames(prompt, node) {
    const map = refNameMap(node);
    if (!map.size) return String(prompt || '');
    return replaceRefNames(prompt, [...map.entries()]);
  }

  /**
   * 提交前补一句「房间以哪张图为准」。
   *
   * 场景板是画布上的环境空镜，但它连过来只是一张参考图 —— 模型不知道这张图
   * 是「这个房间的标准样子」，会当成一张普通的参考照片，该换还是换。
   * 所以要在提交那一刻点名：背景房间以图N为准。
   *
   * 编号必须和 refCandidates 的顺序一致（人排在前、场景板在后），
   * 所以这里现算，不能写死在铺节点的时候。
   */
  function withSceneRef(prompt, node) {
    const text = String(prompt || '');
    const list = refCandidates(node);
    const index = list.findIndex((cand) => cand.agent?.role === 'scene');
    if (index < 0) return text;
    const place = String(list[index].agent?.place || '').trim();
    const line = place
      ? `背景房间就是${refLabel(index)}里那个${place}，墙面、家具位置、光线方向都以它为准，不要改动`
      : `背景房间以${refLabel(index)}为准，墙面、家具位置、光线方向都不要改动`;
    if (text.includes(line)) return text;
    return joinCn([text, line]);
  }

  /* --------------- 有人物参考图时，文字只写衣服和动作 --------------- */

  /**
   * 服饰类片段：带参考图时，文字只需要锁住「穿什么」。
   * 五官 / 发型 / 身材 / 气质这些交给参考图 —— 再写一遍就是让模型拿文字覆盖图，
   * 实测越写越不像（用户直接点出来了：图都给了还写两百字长相）。
   */
  const OUTFIT_RE = /(穿|戴|系|披|套|衣|裙|袍|衫|恤|裤|袜|鞋|靴|帽|纱|甲胄|礼服|正装|西装|制服|汉服|和服|旗袍|上衣|下装|卫衣|毛衣|风衣|大衣|马甲|坎肩|披肩|披帛|围巾|手套|腰带|腰封|束腰|吊带|背带|牛仔|羽绒|棉服|运动服|校服|工装|丝袜|黑丝|高跟|平底|拖鞋|凉鞋|领带|领结|领口|袖|扣|胸针|项链|耳环|耳坠|手镯|手链|戒指|配饰|首饰|簪|冠|背包|挎包|双肩包|帆布包|手袋|眼镜|墨镜)/;
  /** 长相类片段：有人物参考图时一句都不留 */
  const LOOK_RE = /(发|脸|眼|眉|唇|嘴|鼻|肤|肌|身材|体型|身高|体重|岁|年龄|五官|长相|容貌|气质|神情|表情|妆)/;

  /** 从一段角色描写里只挑出「衣服」那几句，其余丢掉 */
  function outfitOf(desc) {
    return String(desc || '')
      .split(/[，,。；;、/]/)
      .map((s) => s.trim())
      .filter((s) => s && OUTFIT_RE.test(s) && !LOOK_RE.test(s))
      .join('，');
  }

  /** 这个角色的长相是不是已经由用户上传的参考图定了 */
  const hasRefPhoto = (character) => Boolean(character && character.fromPhoto && character.refUrl);

  /** 挂了参考图时补在提示词末尾的一句：明确告诉模型长相别自己发挥 */
  const REF_KEEP = '人物长相、发型和肤色以参考图为准，不要改动';

  /**
   * 负面词：这一版所有渠道（豆包桥 / OpenAI 兼容 / 智谱 / 万相）都**没有** negative_prompt 参数，
   * 只能像用户手写的豆包提示词那样，把「不要什么」写成一句自然语言塞在提示词里。
   *
   * 只在镜头图用，视频提示词不加 —— 视频提示词已经很长，再塞负面词会跟动作描述抢权重。
   */
  const NEGATIVE_WORDS = '畸形的手指，多出来的手指，扭曲的肢体，糊掉的脸，重影，多余的人，文字，水印，噪点';

  /**
   * 常见地点词 —— 和服务端 `canvas-director.js` 里那份保持一致。
   *
   * 免费小模型写 scene 经常只写「谁在做什么」，地点一句不写，出图时模型自己编一个房间。
   * 这个表用来从画面文字里认出「这镜在哪」，认不出来就继承上一镜。
   */
  const PLACE_WORDS = [
    '厨房', '客厅', '卧室', '书房', '餐厅', '阳台', '卫生间', '浴室', '玄关', '走廊', '楼梯',
    '院子', '庭院', '花园', '屋顶', '天台', '地下室', '车库', '电梯', '办公室', '会议室',
    '教室', '图书馆', '医院', '病房', '咖啡店', '咖啡厅', '酒吧', '书店', '超市',
    '商场', '街道', '街头', '马路', '巷子', '小巷', '广场', '公园', '河边', '海边', '湖边',
    '山上', '山顶', '森林', '树林', '田野', '草原', '沙漠', '雪地', '地铁站', '车站', '机场',
    '火车站', '码头', '船上', '车内', '车里', '操场', '球场', '泳池', '健身房',
    '宫殿', '大殿', '闺房', '客栈', '酒楼', '茶馆', '庙', '寺', '牢房', '营帐',
    '床前', '床边', '窗前', '窗边', '门口', '门前', '桌边', '灶台', '灶前',
  ];

  /** 从一段画面文字里认出地点（取最先出现、最具体的那个词） */
  function detectPlaceOf(value) {
    const body = String(value || '');
    let best = '';
    let bestAt = Infinity;
    for (const word of PLACE_WORDS) {
      const at = body.indexOf(word);
      if (at < 0) continue;
      if (at < bestAt || (at === bestAt && word.length > best.length)) {
        best = word;
        bestAt = at;
      }
    }
    return best;
  }

  /**
   * 出镜角色的提示词片段。
   *   有参考图 → 只写衣服（+「长相按参考图」）；没参考图 → 照旧写全（文字是它唯一的长相来源）
   */
  function castPartOf(names, characters) {
    return (names || [])
      .map((name) => {
        const character = characters.find((c) => c.name === name);
        if (!character) return String(name);
        if (!hasRefPhoto(character)) return `${name}：${character.desc}`;
        const outfit = outfitOf(character.desc);
        return outfit ? `${name}：${outfit}` : String(name);
      })
      .join('；');
  }

  /** 这一镜里有没有「长相已由参考图决定」的角色 */
  function shotHasRefCast(names, characters) {
    return (names || []).some((name) => hasRefPhoto(characters.find((c) => c.name === name)));
  }

  /** 画幅 → 构图提示。豆包默认出方图，出图后要按目标比例居中裁，
   *  画面里先写明「竖版、主体居中」，裁的时候才不至于把人和脸切掉。 */
  function frameHintOf(ratio) {
    const [w, h] = String(ratio || '').split(':').map(Number);
    if (!(w > 0 && h > 0)) return '';
    if (Math.abs(w - h) < 0.01) return '方形构图，主体居中';
    return w < h ? '竖版构图，主体居中' : '横版构图，主体居中';
  }

  /**
   * 把「全片设定 / 画风 / 光线」几段按逗号合并成一句，重复的片段只留一次。
   *
   * 这三段本来就重叠：「35mm 胶片质感」「浅景深」「自然光」在设定和画风里各写了一遍。
   * 原样拼起来，那几个词在提示词里出现两次，权重翻倍 —— 图像模型会更卖力地表现质感，
   * 人却变小、动作不执行。所以以先出现的为准，后面重复的（含互相包含的）直接剔掉。
   */
  function mergeLook(...values) {
    const out = [];
    for (const value of values) {
      const segs = String(value || '').split(/[，,]/).map((s) => s.trim()).filter(Boolean);
      for (const seg of segs) {
        // 一方含另一方就算同一件事（「35mm 胶片质感」和「胶片质感」）
        if (out.some((kept) => kept.includes(seg) || seg.includes(kept))) continue;
        out.push(seg);
      }
    }
    return out.join('，');
  }

  /**
   * 镜头图的提示词：按「画面（主体+动作+环境）→ 地点 → 衣服 → 景别 → 光线 → 画风 → 画幅」拼。
   *
   * 顺序不是随便定的。图像模型对**开头**的词权重最高，以前把画风顶在最前面，
   * 「电影感、胶片质感」这些占了主语的位置，结果就是人变小、动作不执行、光线也不听。
   * 主体和动作必须是第一句，画风只是收尾的基调，长相约束永远在最后。
   *
   * 地点是「同一场戏不跳场」的唯一硬约束：免费小模型写 scene 经常只写谁在做什么，
   * 一句地点都没有，出图时模型自己编一个房间 —— 于是上一镜厨房、下一镜客厅。
   * 所以这里单独补一句「场景：厨房」，scene 里已经写了就不重复。
   */
  function shotPromptOf({ stylePrompt, setting, castDesc, refKeep, shotSize, angle, light, frame, place, negative = true }, scene) {
    const body = String(scene || '');
    const parts = [body];
    // 地点：scene 里已经提到就不重复（同一句话说两遍会把权重翻倍）
    if (place && !body.includes(place)) parts.push(`场景：${place}`);
    if (castDesc) parts.push(castDesc);
    // 景别 / 机位 / 光线已经写进画面里就不重复 —— 同一句话说两遍会把权重翻倍，画面反而更死板
    if (shotSize && !body.includes(shotSize)) parts.push(shotSize);
    if (angle && !body.includes(angle)) parts.push(`机位：${angle}`);
    if (light && !body.includes(stripTail(light))) parts.push(light);
    // 全片统一设定（空间基调 / 时间 / 光线质感 / 色调 / 画质）：每一镜都带同一段，
    // 这是整片「看起来是一部片子」的锚。放在画风前面 —— 它是具体的，画风是抽象的。
    // 它和画风本来就重叠（都写了胶片质感/浅景深），合并时去重，别让那几个词权重翻倍。
    const look = mergeLook(setting, stylePrompt);
    if (look && !body.includes(look)) parts.push(look);
    if (frame) parts.push(frame);
    if (refKeep) parts.push(REF_KEEP);
    // 负面词放最末：这一版所有渠道都没有 negative_prompt 参数，只能当一句话塞进来。
    // 排在人脸约束之后，是让「别崩脸」压过「别多手指」这类小毛病。
    if (negative) parts.push(`不要出现：${NEGATIVE_WORDS}`);
    return joinCn(parts);
  }

  /** 画面描述截短：视频提示词里只用来「对锚」，整段照抄会跟动作描述抢权重 */
  const anchorOf = (value, max = 40) => {
    const clean = stripTail(value);
    return clean.length > max ? `${clean.slice(0, max)}…` : clean;
  };

  /**
   * 一段镜头视频的提示词：**首帧和尾帧两头都要交代**。
   *
   * 成片是「图 1 → 视频 → 图 2 → 视频 → 图 3」串起来的，一段视频的两端已经被两张图钉死。
   * 以前只写「怎么动 → 结束时要落到 X」，起点一个字不提：模型拿到首帧图也会自己另起一个姿势，
   * 中间的动作又没说是奔着尾帧去的，结果就是「首尾各演各的」（用户原话：不兼顾首尾帧）。
   * 现在固定按「从哪开始 → 中间怎么动 → 停在哪 → 什么不许变」四段写。
   *
   * 尾帧那句要看模型，分三种：
   *   · ark pro / 万相 kf2v 真收尾帧图 → 明说「两张图是第一帧和最后一帧」；
   *   · 本地豆包没有尾帧参数，但收多张图 → 说清「第一张是起点、第二张是这段要走到哪」；
   *   · 其余只吃一张图的 → 不提「两张图」（那张图根本没传，说了是撒谎），把终点写细。
   */
  function videoPromptOf(source, { modelId } = {}) {
    const start = anchorOf(source?.scene);
    const motion = stripTail(source?.motion);
    // 接力：首帧用的是「上一段视频的最后一帧」，不是本镜的镜头图。
    // 这时候还写「开头沿用首帧图：<本镜画面>」就是拿 A 的图配 B 的词 ——
    // 模型会照文字重新构图，接力白做了。必须如实说「接着上一段走」。
    const relay = Boolean(source?.relay);
    // 地点：一段视频通常只在一个地方演完。模型（尤其免费小模型）拿到首帧图也会
    // 自己把人物「搬」到别的房间去，一句「全程待在厨房」比什么运镜描述都管用。
    //
    // 但有一段视频是**跨地点**的（上一镜在卧室、下一镜在厨房）：这时候还写
    // 「全程都在卧室、不许换场」就跟尾帧图正面打架，模型只能二选一 ——
    // 要么无视尾帧（继续待在卧室），要么硬跳（人物瞬移进厨房）。
    // 跨地点时要写的是「怎么走过去的」，把换场变成一个可见的动作。
    const place = stripTail(source?.place);
    const toPlace = stripTail(source?.toPlace);
    const crosses = Boolean(place && toPlace && place !== toPlace);
    // 跨地点时尾帧就是下一镜的镜头图，落点描述必须换成下一镜的画面；
    // 用本镜的 endState（还留在卧室里）会跟「结束时要到厨房」自相矛盾。
    const endState = anchorOf(crosses ? (source?.toScene || source?.endState) : source?.endState);
    const placeLine = crosses
      ? `这一段的起点在${place}，结束时要到${toPlace}：中间必须演出来人物怎么从${place}走到${toPlace}（起身、推门、走过走廊这一类过程），不要凭空跳场`
      : place
        ? `全程都在${place}，不要换到别的房间或场景`
        : '';
    // 画风 / 光线：镜头图的提示词里一直有，视频提示词以前一句没写 ——
    // 图是「电影感胶片质感」，视频却自己渲染成另一种色调，拼起来一截一个样。
    // 放在地点之后、固定约束之前，作为「全片统一」的基调锚。
    // 全片统一设定（空间基调 / 时间 / 光线质感 / 色调 / 画质）也带上：
    // 镜头图每一张都有这一段，视频里不写的话模型会按自己的默认调子重新渲染。
    // 三段本来就重叠，合并去重，别让「胶片质感」在提示词里出现两遍。
    const look = mergeLook(source?.setting, source?.style, source?.light);
    const lookLine = look ? `画面风格和光线跟首帧图保持一致，${look}` : '';
    const takesEnd = Boolean(modelId) && modelTakesLastFrame(modelId);
    const takesTwo = Boolean(modelId) && modelTakesSecondImage(modelId);
    // 第一句的说法取决于首帧到底是什么：本镜镜头图，还是上一段视频的末帧。
    const openLine = relay
      ? '开头这一帧就是上一段视频的最后一帧，直接接着它往下演，不要重新构图'
      : start
        ? `开头沿用首帧图：${start}`
        : '';
    const parts = takesEnd
      ? [
          openLine,
          motion ? `中间这样动：${motion}` : '',
          endState ? `最后一帧停在尾帧图：${endState}` : '',
          '两张图就是这段视频的第一帧和最后一帧，中间只演这一段过渡 —— 不切镜、不转场、不加新人物',
          placeLine,
          lookLine,
          '人物长相、发型、服装、光线和机位保持不变',
        ]
      : takesTwo
        ? [
            openLine,
            motion ? `中间这样动：${motion}` : '',
            endState ? `最后一帧要走到第二张图的画面：${endState}` : '',
            relay
              ? '给了两张图：第一张是上一段视频的最后一帧（这段的起点），第二张是这段结束时要落到的画面，中间只演这一段过渡'
              : '给了两张图：第一张是这段的起点，第二张是这段结束时要落到的画面，中间只演这一段过渡',
            '全程一个连续镜头 —— 不切镜、不转场、不加新人物',
            placeLine,
            lookLine,
            '人物长相、发型、服装、光线和机位保持不变',
          ]
        : [
          openLine,
          motion ? `中间这样动：${motion}` : '',
          endState ? `最后一帧要落到：${endState}` : '',
          '全程一个连续镜头，只演这一段动作 —— 不切镜、不转场、不加新人物',
          placeLine,
          lookLine,
          '人物长相、发型、服装、光线和机位保持不变',
        ];
    return joinCn(parts);
  }

  /**
   * 一段视频提示词的三个原料，全部按画布现状取。
   *
   * 一段视频跨两镜，两端都长在图上：首帧 = 上一镜的 scene，尾帧 = 下一镜的 scene。
   * 所以改任何一镜都要重拼**两段**视频（上一段的尾帧、下一段的首帧），
   * 统一从这里取，省得各处调用把「哪一镜」传错。
   */
  function videoPromptSource(video) {
    const agent = video.agent || {};
    const from = Number(agent.fromShot || agent.shotIndex) || 0;
    const to = Number(agent.toShot) || from + 1;
    const shots = agentNodes().filter((n) => n.agent?.role === 'shot');
    const fromShot = shots.find((n) => n.agent.shotIndex === from);
    const toShot = shots.find((n) => n.agent.shotIndex === to);
    return {
      scene: fromShot?.agent?.scene || agent.scene || '',
      motion: agent.motion || fromShot?.agent?.motion || '',
      endState: agent.endState || fromShot?.agent?.endState || toShot?.agent?.scene || '',
      // 落点那一镜的完整画面：跨地点时尾帧图其实就是下一镜的镜头图，
      // 这时候「最后一帧要走到」的描述必须用下一镜的 scene，不能用本镜的 endState
      toScene: toShot?.agent?.scene || '',
      // 地点：优先用视频节点自己记的，没有就取首帧那一镜的（同一场戏两地应一致）
      place: agent.place || fromShot?.agent?.place || '',
      // 落点地点：跨房间的那一段要靠它把「怎么走过去」写出来（见 videoPromptOf）
      toPlace: agent.toPlace || toShot?.agent?.place || '',
      // 画风 / 光线 / 全片设定：从首帧那一镜取，视频跟镜头图保持一致
      style: fromShot?.agent?.stylePrompt || agent.stylePrompt || '',
      light: fromShot?.agent?.light || agent.light || '',
      setting: fromShot?.agent?.setting || agent.setting || '',
      // 首帧到底是不是「上一段的末帧」：runVideoGen 里同一个判断（relay?.url），
      // 提示词必须跟实际上传的那张图对上口径
      relay: Boolean(relayFrameOf(video)?.url),
    };
  }

  /** 按画布现状重拼一个视频节点的提示词（材料从 videoPromptSource 取） */
  function rebuiltVideoPrompt(video) {
    video.gen = video.gen || {};
    video.gen.prompt = videoPromptOf(videoPromptSource(video), { modelId: video.gen.modelId });
    return video.gen.prompt;
  }

  /**
   * 老画布升级：提示词顺序从「画风在最前」换成「画面在最前」。
   *
   * 只改没被人动过的 —— 提示词还等于当初自动拼出来的那一句。
   * 手改过的一个字不碰；已经出好的图也不作废（这是措辞顺序，不是画面变了）。
   */
  function upgradeShotPrompts(nodes) {
    let fixed = 0;
    for (const node of nodes) {
      const agent = node.agent;
      if (!agent || agent.role !== 'shot') continue;
      const prompt = String(node.gen?.prompt || '');
      if (!prompt) continue;
      const legacy = joinCn([agent.stylePrompt, agent.castDesc, agent.scene]);
      const legacyWithKeep = agent.refKeep
        ? joinCn([agent.stylePrompt, agent.castDesc, agent.scene, REF_KEEP])
        : legacy;
      if (prompt !== legacy && prompt !== legacyWithKeep) continue;
      node.gen.prompt = shotPromptOf(agent, agent.scene);
      fixed += 1;
    }
    return fixed;
  }

  /**
   * 老画布补救：这个规则上线前铺的分镜，不管有没有参考图都把角色长相写进了提示词。
   *
   * 只改「没被人动过」的那些 —— 提示词还等于「画风 + 角色段 + 画面」原样拼出来的结果，
   * 用户自己改过的字一个都不碰（改了还硬覆盖，比不改更糟）。
   * 画面没变，已经出好的图不作废。
   */
  function repairRefPrompts(nodes) {
    // 有参考图的角色：planId|角色名 → 原来的形象描述
    const refCasts = new Map();
    for (const node of nodes) {
      const agent = node.agent;
      if (agent?.role === 'cast' && agent.fromPhoto && node.gen?.resultUrl) {
        refCasts.set(`${agent.planId}|${agent.character}`, agent.desc || '');
      }
    }
    if (!refCasts.size) return 0;

    let fixed = 0;
    for (const node of nodes) {
      const agent = node.agent;
      if (!agent || agent.role !== 'shot' || agent.refKeep) continue;
      // 提示词被手动改过：跳过。认两种「没动过」的写法 ——
      // 当前拼法，和更早那版「画风在最前」的拼法（老画布还是那种，修完顺手升级成新顺序）
      const prompt = String(node.gen?.prompt || '');
      // 认两种「没动过」的写法：带地点的新拼法，和不带地点的老拼法（地点是后加的字段）
      // negative:false —— 负面词也是后加的，老提示词里没有，比对时不能带它
      const base = {
        stylePrompt: agent.stylePrompt,
        castDesc: agent.castDesc,
        refKeep: false,
        shotSize: agent.shotSize,
        light: agent.light,
        frame: agent.frame,
        negative: false,
      };
      const untouched = shotPromptOf({ ...base, place: agent.place }, agent.scene);
      const untouchedNoPlace = shotPromptOf(base, agent.scene);
      const legacy = joinCn([agent.stylePrompt, agent.castDesc, agent.scene]);
      if (prompt !== untouched && prompt !== untouchedNoPlace && prompt !== legacy) continue;
      const names = String(agent.character || '').split('、').filter(Boolean);
      if (!names.some((name) => refCasts.has(`${agent.planId}|${name}`))) continue;

      let changed = false;
      const nextDesc = String(agent.castDesc || '')
        .split('；')
        .map((seg) => {
          const cut = seg.indexOf('：');
          if (cut < 0) return seg;
          const name = seg.slice(0, cut);
          const desc = refCasts.get(`${agent.planId}|${name}`);
          if (desc === undefined) return seg;
          const outfit = outfitOf(desc);
          if (!outfit) return seg; // 一句服饰都抽不出来，宁可留着原样
          changed = true;
          return `${name}：${outfit}`;
        })
        .join('；');
      if (!changed) continue;

      agent.castDesc = nextDesc;
      agent.refKeep = true;
      node.gen = node.gen || {};
      node.gen.prompt = shotPromptOf(agent, agent.scene);
      fixed += 1;
    }
    return fixed;
  }

  /** 老版视频提示词的结尾句（认它 = 提示词还是当初自动拼的，没被人手改过） */
  const LEGACY_VIDEO_TAIL = '这一段的首帧和尾帧已由两张图片给定，中间只做上面这一个动作';

  /**
   * 老画布补救：视频节点入边没角色（早版本存盘会把 role 洗掉）→ 按画面顺序补成首帧/尾帧。
   *
   * 补不出来就只能靠连线顺序猜，猜反了就是「首帧当尾帧」—— 提示词里说的起点
   * 和实际喂进去的第一张图不是一回事。补完存回 db，右键菜单那两条线也就有名字了。
   */
  function backfillFrameRoles() {
    let fixed = 0;
    for (const node of S.graph?.nodes || []) {
      if (node.type !== 'videoGen') continue;
      const incoming = (S.graph.edges || []).filter((e) => e.to === node.id && isImageNode(nodeById(e.from)));
      if (!incoming.length) continue;
      const rest = incoming.filter((e) => !e.role);
      let cursor = 0;
      if (!incoming.some((e) => e.role === 'start') && rest[cursor]) {
        rest[cursor].role = 'start';
        cursor += 1;
        fixed += 1;
      }
      if (!incoming.some((e) => e.role === 'end') && rest[cursor]) {
        rest[cursor].role = 'end';
        fixed += 1;
      }
    }
    return fixed;
  }

  /**
   * 老画布升级：视频提示词补上「首帧」那一头。
   *
   * 老版只写「怎么动 → 结束时要落到 X → 首尾帧已由两张图片给定」：起点一个字不提，
   * 模型拿到的首帧图就只是个参考，中间的动作也没说奔着尾帧去 —— 结果首尾各演各的。
   * 顺便把老存档被洗掉的跨镜信息（fromShot/toShot/endState）按相邻镜头补回来。
   * 只改**没被人动过**的提示词（还带着那句老结尾）；手改过的哪怕一个词都不碰。
   * 画面没变，已经出好的视频不作废。
   */
  function upgradeVideoPrompts(nodes) {
    const shots = nodes.filter((n) => n.agent?.role === 'shot');
    const shotOf = (index) => shots.find((n) => n.agent.shotIndex === index);
    let fixed = 0;
    for (const node of nodes) {
      const agent = node.agent;
      if (agent?.role !== 'video') continue;
      const from = Number(agent.fromShot || agent.shotIndex) || 0;
      // 跨镜信息被洗掉过：按「起点是本镜、终点是下一镜」补回来
      if (from >= 1) {
        if (!agent.fromShot) { agent.fromShot = from; fixed += 1; }
        if (!agent.toShot) { agent.toShot = from + 1; fixed += 1; }
        const fromShot = shotOf(from);
        const toShot = shotOf(from + 1);
        if (!agent.scene && fromShot?.agent?.scene) { agent.scene = fromShot.agent.scene; fixed += 1; }
        if (!agent.endState) {
          const end = fromShot?.agent?.endState || toShot?.agent?.scene || '';
          if (end) { agent.endState = end; fixed += 1; }
        }
      }
      const prompt = String(node.gen?.prompt || '');
      if (!prompt || !prompt.includes(LEGACY_VIDEO_TAIL)) continue;
      rebuiltVideoPrompt(node);
      if (node.gen.prompt !== prompt) fixed += 1;
    }
    return fixed;
  }

  /**
   * 剧本卡该多高：按字数估行数（宽 460 大约 32 个汉字一行），夹在 MIN/MAX 之间。
   *
   * 以前只在铺分镜那一刻算一次，之后卡就定死。改上限 / 换了剧本模型后，
   * 老画布上那张卡还是旧高度 —— 明明文字都在节点里，却只露出三分之一。
   */
  function scriptCardHeight(text, width = 460) {
    const charsPerLine = Math.max(16, Math.floor((width - 28) / 13.5));
    let visualLines = 0;
    for (const line of String(text || '').split('\n')) {
      visualLines += Math.max(1, Math.ceil(line.length / charsPerLine));
    }
    return Math.round(Math.max(SCRIPT_CARD_MIN_H, Math.min(SCRIPT_CARD_MAX_H, HEAD_H + visualLines * 21 + 22)));
  }

  /**
   * 把剧本卡的高度按当前内容重算一遍（只对「自己铺出来的剧本卡」生效）。
   *
   * 判据：文本卡 + 标题以「剧本 · 」开头。用户自己拖过高度的也不硬改 ——
   * 但剧本卡本来就标了 manualSize，拖过之后 h 会明显偏离估算值，这里只在
   * 「明显装不下」时补高，不会把用户特意缩小的卡又撑回去。
   */
  function fitScriptCards(nodes) {
    let fixed = 0;
    for (const node of nodes) {
      if (node.type !== 'text') continue;
      if (!String(node.title || '').startsWith('剧本 · ')) continue;
      const want = scriptCardHeight(node.text, node.w || 460);
      if (want <= (node.h || 0) + 4) continue;
      node.h = want;
      fixed += 1;
    }
    return fixed;
  }

  /**
   * 老画布补救：镜头节点补上「地点」。
   *
   * 地点是后加的字段，之前铺的分镜节点根本没有 agent.place —— 提示词里也就没有那句
   * 「场景：厨房」，出图全靠模型自己编房间，于是同一场戏一镜厨房一镜客厅。
   * 这里从 scene 文字里认地点；认不出来就继承上一镜（同一场戏默认不换地方）。
   * 认不出又没上一镜可继承的，留空 —— 瞎填一个地点比不填更糟。
   */
  function backfillPlaces(nodes) {
    const shots = nodes
      .filter((n) => n.agent?.role === 'shot')
      .sort((a, b) => (a.agent.shotIndex || 0) - (b.agent.shotIndex || 0));
    let carried = '';
    let fixed = 0;
    for (const node of shots) {
      const agent = node.agent;
      if (agent.place) { carried = agent.place; continue; }
      const detected = detectPlaceOf(agent.scene);
      const place = detected || carried;
      if (!place) continue;
      agent.place = place;
      carried = place;
      // 提示词是自动拼的才重拼（手改过的一个字不碰）：判据同 repairRefPrompts
      const prompt = String(node.gen?.prompt || '');
      const untouched = shotPromptOf({ ...agent, refKeep: false, place: '', negative: false }, agent.scene);
      const legacy = joinCn([agent.stylePrompt, agent.castDesc, agent.scene]);
      if (prompt === untouched || prompt === legacy) {
        node.gen = node.gen || {};
        node.gen.prompt = shotPromptOf(agent, agent.scene);
      }
      fixed += 1;
    }
    return fixed;
  }

  /** 直接落一个已经填好内容的节点（不走 addNode 的「找空位 + 单独入历史」） */
  function addAgentNode(type, x, y, patch) {
    const base = NODE_SIZE[type] || [CARD_W, 240];
    const node = {
      id: uid('nd'),
      type,
      x: Math.round(x),
      y: Math.round(y),
      w: patch.w || base[0],
      h: patch.h || base[1],
      title: patch.title || NODE_LABEL[type],
      text: patch.text || '',
      url: '',
      localUrl: '',
      localPath: '',
      fileName: '',
      assetId: '',
      gen: patch.gen || {},
      agent: patch.agent,
    };
    if (patch.manualSize) node.manualSize = true;
    S.graph.nodes.push(node);
    $('#ncNodes').appendChild(buildNode(node));
    return node;
  }

  /**
   * 图片尺寸：按模型协议的比例表查。
   *
   * 三种结果要分清：
   *   1. 表里有这个比例 → 具体尺寸（ark 的 2560x1440 之类）；
   *   2. 表里没这个比例（openai 只有 1:1 / 3:2 / 2:3）→ 空串，交服务端默认值。
   *      这里**不能**退回比例本身，否则会把 9:16 当成尺寸发给只认宽高的接口，直接 400；
   *   3. 整个协议没有档位表（本机豆包桥）→ 存比例本身。服务端会把它交给豆包桥：
   *      先在豆包界面上切画幅，切不到就在抓图时按这个比例居中裁。
   */
  function imageSizeFor(model, ratio) {
    const map = S.imageDefaults?.ratioPresets?.[model?.protocol];
    if (!map || typeof map !== 'object') return String(ratio || '');
    const list = map[ratio];
    return Array.isArray(list) && list.length ? list[0] : '';
  }

  /** 视频参数：时长按模型档位就近取，比例/分辨率按各自协议的表来 */
  function videoParamsFor(model, ratio, wantDuration) {
    const preset = videoPresetOf(model);
    if (!preset) return {};
    const out = {};
    const durations = (preset.durations || []).map(Number).filter((n) => n > 0);
    if (durations.length) {
      const target = Number(wantDuration) || DEFAULT_SHOT_DURATION;
      out.duration = String(durations.reduce((best, cur) => (Math.abs(cur - target) < Math.abs(best - target) ? cur : best), durations[0]));
    }
    if (preset.ratios?.length) {
      out.ratio = preset.ratios.includes(ratio) ? ratio : preset.ratios[0];
    }
    if (preset.resolutions?.length) {
      // 智谱那类「宽x高」的档位靠 sizeLabels 反查；方舟那种 p 档就直接取 1080p / 第一档
      const byLabel = preset.resolutions.find((res) => preset.sizeLabels?.[res] === ratio);
      out.resolution = byLabel || preset.resolutions.find((res) => String(res).includes('1080')) || preset.resolutions[0];
    }
    return out;
  }

  function agentNodes() {
    return (S.graph?.nodes || []).filter((n) => n?.agent?.role);
  }

  /**
   * 画幅应用到画布上**所有**编导节点（定妆照 / 镜头图 / 镜头视频）。
   *
   * 用户原话：「分镜还得分别选尺寸，在对话里直接选择应用全部就行」——
   * 之前只有新铺的分镜吃参数面板的画幅，铺完之后想改就得一张张点开卡片。
   * 现在面板里选一次就全改；每张卡挂的模型可能不同，尺寸按各自的模型重新查表。
   *
   * 返回 { changed, skipped }：
   *   changed = 真改了的节点数；skipped = 模型压根不支持这个比例（比如 openai 只认
   *   1:1 / 3:2 / 2:3，选了 16:9 就只能跳过）。跳过要报出来，不能假装改成功。
   */
  function applyRatioToAgents(ratio) {
    if (!S.graph || !ratio) return { changed: 0, skipped: 0 };
    const plan = [];
    const frames = new Map();
    let skipped = 0;
    for (const node of agentNodes()) {
      const gen = node.gen || {};
      if (node.type === 'imageGen') {
        // 有档位表的按表查尺寸；本机豆包那种没有表的（尺寸由客户端自己定）
        // 就把比例本身记在卡上 —— 卡片上至少看得出这镜要横还是竖
        const size = imageSizeFor(modelOf(node, 'image'), ratio);
        if (!size) {
          skipped += 1;
          continue;
        }
        const patch = {};
        if (gen.size !== size) patch.size = size;
        // 画幅换了，提示词末尾那句「竖版构图」也得跟着换 ——
        // 文字说竖版、实际出横图，模型会两边打架。只替换这一小段，
        // 用户手改过的其余文字一个字不碰。
        const agent = node.agent || {};
        const nextFrame = frameHintOf(ratio);
        if (nextFrame && gen.prompt) {
          const oldFrame = String(agent.frame || '');
          const replaced = oldFrame && gen.prompt.includes(oldFrame)
            ? gen.prompt.split(oldFrame).join(nextFrame)
            : gen.prompt.replace(/[竖横方]版构图，主体居中/g, nextFrame);
          if (replaced !== gen.prompt) {
            patch.prompt = replaced;
            frames.set(node.id, nextFrame);
          }
        }
        if (Object.keys(patch).length) plan.push([node, patch]);
      } else if (node.type === 'videoGen') {
        // 视频只搬比例 / 分辨率，时长是每一镜自己的，别被顺手改掉
        const model = modelOf(node, 'video');
        const preset = videoPresetOf(model);
        const params = videoParamsFor(model, ratio, gen.duration);
        const patch = {};
        if (preset?.ratios?.length) {
          // 有比例档位却不含这个比例：不硬改（改成别的比例更糟），报给用户
          if (params.ratio !== ratio) {
            skipped += 1;
            continue;
          }
          if (gen.ratio !== ratio) patch.ratio = ratio;
        } else if (gen.ratio !== ratio) {
          // 豆包桥没有比例档位：记个标签，别让卡片空着
          patch.ratio = ratio;
        }
        if (params.resolution && gen.resolution !== params.resolution) patch.resolution = params.resolution;
        if (Object.keys(patch).length) plan.push([node, patch]);
      }
    }
    if (!plan.length) return { changed: 0, skipped };
    // 一次全改，撤销要一步退回去 —— 所以历史只记一次
    pushHistory();
    for (const [node, patch] of plan) {
      Object.assign((node.gen ||= {}), patch);
      if (frames.has(node.id) && node.agent) node.agent.frame = frames.get(node.id);
      // rerenderNode 而不是 rerenderCard：底栏编辑面板正开着这张卡时，
      // 面板里的比例格子也要跟着换，别出现「卡上是 9:16、面板还写着 16:9」
      rerenderNode(node);
    }
    markDirty();
    return { changed: plan.length, skipped };
  }

  /**
   * 画幅落到画布上的唯一入口（参数面板的下拉和快捷条的「统一尺寸」共用）。
   * 顺序很重要：先存偏好，再改节点 —— 中途失败也不会出现「参数面板记着新的、卡片还是旧的」。
   */
  function applyRatioEverywhere(ratio) {
    if (!ratio) return;
    writeDirectorPrefs({ ratio });
    // 参数面板里的下拉跟着走：从快捷条进来时，那格可能还停在旧值
    const sel = $('#ncDirRatio');
    for (const opt of sel?.querySelectorAll?.('option') || []) {
      if (opt.value === ratio) opt.setAttribute('selected', '');
      else opt.removeAttribute('selected');
    }
    const { changed, skipped } = applyRatioToAgents(ratio);
    const label = DIRECTOR_RATIOS.find((r) => r.id === ratio)?.label || ratio;
    const miss = skipped ? `，${skipped} 个模型不支持这个比例（没动）` : '';
    if (!changed && !skipped) {
      toast(`画布上的镜头已经是「${label}」了`);
      return;
    }
    toast(changed ? `${label} 已应用到 ${changed} 个镜头${miss}` : `这个比例有 ${skipped} 个模型不支持（没动）`, !changed);
  }

  /** 快捷条「统一尺寸」：浮层里挑一个画幅，画布上所有镜头一起改 */
  function openRatioPicker(anchor) {
    const current = readDirectorPrefs().ratio || DEFAULT_DIRECTOR_RATIO;
    openPicker(anchor, (el) => {
      pickerSection(el, '画布上全部镜头都改成');
      for (const r of DIRECTOR_RATIOS) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = `nc-picker-item${r.id === current ? ' on' : ''}`;
        item.innerHTML =
          `<span class="nc-picker-icon">${ratioGlyph(r.id).outerHTML}</span>`
          + `<span class="nc-picker-main"><span class="nc-picker-name">${esc(r.label)}</span></span>`
          + (r.id === current ? '<span class="nc-picker-check">✓</span>' : '');
        item.addEventListener('click', () => {
          closePicker();
          applyRatioEverywhere(r.id);
        });
        el.appendChild(item);
      }
    });
  }

  const needRun = (node) => !node.gen?.resultUrl && !PENDING.has(node.gen?.status);

  function focusBounds(x, y, w, h) {
    const { w: vw, h: vh } = viewportSize();
    const zoom = Math.max(0.25, Math.min(0.85, (vw - 120) / Math.max(1, w), (vh - 200) / Math.max(1, h)));
    S.graph.viewport = {
      zoom,
      x: (vw - w * zoom) / 2 - x * zoom,
      y: (vh - h * zoom) / 2 - y * zoom,
    };
    applyViewport();
    markDirty();
  }

  /**
   * 剧本卡的内容。
   *
   * 卡上是「角色 + 每镜 画面/首帧提示词/运镜/台词」，首帧提示词就是镜头图实际提交的
   * gen.prompt（同一个 shotPromptOf 拼出来的），所以卡上写的和真跑的一定是同一句。
   */
  function shotScript(plan, frame = '') {
    const characters = plan.characters || [];
    const lines = [`《${plan.title}》`, ''];
    // 全片设定放在最上面：这一片「长什么样」全片共用一段，改一次就是改一整片
    if (plan.setting) {
      lines.push(`全片设定：${plan.setting}`);
      lines.push('');
    }
    if (characters.length) {
      lines.push('角色');
      for (const c of characters) {
        // 有参考图的角色不写长相：卡上写的和真正提交的提示词要保持一致
        const look = hasRefPhoto(c) ? (outfitOf(c.desc) || c.desc) : c.desc;
        lines.push(`${c.name}：${look}${hasRefPhoto(c) ? '（长相见你的参考图）' : c.fromPhoto ? '（已用你的图定妆）' : ''}`);
      }
      lines.push('');
    }
    // 场景表：一个地点一块板。卡上写出来，用户才知道「这几镜共用一个房间」是设计，不是巧合
    const places = (Array.isArray(plan.places) ? plan.places : []).filter(Boolean);
    if (places.length) {
      lines.push(`场景（每个地点先出一张环境空镜，同地点的镜头图都挂它当参考图）`);
      for (const place of places) {
        const count = plan.shots.filter((s) => s.place === place).length;
        lines.push(`${place}：${count} 个镜头`);
      }
      lines.push('');
    }
    for (const shot of plan.shots) {
      const cast = shot.characters || [];
      const castDesc = castPartOf(cast, characters);
      const refKeep = shotHasRefCast(cast, characters);
      lines.push(`镜头 ${shot.index} · ${shot.duration || DEFAULT_SHOT_DURATION} 秒${shot.place ? ` · ${shot.place}` : ''}`);
      lines.push(`画面：${shot.scene}`);
      lines.push(
        `首帧提示词：${shotPromptOf(
          { stylePrompt: plan.style, setting: plan.setting, castDesc, refKeep, shotSize: shot.shotSize, angle: shot.angle, light: shot.light, frame, place: shot.place },
          shot.scene,
        )}`,
      );
      // 收尾画面：既是这一镜动作的落点，也是下一段视频的尾帧（下一镜的图）
      if (shot.endState) lines.push(`收尾：${shot.endState}`);
      if (shot.motion) lines.push(`运镜：${shot.motion}`);
      if (shot.dialogue) lines.push(`台词：${shot.dialogue}`);
      lines.push('');
    }
    return lines.join('\n').trim();
  }

  /**
   * 把一份分镜结构铺成节点。
   *
   * 布局是四行：
   *   第一行 剧本卡 + 角色定妆照（每个角色一张）
   *   第二行 场景板（每个地点一张环境空镜）
   *   第三行 镜头图（每个镜头一张，连到该镜头出现的角色 + 地点对应的场景板）
   *   第四行 镜头视频（从对应的镜头图出发）
   * 摆在现有内容右侧，不覆盖用户已经画好的东西。
   */
  function layOutPlan(plan, { ratio, imageModelId, videoModelId }) {
    if (!S.graph) return null;
    const imageModel = S.imageModels.find((m) => m.id === imageModelId) || null;
    const videoModel = videoModelId ? S.videoModels.find((m) => m.id === videoModelId) || null : null;
    const size = imageSizeFor(imageModel, ratio);
    // 画幅的构图提示：三处拼提示词都要带上，不然重拼一次方向就丢了
    const frame = frameHintOf(ratio);

    const shots = plan.shots || [];
    const characters = plan.characters || [];
    /**
     * 场景板：一个地点一张环境空镜，同一地点的镜头图全都挂它当参考图。
     *
     * 之前「场景一致」只靠提示词里那句「场景：厨房」—— 那是文字，
     * 模型每次都会自己重新编一个厨房（灶台在左在右、窗子朝哪、什么颜色全变）。
     * 给它一张房间的照片当参考图，房间才真的不会换。
     * 编导没给 places 时（老画布 / 单地点剧本）就从镜头里就地归并。
     */
    const places = Array.isArray(plan.places) && plan.places.length
      ? plan.places.filter(Boolean)
      : [...new Set(shots.map((s) => s.place).filter(Boolean))];
    const planId = uid('plan');
    const COL = CARD_W + 44;
    const cols = Math.max(shots.length, characters.length, places.length, 1);
    const totalW = (cols - 1) * COL + CARD_W;

    const existing = S.graph.nodes || [];
    const baseX = existing.length ? Math.round(Math.max(...existing.map((n) => n.x + n.w)) + 120) : Math.round(centerWorld().x - totalW / 2);
    const baseY = existing.length ? Math.round(Math.min(...existing.map((n) => n.y))) : Math.round(centerWorld().y - 260);
    // 有场景板就多占一行，没有就保持原来的三行布局（老画布的排版不会突然被推乱）
    const sceneY = baseY + AGENT_ROW_GAP;
    const shotY = baseY + AGENT_ROW_GAP * (places.length ? 2 : 1);
    const videoY = baseY + AGENT_ROW_GAP * (places.length ? 3 : 2);

    pushHistory();

    // 剧本卡：卡上是「角色 + 每镜 画面/首帧提示词/运镜/台词」，一行行排下来能有几十行，
    // 固定 170 高只看得见两行。高度按字数估（见 scriptCardHeight），
    // 封顶 SCRIPT_CARD_MAX_H —— 再长就框内滚动，右下角还能自己拉大。
    const scriptText = shotScript(plan, frame);
    const scriptWidth = 460;
    const scriptH = scriptCardHeight(scriptText, scriptWidth);
    const scriptNode = addAgentNode('text', baseX, baseY - scriptH - 44, {
      w: scriptWidth,
      h: scriptH,
      // 剧本卡比普通卡宽一倍（要装下整段分镜文字），标成手动尺寸，重开画布才不会被收回 210
      manualSize: true,
      title: `剧本 · ${plan.title}`,
      text: scriptText,
    });

    // 第一行：角色定妆照 —— 全片人物长相的「源头」，后面每个镜头都挂它当参考图
    const castNodes = new Map();
    characters.forEach((character, i) => {
      // 用户自己传了人物图：这张图直接就是定妆照，不要再花一次生成
      const fromPhoto = Boolean(character.fromPhoto && character.refUrl);
      const node = addAgentNode('imageGen', baseX + i * COL, baseY, {
        title: fromPhoto ? `定妆照 · ${character.name}（你的图）` : `定妆照 · ${character.name}`,
        gen: {
          modelId: imageModelId,
          prompt: joinCn([
            plan.style,
            `角色定妆照：${character.desc}`,
            '正面半身特写，看着镜头，表情自然，背景干净简洁，人物占据画面主体，五官清晰',
            '这是同一个角色的标准形象参考图，请保持外貌特征稳定',
          ]),
          size,
          count: 1,
          // 标成已经有结果：跑批会跳过它，后续镜头照样拿它当参考图
          ...(fromPhoto ? { resultUrl: character.refUrl, status: 'success' } : {}),
        },
        agent: {
          role: 'cast',
          planId,
          planTitle: plan.title,
          shotIndex: 0,
          character: character.name,
          // desc 留着：之后聊天里改某一镜时，要重新拼「角色描述段」进提示词
          desc: character.desc,
          motion: '',
          dialogue: '',
          fromPhoto,
        },
      });
      castNodes.set(character.name, node);
    });

    // 第一行半：场景板 —— 一个地点一张空镜，同地点的镜头图都挂它当参考图。
    // 放在定妆照下面一行：人物的锚在上、环境的锚在中、镜头在下，一眼能看出层级。
    const sceneNodes = new Map();
    places.forEach((place, i) => {
      const node = addAgentNode('imageGen', baseX + i * COL, sceneY, {
        title: `场景 · ${place}`,
        gen: {
          modelId: imageModelId,
          prompt: joinCn([
            // 主体（这个房间）排最前：图像模型对开头的词权重最高，
            // 把画风顶在前面会把「空房间」本身压成背景，出来的图看不出是哪间屋
            `${place}，室内空镜，没有任何人`,
            '环境细节完整：墙面、地面、主要家具的位置和朝向都要看清楚',
            plan.setting,
            plan.style,
            '这是这部片子里这个房间的标准样子，之后每个镜头都以它为准',
          ]),
          size,
          count: 1,
        },
        agent: {
          role: 'scene',
          planId,
          planTitle: plan.title,
          shotIndex: 0,
          // place 就是这张板代表的地点：镜头图靠它认领「我该挂哪张板」
          place,
          desc: plan.setting || '',
          stylePrompt: plan.style || '',
          setting: plan.setting || '',
          motion: '',
          dialogue: '',
        },
      });
      sceneNodes.set(place, node);
    });

    // 第二行：镜头图。出现的角色除了连参考图，也把提示词写进去 ——
    // 有人物参考图的角色只写衣服（长相交给图），没图的才写全
    const shotNodes = [];
    shots.forEach((shot, i) => {
      const cast = shot.characters || [];
      const descPart = castPartOf(cast, characters);
      const refKeep = shotHasRefCast(cast, characters);
      const node = addAgentNode('imageGen', baseX + i * COL, shotY, {
        title: `镜头 ${shot.index}`,
        gen: {
          modelId: imageModelId,
          prompt: shotPromptOf(
            { stylePrompt: plan.style, setting: plan.setting, castDesc: descPart, refKeep, shotSize: shot.shotSize, angle: shot.angle, light: shot.light, frame, place: shot.place },
            shot.scene,
          ),
          size,
          count: 1,
        },
        agent: {
          role: 'shot',
          planId,
          planTitle: plan.title,
          shotIndex: shot.index,
          character: cast.join('、'),
          motion: shot.motion || '',
          dialogue: shot.dialogue || '',
          // 提示词是几段拼起来的，拆开各存一份：改了一镜能原样重拼，
          // 只留拼好的整串就改不动了（搜不到边界，越改越乱）
          scene: shot.scene,
          // 这一镜发生在哪：同一场戏的几镜共用一个地点，是防止「镜头跳场」的锚
          place: shot.place || '',
          stylePrompt: plan.style,
          // 全片统一设定也存一份：改一镜重拼时要带回来，不然这一镜的基调就掉了
          setting: plan.setting || '',
          castDesc: descPart,
          // 摄影参数单独存：改了画面重拼时还得带上，不然景别和光线就丢了
          shotSize: shot.shotSize || '',
          angle: shot.angle || '',
          light: shot.light || '',
          // 画幅的构图提示也存一份：重拼时不带它，方向就漂了
          frame,
          // 这一镜有人物参考图 → 重拼提示词时末尾那句「长相按参考图」不能丢
          refKeep,
          // 这一镜动作做完时画面停在哪：既是下一镜的起点，也是这一段视频的终点
          endState: shot.endState || '',
        },
      });
      for (const name of cast) {
        const castNode = castNodes.get(name);
        if (castNode) S.graph.edges.push({ id: uid('eg'), from: castNode.id, to: node.id });
      }
      // 环境参考图：和人物参考图一样是「参考图边」，但排在人后面 ——
      // 这样 [图N] 的编号始终是「先人后景」，提示词里写「图1」还是指人。
      const sceneNode = shot.place ? sceneNodes.get(shot.place) : null;
      if (sceneNode) S.graph.edges.push({ id: uid('eg'), from: sceneNode.id, to: node.id });
      shotNodes.push(node);
    });

    /**
     * 第三行：镜头视频 —— **一段视频连两张图**。
     *
     * 成片是「图 1 → 视频 → 图 2 → 视频 → 图 3 …」：第一张图只当首帧（它前面没有镜头，
     * 不需要单独出一段视频），从第 1 镜到第 2 镜的过渡才是第一段视频。
     * 所以 N 个镜头铺 N-1 段视频，每段连「上一张图 = 首帧」「下一张图 = 尾帧」两条线，
     * 连线上的 role 就是用来分清哪条是首、哪条是尾的。
     */
    if (videoModel && shotNodes.length >= 2) {
      shots.forEach((shot, i) => {
        const next = shots[i + 1];
        if (!next) return;
        const params = videoParamsFor(videoModel, ratio, shot.duration);
        // 豆包桥这类没有比例档位的：比例只作标签记在卡上（客户端不认，卡片别空着）
        if (!params.ratio) params.ratio = ratio;
        const node = addAgentNode('videoGen', baseX + i * COL, videoY, {
          title: `镜头 ${shot.index}→${next.index} · 视频`,
          gen: {
            modelId: videoModel.id,
            // 首帧 = 本镜那张图，尾帧 = 下一镜那张图：两头都要写进提示词，
            // 只写「怎么动」的话模型会自己另起一个姿势（见 videoPromptOf 的注释）
            prompt: videoPromptOf({
              scene: shot.scene,
              motion: shot.motion,
              endState: shot.endState || next.scene,
              // 这段视频从本镜演到下一镜：地点取本镜的；本镜没写地点就退到下一镜
              place: shot.place || next.place || '',
              // 落点地点：跨房间那一段要写出「怎么走过去」，不能还写「全程都在本镜」
              toPlace: next.place || '',
              // 画风 / 光线 / 全片设定跟着本镜的镜头图走：图和视频得是同一个调子
              style: plan.style || '',
              light: shot.light || '',
              setting: plan.setting || '',
            }, { modelId: videoModel.id }),
            ...params,
          },
          agent: {
            role: 'video',
            planId,
            planTitle: plan.title,
            shotIndex: shot.index,
            // 这段视频跨两个镜头：起点是本镜、终点是下一镜，改分镜重拼提示词时要两边都认得
            fromShot: shot.index,
            toShot: next.index,
            character: (shot.characters || []).join('、'),
            motion: shot.motion || '',
            dialogue: shot.dialogue || '',
            // 首帧画面也存一份：老画布/节点被删时还能拼出「从哪开始」
            scene: shot.scene || '',
            // 地点：这段视频在哪一个地方演完，提示词里要写死，不许中途换场
            place: shot.place || '',
            // 落点地点：跨房间那一段要靠它拼出「从 A 走到 B」的过程，不能只认起点
            toPlace: next.place || '',
            // 落点存一份：换个不吃尾帧的模型时，提示词里那句「最后一帧要落到…」靠它拼
            endState: shot.endState || '',
            // 全片设定也存一份：改分镜重拼视频提示词时基调不能丢
            stylePrompt: plan.style || '',
            setting: plan.setting || '',
            light: shot.light || '',
          },
        });
        const head = shotNodes[i];
        const tail = shotNodes[i + 1];
        if (head) S.graph.edges.push({ id: uid('eg'), from: head.id, to: node.id, role: 'start' });
        if (tail) S.graph.edges.push({ id: uid('eg'), from: tail.id, to: node.id, role: 'end' });
      });
    }

    markDirty();
    renderEdges();
    drawMinimap();
    syncEmpty();
    updateStatusBar();

    const bottom = videoModel ? videoY : shotY;
    const top = baseY - 200;
    focusBounds(baseX, top, totalW, bottom + CARD_W - top);

    S.runHidden = false;
    updateRunBar();
    // 不选中任何节点：一选中就会弹出编辑面板，正好挡住刚铺好的三行
    S.selected.clear();
    syncSelection();
    return scriptNode;
  }

  /* ---------------------------- 批量生成（波次） ---------------------------- */

  /** 等一个节点落地：用状态轮询，不并发查询，节点多的时候也不会打爆服务端 */
  function waitNodeDone(node, timeoutMs = 15 * 60 * 1000) {
    return new Promise((resolve) => {
      const started = Date.now();
      const tick = () => {
        const gen = node.gen || {};
        if (gen.status === 'success' && gen.resultUrl) return resolve('success');
        if (['failed', 'cancelled', 'canceled'].includes(gen.status)) return resolve('failed');
        // 已经提交过但状态被清空了（比如提交时就报错），别一直等
        if (!gen.taskId && !PENDING.has(gen.status)) return resolve('failed');
        if (Date.now() - started > timeoutMs) return resolve('timeout');
        setTimeout(tick, 1200);
      };
      tick();
    });
  }

  /**
   * 一键生成：定妆照 → 场景板 → 镜头图 → 镜头视频，一波跑完再跑下一波。
   *
   * 为什么一定要等：镜头图要把定妆照当参考图、把场景板当环境参考图（连线已经铺好，
   * refCandidates 靠 mediaUrlOf 取图），上一波没出图就提交，参考图是空的，
   * 人物跑偏、房间每镜换一个。
   */
  async function runAgentPlan() {
    if (S.agentRun?.busy) return;
    const all = agentNodes();
    if (!all.length) return;
    S.agentRun = { busy: true, stop: false };
    updateRunBar();
    let ok = 0;
    let bad = 0;
    try {
      // 波次：定妆照 → 场景板 → 镜头图 → 视频。
      // 场景板必须排在镜头图前面：镜头图要挂它当参考图，板还没出图就只能退化成纯文字，
      // 房间又每镜换一个 —— 顺序错了这一层等于白加。
      for (const role of ['cast', 'scene', 'shot', 'video']) {
        if (S.agentRun.stop) break;
        const group = all.filter((n) => n.agent.role === role && needRun(n));
        if (!group.length) continue;
        // 视频节点要先有首帧图：上游那波失败时这里就跳过，否则只会得到一串「缺少首帧图」
        // 认 frameNodes 而不是「随便一条入边有图」—— 尾帧那张图先出、首帧失败的情况要拦住
        const ready = role === 'video' ? group.filter((n) => mediaUrlOf(frameNodes(n).start)) : group;
        if (!ready.length) continue;
        const settled = [];
        if (role === 'video') {
          // 视频必须一段一段来：下一段的首帧要用上一段的最后一帧（末帧接力），
          // 不等出片就提交只能退回用镜头图，接缝又断了。而且豆包桥本来就共用一个窗口，
          // 同时排几条只会互相抢。
          for (const node of ready) {
            if (S.agentRun.stop) break;
            await runVideoGen(node);
            settled.push(await waitNodeDone(node));
          }
        } else {
          for (const node of ready) {
            if (S.agentRun.stop) break;
            await runImageGen(node);
          }
          settled.push(...await Promise.all(ready.map((n) => waitNodeDone(n))));
        }
        ok += settled.filter((result) => result === 'success').length;
        bad += settled.filter((result) => result !== 'success').length;
      }
    } finally {
      const stopped = S.agentRun?.stop;
      S.agentRun = null;
      updateRunBar();
      if (stopped) toast(`已停止：${ok} 个完成，剩下的还能再点一键生成`);
      else if (bad) toast(`${ok} 个完成，${bad} 个失败 —— 点失败的节点看原因，改完再点一键生成`, true);
      else toast('分镜全部生成完成');
    }
  }

  function updateRunBar() {
    const bar = $('#ncRunBar');
    if (!bar) return;
    const list = agentNodes();
    if (!list.length || S.runHidden) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    const parts = [];
    let todo = 0;
    let failed = 0;
    for (const role of ['cast', 'scene', 'shot', 'video']) {
      const group = list.filter((n) => n.agent.role === role);
      if (!group.length) continue;
      const done = group.filter((n) => n.gen?.resultUrl).length;
      parts.push(`${AGENT_ROLE_LABEL[role]} ${done}/${group.length}`);
      todo += group.filter(needRun).length;
      failed += group.filter((n) => n.gen?.status === 'failed').length;
    }
    const stat = $('#ncRunBarStat');
    if (stat) stat.textContent = parts.join(' · ') + (failed ? ` · ${failed} 个失败` : '');
    const name = $('#ncRunBarName');
    if (name && list[0].agent.planTitle) name.textContent = list[0].agent.planTitle;

    const go = $('#ncRunBarGo');
    const busy = Boolean(S.agentRun?.busy);
    if (go) {
      go.textContent = busy ? '生成中…' : todo ? `一键生成（${todo}）` : '全部完成';
      go.disabled = busy || !todo;
    }
    const stop = $('#ncRunBarStop');
    if (stop) stop.hidden = !busy;
    updateChatQuick();
  }

  function bindDirector() {
    const panel = $('#ncChat');
    if (panel && panel.dataset.bound !== '1') {
      panel.dataset.bound = '1';
      // 聊天窗是浮在画布上的：交互一个都不能漏下去。
      // pointerdown 漏了 → 点输入框等于在画布空白处按下（清选择 + 起框选）；
      // wheel 漏了 → 滚聊天记录会把画布一起缩放；
      // dblclick 漏了 → 双击聊天记录会在底下新建一个文本节点。
      for (const type of ['pointerdown', 'wheel', 'dblclick', 'contextmenu']) {
        panel.addEventListener(type, (event) => event.stopPropagation(), { passive: true });
      }

      $('#ncChatClose')?.addEventListener('click', closeDirector);
      $('#ncChatHistoryBtn')?.addEventListener('click', () => {
        const box = $('#ncChatHistory');
        if (!box) return;
        box.hidden = !box.hidden;
        $('#ncChatHistoryBtn')?.classList.toggle('on', !box.hidden);
        // 参数面板和历史面板占同一块地方，开一个就收另一个
        if (!box.hidden) {
          const params = $('#ncChatParams');
          if (params) params.hidden = true;
          $('#ncChatGear')?.classList.remove('on');
          renderChatHistory();
          box.querySelector('.nc-chat-history-list')?.scrollTo?.({ top: 0 });
        }
      });
      $('#ncChatGear')?.addEventListener('click', () => {
        const box = $('#ncChatParams');
        if (!box) return;
        box.hidden = !box.hidden;
        $('#ncChatGear')?.classList.toggle('on', !box.hidden);
        if (!box.hidden) {
          const history = $('#ncChatHistory');
          if (history) history.hidden = true;
          $('#ncChatHistoryBtn')?.classList.remove('on');
        }
      });

      $('#ncChatForm')?.addEventListener('submit', (event) => {
        event.preventDefault();
        sendChat();
      });

      // 参考图：点按钮先列素材库里已有的图（多数时候不用重传），弹层里还有「上传新图」
      $('#ncChatPick')?.addEventListener('click', () => openLibPick());
      $('#ncChatFile')?.addEventListener('change', (event) => {
        addChatPhotos(event.target.files);
        event.target.value = '';
      });
      panel.addEventListener('dragover', (event) => {
        if (![...(event.dataTransfer?.types || [])].includes('Files')) return;
        event.preventDefault();
        panel.classList.add('dropping');
      });
      panel.addEventListener('dragleave', () => panel.classList.remove('dropping'));
      panel.addEventListener('drop', (event) => {
        panel.classList.remove('dropping');
        const files = event.dataTransfer?.files;
        if (!files?.length) return;
        event.preventDefault();
        event.stopPropagation();
        addChatPhotos(files);
      });

      const input = $('#ncChatInput');
      input?.addEventListener('input', autoGrowChatInput);
      // 截图后在输入框里 Ctrl+V：直接当成参考图收下（从网页复制的是链接，不是文件，收不到）
      input?.addEventListener('paste', (event) => {
        const files = [...(event.clipboardData?.files || [])].filter((f) => String(f.type || '').startsWith('image/'));
        if (!files.length) return;
        event.preventDefault();
        addChatPhotos(files);
      });
      input?.addEventListener('keydown', (event) => {
        // 回车发、Shift+回车换行；中文输入法选词时的那次回车不能当发送
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          sendChat();
        }
      });

      $('#ncChatQuick')?.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-quick]');
        if (!btn) return;
        const act = btn.dataset.quick;
        if (act === 'run') runAgentPlan();
        else if (act === 'ratio') openRatioPicker(btn);
        else if (act === 'more') sendChat('再加两个镜头，接着原来的故事往下拍');
        else if (act === 'reset') resetChat();
      });

      for (const sel of ['#ncDirShots', '#ncDirStyle', '#ncDirRatio', '#ncDirImageModel', '#ncDirVideoModel']) {
        $(sel)?.addEventListener('change', () => {
          // 画幅是「选一次、画布上所有镜头跟着改」：新铺的分镜用它，已经铺好的也一起改，
          // 不用再一张张点开卡片挑尺寸
          if (sel === '#ncDirRatio') {
            applyRatioEverywhere(readSelect('#ncDirRatio', DEFAULT_DIRECTOR_RATIO) || DEFAULT_DIRECTOR_RATIO);
            updateDirectorHint();
            return;
          }
          saveDirectorPrefsFromForm();
          updateDirectorHint();
        });
      }

      $('#ncDirModelBtn')?.addEventListener('click', () => openDirectorModelModal());
    }

    // 分镜那边改了剧本模型 → 这里重拉一次配置，按钮上的模型名才不会停在旧的
    document.addEventListener('story-model-changed', async () => {
      try {
        S.director.info = await api('/api/canvas/director');
        S.director.styles = S.director.info?.styles || S.director.styles;
        if (!$('#ncChat')?.hidden) renderDirectorForm();
      } catch (err) {
        /* 拉不到就先用旧值，下次开编导会再拉 */
      }
    });

    const bar = $('#ncRunBar');
    if (bar && bar.dataset.bound !== '1') {
      bar.dataset.bound = '1';
      // 跑批条在画布顶部，别让它把点击传给画布（否则点到就是框选/取消选择）
      bar.addEventListener('pointerdown', (event) => event.stopPropagation());
      $('#ncRunBarGo')?.addEventListener('click', runAgentPlan);
      $('#ncRunBarStop')?.addEventListener('click', () => {
        if (S.agentRun) S.agentRun.stop = true;
        updateRunBar();
      });
      $('#ncRunBarClose')?.addEventListener('click', () => {
        S.runHidden = true;
        updateRunBar();
      });
    }
    updateRunBar();
  }

  function bindEvents() {
    // 节点：拖动 / 选择 / 连线 / 缩放（右键留给 contextmenu 菜单）
    $('#ncNodes').addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const el = event.target.closest('.nc-node');
      if (!el) return;
      const node = nodeById(el.dataset.id);
      if (!node) return;

      const actBtn = event.target.closest('[data-act]');
      if (actBtn) {
        event.stopPropagation();
        if (actBtn.dataset.act === 'dup') duplicateNode(node.id);
        if (actBtn.dataset.act === 'del') deleteNode(node.id);
        return;
      }

      const port = event.target.closest('.nc-port');
      if (port) {
        event.stopPropagation();
        startConnect(event, node, port.dataset.port);
        return;
      }

      const grip = event.target.closest('.nc-resize');
      if (grip) {
        event.stopPropagation();
        startResize(event, node);
        return;
      }

      // 卡片已是缩略图态：里面的 video 是无 controls 的封面帧，不该拦点击；
      // 真正要放行的是表单控件（现在都在底部编辑面板里，面板本身已 stopPropagation）
      if (event.target.closest('input, textarea, select, button, audio, video[controls]')) return;

      if (event.shiftKey) {
        selectNode(node.id, true);
        return; // Shift 点击只切换选择，不进入拖拽
      }
      // 已选中的节点直接拖 = 拖动整组/整个多选；未选中的节点则只选它
      if (!S.selected.has(node.id)) {
        S.selected.clear();
        S.selected.add(node.id);
        expandSelectionByGroup();
      }
      S.selectedEdge = '';
      syncSelection();
      startDrag(event, node);
    });

    // 连线点击 / 双击删除
    $('#ncEdges').addEventListener('pointerdown', (event) => {
      const path = event.target.closest('[data-id]');
      if (!path) return;
      event.stopPropagation();
      // 剪断模式：点一下直接断开
      if (S.cutMode) {
        pushHistory();
        S.graph.edges = S.graph.edges.filter((e) => e.id !== path.dataset.id);
        S.selectedEdge = '';
        markDirty();
        renderEdges();
        drawMinimap();
        toast('已断开连线');
        return;
      }
      S.selectedEdge = path.dataset.id;
      S.selected.clear();
      syncSelection();
    });

    $('#ncEdges').addEventListener('dblclick', (event) => {
      const path = event.target.closest('[data-id]');
      if (!path || !S.graph) return;
      event.stopPropagation();
      pushHistory();
      S.graph.edges = S.graph.edges.filter((e) => e.id !== path.dataset.id);
      S.selectedEdge = '';
      markDirty();
      renderEdges();
    });

    // 工具栏
    for (const btn of $$('[data-add]')) {
      btn.addEventListener('click', () => addNode(btn.dataset.add));
    }
    $('#ncFit').addEventListener('click', fitView);
    $('#ncUndo').addEventListener('click', undo);
    $('#ncRedo').addEventListener('click', redo);

    // 底部编辑面板：定位 / 收起；面板本身不触发画布平移与框选
    const panel = $('#ncPanel');
    if (panel) {
      panel.addEventListener('pointerdown', (event) => event.stopPropagation());
      $('#ncPanelClose')?.addEventListener('click', () => closePanel());
      $('#ncPanelLocate')?.addEventListener('click', () => {
        const node = nodeById(S.panelNode);
        if (node) focusNode(node.id);
      });
    }

    // 左下角缩放控件
    $('#ncZoomIn')?.addEventListener('click', () => zoomStep(ZOOM_STEP));
    $('#ncZoomOut')?.addEventListener('click', () => zoomStep(1 / ZOOM_STEP));
    $('#ncZoomLevel')?.addEventListener('click', zoomReset);
    $('#ncZoomFit')?.addEventListener('click', fitView);
    $('#ncFullscreen')?.addEventListener('click', togglePseudoFullscreen);
    $('#ncShortcutBtn')?.addEventListener('click', () => openHelp());

    // 伪全屏：Electron 里 requestFullscreen 对普通元素不可靠，
    // 改为隐藏侧边栏和顶栏，让画布占满整个窗口
    function togglePseudoFullscreen() {
      const moduleEl = document.getElementById('moduleNode');
      if (!moduleEl) return;
      const shell = document.querySelector('.app-shell');
      const sidebar = document.querySelector('.app-sidebar');
      const topbar = document.querySelector('.app-topbar');
      const isFull = moduleEl.classList.toggle('nc-fullscreen');
      if (sidebar) sidebar.style.display = isFull ? 'none' : '';
      if (topbar) topbar.style.display = isFull ? 'none' : '';
      if (shell) shell.classList.toggle('nc-fullscreen-shell', isFull);
      const btn = $('#ncFullscreen');
      if (btn) btn.textContent = isFull ? '⛶' : '⛶';
      // 全屏后需要重新计算视口并适配
      if (isFull) {
        setTimeout(() => fitView(), 50);
      }
    }
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && document.getElementById('moduleNode')?.classList.contains('nc-fullscreen')) {
        togglePseudoFullscreen();
      }
    });
    $('#ncNewCanvas').addEventListener('click', async () => {
      const name = await promptDialog('新建节点画布', '画布名称', `节点画布 ${S.graphs.length + 1}`);
      if (name === null) return;
      try {
        const graph = await api('/api/node-canvases', { method: 'POST', body: { name } });
        justCreated.add(graph.id);
        S.graphs.unshift(graph);
        loadGraph(graph.id);
      } catch (err) {
        toast(`新建失败：${err.message}`, true);
      }
    });
    $('#ncRenameCanvas').addEventListener('click', async () => {
      if (!S.graph) return;
      const name = await promptDialog('重命名画布', '画布名称', S.graph.name);
      if (name === null || !name.trim()) return;
      S.graph.name = name.trim();
      markDirty();
      renderCanvasSelect();
      updateStatusBar();
    });
    $('#ncDeleteCanvas').addEventListener('click', async () => {
      if (!S.graph) return;
      const ok = await util().confirmDialog?.(`删除画布「${S.graph.name}」？画布上的节点会一起删除，素材库里的文件不受影响。`, {
        okText: '删除画布',
      });
      if (ok === false) return;
      const id = S.graph.id;
      try {
        await api(`/api/node-canvases/${id}`, { method: 'DELETE' });
        S.graphs = S.graphs.filter((g) => g.id !== id);
        S.graph = null;
        S.currentId = '';
        if (S.graphs.length) loadGraph(S.graphs[0].id);
        else await ensureCanvas();
      } catch (err) {
        toast(`删除失败：${err.message}`, true);
      }
    });
    $('#ncCanvasSelect').addEventListener('change', (event) => loadGraph(event.target.value));

    $('#ncExport').addEventListener('click', () => {
      if (!S.graph) return;
      saveNow();
      const payload = {
        format: 'lumen-forge-node-canvas',
        version: 1,
        name: S.graph.name,
        nodes: S.graph.nodes,
        edges: S.graph.edges,
        viewport: S.graph.viewport,
        exportedAt: new Date().toISOString(),
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${S.graph.name.replace(/[\\/:*?"<>|]/g, '_') || 'node-canvas'}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    });

    $('#ncImport').addEventListener('click', () => {
      const input = $('#ncImportInput');
      input.value = '';
      input.click();
    });
    $('#ncImportInput').addEventListener('change', async () => {
      const file = $('#ncImportInput').files?.[0];
      if (!file) return;
      try {
        const raw = JSON.parse(await file.text());
        const graph = await api('/api/node-canvases/import', { method: 'POST', body: { graph: raw } });
        justCreated.add(graph.id);
        S.graphs.unshift(graph);
        loadGraph(graph.id);
        toast(`已导入「${graph.name}」`);
      } catch (err) {
        toast(`导入失败：${err.message}`, true);
      }
    });

    // 素材库
    $('#ncUpload').addEventListener('click', () => $('#ncAssetInput').click());
    $('#ncAssetInput').addEventListener('change', () => {
      uploadFiles($('#ncAssetInput').files || []);
      $('#ncAssetInput').value = '';
    });
    $('#ncAssetTabs').addEventListener('click', (event) => {
      const btn = event.target.closest('[data-view]');
      if (!btn) return;
      S.assetView = btn.dataset.view;
      for (const b of $$('#ncAssetTabs [data-view]')) b.classList.toggle('active', b === btn);
      renderAssets();
    });
    $('#ncAssetKinds').addEventListener('click', (event) => {
      const btn = event.target.closest('[data-kind]');
      if (!btn) return;
      S.assetKind = btn.dataset.kind;
      for (const b of $$('#ncAssetKinds [data-kind]')) b.classList.toggle('active', b === btn);
      renderAssets();
    });

    document.addEventListener('keydown', (event) => {
      if (document.body.dataset.module !== 'node') return;
      const ctrlAny = event.ctrlKey || event.metaKey;
      const keyAny = event.key.toLowerCase();

      // 命令面板 / 查找 / 帮助：即使在输入框里也要能开关
      if (ctrlAny && keyAny === 'k') {
        event.preventDefault();
        if (isCmdOpen()) closeCmd(); else openCmd();
        return;
      }
      if (ctrlAny && keyAny === 'f') {
        event.preventDefault();
        if (S.find.open) closeFind(); else openFind();
        return;
      }
      if (event.key === 'Escape') {
        let hit = false;
        // 浮层最上层，先关它
        if (!$('#ncLibPick')?.hidden) { closeLibPick(); hit = true; }
        if (S.picker) { closePicker(); hit = true; }
        // 聊天窗不参与 Esc：它是常驻的，关了还要再按 D 找回来；
        // 而且 Esc 是画布最常用的「取消」，不该顺手把聊天窗一起收掉
        if (isCmdOpen()) { closeCmd(); hit = true; }
        if (!$('#ncHelp')?.hidden) { closeHelp(); hit = true; }
        if (isSkinOpen()) { closeSkinPanel(); hit = true; }
        if (S.find.open) { closeFind(); hit = true; }
        if (S.panMode) { setPanMode(false); hit = true; }
        if (S.cutMode) { setCutMode(false); hit = true; }
        // 底部编辑面板：Esc 收起（节点保持选中）
        if (S.panelNode) { closePanel(); hit = true; }
        if (hit) { event.preventDefault(); return; }
      }

      if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
      const ctrl = ctrlAny;
      const key = keyAny;
      // 页面上真有选中文字（聊天记录、卡片上的字）时，Ctrl+C / Ctrl+A 交给原生：
      // 以前这里无脑 preventDefault 去复制/全选节点，用户选中一段分镜按 Ctrl+C 毫无反应
      if (ctrl && (key === 'c' || key === 'a') && String(window.getSelection?.()?.toString?.() || '').trim()) return;
      if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault();
        deleteSelected();
      } else if (ctrl && key === 'd') {
        event.preventDefault();
        for (const id of [...S.selected]) duplicateNode(id);
      } else if (ctrl && key === 's') {
        event.preventDefault();
        S.dirty = true;
        saveNow();
      } else if (ctrl && key === 'c') {
        event.preventDefault();
        copySelection();
      } else if (ctrl && key === 'v') {
        event.preventDefault();
        pasteClipboard();
      } else if (ctrl && key === 'z' && !event.shiftKey) {
        event.preventDefault();
        undo();
      } else if ((ctrl && key === 'y') || (ctrl && event.shiftKey && key === 'z')) {
        event.preventDefault();
        redo();
      } else if (ctrl && key === 'g' && !event.shiftKey) {
        event.preventDefault();
        groupSelected();
      } else if (ctrl && event.shiftKey && key === 'g') {
        event.preventDefault();
        ungroupSelected();
      } else if (ctrl && key === 'a') {
        event.preventDefault();
        S.selected = new Set(S.graph?.nodes.map((n) => n.id) || []);
        S.selectedEdge = '';
        syncSelection();
      } else if (event.altKey && event.key === 'ArrowUp') {
        event.preventDefault();
        selectLinked('up');
      } else if (event.altKey && event.key === 'ArrowDown') {
        event.preventDefault();
        selectLinked('down');
      } else if (!ctrl && !event.altKey && key === 'd') {
        // 编导聊天窗：没开就打开，已经开着就直接把光标送进输入框（比「再按一次收起」有用）
        event.preventDefault();
        if (isDirectorOpen()) $('#ncChatInput')?.focus?.();
        else openDirector();
      } else if (!ctrl && !event.altKey && key === 'f') {
        event.preventDefault();
        if (event.shiftKey) zoomToSelection(); else fitView();
      } else if (!ctrl && !event.altKey && key === 'm') {
        event.preventDefault();
        setMinimapOn(!S.minimapOn);
      } else if (!ctrl && !event.altKey && key === 'g') {
        event.preventDefault();
        setGridOn(!S.gridOn);
      } else if (!ctrl && !event.altKey && key === 't') {
        event.preventDefault();
        tidyLayout('grid');
      } else if (!ctrl && !event.altKey && key === 'l') {
        event.preventDefault();
        setEdgesMode(EDGE_MODE_NEXT[S.edgesMode]);
      } else if (!ctrl && !event.altKey && key === 's') {
        event.preventDefault();
        setSnapOn(!S.snapOn);
      } else if (!ctrl && !event.altKey && key === 'h') {
        event.preventDefault();
        setPanMode(!S.panMode);
      } else if (!ctrl && !event.altKey && key === 'c') {
        event.preventDefault();
        setCutMode(!S.cutMode);
      } else if (!ctrl && !event.altKey && key === 'a') {
        event.preventDefault();
        addNode('text');
      } else if (!ctrl && !event.altKey && (event.key === '?' || (event.shiftKey && key === '/'))) {
        event.preventDefault();
        openHelp();
      }
    });

    window.addEventListener('beforeunload', () => {
      // 聊天记录也是「复用」要保的东西：关窗口前把最后一段对话塞出去
      if (chatDirty && S.graph) {
        fetch(`/api/node-canvases/${S.graph.id}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: chatPayload() }),
          keepalive: true,
        }).catch(() => {});
      }
      if (!S.dirty || !S.graph) return;
      // sendBeacon 只能发 POST，这里用 keepalive 的 fetch 把最后一次改动送出去
      fetch(`/api/node-canvases/${S.graph.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: S.graph.nodes, edges: S.graph.edges, viewport: S.graph.viewport }),
        keepalive: true,
      }).catch(() => {});
    });
  }

  function startDrag(event, node) {
    pushHistory(); // 拖拽前压快照，松开时一次性入栈
    const start = screenToWorld(event.clientX, event.clientY);
    const startClient = { x: event.clientX, y: event.clientY };
    let moved = false;
    const originals = [...S.selected].map((id) => {
      const n = nodeById(id);
      return n ? { node: n, x: n.x, y: n.y } : null;
    }).filter(Boolean);
    if (!originals.some((o) => o.node === node)) originals.push({ node, x: node.x, y: node.y });
    const movingIds = new Set(originals.map((o) => o.node.id));

    const move = (e) => {
      if (!moved && Math.hypot(e.clientX - startClient.x, e.clientY - startClient.y) > 3) moved = true;
      const p = screenToWorld(e.clientX, e.clientY);
      const dx = p.x - start.x;
      const dy = p.y - start.y;
      for (const item of originals) {
        item.node.x = Math.round(item.x + dx);
        item.node.y = Math.round(item.y + dy);
        updateNodePosition(item.node);
      }
      renderEdges();
      showGuides(movingIds);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      snapByGuides(movingIds);
      hideGuides();
      markDirty();
      // 点一下（没拖动）= 打开/确认编辑面板。
      // 关键场景：Esc 收起面板后再点已选中的节点，选中态没变、syncSelection 不会跑，
      // 这里补一次，面板才能重新弹出来。
      // 注意短路：pointerdown 时 syncSelection 已经开过这个节点的面板了，
      // 这里再 openPanel 会重建表单、打断正在输入的焦点
      if (!moved && S.selected.size === 1 && S.selected.has(node.id) && S.panelNode !== node.id) openPanel(node);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  function startResize(event, node) {
    pushHistory();
    const start = { x: event.clientX, y: event.clientY, w: node.w, h: node.h };
    const move = (e) => {
      const zoom = S.graph.viewport.zoom;
      node.w = Math.max(160, Math.round(start.w + (e.clientX - start.x) / zoom));
      node.h = Math.max(110, Math.round(start.h + (e.clientY - start.y) / zoom));
      // 手动改过尺寸就尊重用户的选择，别再自动收高度
      node.manualSize = true;
      updateNodePosition(node);
      renderEdges();
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      markDirty();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  /** 连线时的合法目标：给它们加 connectable（端口亮起），让用户一眼看出能往哪连 */
  function markConnectable(fromNode, fromSide) {
    clearConnectable();
    for (const n of S.graph?.nodes || []) {
      if (n.id === fromNode.id) continue;
      const ok = fromSide === 'out'
        ? (ALLOWED_TARGET_TYPES[fromNode.type] || []).includes(n.type)
        : (ALLOWED_TARGET_TYPES[n.type] || []).includes(fromNode.type);
      if (!ok) continue;
      $(`.nc-node[data-id="${n.id}"]`)?.classList.add('connectable');
    }
  }

  function clearConnectable() {
    for (const el of $$('.nc-node.connectable')) el.classList.remove('connectable');
    clearMagnet();
  }

  /** 清掉磁吸高亮（端口 + 节点） */
  function clearMagnet() {
    for (const el of $$('.nc-port.magnet')) el.classList.remove('magnet');
    for (const el of $$('.nc-node.link-target')) el.classList.remove('link-target');
  }

  /**
   * 磁吸：指针附近 30px（屏幕距离）内有合法端口时，
   * 临时线终点吸到端口圆心，端口与节点高亮，松手即连 —— 不用再把鼠标对到 14px 圆点上
   */
  function snapPort(p, conn) {
    const zoom = S.graph?.viewport?.zoom || 1;
    const threshold = 30 / zoom;
    const fromNode = nodeById(conn.from);
    if (!fromNode) return null;
    let best = null;
    for (const n of S.graph.nodes || []) {
      if (n.id === conn.from) continue;
      // out 拉出的线只吸别人的 in；in 拉出的只吸别人的 out；类型不合法的不吸
      const side = conn.fromSide === 'out' ? 'in' : 'out';
      const ok = conn.fromSide === 'out'
        ? (ALLOWED_TARGET_TYPES[fromNode.type] || []).includes(n.type)
        : (ALLOWED_TARGET_TYPES[n.type] || []).includes(fromNode.type);
      if (!ok) continue;
      const pt = portPoint(n, side);
      const d = Math.hypot(pt.x - p.x, pt.y - p.y);
      if (d < threshold && (!best || d < best.d)) best = { id: n.id, side, pt, d };
    }
    return best;
  }

  function applyMagnet(snap) {
    clearMagnet();
    if (!snap) return;
    const el = $(`.nc-node[data-id="${snap.id}"]`);
    el?.classList.add('link-target');
    el?.querySelector(`.nc-port-${snap.side}`)?.classList.add('magnet');
  }

  function startConnect(event, node, portSide) {
    const svg = $('#ncEdges');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', 'nc-edge temp');
    svg.appendChild(path);
    const anchor = portPoint(node, portSide);

    // 能连的节点把端口亮出来
    markConnectable(node, portSide);

    // 拖动过程中的临时线：跟随鼠标，靠近端口时磁吸，松手才锚定到目标节点
    const move = (e) => {
      const conn = S.connecting;
      if (!conn) return;
      const p = screenToWorld(e.clientX, e.clientY);
      const snap = snapPort(p, conn);
      conn.magnet = snap ? { id: snap.id, side: snap.side } : null;
      applyMagnet(snap);
      const end = snap ? snap.pt : p;
      const dx = Math.max(40, Math.abs(end.x - anchor.x) * 0.45);
      if (portSide === 'out') {
        path.setAttribute('d', `M ${anchor.x} ${anchor.y} C ${anchor.x + dx} ${anchor.y}, ${end.x - dx} ${end.y}, ${end.x} ${end.y}`);
      } else {
        path.setAttribute('d', `M ${end.x} ${end.y} C ${end.x + dx} ${end.y}, ${anchor.x - dx} ${anchor.y}, ${anchor.x} ${anchor.y}`);
      }
    };
    S.connecting = { from: node.id, fromSide: portSide, tempPath: path, move, magnet: null };

    // 松手时可能已经移出画布区域，viewport 收不到 pointerup，这里兜一份
    const fallback = (e) => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', fallback);
      if (S.connecting) finishConnect(e);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', fallback);
  }

  /** 松手位置命中的节点：优先认「端口」，其次节点本体 */
  function nodeAtPoint(x, y) {
    const hit = document.elementFromPoint(x, y);
    if (!hit) return null;
    const port = hit.closest('.nc-port');
    if (port) {
      const owner = port.closest('.nc-node');
      if (owner) return { id: owner.dataset.id, side: port.dataset.port };
    }
    const nodeEl = hit.closest('.nc-node');
    if (nodeEl) return { id: nodeEl.dataset.id, side: '' };
    return null;
  }

  /**
   * 连线拖到空白处松手：像 MiniMax 一样弹出「新建并连接」菜单，
   * 列出的类型按连线约束过滤（不能连的不出现）
   */
  function offerSpawnConnect(conn, world, clientX, clientY) {
    const fromNode = nodeById(conn.from);
    if (!fromNode) { renderEdges(); return; }
    const types = conn.fromSide === 'out'
      ? (ALLOWED_TARGET_TYPES[fromNode.type] || [])
      : Object.keys(ALLOWED_TARGET_TYPES).filter((t) => ALLOWED_TARGET_TYPES[t].includes(fromNode.type));
    if (!types.length) { renderEdges(); return; }
    const items = types.map((t) => ({
      label: `新建${NODE_LABEL[t] || t}并连接`,
      handler: () => {
        // addNode 自带 pushHistory + 选中 + 打开编辑面板，这里只补连线
        const created = addNode(t, { at: world });
        if (!created) return;
        const fromId = conn.fromSide === 'out' ? conn.from : created.id;
        const toId = conn.fromSide === 'out' ? created.id : conn.from;
        if (!S.graph.edges.some((e) => e.from === fromId && e.to === toId)) {
          S.graph.edges.push({ id: uid('eg'), from: fromId, to: toId, ...edgeRoleAttr(fromId, toId) });
        }
        markDirty();
        renderEdges();
      },
    }));
    items.push('-');
    items.push({ label: '取消', handler: () => {} });
    openMenu(items, clientX, clientY);
  }

  function finishConnect(event) {
    const conn = S.connecting;
    S.connecting = null;
    conn?.tempPath?.remove();
    if (conn?.move) window.removeEventListener('pointermove', conn.move);
    clearConnectable();
    if (!conn || !S.graph) return;

    // 磁吸优先：拖动时吸住了哪个端口，松手就直接认它，不再依赖元素命中
    const hit = conn.magnet || nodeAtPoint(event.clientX, event.clientY);
    if (!hit || hit.id === conn.from) {
      renderEdges();
      // 拖到空白处松手 = 想从这里长出一个新节点（MiniMax 的「+」）
      if (!hit) {
        offerSpawnConnect(conn, screenToWorld(event.clientX, event.clientY), event.clientX, event.clientY);
      }
      return;
    }
    // 从 out 拉出：只能连 in 或节点本体；从 in 拉出：只能连 out 或节点本体
    let fromId, toId;
    if (conn.fromSide === 'out') {
      if (hit.side === 'out') { renderEdges(); return; }
      fromId = conn.from;
      toId = hit.id;
    } else {
      if (hit.side === 'in') { renderEdges(); return; }
      fromId = hit.id;
      toId = conn.from;
    }
    if (S.graph.edges.some((e) => e.from === fromId && e.to === toId)) {
      renderEdges();
      return;
    }
    // 类型约束：像「视频喂给图片生成」这种无意义连线，在建立前就说明原因
    const fromNode = nodeById(fromId);
    const toNode = nodeById(toId);
    const allowed = ALLOWED_TARGET_TYPES[fromNode?.type] || [];
    if (!allowed.includes(toNode?.type)) {
      renderEdges();
      toast(`${NODE_LABEL[fromNode?.type] || fromNode?.type} 不能作为 ${NODE_LABEL[toNode?.type] || toNode?.type} 的输入`);
      return;
    }

    pushHistory();
    S.graph.edges.push({ id: uid('eg'), from: fromId, to: toId, ...edgeRoleAttr(fromId, toId) });
    markDirty();
    renderEdges();
  }

  /* --------------------------------- 小工具 --------------------------------- */

  /**
   * 重新生成时问一句「这次想改什么」。
   *
   * 不另开一套 UI：和 promptDialog 同一个弹窗骨架，只是把输入换成多行。
   * 留空直接确定 = 原样重跑（以前只能点底栏那个 ↑，看不出是重跑）。
   * 填了字就追加进提示词末尾 —— 追加而不是替换，因为上一次的镜头描述
   * 通常是有效的，人想改的往往只是其中一两个细节（「把窗帘改成拉开」）。
   */
  function reviseDialog(title, label, initial, tip) {
    return new Promise((resolve) => {
      const mask = document.createElement('div');
      mask.className = 'nc-prompt-mask';
      mask.innerHTML = `
        <div class="nc-prompt wide">
          <h3>${esc(title)}</h3>
          <label>${esc(label)}<textarea rows="4"></textarea></label>
          ${tip ? `<p class="nc-prompt-tip">${esc(tip)}</p>` : ''}
          <div class="nc-prompt-actions">
            <button type="button" class="btn tiny" data-act="cancel">取消</button>
            <button type="button" class="btn tiny primary" data-act="ok">重新生成</button>
          </div>
        </div>`;
      const area = mask.querySelector('textarea');
      area.value = initial || '';
      const close = (value) => {
        mask.remove();
        document.removeEventListener('keydown', onKey);
        resolve(value);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') close(null);
        // Ctrl/Cmd+Enter 提交：多行输入里单敲 Enter 是换行，不能当确认
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) close(area.value);
      };
      mask.querySelector('[data-act="cancel"]').addEventListener('click', () => close(null));
      mask.querySelector('[data-act="ok"]').addEventListener('click', () => close(area.value));
      document.addEventListener('keydown', onKey);
      document.body.appendChild(mask);
      area.focus();
      area.select();
    });
  }

  /** Electron 里不支持 window.prompt（会卡死）, 用页内小弹窗代替 */
  function promptDialog(title, label, initial) {
    return new Promise((resolve) => {
      const mask = document.createElement('div');
      mask.className = 'nc-prompt-mask';
      mask.innerHTML = `
        <div class="nc-prompt">
          <h3>${esc(title)}</h3>
          <label>${esc(label)}<input type="text" value="${esc(initial || '')}" /></label>
          <div class="nc-prompt-actions">
            <button type="button" class="btn tiny" data-act="cancel">取消</button>
            <button type="button" class="btn tiny primary" data-act="ok">确定</button>
          </div>
        </div>`;
      const input = mask.querySelector('input');
      const close = (value) => {
        mask.remove();
        document.removeEventListener('keydown', onKey);
        resolve(value);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') close(null);
        if (e.key === 'Enter') close(input.value);
      };
      mask.querySelector('[data-act="cancel"]').addEventListener('click', () => close(null));
      mask.querySelector('[data-act="ok"]').addEventListener('click', () => close(input.value));
      document.addEventListener('keydown', onKey);
      document.body.appendChild(mask);
      input.focus();
      input.select();
    });
  }

  /* --------------------------- 画布体验增强（对齐 MiniMax Design） --------------------------- */

  const EDGE_MODE_LABEL = { all: '全部连线', sel: '仅选中', none: '隐藏连线' };
  const EDGE_MODE_NEXT = { all: 'sel', sel: 'none', none: 'all' };
  // 小地图配色：整张画布唯一的强调色只留给「当前视口」，
  // 节点统一灰块、选中的稍微提亮——避免缩略图上五颜六色反而看不清结构。
  /** 小地图配色：canvas 是画出来的，吃不到 CSS 变量，只能自己按主题取一档 */
  function minimapColors() {
    return document.documentElement.dataset.theme === 'dark'
      ? {
        node: '#5a616c',
        accent: '#a78bfa',
        accentSoft: 'rgba(167, 139, 250, .22)',
        viewport: 'rgba(255, 255, 255, .30)',
      }
      : {
        node: '#c4c4c4',
        accent: '#7352a9',
        accentSoft: 'rgba(115, 82, 169, .16)',
        viewport: 'rgba(0, 0, 0, .2)',
      };
  }

  /** 吸附开关：关掉后拖拽不再出现对齐辅助线 */
  function setSnapOn(on) {
    S.snapOn = Boolean(on);
    localStorage.setItem('nc.snap', S.snapOn ? '1' : '0');
    if (!S.snapOn) hideGuides();
    syncToolState();
    toast(S.snapOn ? '对齐吸附已开启' : '对齐吸附已关闭');
  }

  /** 连线显示：全部 / 仅选中 / 隐藏 */
  function setEdgesMode(mode, silent) {
    S.edgesMode = EDGE_MODE_LABEL[mode] ? mode : 'all';
    localStorage.setItem('nc.edgesMode', S.edgesMode);
    renderEdges();
    syncToolState();
    if (!silent) toast(`连线显示：${EDGE_MODE_LABEL[S.edgesMode]}`);
  }

  function edgeVisible(edge) {
    if (S.edgesMode === 'none') return false;
    if (S.edgesMode === 'sel') {
      return (
        S.selectedEdge === edge.id ||
        Boolean(S.selectedEdges?.includes(edge.id)) ||
        S.selected.has(edge.from) ||
        S.selected.has(edge.to)
      );
    }
    return true;
  }

  /** 小地图开关 */
  function setMinimapOn(on) {
    S.minimapOn = Boolean(on);
    localStorage.setItem('nc.minimap', S.minimapOn ? '1' : '0');
    drawMinimap();
    syncToolState();
  }

  /** 把整张画布画成缩略图：节点方框 + 当前视口框 */
  function drawMinimap() {
    const wrap = $('#ncMinimap');
    const canvas = $('#ncMinimapCanvas');
    if (!wrap || !canvas) return;
    if (!S.minimapOn || !S.graph) {
      wrap.hidden = true;
      S.minimap = null;
      return;
    }
    wrap.hidden = false;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cw = Math.max(60, (wrap.clientWidth || 200) - 8);
    const ch = Math.max(40, (wrap.clientHeight || 130) - 8);
    if (canvas.width !== Math.round(cw * dpr) || canvas.height !== Math.round(ch * dpr)) {
      canvas.width = Math.round(cw * dpr);
      canvas.height = Math.round(ch * dpr);
    }
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);

    const MINIMAP = minimapColors();
    const vp = S.graph.viewport;
    const { w: vw, h: vh } = viewportSize();
    const viewMinX = -vp.x / vp.zoom;
    const viewMinY = -vp.y / vp.zoom;
    const viewMaxX = (vw - vp.x) / vp.zoom;
    const viewMaxY = (vh - vp.y) / vp.zoom;

    const nodes = S.graph.nodes || [];
    let minX = viewMinX, minY = viewMinY, maxX = viewMaxX, maxY = viewMaxY;
    for (const n of nodes) {
      minX = Math.min(minX, n.x);
      minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.w);
      maxY = Math.max(maxY, n.y + n.h);
    }
    const pad = 24;
    minX -= pad; minY -= pad; maxX += pad; maxY += pad;
    const worldW = Math.max(1, maxX - minX);
    const worldH = Math.max(1, maxY - minY);
    const scale = Math.min(cw / worldW, ch / worldH);
    const ox = (cw - worldW * scale) / 2 - minX * scale;
    const oy = (ch - worldH * scale) / 2 - minY * scale;
    S.minimap = { scale, ox, oy };

    // 节点：统一灰块，选中的才提亮（对齐 MiniMax 小地图的克制配色）
    for (const n of nodes) {
      const x = n.x * scale + ox;
      const y = n.y * scale + oy;
      const w = Math.max(2, n.w * scale);
      const h = Math.max(2, n.h * scale);
      const on = S.selected.has(n.id);
      ctx.fillStyle = on ? MINIMAP.accent : MINIMAP.node;
      ctx.globalAlpha = on ? 0.9 : 0.62;
      ctx.fillRect(x, y, w, h);
      ctx.globalAlpha = 1;
    }

    // 视口框：面积占比够大时才填色，小视口只描边，避免糊成一团
    const rx = viewMinX * scale + ox;
    const ry = viewMinY * scale + oy;
    const rw = (viewMaxX - viewMinX) * scale;
    const rh = (viewMaxY - viewMinY) * scale;
    const ratio = (rw * rh) / Math.max(1, cw * ch);
    if (ratio > 0.06) {
      ctx.fillStyle = MINIMAP.accentSoft;
      ctx.fillRect(rx, ry, rw, rh);
    }
    ctx.strokeStyle = MINIMAP.viewport;
    ctx.lineWidth = 1;
    ctx.strokeRect(rx + 0.5, ry + 0.5, rw - 1, rh - 1);
    ctx.strokeStyle = MINIMAP.accent;
    ctx.setLineDash([3, 2]);
    ctx.strokeRect(rx + 0.5, ry + 0.5, rw - 1, rh - 1);
    ctx.setLineDash([]);
  }

  /** 点小地图 = 把视口中心移过去 */
  function minimapTo(clientX, clientY) {
    if (!S.minimap || !S.graph) return;
    const canvas = $('#ncMinimapCanvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const { scale, ox, oy } = S.minimap;
    const wx = (clientX - rect.left - ox) / scale;
    const wy = (clientY - rect.top - oy) / scale;
    const { w, h } = viewportSize();
    const vp = S.graph.viewport;
    vp.x = w / 2 - wx * vp.zoom;
    vp.y = h / 2 - wy * vp.zoom;
    applyViewport();
    markDirty();
  }

  /** 平移模式：左键拖空白也能挪画布 */
  function setPanMode(on) {
    S.panMode = Boolean(on);
    $('#moduleNode')?.classList.toggle('nc-panmode', S.panMode);
    syncToolState();
  }

  /** 剪断模式：点连线直接断开 */
  function setCutMode(on) {
    S.cutMode = Boolean(on);
    $('#moduleNode')?.classList.toggle('nc-cutmode', S.cutMode);
    syncToolState();
  }

  /** 工具条按钮的激活态 */
  function syncToolState() {
    const root = $('#ncViewTools');
    if (!root) return;
    const set = (name, on) => root.querySelector(`[data-vtool="${name}"]`)?.classList.toggle('on', Boolean(on));
    set('pan', S.panMode);
    set('cut', S.cutMode);
    set('snap', S.snapOn);
    set('minimap', S.minimapOn);
    const edgesBtn = root.querySelector('[data-vtool="edges"]');
    if (edgesBtn) {
      edgesBtn.classList.toggle('on', S.edgesMode !== 'all');
      edgesBtn.title = `连线显示（L）：当前「${EDGE_MODE_LABEL[S.edgesMode]}」`;
    }
  }

  /* --------------------------------- 自动排版 --------------------------------- */

  function tidyLayout(mode = 'grid') {
    if (!S.graph) return;
    const all = S.graph.nodes || [];
    if (all.length < 2) return toast('节点太少，不用整理', true);
    const scoped = S.selected.size >= 2 ? all.filter((n) => S.selected.has(n.id)) : all.slice();
    if (scoped.length < 2) return toast('至少选中 2 个节点才能整理', true);

    pushHistory();
    // 排版间距跟画布基准一致：卡宽 350 + 间距 100 = 450 的模数
    const gapX = LAYOUT_GAP;
    const gapY = LAYOUT_GAP;
    const originX = Math.min(...scoped.map((n) => n.x));
    const originY = Math.min(...scoped.map((n) => n.y));

    if (mode === 'row' || mode === 'col') {
      // 单行 / 单列：按当前位置排序，保持阅读顺序
      const key = mode === 'row' ? (n) => n.x + n.y * 0.001 : (n) => n.y + n.x * 0.001;
      const list = scoped.slice().sort((a, b) => key(a) - key(b));
      let cursor = mode === 'row' ? originX : originY;
      if (mode === 'row') {
        const top = Math.min(...list.map((n) => n.y));
        for (const n of list) { n.x = cursor; n.y = top; cursor += n.w + gapX; }
      } else {
        const left = Math.min(...list.map((n) => n.x));
        for (const n of list) { n.y = cursor; n.x = left; cursor += n.h + gapY; }
      }
    } else if (mode === 'layer') {
      // 按连线的层级排：没有上游的在第一列，顺着连线一列列往右
      const ids = new Set(scoped.map((n) => n.id));
      const depth = new Map(scoped.map((n) => [n.id, 0]));
      const incoming = new Map(scoped.map((n) => [n.id, []]));
      for (const e of S.graph.edges || []) {
        if (ids.has(e.from) && ids.has(e.to)) incoming.get(e.to).push(e.from);
      }
      for (let round = 0; round < scoped.length; round += 1) {
        let changed = false;
        for (const n of scoped) {
          for (const from of incoming.get(n.id)) {
            const want = (depth.get(from) || 0) + 1;
            if ((depth.get(n.id) || 0) < want) { depth.set(n.id, want); changed = true; }
          }
        }
        if (!changed) break;
      }
      const cols = new Map();
      for (const n of scoped) {
        const d = depth.get(n.id) || 0;
        if (!cols.has(d)) cols.set(d, []);
        cols.get(d).push(n);
      }
      const ordered = [...cols.keys()].sort((a, b) => a - b);
      let x = originX;
      for (const d of ordered) {
        const list = cols.get(d).sort((a, b) => a.y - b.y);
        let y = originY;
        let colW = 0;
        for (const n of list) { n.x = x; n.y = y; y += n.h + gapY; colW = Math.max(colW, n.w); }
        x += colW + gapX;
      }
    } else {
      // 默认：网格排布，按当前阅读顺序（先上后下、先左后右）
      const list = scoped.slice().sort((a, b) => (Math.abs(a.y - b.y) > 24 ? a.y - b.y : a.x - b.x));
      const cols = Math.max(1, Math.ceil(Math.sqrt(list.length)));
      const colW = Math.max(...list.map((n) => n.w));
      const rowH = Math.max(...list.map((n) => n.h));
      list.forEach((n, i) => {
        n.x = originX + (i % cols) * (colW + gapX);
        n.y = originY + Math.floor(i / cols) * (rowH + gapY);
      });
    }

    for (const n of scoped) updateNodePosition(n);
    renderEdges();
    markDirty();
    drawMinimap();
    if (S.selected.size < 2) {
      fitView();
      toast('已整理整张画布');
    } else {
      toast(`已整理 ${scoped.length} 个节点`);
    }
  }

  /* ------------------------------ 定位 / 查找 / 聚焦 ------------------------------ */

  function nodeSearchText(node) {
    return [
      NODE_LABEL[node.type] || node.type,
      node.title,
      node.text,
      node.fileName,
      node.gen?.prompt,
      node.gen?.modelId,
    ]
      .filter(Boolean)
      .join('\n')
      .toLowerCase();
  }

  /** 节点摘要：查找和命令面板里显示得好看一点 */
  function nodeCaption(node) {
    const raw = node.text || node.gen?.prompt || node.fileName || node.title || '';
    const one = String(raw).replace(/\s+/g, ' ').trim();
    return one.length > 42 ? `${one.slice(0, 42)}…` : one || NODE_LABEL[node.type] || node.type;
  }

  /** 把某个节点挪到视口正中并选中 */
  function focusNode(id, keepZoom) {
    const node = nodeById(id);
    if (!node || !S.graph) return;
    const { w, h } = viewportSize();
    const vp = S.graph.viewport;
    if (!keepZoom) {
      const pad = 120;
      const fit = Math.min((w - pad) / Math.max(1, node.w), (h - pad) / Math.max(1, node.h));
      vp.zoom = Math.max(ZOOM_MIN, Math.min(1, fit));
    }
    vp.x = w / 2 - (node.x + node.w / 2) * vp.zoom;
    vp.y = h / 2 - (node.y + node.h / 2) * vp.zoom;
    S.selected = new Set([node.id]);
    S.selectedEdge = '';
    syncSelection();
    applyViewport();
    markDirty();
    const el = $(`.nc-node[data-id="${id}"]`);
    if (el) {
      el.classList.add('hit');
      setTimeout(() => el.classList.remove('hit'), 1200);
    }
  }

  /** 选中上游 / 下游：顺着连线把一整条链选出来 */
  function selectLinked(dir) {
    if (!S.graph) return;
    const seen = new Set(S.selected);
    if (!seen.size) return toast('先选中一个节点', true);
    const queue = [...seen];
    while (queue.length) {
      const id = queue.shift();
      for (const e of S.graph.edges || []) {
        const next = dir === 'up' ? (e.to === id ? e.from : '') : (e.from === id ? e.to : '');
        if (!next || seen.has(next)) continue;
        seen.add(next);
        queue.push(next);
      }
    }
    S.selected = seen;
    S.selectedEdge = '';
    syncSelection();
    toast(`已选中${dir === 'up' ? '上游' : '下游'} ${seen.size} 个节点`);
  }

  function openFind() {
    const el = $('#ncFind');
    if (!el) return;
    el.hidden = false;
    S.find.open = true;
    const input = $('#ncFindInput');
    if (input) {
      input.focus();
      input.select();
      runFind(input.value);
    }
  }

  function closeFind() {
    const el = $('#ncFind');
    if (el) el.hidden = true;
    S.find.open = false;
    for (const n of $$('.nc-node.hit')) n.classList.remove('hit');
  }

  function runFind(keyword) {
    const kw = String(keyword || '').trim().toLowerCase();
    S.find.kw = kw;
    S.find.ids = kw ? (S.graph?.nodes || []).filter((n) => nodeSearchText(n).includes(kw)).map((n) => n.id) : [];
    S.find.index = S.find.ids.length ? 0 : -1;
    renderFindCount();
    if (S.find.index >= 0) focusNode(S.find.ids[S.find.index]);
  }

  function renderFindCount() {
    const el = $('#ncFindCount');
    if (!el) return;
    const total = S.find.ids.length;
    el.textContent = total ? `${Math.min(S.find.index + 1, total)}/${total}` : '0/0';
  }

  function findStep(dir) {
    if (!S.find.ids.length) {
      runFind($('#ncFindInput')?.value || '');
      return;
    }
    const n = S.find.ids.length;
    S.find.index = (S.find.index + (dir > 0 ? 1 : -1) + n) % n;
    renderFindCount();
    focusNode(S.find.ids[S.find.index]);
  }

  /* -------------------------------- 命令面板 -------------------------------- */

  function commandEntries() {
    const list = [
      { icon: '＋', label: '新建文本节点', hint: 'A', run: () => addNode('text') },
      { icon: '＋', label: '新建图片节点', hint: '', run: () => addNode('image') },
      { icon: '＋', label: '新建视频节点', hint: '', run: () => addNode('video') },
      { icon: '＋', label: '新建音频节点', hint: '', run: () => addNode('audio') },
      { icon: '＋', label: '新建 AI 生图节点', hint: '', run: () => addNode('imageGen') },
      { icon: '＋', label: '新建 AI 视频节点', hint: '', run: () => addNode('videoGen') },
      { icon: '▦', label: '一键整理画布', hint: 'T', run: () => tidyLayout('grid') },
      { icon: '▦', label: '整理成单行', hint: '', run: () => tidyLayout('row') },
      { icon: '▦', label: '整理成单列', hint: '', run: () => tidyLayout('col') },
      { icon: '▦', label: '按连线层级排版', hint: '', run: () => tidyLayout('layer') },
      { icon: '⤢', label: '适应视图', hint: 'F', run: () => fitView() },
      { icon: '◎', label: '缩放到选中节点', hint: 'Shift+F', run: () => zoomToSelection() },
      { icon: '⌕', label: '查找节点', hint: 'Ctrl+F', run: () => openFind() },
      { icon: '⌁', label: `连线显示：${EDGE_MODE_LABEL[S.edgesMode]}`, hint: 'L', run: () => setEdgesMode(EDGE_MODE_NEXT[S.edgesMode]) },
      { icon: '⊹', label: S.snapOn ? '关闭对齐吸附' : '开启对齐吸附', hint: 'S', run: () => setSnapOn(!S.snapOn) },
      { icon: '▣', label: S.minimapOn ? '隐藏小地图' : '显示小地图', hint: 'M', run: () => setMinimapOn(!S.minimapOn) },
      { icon: '◐', label: '画布皮肤…', hint: '', run: () => openSkinPanel() },
      ...PATTERNS.map((p) => ({
        icon: p.key === 'flow' ? '✧' : p.key === 'none' ? '·' : '⌗',
        label: `底纹：${p.label}`,
        hint: p.key === S.pattern ? '当前' : '',
        run: () => setPattern(p.key),
      })),
      { icon: '✥', label: S.panMode ? '退出平移模式' : '平移模式', hint: 'H', run: () => setPanMode(!S.panMode) },
      { icon: '✂', label: S.cutMode ? '退出剪断模式' : '剪断连线模式', hint: 'C', run: () => setCutMode(!S.cutMode) },
      { icon: '⛶', label: '全屏画布', hint: '', run: () => $('#ncFullscreen')?.click() },
      { icon: '⌗', label: S.gridOn ? '隐藏底纹' : '显示底纹', hint: 'G', run: () => setGridOn(!S.gridOn) },
      { icon: '↺', label: '撤销', hint: 'Ctrl+Z', run: () => undo() },
      { icon: '↷', label: '重做', hint: 'Ctrl+Y', run: () => redo() },
      { icon: '⧉', label: '复制选中', hint: 'Ctrl+C', run: () => copySelection() },
      { icon: '⌫', label: '删除选中', hint: 'Delete', run: () => deleteSelected() },
      { icon: '◫', label: '打组', hint: 'Ctrl+G', run: () => groupSelected() },
      { icon: '◌', label: '取消打组', hint: 'Ctrl+Shift+G', run: () => ungroupSelected() },
      { icon: '⇡', label: '选中上游节点', hint: '', run: () => selectLinked('up') },
      { icon: '⇣', label: '选中下游节点', hint: '', run: () => selectLinked('down') },
      { icon: '≡', label: '左对齐', hint: '', run: () => alignSelected('left') },
      { icon: '≡', label: '水平居中', hint: '', run: () => alignSelected('hcenter') },
      { icon: '≡', label: '顶部对齐', hint: '', run: () => alignSelected('top') },
      { icon: '≡', label: '垂直居中', hint: '', run: () => alignSelected('vcenter') },
      { icon: '⋯', label: '水平等距', hint: '', run: () => alignSelected('hdist') },
      { icon: '⋮', label: '垂直等距', hint: '', run: () => alignSelected('vdist') },
      { icon: '?', label: '键盘快捷键一览', hint: '?', run: () => openHelp() },
    ];
    for (const n of S.graph?.nodes || []) {
      list.push({
        icon: '◇',
        label: `定位：${nodeCaption(n)}`,
        hint: NODE_LABEL[n.type] || n.type,
        run: () => focusNode(n.id),
        search: nodeSearchText(n),
      });
    }
    return list;
  }

  function renderCmdList(keyword) {
    const listEl = $('#ncCmdList');
    if (!listEl) return;
    const kw = String(keyword || '').trim().toLowerCase();
    const items = commandEntries().filter((it) => {
      if (!kw) return true;
      const hay = `${it.label}\n${it.hint || ''}\n${it.search || ''}`.toLowerCase();
      return hay.includes(kw);
    }).slice(0, 60);
    S.cmd = { items, index: items.length ? 0 : -1 };
    listEl.textContent = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'nc-cmd-empty';
      empty.textContent = '没有匹配的命令或节点';
      listEl.appendChild(empty);
      return;
    }
    items.forEach((it, i) => {
      const row = document.createElement('div');
      row.className = `nc-cmd-item${i === 0 ? ' active' : ''}`;
      row.dataset.index = String(i);
      row.innerHTML = `<span class="nc-cmd-icon">${esc(it.icon || '·')}</span><span class="nc-cmd-label">${esc(it.label)}</span><span class="nc-cmd-hint">${esc(it.hint || '')}</span>`;
      row.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        closeCmd();
        it.run();
      });
      listEl.appendChild(row);
    });
  }

  function cmdStep(dir) {
    const listEl = $('#ncCmdList');
    if (!listEl || !S.cmd?.items?.length) return;
    const n = S.cmd.items.length;
    S.cmd.index = (S.cmd.index + dir + n) % n;
    for (const row of $$('.nc-cmd-item', listEl)) {
      row.classList.toggle('active', Number(row.dataset.index) === S.cmd.index);
    }
    listEl.querySelector('.nc-cmd-item.active')?.scrollIntoView({ block: 'nearest' });
  }

  function cmdRun() {
    const item = S.cmd?.items?.[S.cmd.index];
    closeCmd();
    item?.run();
  }

  function openCmd() {
    const el = $('#ncCmd');
    if (!el) return;
    el.hidden = false;
    const input = $('#ncCmdInput');
    if (input) {
      input.value = '';
      input.focus();
    }
    renderCmdList('');
  }

  function closeCmd() {
    const el = $('#ncCmd');
    if (el) el.hidden = true;
  }

  const isCmdOpen = () => !$('#ncCmd')?.hidden;

  /* ------------------------------- 快捷键一览 ------------------------------- */

  const SHORTCUT_GROUPS = [
    ['视图', [
      ['平移画布', '中键拖 / Shift+拖 / H'],
      ['缩放', '滚轮'],
      ['适应视图', 'F'],
      ['缩放到选中', 'Shift+F'],
      ['显示 / 隐藏小地图', 'M'],
      ['显示 / 隐藏底纹', 'G'],
      ['画布皮肤（底色 + 底纹）', '工具条 ◐'],
    ]],
    ['选择', [
      ['框选', '左键拖空白'],
      ['加选 / 减选', 'Shift+点击'],
      ['全选', 'Ctrl+A'],
      ['编辑节点（底部面板）', '点一下节点'],
      ['收起编辑面板', 'Esc'],
      ['选中上游 / 下游', 'Alt+↑ / Alt+↓'],
      ['多选后浮动条', '布局 / 对齐 / 打组 / 复制'],
      ['取消选择', 'Esc'],
    ]],
    ['编辑', [
      ['撤销 / 重做', 'Ctrl+Z / Ctrl+Y'],
      ['复制 / 粘贴', 'Ctrl+C / Ctrl+V'],
      ['复制一份', 'Ctrl+D'],
      ['删除', 'Delete'],
      ['打组 / 取消打组', 'Ctrl+G / Ctrl+Shift+G'],
      ['保存', 'Ctrl+S'],
    ]],
    ['画布工具', [
      ['AI 编导（剧本 → 分镜）', 'D / 工具条「编导」'],
      ['一键生成分镜（分波次）', '顶部跑批条「一键生成」'],
      ['新建文本节点', '双击空白 / A'],
      ['连线', '拖端口或卡片右侧 ⊕ 到节点；拖到空白处新建并连接'],
      ['一键整理', 'T'],
      ['对齐 / 等距', '多选浮动条 ▸ 对齐'],
      ['连线显示切换', 'L'],
      ['连线换颜色', '右键连线'],
      ['对齐吸附开关', 'S'],
      ['剪断连线模式', 'C'],
      ['平移模式', 'H'],
      ['查找节点', 'Ctrl+F'],
      ['命令面板', 'Ctrl+K'],
      ['快捷键一览', '? / 左下角「快捷键」'],
      ['全屏画布', 'Esc 退出'],
    ]],
  ];

  function renderHelp() {
    const grid = $('#ncHelpGrid');
    if (!grid) return;
    if (grid.dataset.done === '1') return;
    grid.dataset.done = '1';
    grid.textContent = '';
    for (const [group, rows] of SHORTCUT_GROUPS) {
      const head = document.createElement('div');
      head.className = 'nc-help-group';
      head.textContent = group;
      grid.appendChild(head);
      for (const [label, keys] of rows) {
        const row = document.createElement('div');
        row.className = 'nc-help-row';
        row.innerHTML = `<span>${esc(label)}</span><kbd>${esc(keys)}</kbd>`;
        grid.appendChild(row);
      }
    }
  }

  function openHelp() {
    const el = $('#ncHelp');
    if (!el) return;
    renderHelp();
    el.hidden = false;
  }

  function closeHelp() {
    const el = $('#ncHelp');
    if (el) el.hidden = true;
  }

  /* --------------------------------- 复制 / 粘贴 --------------------------------- */

  function copySelection() {
    const ids = S.selected;
    if (!ids.size || !S.graph) return;
    const nodes = S.graph.nodes.filter((n) => ids.has(n.id)).map((n) => JSON.parse(JSON.stringify(n)));
    const edges = S.graph.edges
      .filter((e) => ids.has(e.from) && ids.has(e.to))
      .map((e) => ({ ...e }));
    S.clipboard = { nodes, edges };
    toast(`已复制 ${nodes.length} 个节点`);
  }

  function pasteClipboard() {
    if (!S.clipboard?.nodes?.length || !S.graph) return;
    pushHistory();
    const map = new Map();
    const created = [];
    for (const src of S.clipboard.nodes) {
      const copy = JSON.parse(JSON.stringify(src));
      copy.id = uid('nd');
      copy.x += 40;
      copy.y += 40;
      delete copy.groupId;
      // 生成中的任务不要跟着复制，否则两个节点抢同一个任务
      if (copy.gen) copy.gen = {};
      map.set(src.id, copy.id);
      S.graph.nodes.push(copy);
      created.push(copy);
      $('#ncNodes').appendChild(buildNode(copy));
    }
    for (const e of S.clipboard.edges) {
      const from = map.get(e.from);
      const to = map.get(e.to);
      if (from && to) S.graph.edges.push({ id: uid('eg'), from, to });
    }
    // 粘贴出来的这一批成为新的选中，连续粘贴才会一层层错开
    S.selected = new Set(created.map((n) => n.id));
    S.clipboard.nodes = created.map((n) => JSON.parse(JSON.stringify(n)));
    S.clipboard.edges = S.graph.edges.filter((e) => S.selected.has(e.from) && S.selected.has(e.to)).map((e) => ({ ...e }));
    S.selectedEdge = '';
    syncSelection();
    markDirty();
    renderEdges();
    syncEmpty();
    drawMinimap();
    toast(`已粘贴 ${created.length} 个节点`);
  }

  /** 缩放到选中的节点（没选就全图） */
  function zoomToSelection() {
    if (!S.graph) return;
    const list = S.selected.size
      ? S.graph.nodes.filter((n) => S.selected.has(n.id))
      : S.graph.nodes;
    if (!list.length) return fitView();
    const minX = Math.min(...list.map((n) => n.x));
    const minY = Math.min(...list.map((n) => n.y));
    const maxX = Math.max(...list.map((n) => n.x + n.w));
    const maxY = Math.max(...list.map((n) => n.y + n.h));
    const { w, h } = viewportSize();
    const zoom = Math.max(ZOOM_MIN, Math.min(1.5, (w - 120) / Math.max(1, maxX - minX), (h - 120) / Math.max(1, maxY - minY)));
    S.graph.viewport = {
      zoom,
      x: (w - (maxX - minX) * zoom) / 2 - minX * zoom,
      y: (h - (maxY - minY) * zoom) / 2 - minY * zoom,
    };
    applyViewport();
    markDirty();
  }

  /** 绑定视图工具条 / 小地图 / 查找 / 命令面板 / 快捷键面板 */
  function bindTools() {
    const tools = $('#ncViewTools');
    if (tools && tools.dataset.bound !== '1') {
      tools.dataset.bound = '1';
      tools.addEventListener('click', (event) => {
        const btn = event.target.closest('[data-vtool]');
        if (!btn) return;
        switch (btn.dataset.vtool) {
          case 'director': if (isDirectorOpen()) closeDirector(); else openDirector(); break;
          case 'pan': setPanMode(!S.panMode); break;
          case 'add': openCmd(); break;
          case 'tidy': tidyLayout('grid'); break;
          case 'edges': setEdgesMode(EDGE_MODE_NEXT[S.edgesMode]); break;
          case 'cut': setCutMode(!S.cutMode); break;
          case 'snap': setSnapOn(!S.snapOn); break;
          case 'minimap': setMinimapOn(!S.minimapOn); break;
          case 'skin': if (isSkinOpen()) closeSkinPanel(); else openSkinPanel(); break;
          case 'find': if (S.find.open) closeFind(); else openFind(); break;
          case 'cmd': openCmd(); break;
          case 'help': openHelp(); break;
          default: break;
        }
      });
    }

    // 小地图：按下就跳，拖动持续跟随
    const minimap = $('#ncMinimap');
    if (minimap && minimap.dataset.bound !== '1') {
      minimap.dataset.bound = '1';
      let dragging = false;
      minimap.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        event.stopPropagation();
        dragging = true;
        minimap.setPointerCapture(event.pointerId);
        minimapTo(event.clientX, event.clientY);
      });
      minimap.addEventListener('pointermove', (event) => {
        if (!dragging) return;
        minimapTo(event.clientX, event.clientY);
      });
      minimap.addEventListener('pointerup', () => { dragging = false; });
    }

    // 查找
    const findInput = $('#ncFindInput');
    if (findInput && findInput.dataset.bound !== '1') {
      findInput.dataset.bound = '1';
      findInput.addEventListener('input', () => runFind(findInput.value));
      findInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          findStep(event.shiftKey ? -1 : 1);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          closeFind();
        }
      });
    }
    const findWrap = $('#ncFind');
    if (findWrap && findWrap.dataset.bound !== '1') {
      findWrap.dataset.bound = '1';
      findWrap.addEventListener('pointerdown', (event) => event.stopPropagation());
      $('#ncFindPrev')?.addEventListener('click', () => findStep(-1));
      $('#ncFindNext')?.addEventListener('click', () => findStep(1));
      $('#ncFindClose')?.addEventListener('click', closeFind);
    }

    // 素材库选图弹层：盖在画布上，交互一个都不能漏下去（否则点图等于在画布上框选）
    const libPick = $('#ncLibPick');
    if (libPick && libPick.dataset.bound !== '1') {
      libPick.dataset.bound = '1';
      libPick.addEventListener('pointerdown', (event) => {
        event.stopPropagation();
        if (event.target === libPick) closeLibPick();
      });
      libPick.addEventListener('wheel', (event) => event.stopPropagation(), { passive: true });
      $('#ncLibPickClose')?.addEventListener('click', closeLibPick);
      $('#ncLibPickOk')?.addEventListener('click', () => confirmLibPick());
      $('#ncLibPickUpload')?.addEventListener('click', () => {
        // 素材库里没有想要的：关掉弹层走系统选文件，传完直接在待发列表里看到
        closeLibPick();
        $('#ncChatFile')?.click?.();
      });
    }

    // 命令面板
    const cmd = $('#ncCmd');
    if (cmd && cmd.dataset.bound !== '1') {
      cmd.dataset.bound = '1';
      cmd.addEventListener('pointerdown', (event) => {
        if (event.target === cmd) closeCmd();
        else event.stopPropagation();
      });
      const input = $('#ncCmdInput');
      input?.addEventListener('input', () => renderCmdList(input.value));
      input?.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown') { event.preventDefault(); cmdStep(1); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); cmdStep(-1); }
        else if (event.key === 'Enter') { event.preventDefault(); cmdRun(); }
        else if (event.key === 'Escape') { event.preventDefault(); closeCmd(); }
      });
    }

    // 快捷键面板
    const help = $('#ncHelp');
    if (help && help.dataset.bound !== '1') {
      help.dataset.bound = '1';
      help.addEventListener('pointerdown', (event) => {
        if (event.target === help) closeHelp();
        else event.stopPropagation();
      });
      $('#ncHelpClose')?.addEventListener('click', closeHelp);
    }

    // 皮肤面板：点面板外的空白就关掉
    const skin = $('#ncSkin');
    if (skin && skin.dataset.bound !== '1') {
      skin.dataset.bound = '1';
      skin.addEventListener('pointerdown', (event) => {
        if (event.target === skin) closeSkinPanel();
        else event.stopPropagation();
      });
      $('#ncSkinClose')?.addEventListener('click', closeSkinPanel);
    }

    // 窗口尺寸变化：小地图要按新的画布尺寸重算
    if (!S.resizeBound) {
      S.resizeBound = true;
      let timer = 0;
      window.addEventListener('resize', () => {
        clearTimeout(timer);
        timer = setTimeout(() => drawMinimap(), 120);
      });
    }

    syncToolState();
  }

  /* -------------------------------- 外部接口 -------------------------------- */

  /** app.js 每 1.5 秒喂一次状态：只同步数据，不打断正在编辑的节点 */
  function sync(state) {
    S.stateSeen = true;
    S.graphs = state.nodeCanvases || S.graphs;
    S.assets = state.nodeAssets || S.assets;
    S.imageModels = state.imageModels || [];
    S.videoModels = state.videoModels || [];
    S.videoPresets = state.videoDefaults?.providers || {};
    // 图片的比例 → 尺寸预设（按协议分），编辑区「比例」网格用
    S.imageDefaults = state.imageDefaults || S.imageDefaults || {};
    S.imageTasks = state.imageTasks || [];
    S.videoTasks = state.videoTasks || [];

    const modelsSig = [
      S.imageModels.map((m) => `${m.id}:${m.name}`).join(','),
      S.videoModels.map((m) => `${m.id}:${m.name}:${m.provider}`).join(','),
    ].join('|');
    const modelsChanged = modelsSig !== S.modelSig;
    S.modelSig = modelsSig;

    if (!S.graph) {
      if (S.graphs.length) loadGraph(S.graphs[0].id);
      else ensureCanvas();
      renderAssets();
      return;
    }

    // 服务端已经带上本地刚建的画布了，就不用再护着它
    for (const id of [...justCreated]) {
      if (S.graphs.some((g) => g.id === id)) justCreated.delete(id);
    }

    // 当前画布被别人删了：退回第一张。
    // 但「刚新建 / 刚导入」的那张要例外：POST 已经返回、本地也切过去了，
    // 可这一轮 state 可能是 POST 之前发出、之后才回来的旧快照，里面还没有这张画布。
    // 不挡住的话用户会被踢回第一张，新画布像是没建成（还会诱导他重复新建）。
    if (S.currentId && !S.graphs.some((g) => g.id === S.currentId)) {
      if (justCreated.has(S.currentId)) {
        renderCanvasSelect();
        return;
      }
      if (S.graphs.length) loadGraph(S.graphs[0].id);
      else ensureCanvas();
      return;
    }

    renderCanvasSelect();
    if (modelsChanged) {
      // 模型列表变了才重建生图节点的表单；正在输入的节点不动
      for (const node of S.graph.nodes) {
        if (!['imageGen', 'videoGen'].includes(node.type)) continue;
        const el = $(`.nc-node[data-id="${node.id}"]`);
        if (el?.contains(document.activeElement)) continue;
        rerenderNode(node);
      }
    }
    syncGenStatuses();
    updateRunBar();
    renderAssets();
  }

  /** 生成任务跑完 → 把结果写回节点 */
  function syncGenStatuses() {
    let changed = false;
    for (const node of S.graph?.nodes || []) {
      if (!['imageGen', 'videoGen'].includes(node.type)) continue;
      const gen = node.gen;
      if (!gen?.taskId || !PENDING.has(gen.status)) continue;

      if (node.type === 'imageGen') {
        const task = S.imageTasks.find((t) => t.id === gen.taskId);
        if (!task) continue;
        // 长任务（本地豆包桥一轮 30~60 秒）会往 task.progress 写进度文案。
        // 只改状态那一行文字，不重建节点 —— 重建会打断用户正在输入的提示词
        if (task.status === 'running' && task.progress && gen.progress !== task.progress) {
          gen.progress = task.progress;
          changed = true;
          const el = genStatusElOf(node);
          if (el) el.textContent = task.progress;
        }
        if (task.status === 'success') {
          const image = task.images?.[0];
          if (image?.localUrl) {
            gen.status = 'success';
            gen.progress = '';
            gen.resultUrl = image.localUrl;
            changed = true;
            rerenderNode(node);
          }
        } else if (['failed', 'cancelled'].includes(task.status)) {
          gen.status = task.status;
          gen.progress = '';
          gen.error = task.error || '生成失败';
          changed = true;
          rerenderNode(node);
        }
      } else {
        const task = S.videoTasks.find((t) => t.id === gen.taskId);
        if (!task) continue;
        // 视频一轮动辄几分钟，进度也得逐条推到卡片上，否则用户只能盯着「生成中」猜
        if (task.status === 'running' && task.progress && gen.progress !== task.progress) {
          gen.progress = task.progress;
          changed = true;
          const el = genStatusElOf(node);
          if (el) el.textContent = task.progress;
        }
        if (task.status === 'success' && task.video?.localUrl) {
          gen.status = 'success';
          gen.progress = '';
          gen.resultUrl = task.video.localUrl;
          changed = true;
          rerenderNode(node);
        } else if (['failed', 'cancelled', 'canceled'].includes(task.status)) {
          gen.progress = '';
          gen.status = 'failed';
          gen.error = task.error || '生成失败';
          changed = true;
          rerenderNode(node);
        }
      }
    }
    if (changed) markDirty();
  }

  function onShow() {
    if (!S.inited) {
      S.inited = true;
      bindViewport();
      bindEvents();
      bindTools();
      bindDirector();
      bindSelBar();
      applySkin();
      refreshHistoryButtons();
      // 聊天窗是画布的一部分，默认就摆在那儿（用户关过才记住关闭）
      if (localStorage.getItem(CHAT_OPEN_KEY) !== '0') openDirector({ focus: false });
    }
    // 状态还没到：先把界面挂起来，等 sync() 里再决定加载/新建画布
    if (!S.stateSeen) return;
    if (!S.graph) {
      if (S.graphs.length) loadGraph(S.graphs[0].id);
      else ensureCanvas();
    } else {
      applyViewport();
      renderEdges();
    }
    renderAssets();
    // 模块刚打开时把素材库刷一遍，避免显示的是很久以前的列表
    reloadAssets();
    // 缩略卡不需要按布局量高度：图片加载完会自己按比例精调（fitThumbHeight）
  }

  // 皮肤是「整个壳」的（侧栏 / 顶栏 / 画布都是同一张纸色），所以不等进画布模块就
  // 先应用一次：否则用户切到视频生成或素材页时，外壳会退回默认中性灰。
  applySkin();

  // 切主题时重画小地图 + 重刷皮肤面板（皮肤面板要给夜间模式加一句说明）
  document.addEventListener('rh:theme', () => {
    drawMinimap();
    if (isSkinOpen()) renderSkinPanel();
  });

  window.RHNode = { sync, onShow };
})();
