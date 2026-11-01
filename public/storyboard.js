/**
 * 分镜（小剧场）前端
 *
 * 流程：给个题材 → AI 写剧本（角色 + N 个镜头）→ 逐个角色出「定妆照」→ 确认形象 →
 * 一键批量出分镜图。
 *
 * 角色一致性全靠「定妆照」：每个镜头出图时都拿角色的定妆照当图生图参考图，
 * 所以定妆照没出来之前不让出分镜图（后端也会拦一道）。
 *
 * 只暴露 window.RHStoryboard = { sync(state), onShow() }，由 app.js 调用。
 * 工具函数统一从 window.RHUtil 现取（本文件在 app.js 之前加载，不能在加载时缓存）。
 */

const SB = {
  boards: [],
  defaults: {},
  currentId: '', // 当前在看哪份剧本
  creating: false, // 「新建剧本」面板是否展开
  busy: false, // 正在写剧本/派发任务，防重复点
  // 新建剧本时用户上传的人物图（已登记进图库的记录，带 id / localUrl）
  photos: [],
  // 刚落地的本地版本，等服务器列表追上来（见 sync 里的竞态保护）
  fresh: new Map(),
};

/** 把一份「刚拿到的最新剧本」记进本地，并压过可能还在路上的旧轮询响应 */
function keepFresh(board) {
  const index = SB.boards.findIndex((b) => b.id === board.id);
  if (index >= 0) SB.boards[index] = board;
  else SB.boards.unshift(board);
  SB.fresh.set(board.id, { board, at: Date.now() });
}

const sb$ = (sel) => document.querySelector(sel);
const sbUtil = () => window.RHUtil || {};
const sbApi = (path, options) => sbUtil().api(path, options);
const sbToast = (message, isError) => sbUtil().toast(message, isError);

const SB_STATUS_LABEL = {
  draft: '刚出剧本',
  casting: '在定妆',
  drawing: '在出图',
  done: '已完成',
};
const SB_IMG_STATUS = {
  queued: '排队中',
  running: '生成中',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

/* --------------------------------- 小工具 --------------------------------- */

/** 剧本列表下拉 */
function renderPick() {
  const select = sb$('#sbPick');
  if (!select) return;
  const sig = SB.boards.map((b) => `${b.id}:${b.title}:${b.status}`).join('|');
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    if (!SB.boards.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = '（还没有剧本）';
      select.appendChild(option);
    }
    for (const board of SB.boards) {
      const option = document.createElement('option');
      option.value = board.id;
      option.textContent = `${board.title}（${SB_STATUS_LABEL[board.status] || board.status}）`;
      select.appendChild(option);
    }
  }
  select.value = SB.currentId || '';
}

/** 画风下拉 */
function renderStyles() {
  const select = sb$('#sbStyle');
  if (!select || select.dataset.built) return;
  select.dataset.built = '1';
  for (const style of SB.defaults.styles || []) {
    const option = document.createElement('option');
    option.value = style.prompt;
    option.textContent = style.label;
    select.appendChild(option);
  }
  select.value = SB.defaults.defaultStyle || '';
  const count = sb$('#sbShotCount');
  if (count) {
    count.min = SB.defaults.minShots || 3;
    count.max = SB.defaults.maxShots || 12;
  }
}

const currentBoard = () => SB.boards.find((b) => b.id === SB.currentId) || null;

/** 一张图片任务的状态徽章 */
function statusBadge(status) {
  const label = SB_IMG_STATUS[status] || status || '';
  if (!label) return '';
  const cls = status === 'success' ? 'ok' : status === 'failed' ? 'err' : 'run';
  return `<span class="sb-badge ${cls}">${sbUtil().esc(label)}</span>`;
}

/* --------------------------------- 渲染 --------------------------------- */

function renderStage() {
  const stage = sb$('#sbStage');
  if (!stage) return;

  const board = currentBoard();
  if (!board) {
    stage.innerHTML = `<p class="hint sb-empty">还没有剧本。点上面「+ 新建剧本」，给个题材（或留空让 AI 自己编），它会把角色和镜头都排好。</p>`;
    stage.dataset.sig = 'empty';
    return;
  }

  // 签名守卫：内容没变就别重建。这一屏有十几张 <img>（定妆照 + 每个镜头），
  // sync() 每 1.5 秒调一次 renderAll()，没有守卫就是持续销毁重建 DOM 与图片。
  const sig = [
    board.id,
    board.title,
    board.style,
    board.error || '',
    (board.characters || []).map((c) => `${c.name}|${c.refUrl || ''}|${c.cast?.status || ''}|${c.note || ''}`).join(','),
    (board.shots || []).map((s) => `${s.index}|${s.image?.status || ''}|${s.image?.url || ''}|${s.image?.error || ''}`).join(','),
  ].join('#');
  if (stage.dataset.sig === sig) return;
  stage.dataset.sig = sig;

  const castedCount = board.characters.filter((c) => c.refUrl).length;
  const drawn = board.shots.filter((s) => s.image?.status === 'success').length;
  const allCasted = castedCount === board.characters.length && castedCount > 0;

  // 角色区：形象图（用户给的 or AI 定妆照）+ 每个角色一个「出定妆照 / 重出」
  const castHtml = board.characters
    .map((c) => {
      const thumb = c.refUrl
        ? `<img src="${sbUtil().esc(c.refUrl)}" alt="" loading="lazy" />`
        : `<span class="sb-cast-empty">未定妆</span>`;
      const busy = c.cast && ['queued', 'running'].includes(c.cast.status);
      // 用户自己给的人物图：标一下，并且不提示「重新生成」（重出会覆盖用户给的那张）
      const tag = c.fromPhoto ? '<span class="sb-badge ok">你给的人物图</span>' : '';
      return `<div class="sb-cast">
        <div class="sb-cast-thumb">${thumb}</div>
        <div class="sb-cast-body">
          <strong>${sbUtil().esc(c.name)} ${tag}</strong>
          <span class="hint sb-cast-desc" title="${sbUtil().esc(c.desc)}">${sbUtil().esc(c.desc)}</span>
          <div class="sb-cast-actions">
            ${busy ? statusBadge(c.cast.status) : ''}
            ${
              c.refUrl
                ? `<button class="btn tiny" type="button" data-sb-cast="${sbUtil().esc(c.name)}">重出定妆照</button>`
                : `<button class="btn tiny primary" type="button" data-sb-cast="${sbUtil().esc(c.name)}" ${busy ? 'disabled' : ''}>生成定妆照</button>`
            }
            ${c.note ? `<span class="hint sb-err">${sbUtil().esc(c.note)}</span>` : ''}
          </div>
        </div>
      </div>`;
    })
    .join('');

  // 镜头区：每镜头一张卡
  const shotsHtml = board.shots
    .map((s) => {
      const img = s.image;
      const media = img?.url
        ? `<img src="${sbUtil().esc(img.url)}" alt="" loading="lazy" />`
        : `<span class="sb-shot-empty">${img ? SB_IMG_STATUS[img.status] || '' : '未出图'}</span>`;
      return `<article class="sb-shot">
        <div class="sb-shot-head">
          <span class="sb-shot-no">#${s.index}</span>
          ${img ? statusBadge(img.status) : ''}
        </div>
        <div class="sb-shot-media">${media}</div>
        <p class="sb-shot-scene">${sbUtil().esc(s.scene)}</p>
        ${s.dialogue ? `<p class="sb-shot-line">「${sbUtil().esc(s.dialogue)}」</p>` : ''}
        <div class="sb-shot-foot">
          ${s.characters.length ? `<span class="hint">${sbUtil().esc(s.characters.join(' / '))}</span>` : ''}
          ${img?.error ? `<span class="hint sb-err" title="${sbUtil().esc(img.error)}">${sbUtil().esc(img.error.slice(0, 40))}</span>` : ''}
        </div>
      </article>`;
    })
    .join('');

  stage.innerHTML = `
    <div class="sb-board">
      <div class="sb-board-head">
        <div class="sb-board-title">
          <h3>${sbUtil().esc(board.title)}</h3>
          <span class="hint">${sbUtil().esc(board.style)}</span>
        </div>
        <div class="sb-board-actions">
          <span class="hint">定妆 ${castedCount}/${board.characters.length} · 出图 ${drawn}/${board.shots.length}</span>
          <button class="btn tiny primary" type="button" id="sbDraw" ${allCasted ? '' : 'disabled'}
            title="${allCasted ? '给所有还没出图的镜头批量出图' : '先把所有角色的定妆照出好，才能保证每个镜头里人物长相一致'}">
            一键出分镜图
          </button>
          <button class="btn tiny danger" type="button" id="sbDelete">删除剧本</button>
        </div>
      </div>
      ${board.error ? `<div class="error-box">${sbUtil().esc(board.error)}</div>` : ''}
      <div class="sb-cast-list">${castHtml}</div>
      <div class="sb-shot-grid">${shotsHtml}</div>
    </div>`;
}

function renderAll() {
  renderStyles();
  renderPick();
  renderStage();
  renderPhotos();
  const create = sb$('#sbCreate');
  if (create) create.hidden = !SB.creating;
}

/** 新建剧本区的人物图缩略图 */
function renderPhotos() {
  const list = sb$('#sbPhotoList');
  if (!list) return;
  const sig = SB.photos.map((p) => p.id).join(',');
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  list.textContent = '';
  SB.photos.forEach((photo, index) => {
    const item = document.createElement('div');
    item.className = 'sb-photo-item';
    const img = document.createElement('img');
    img.src = photo.localUrl;
    img.alt = '';
    img.loading = 'lazy';
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'sb-photo-del';
    del.title = '移除这张图';
    del.textContent = '×';
    del.addEventListener('click', () => {
      SB.photos.splice(index, 1);
      renderPhotos();
    });
    const no = document.createElement('span');
    no.className = 'sb-photo-no';
    no.textContent = `第 ${index + 1} 位`;
    item.append(img, del, no);
    list.appendChild(item);
  });
  const hint = sb$('#sbPhotoHint');
  if (hint) {
    hint.textContent = SB.photos.length
      ? `已选 ${SB.photos.length} 张：AI 会按顺序把第 1 张当第 1 个角色，并照着图写外貌描述`
      : '给了图，AI 会照着图里的人写角色，人物就是你指定的人；不给就由 AI 自己编角色';
  }
}

/** 上传人物图：复用 app.js 已有的参考图上传（原始二进制 + x-filename 头） */
async function uploadPhotos(files) {
  const max = SB.defaults.maxCharacters || 4;
  const room = max - SB.photos.length;
  if (room <= 0) {
    sbToast(`最多 ${max} 张人物图`, true);
    return;
  }
  const upload = sbUtil().uploadRefBlob;
  if (!upload) {
    sbToast('上传接口不可用，刷新界面再试', true);
    return;
  }
  for (const file of [...files].slice(0, room)) {
    try {
      const ref = await upload(file, file.name);
      SB.photos.push(ref);
    } catch (err) {
      sbToast(`「${file.name}」上传失败：${err.message}`, true);
    }
  }
  renderPhotos();
}

/* --------------------------------- 交互 --------------------------------- */

/** 新建剧本：调 AI 写 */
async function writeScript() {
  if (SB.busy) return;
  const hint = sb$('#sbCreateHint');
  const btn = sb$('#sbWrite');
  SB.busy = true;
  if (btn) btn.disabled = true;
  if (hint) {
    hint.textContent = SB.photos.length
      ? `AI 正在照着 ${SB.photos.length} 张人物图编剧本，可能要十几秒…`
      : 'AI 正在编剧本，可能要十几秒…';
  }
  try {
    const board = await sbApi('/api/storyboards', {
      method: 'POST',
      body: {
        idea: sb$('#sbIdea')?.value || '',
        shotCount: Number(sb$('#sbShotCount')?.value) || 6,
        style: sb$('#sbStyle')?.value || '',
        // 人物图按顺序对应角色：第 1 张 = 第 1 个角色
        photoIds: SB.photos.map((p) => p.id),
      },
      // 写剧本比普通请求慢得多，给足超时（后端自己也有 120 秒上限）
      timeoutMs: 150000,
    });
    SB.boards.unshift(board);
    SB.fresh.set(board.id, { board, at: Date.now() });
    SB.currentId = board.id;
    SB.creating = false;
    SB.photos = [];
    if (hint) hint.textContent = '';
    sbToast(`剧本《${board.title}》已生成：${board.characters.length} 个角色、${board.shots.length} 个镜头`);
    renderAll();
  } catch (err) {
    if (hint) hint.textContent = err.message;
  } finally {
    SB.busy = false;
    if (btn) btn.disabled = false;
  }
}

/** 给某个角色出定妆照 */
async function castOne(name) {
  const board = currentBoard();
  if (!board) return;
  try {
    const out = await sbApi(`/api/storyboards/${board.id}/cast`, { method: 'POST', body: { name } });
    // 用返回的最新快照覆盖本地那份，界面立刻显示「定妆中」
    keepFresh(out.board);
    sbToast(`「${name}」的定妆照已排队，出好后会自动显示`);
    renderAll();
  } catch (err) {
    sbToast(err.message, true);
  }
}

/** 一键批量出分镜图 */
async function drawAll() {
  const board = currentBoard();
  if (!board) return;
  const btn = sb$('#sbDraw');
  if (btn) btn.disabled = true;
  try {
    const out = await sbApi(`/api/storyboards/${board.id}/draw`, { method: 'POST', body: {} });
    keepFresh(out.board);
    sbToast(out.created ? `已排入 ${out.created} 个镜头，图片是串行生成的，请耐心等` : '没有需要出图的镜头');
    renderAll();
  } catch (err) {
    sbToast(err.message, true);
    if (btn) btn.disabled = false;
  }
}

async function deleteBoard() {
  const board = currentBoard();
  if (!board) return;
  // 必须用项目自己的弹窗：Electron 里原生 confirm() 关掉后窗口会整个点不动
  const ask = sbUtil().confirmDialog;
  const yes = ask
    ? await ask(`删除剧本《${board.title}》？已经生成出来的图片不会被删，仍在「图片生成」里。`, {
        title: '删除剧本',
        okText: '删除',
      })
    : true;
  if (!yes) return;
  try {
    await sbApi(`/api/storyboards/${board.id}`, { method: 'DELETE' });
    SB.boards = SB.boards.filter((b) => b.id !== board.id);
    // 顺手清掉本地暂存：sync() 里那段「刚写完的剧本先留 15 秒」的竞态保护
    // 会把 SB.fresh 里的条目重新塞回列表 —— 不清的话刚删的剧本十几秒内又冒出来
    SB.fresh.delete(board.id);
    SB.currentId = SB.boards[0]?.id || '';
    sbToast('剧本已删除');
    renderAll();
  } catch (err) {
    sbToast(err.message, true);
  }
}

/* --------------------------------- 剧本模型 --------------------------------- */

/**
 * 「AI 编导」（剧本 → 分镜）用的是哪家对话模型，就在这个弹窗里改。
 * 配置存在 db.storyModel，接口是 /api/story-model；apiKey 回显的是掩码，
 * 留空 = 保持原样（不重填就不该把原来的 Key 冲掉）。
 */
function renderStoryModelForm() {
  const form = sb$('#storyModelForm');
  if (!form) return;
  const own = SB.defaults || {};
  const engine = own.engine === 'doubao' ? 'doubao' : 'api';
  form.elements.namedItem('engine').value = engine;
  form.elements.namedItem('scriptEngine').value = own.scriptEngine === 'codex' ? 'codex' : '';
  form.elements.namedItem('baseUrl').value = own.baseUrl || '';
  form.elements.namedItem('model').value = own.model || '';
  const key = form.elements.namedItem('apiKey');
  key.value = '';
  key.placeholder = own.hasKey ? `已保存 ${own.apiKey || '****'}（留空 = 不修改）` : 'API Key';
  const src = sb$('#storyKeySrc');
  if (src) src.textContent = own.hasKey ? '已配置 Key，留空即保持不变' : '还没配 Key，编导跑不起来';
  // 选了本地豆包，上面三格就不参与判断了（它压根不调那套接口），
  // 但**不能禁掉**：用户可能只是临时切过去试试，回头还要切回云端模型。
  const isDoubao = engine === 'doubao';
  form.dataset.engine = engine;
  const hint = sb$('#storyModelHint');
  if (hint) {
    hint.textContent = isDoubao
      ? '当前：本地豆包（要开着豆包窗口；一版分镜约 10~30 秒）'
      : (own.model
        ? `当前：${own.model}${own.baseUrl ? ` @ ${own.baseUrl}` : ''}`
        : '还没配编导模型 —— AI 编导会用不了。');
  }
  // 写剧本那一层的说明：这一层是「可选加成」，没装 Codex 也不影响编导能用
  const scriptSrc = sb$('#scriptEngineSrc');
  if (scriptSrc) {
    scriptSrc.textContent = own.scriptEngine === 'codex'
      ? '写剧本：本地 GPT（只在你给的是「一句题材」时才动手，写一版约 20~40 秒）'
      : '写剧本：不额外写（你给什么就拆什么）';
  }
}

function openStoryModelModal() {
  renderStoryModelForm();
  const modal = sb$('#modalStoryModel');
  if (modal) modal.hidden = false;
  sb$('#storyModelForm')?.elements.namedItem('model')?.focus();
  // 再拉一次权威值：state 里的可能是上一轮轮询留下的，别拿旧值糊在表单上
  sbApi('/api/story-model')
    .then((snapshot) => {
      if (snapshot && !modal?.hidden) {
        SB.defaults = snapshot;
        renderStoryModelForm();
      }
    })
    .catch(() => {});
}

async function saveStoryModelForm(event) {
  event.preventDefault();
  const form = event.target;
  const value = (role) => String(form.elements.namedItem(role).value || '').trim();
  try {
    const snapshot = await sbApi('/api/story-model', {
      method: 'POST',
      body: {
        engine: value('engine'),
        scriptEngine: value('scriptEngine'),
        baseUrl: value('baseUrl'),
        model: value('model'),
        apiKey: value('apiKey'),
      },
    });
    if (snapshot) SB.defaults = snapshot;
    renderStoryModelForm();
    sbToast('剧本模型已保存');
    // 节点画布那边的「AI 编导」也读同一份配置，通知它重拉一次，否则它还显示旧模型名
    document.dispatchEvent(new CustomEvent('story-model-changed', { detail: snapshot || null }));
    await sbUtil().refresh?.();
  } catch (err) {
    sbToast(err.message, true);
  }
}

/* --------------------------------- 事件绑定 --------------------------------- */

function bindEvents() {
  sb$('#sbNew')?.addEventListener('click', () => {
    SB.creating = !SB.creating;
    const create = sb$('#sbCreate');
    if (create) create.hidden = !SB.creating;
    if (SB.creating) sb$('#sbIdea')?.focus();
  });

  sb$('#sbCancelNew')?.addEventListener('click', () => {
    SB.creating = false;
    const create = sb$('#sbCreate');
    if (create) create.hidden = true;
  });

  sb$('#sbWrite')?.addEventListener('click', () => writeScript());

  // 剧本模型（编导用哪个对话模型）
  sb$('#sbModelManage')?.addEventListener('click', () => openStoryModelModal());
  // 切引擎时把提示文案跟着换掉，不用保存就能看出「豆包不需要 Key」
  sb$('#storyModelForm')?.elements?.namedItem('engine')
    ?.addEventListener('change', () => {
      const form = sb$('#storyModelForm');
      if (!form) return;
      SB.defaults = { ...(SB.defaults || {}), engine: form.elements.namedItem('engine').value };
      renderStoryModelForm();
    });
  // 切「写剧本」的引擎时同理：文案要跟着变
  sb$('#storyModelForm')?.elements?.namedItem('scriptEngine')
    ?.addEventListener('change', () => {
      const form = sb$('#storyModelForm');
      if (!form) return;
      SB.defaults = { ...(SB.defaults || {}), scriptEngine: form.elements.namedItem('scriptEngine').value };
      renderStoryModelForm();
    });
  // 检测本地 GPT：装没装、登没登录，现场探一次
  sb$('#scriptEngineProbe')?.addEventListener('click', async () => {
    const src = sb$('#scriptEngineSrc');
    if (src) src.textContent = '正在检测本地 GPT…';
    try {
      const info = await sbApi('/api/script-engine');
      if (src) {
        src.textContent = info?.available
          ? `本地 GPT 可用：${info.version || 'codex'}（${info.exe}）`
          : `本地 GPT 不可用：${info?.reason === 'no-exe' ? '没找到 codex.exe，装一个 Codex 桌面版并登录' : (info?.reason || '检测失败')}`;
      }
    } catch (err) {
      if (src) src.textContent = `检测失败：${err.message}`;
    }
  });
  sb$('#modalStoryModel')?.addEventListener('click', (event) => {
    if (event.target.id === 'modalStoryModel' || event.target.closest('[data-close]')) {
      sb$('#modalStoryModel').hidden = true;
    }
  });
  sb$('#storyModelForm')?.addEventListener('submit', (event) => saveStoryModelForm(event));

  // 人物图上传
  sb$('#sbPhotoAdd')?.addEventListener('click', () => sb$('#sbPhotoInput')?.click());
  sb$('#sbPhotoInput')?.addEventListener('change', (event) => {
    const files = event.target.files;
    if (files?.length) uploadPhotos(files);
    event.target.value = ''; // 清掉，同一个文件再选一次也能触发 change
  });

  // 题材框里按 Ctrl+Enter 直接开写
  sb$('#sbIdea')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      writeScript();
    }
  });

  sb$('#sbPick')?.addEventListener('change', (event) => {
    SB.currentId = event.target.value;
    renderStage();
  });

  // 剧本舞台里的按钮都是动态生成的，统一委托
  sb$('#sbStage')?.addEventListener('click', (event) => {
    const castBtn = event.target.closest('[data-sb-cast]');
    if (castBtn) return castOne(castBtn.dataset.sbCast);
    if (event.target.closest('#sbDraw')) return drawAll();
    if (event.target.closest('#sbDelete')) return deleteBoard();
    return undefined;
  });

  // 视频模块的两个子 Tab
  sb$('#vdTabs')?.addEventListener('click', (event) => {
    const tab = event.target.closest('.vd-tab');
    if (tab) switchVideoTab(tab.dataset.vtab);
  });
}

/** 视频模块内切换子 Tab */
function switchVideoTab(name) {
  const next = name === 'story' ? 'story' : 'single';
  for (const tab of document.querySelectorAll('#vdTabs .vd-tab')) {
    tab.classList.toggle('active', tab.dataset.vtab === next);
  }
  const single = sb$('#vdPaneSingle');
  const story = sb$('#vdPaneStory');
  if (single) single.hidden = next !== 'single';
  if (story) story.hidden = next !== 'story';
  try {
    localStorage.setItem('rh.videoTab', next);
  } catch (err) {
    /* 隐私模式下 localStorage 不可用，忽略 */
  }
  if (next === 'story') renderAll();
}

/** 恢复上次停在哪个子 Tab */
function restoreVideoTab() {
  let saved = 'single';
  try {
    saved = localStorage.getItem('rh.videoTab') || 'single';
  } catch (err) {
    saved = 'single';
  }
  switchVideoTab(saved);
}

bindEvents();
restoreVideoTab();

/* --------------------------------- 对外接口 --------------------------------- */

window.RHStoryboard = {
  /** app.js 每次刷新状态都会调这里 */
  sync(state) {
    const fromServer = state.storyboards || [];
    SB.defaults = state.storyDefaults || {};

    // 竞态保护：刚写完剧本/刚派发任务时，本地已经拿到最新那份，
    // 但此刻可能有一个「在它之前发出」的轮询响应正在路上，带回来的是旧列表 ——
    // 直接覆盖会让刚生成的东西闪一下就不见了。所以把「刚拿到的本地版本」留一小会儿，
    // 等服务器列表追上（或被别的操作改掉）再放手。
    const now = Date.now();
    const merged = [...fromServer];
    for (const [id, entry] of SB.fresh) {
      if (now - entry.at > 15000) {
        SB.fresh.delete(id);
        continue;
      }
      if (!merged.some((b) => b.id === id)) merged.unshift(entry.board);
    }
    // 服务端已经是权威版本了就清掉本地暂存
    for (const id of [...SB.fresh.keys()]) {
      if (fromServer.some((b) => b.id === id)) SB.fresh.delete(id);
    }
    SB.boards = merged;

    // 当前看的那份没了（被删或超上限裁剪）就退回第一份
    if (SB.currentId && !SB.boards.some((b) => b.id === SB.currentId)) SB.currentId = '';
    if (!SB.currentId && SB.boards.length) SB.currentId = SB.boards[0].id;
    // 只有「视频模块 + 分镜子页」都露着才重渲染：否则每 1.5 秒白建一次 DOM。
    // 这里必须连模块一起判 —— switchModule() 只改 body.dataset.module，
    // 不会去动子页的 hidden，光看 hidden 的话切到别的模块后照样每 1.5 秒重建 12 个镜头的 <img>。
    if (document.body.dataset.module === 'video' && sb$('#vdPaneStory') && !sb$('#vdPaneStory').hidden) {
      renderAll();
    } else {
      renderStyles();
      renderPick();
    }
  },

  /** 切到视频模块时调一次 */
  onShow() {
    renderAll();
  },

  /** 给节点画布的「AI 编导」参数面板用：那边也有个「写剧本的模型」按钮，共用同一个弹窗 */
  openModelModal() {
    openStoryModelModal();
  },
};
