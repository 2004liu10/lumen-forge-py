/**
 * 视频生成模块前端
 *
 * 整个视频模块（工作台 / 结果 / 历史 / 模型管理）都在这里，只对外暴露三个方法：
 *   window.RHVideo = { sync(state), receiveImage(taskId, imageId), onShow() }
 * app.js 的 refresh() 每次拿到 /api/state 就调 sync()，其他一概不关心。
 *
 * 工具函数由 app.js 通过 window.RHUtil 提供（本文件在 app.js 之前加载，
 * 所以只能在真正用到的时候再去取，不能在加载时缓存）。
 */

const VS = {
  models: [],
  tasks: [],
  inputs: [],
  defaults: {},
  currentInputId: '', // 当前选中的输入图（纯前端状态，后端只记任务上的快照）
  submitting: false,
};

const v$ = (sel) => document.querySelector(sel);
const util = () => window.RHUtil || {};
const vApi = (path, options) => util().api(path, options);
const vToast = (message, isError) => util().toast(message, isError);
const vFmtSize = (bytes) => (util().fmtSize ? util().fmtSize(bytes) : '');

/** 服务商预设由后端下发，前后端共用一份，避免候选值两边各写一遍 */
const vProviders = () => VS.defaults.providers || {};
const vProviderOf = (model) => vProviders()[model?.provider] || null;

const V_STATUS_LABEL = {
  queued: '排队中',
  submitting: '提交中',
  polling: '生成中',
  downloading: '下载中',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
const V_ACTIVE = new Set(['queued', 'submitting', 'polling', 'downloading']);
const V_PARAM_LABEL = { duration: '时长', resolution: '分辨率', ratio: '比例', seed: '种子' };
const DEFAULT_INPUT_HINT = '作为视频首帧，也可在自用模块的结果图点「送去视频」';

/* --------------------------------- 小工具 --------------------------------- */

/** 表单里的一行下拉：候选值来自服务商预设，模型自带的默认值也保证能选中。
 *  labels 是「值 → 显示文案」的映射（智谱用宽x高当值、显示成比例），没给就直接显示值 */
function fillSelect(select, values, preferred, labels) {
  if (!select) return;
  const items = ['', ...(values || [])];
  const want = String(preferred ?? '').trim();
  if (want && !items.includes(want)) items.unshift(want);
  const sig = items.join(',') + '|' + Object.keys(labels || {}).length;
  if (select.dataset.sig === sig) {
    select.value = want;
    return;
  }
  select.dataset.sig = sig;
  select.textContent = '';
  for (const value of items) {
    const option = document.createElement('option');
    option.value = value;
    const label = labels?.[value];
    option.textContent = value ? (label ? `${label} · ${value}` : value) : '（不指定）';
    select.appendChild(option);
  }
  select.value = want;
}

function buildSelect(className, role, title) {
  const select = document.createElement('select');
  select.className = className;
  select.dataset.role = role;
  select.title = title;
  return select;
}

function buildInput(className, role, placeholder, title) {
  const input = document.createElement('input');
  input.className = className;
  input.dataset.role = role;
  input.placeholder = placeholder;
  input.title = title || placeholder;
  input.autocomplete = 'off';
  return input;
}

function buildHint(text, className) {
  const hint = document.createElement('span');
  hint.className = `hint ${className || ''}`.trim();
  hint.textContent = text;
  return hint;
}

/** 任务内容签名：没变就不重建 DOM，否则结果里的 <video> 会被反复销毁、播放进度归零 */
const vTaskSig = (task) =>
  task
    ? [
        task.id,
        task.status,
        task.remoteStatus,
        task.error,
        task.video?.localUrl,
        task.attempts,
        // 提升画质的结果也要算进签名：它不改变 localUrl，漏掉的话
        // 「送去提升画质」成功后按钮会一直卡在「提交中…」，且「已提升过」标签永远不出现
        task.video?.topaz ? `${task.video.topaz.tierLabel || ''}@${task.video.topaz.at || ''}` : '',
      ].join('|')
    : 'none';

const vParamText = (params) =>
  Object.entries(params || {})
    .filter(([, value]) => value !== '' && value !== null && value !== undefined)
    .map(([key, value]) => `${V_PARAM_LABEL[key] || key}=${value}`)
    .join(' · ');

/* ------------------------------- 工作台：渲染 ------------------------------- */

let vModelSig = null;

function renderModelOptions() {
  const select = v$('#vdModel');
  const list = VS.models.filter((m) => m.enabled);
  const sig = list.map((m) => `${m.id}:${m.name}:${m.provider}:${m.model}`).join(',');
  if (sig === vModelSig) return;
  vModelSig = sig;
  const previous = select.value;
  select.textContent = '';
  if (!list.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '还没有视频模型，点右侧「视频模型管理」添加';
    select.appendChild(option);
    select.disabled = true;
    syncParamOptions();
    return;
  }
  select.disabled = false;
  for (const model of list) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.name;
    select.appendChild(option);
  }
  select.value = list.some((m) => m.id === previous) ? previous : list[0].id;
  syncParamOptions();
}

/** 时长/分辨率/比例的候选值跟着「当前选中的模型」所属服务商走 */
function syncParamOptions() {
  const model = VS.models.find((m) => m.id === v$('#vdModel').value);
  const preset = vProviderOf(model);
  fillSelect(v$('#vdDuration'), preset?.durations, model?.defaultDuration);
  // 智谱的 size 就是「宽x高」，等于直接定比例：这一栏改叫「比例」，选项显示成「16:9 · 1920x1080」
  fillSelect(v$('#vdResolution'), preset?.resolutions, model?.defaultResolution, preset?.sizeLabels);
  v$('#vdResLabel').textContent = preset?.sizeLabels ? '比例' : '分辨率';
  const ratios = preset?.ratios || [];
  v$('#vdRatioWrap').hidden = !ratios.length;
  fillSelect(v$('#vdRatio'), ratios, model?.defaultRatio);
  // 没有比例参数的（百炼）就直说比例跟着输入图走，免得用户以为是漏做了
  v$('#vdRatioNote').textContent = preset?.ratioNote || '';

  const hint = v$('#vdModelHint');
  if (!preset) {
    hint.textContent = '';
    return;
  }
  const free = preset.freeModel && preset.freeModel === model?.model ? '（免费模型）' : '';
  hint.textContent = `${preset.label}${free} · ${preset.note || ''}`;
}

function renderInputPreview() {
  const wrap = v$('#vdInputPreview');
  const input = VS.inputs.find((i) => i.id === VS.currentInputId);
  const sig = input ? `${input.id}|${input.localUrl}` : 'none';
  if (wrap.dataset.sig === sig) return;
  wrap.dataset.sig = sig;
  wrap.textContent = '';

  if (!input) {
    wrap.appendChild(buildHint('还没有输入图片'));
    return;
  }
  const img = document.createElement('img');
  img.src = input.localUrl;
  img.alt = '';
  img.loading = 'lazy';
  wrap.appendChild(img);

  const meta = document.createElement('div');
  meta.className = 'vd-input-meta';
  const name = document.createElement('span');
  name.textContent = input.originalName || input.fileName;
  name.title = name.textContent;
  meta.appendChild(name);
  meta.appendChild(buildHint(vFmtSize(input.size)));
  const tag = document.createElement('span');
  tag.className = 'used-tag';
  tag.textContent = input.source === 'fromImage' ? '来自图片生成' : '本地上传';
  meta.appendChild(tag);
  wrap.appendChild(meta);
}

function buildTaskMedia(task) {
  const media = document.createElement('div');
  media.className = 'vd-media';

  if (task.input?.localUrl) {
    const figure = document.createElement('figure');
    const img = document.createElement('img');
    img.src = task.input.localUrl;
    img.alt = '';
    img.loading = 'lazy';
    figure.appendChild(img);
    figure.appendChild(buildHint('输入图'));
    media.appendChild(figure);
  }
  if (task.video?.localUrl) {
    const figure = document.createElement('figure');
    // 用 createElement 建、且外层有签名守卫，播放进度才不会被 1.5 秒一次的刷新打断
    const video = document.createElement('video');
    video.src = task.video.localUrl;
    video.controls = true;
    video.preload = 'metadata';
    video.playsInline = true;
    figure.appendChild(video);

    // 结果的操作条：下载 / 送去提升画质 / 打开所在文件夹
    const bar = document.createElement('div');
    bar.className = 'vd-out-bar';

    const caption = document.createElement('a');
    caption.className = 'btn tiny';
    caption.href = task.video.localUrl;
    caption.download = task.video.fileName || '';
    caption.textContent = `下载（${vFmtSize(task.video.size)}）`;
    bar.appendChild(caption);

    // 送去提升画质：直接调 Topaz，不用先去翻文件夹再回「提升画质」页找
    const up = document.createElement('button');
    up.type = 'button';
    up.className = `btn tiny${task.video.topaz?.at ? '' : ' primary'}`;
    up.textContent = task.video.topaz?.at ? '已提升过 · 再提升' : '送去提升画质';
    up.title = task.video.topaz?.at
      ? `已经提升过：${task.video.topaz.tierLabel || ''}（再点会基于当前文件再提一次）`
      : '用本机 Topaz 提升这条视频的画质 / 分辨率（原片会保留）';
    up.addEventListener('click', () => sendToTopaz(task, up));
    bar.appendChild(up);

    figure.appendChild(bar);
    media.appendChild(figure);
  }
  return media;
}

/** 「送去提升画质」：按当前档位把这条视频排队给 Topaz，并打开弹窗看进度 */
async function sendToTopaz(task, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = '提交中…';
  }
  try {
    const tier = (window.RHUtil || {}).getTopazTier?.() || 'max';
    await vApi('/api/topaz/enhance', {
      method: 'POST',
      body: { source: 'video', taskId: task.id, index: 0, tier },
      timeoutMs: 30000,
    });
    vToast('已排队提升画质，正在处理…');
    (window.RHUtil || {}).openTopaz?.();
    // 成功也要把按钮放回来：以前只在 catch 里复位，于是提交成功后按钮
    // 永久停在「提交中…」，而卡片又因为签名没变不会重建，等于再也点不动
    if (btn) {
      btn.disabled = false;
      btn.textContent = '送去提升画质';
    }
    if (typeof refresh === 'function') refresh().catch(() => {});
  } catch (err) {
    vToast(err.message, true);
    if (btn) {
      btn.disabled = false;
      btn.textContent = '送去提升画质';
    }
  }
}

function buildTaskCard(task) {
  const card = document.createElement('article');
  card.className = `vd-card status-${task.status}`;
  card.dataset.taskId = task.id;

  const head = document.createElement('header');
  head.className = 'vd-card-head';
  const title = document.createElement('strong');
  title.textContent = task.modelName || '视频模型';
  head.appendChild(title);
  const badge = document.createElement('span');
  badge.className = `badge v-${task.status}`;
  badge.textContent = V_STATUS_LABEL[task.status] || task.status;
  head.appendChild(badge);
  const time = document.createElement('span');
  time.className = 'hint';
  time.textContent = new Date(task.createdAt).toLocaleString();
  head.appendChild(time);
  card.appendChild(head);

  const prompt = document.createElement('p');
  prompt.className = 'vd-prompt';
  prompt.textContent = task.prompt || '（没有提示词）';
  card.appendChild(prompt);

  const params = vParamText(task.params);
  if (params) card.appendChild(buildHint(params, 'vd-params-text'));

  card.appendChild(buildTaskMedia(task));

  if (task.status === 'failed' && task.error) {
    const error = document.createElement('div');
    error.className = 'error-box';
    error.textContent = task.error;
    card.appendChild(error);
  } else if (V_ACTIVE.has(task.status)) {
    const note = task.status === 'queued' ? '排队中，等前一个任务跑完…' : '生成中，每 15 秒向厂商查一次进度…';
    card.appendChild(buildHint(note, 'vd-progress'));
  }

  const actions = document.createElement('div');
  actions.className = 'vd-card-actions';
  if (task.status === 'queued') actions.appendChild(buildActButton('vd-cancel', '取消'));
  if (!V_ACTIVE.has(task.status)) actions.appendChild(buildActButton('vd-retry', '重新生成'));
  actions.appendChild(buildActButton('vd-delete', '删除记录', 'danger'));
  card.appendChild(actions);
  return card;
}

function buildActButton(act, text, extraClass) {
  const button = document.createElement('button');
  button.className = `btn tiny ${extraClass || ''}`.trim();
  button.dataset.act = act;
  button.textContent = text;
  return button;
}

function renderResult() {
  const wrap = v$('#vdResult');
  const task = VS.tasks[0] || null;
  const sig = vTaskSig(task);
  if (wrap.dataset.sig === sig) return;
  wrap.dataset.sig = sig;
  wrap.textContent = '';
  if (!task) {
    wrap.appendChild(buildHint('还没有生成记录。选好模型与输入图，填上提示词就能开始。'));
    return;
  }
  wrap.appendChild(buildTaskCard(task));
}

function renderHistory() {
  const wrap = v$('#vdHistory');
  const rest = VS.tasks.slice(1);
  const sig = rest.map(vTaskSig).join(',');
  if (wrap.dataset.sig === sig) return;
  wrap.dataset.sig = sig;
  wrap.textContent = '';
  if (!rest.length) return;

  const title = document.createElement('h3');
  title.textContent = `历史记录（${rest.length}）`;
  wrap.appendChild(title);
  const list = document.createElement('div');
  list.className = 'vd-history-list';
  for (const task of rest) list.appendChild(buildTaskCard(task));
  wrap.appendChild(list);
}

function syncFormStatus() {
  const busy = VS.tasks.find((t) => V_ACTIVE.has(t.status));
  v$('#vdSubmit').disabled = VS.submitting;
  const hint = v$('#vdStatus');
  if (VS.submitting) hint.textContent = '提交中…';
  else if (busy) hint.textContent = `队列里还有任务在跑（${V_STATUS_LABEL[busy.status] || busy.status}），新任务会排在后面`;
  else hint.textContent = '';
}

/* ------------------------------- 工作台：交互 ------------------------------- */

v$('#vdModel').addEventListener('change', () => {
  // 换模型就跟着换参数候选与默认值（和图片生成面板换模型带出尺寸是一样的手感）
  syncParamOptions();
});

v$('#vdInputAdd').addEventListener('click', () => v$('#vdInputFile').click());

v$('#vdInputClear').addEventListener('click', () => {
  VS.currentInputId = '';
  renderInputPreview();
});

v$('#vdInputFile').addEventListener('change', async (event) => {
  const fileInput = event.target;
  const file = fileInput.files?.[0];
  fileInput.value = '';
  if (!file) return;
  // accept 只过滤了选择框，用户仍可手动切到「所有文件」，这里再拦一道
  if (file.type && !file.type.startsWith('image/')) {
    return vToast('输入图只能是图片文件', true);
  }

  const hint = v$('#vdInputHint');
  hint.textContent = '上传中…';
  try {
    const res = await fetch('/api/video-inputs', {
      method: 'POST',
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'x-filename': encodeURIComponent(file.name),
      },
      body: file,
    });
    const json = await res.json().catch(() => null);
    if (!res.ok) throw new Error(json?.error || `上传失败（HTTP ${res.status}）`);
    VS.inputs.unshift(json);
    VS.currentInputId = json.id;
    renderInputPreview();
    vToast('输入图已就绪');
  } catch (err) {
    vToast(err.message, true);
  } finally {
    hint.textContent = DEFAULT_INPUT_HINT;
  }
});

v$('#vdForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const modelId = v$('#vdModel').value;
  const prompt = v$('#vdPrompt').value.trim();
  if (!modelId) return vToast('请先选择视频模型', true);
  if (!prompt) return vToast('请填写提示词', true);
  if (!VS.currentInputId) return vToast('请先添加输入图片', true);

  VS.submitting = true;
  syncFormStatus();
  try {
    const created = await vApi('/api/video-tasks', {
      method: 'POST',
      body: {
        modelId,
        prompt,
        duration: v$('#vdDuration').value,
        resolution: v$('#vdResolution').value,
        ratio: v$('#vdRatio').value,
        seed: v$('#vdSeed').value.trim(),
        inputId: VS.currentInputId,
      },
    });
    // 先塞进本地列表，界面立刻有反馈；下一次 refresh() 会用服务端数据覆盖
    VS.tasks.unshift(created);
    renderResult();
    renderHistory();
    v$('#vdPrompt').value = '';
    vToast('已加入生成队列');
  } catch (err) {
    vToast(err.message, true);
  } finally {
    VS.submitting = false;
    syncFormStatus();
  }
});

v$('#moduleVideo').addEventListener('click', async (event) => {
  const act = event.target.closest('[data-act]')?.dataset.act;
  if (!act) return;
  const taskId = event.target.closest('.vd-card')?.dataset.taskId;
  if (!taskId) return;

  try {
    if (act === 'vd-retry') {
      await vApi(`/api/video-tasks/${taskId}/retry`, { method: 'POST', body: {} });
      vToast('已重新排队');
    } else if (act === 'vd-cancel') {
      await vApi(`/api/video-tasks/${taskId}/cancel`, { method: 'POST', body: {} });
      vToast('已取消');
    } else if (act === 'vd-delete') {
      if (!(await confirmDialog('删除这条生成记录？本地视频文件也会一起删除。'))) return;
      await vApi(`/api/video-tasks/${taskId}`, { method: 'DELETE' });
      vToast('已删除记录');
    } else {
      return;
    }
    await util().refresh?.();
  } catch (err) {
    vToast(err.message, true);
  }
});

/* ------------------------------- 视频模型管理 ------------------------------- */

function fillProviderSelect(select, value) {
  const map = vProviders();
  const sig = Object.entries(map)
    .map(([key, preset]) => `${key}:${preset.label}`)
    .join(',');
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.textContent = '';
    for (const [key, preset] of Object.entries(map)) {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = preset.label;
      select.appendChild(option);
    }
  }
  if (value) select.value = value;
}

/** 模型 ID 候选：所有服务商的候选值合并进一个 datalist（ID 全局唯一，不会混淆） */
function fillModelIdDatalist() {
  const list = v$('#vdModelIdList');
  const all = [];
  for (const preset of Object.values(vProviders())) {
    for (const id of preset.models || []) if (!all.includes(id)) all.push(id);
  }
  const sig = all.join(',');
  if (list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  list.textContent = '';
  for (const id of all) {
    const option = document.createElement('option');
    option.value = id;
    list.appendChild(option);
  }
}

/** 添加表单里那三个参数下拉跟着当前选中的服务商走 */
function syncFormParamOptions() {
  const form = v$('#videoModelForm');
  const provider = form.elements.namedItem('provider').value;
  const preset = vProviders()[provider] || {};
  fillSelect(form.elements.namedItem('defaultDuration'), preset.durations, '');
  fillSelect(form.elements.namedItem('defaultResolution'), preset.resolutions, '', preset.sizeLabels);
  fillSelect(form.elements.namedItem('defaultRatio'), preset.ratios, '');
  v$('#vdProviderTip').textContent = preset.note || '';
  renderKeySrc(preset);
  applyLocalBridgeHints(form, provider);
}

/**
 * 本地豆包桥不走 API：没有 Key、也不读模型 ID。
 * 把这两格的提示换掉，否则用户会以为自己漏填了什么。
 */
function applyLocalBridgeHints(scope, provider) {
  const isLocal = provider === 'doubao';
  const key = scope.querySelector('[data-role="apiKey"]') || scope.querySelector('[name="apiKey"]');
  if (key) {
    key.disabled = isLocal;
    key.placeholder = isLocal ? '本地桥不需要 Key，留空' : 'API Key';
    if (isLocal) key.value = '';
  }
  const url = scope.querySelector('[data-role="baseUrl"]') || scope.querySelector('[name="baseUrl"]');
  if (url) url.placeholder = isLocal ? '豆包调试端口，如 http://127.0.0.1:9555' : '接口地址';
}

/** API Key 输入框下方标注该服务商取 Key 的页面，点一下跳到浏览器 */
function renderKeySrc(preset) {
  const box = v$('#vdKeySrc');
  const sig = `${preset.keyUrl || ''}|${preset.keyLabel || ''}`;
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.textContent = '';
  if (!preset.keyUrl) return;
  const link = document.createElement('a');
  link.href = preset.keyUrl;
  link.target = '_blank';
  link.rel = 'noreferrer';
  link.textContent = `${preset.keyLabel || '去获取 API Key'} ↗`;
  link.title = preset.keyUrl;
  box.appendChild(link);
}

/** 只把 baseUrl 从「某个预设的默认值」换成新服务商的默认值，用户手填过的地址不动 */
function applyProviderBaseUrl(select, urlInput) {
  const preset = vProviders()[select.value] || {};
  const known = new Set(Object.values(vProviders()).map((p) => p.baseUrl));
  const current = urlInput.value.trim();
  if (!current || known.has(current)) urlInput.value = preset.baseUrl || current;
}

function applyVideoModelFormDefaults() {
  const form = v$('#videoModelForm');
  fillProviderSelect(form.elements.namedItem('provider'), 'zhipu');
  fillModelIdDatalist();
  if (form.dataset.ready === '1') return;
  form.dataset.ready = '1';
  applyProviderBaseUrl(form.elements.namedItem('provider'), form.elements.namedItem('baseUrl'));
  syncFormParamOptions();
}

function buildVideoModelRow(model) {
  const row = document.createElement('div');
  row.className = 'image-model-row';
  row.dataset.modelId = model.id;

  const name = buildInput('im-name', 'name', '模型名称');
  name.value = model.name;

  const provider = buildSelect('im-protocol', 'provider', '服务商');
  fillProviderSelect(provider, model.provider);

  const baseUrl = buildInput('im-url', 'baseUrl', '接口地址');
  baseUrl.value = model.baseUrl;

  const apiKey = document.createElement('input');
  apiKey.className = 'im-key';
  apiKey.dataset.role = 'apiKey';
  apiKey.type = 'password';
  apiKey.value = '';
  apiKey.placeholder = model.hasKey ? '已配置，留空不修改' : '填 API Key';
  apiKey.title = model.hasKey ? `当前：${model.apiKey}（留空表示不修改）` : 'API Key';
  apiKey.autocomplete = 'off';

  const modelId = buildInput('im-model', 'model', '模型 ID');
  modelId.setAttribute('list', 'vdModelIdList');
  modelId.value = model.model;

  const duration = buildSelect('vd-duration', 'defaultDuration', '默认时长');
  const resolution = buildSelect('vd-resolution', 'defaultResolution', '默认分辨率');
  const ratio = buildSelect('vd-ratio', 'defaultRatio', '默认比例');
  const preset = vProviderOf(model) || {};
  fillSelect(duration, preset.durations, model.defaultDuration);
  fillSelect(resolution, preset.resolutions, model.defaultResolution, preset.sizeLabels);
  fillSelect(ratio, preset.ratios, model.defaultRatio);
  // 智谱这一栏存的是宽x高、本质是比例，标题也跟着改
  if (preset.sizeLabels) resolution.title = '默认比例';
  ratio.hidden = !(preset.ratios || []).length;

  const extraParams = buildInput('im-extra', 'extraParams', '{"seed": 12345}');
  extraParams.value = model.extraParams || '';

  applyLocalBridgeHints(row, model.provider);

  const enabled = document.createElement('label');
  enabled.className = 'switch im-enabled';
  const enabledBox = document.createElement('input');
  enabledBox.type = 'checkbox';
  enabledBox.dataset.role = 'enabled';
  enabledBox.checked = Boolean(model.enabled);
  enabled.appendChild(enabledBox);
  enabled.appendChild(document.createTextNode('启用'));

  const del = document.createElement('button');
  del.className = 'btn tiny danger';
  del.dataset.act = 'delete-video-model';
  del.textContent = '删除';

  row.append(name, provider, baseUrl, apiKey, modelId, duration, resolution, ratio, extraParams, enabled, del);

  const hint = buildHint(preset.note || '', 'im-ark-hint');
  row.appendChild(hint);
  return row;
}

function renderModelList() {
  const wrap = v$('#videoModelList');
  wrap.textContent = '';
  if (!VS.models.length) {
    wrap.appendChild(buildHint('还没有视频模型，可在上面直接添加一个（内置的智谱免费模型被删掉后不会自动恢复）。'));
    return;
  }
  for (const model of VS.models) wrap.appendChild(buildVideoModelRow(model));
}

const vSaveTimers = new Map();
const vSavePending = new Map();

/** 与图片模型一致的防抖 PATCH：编辑中的多次改动合并成一次请求 */
function queueVideoModelSave(id, patch) {
  clearTimeout(vSaveTimers.get(id));
  vSavePending.set(id, { ...(vSavePending.get(id) || {}), ...patch });
  vSaveTimers.set(
    id,
    setTimeout(async () => {
      const body = vSavePending.get(id);
      vSaveTimers.delete(id);
      vSavePending.delete(id);
      if (!body || !Object.keys(body).length) return;
      try {
        await vApi(`/api/video-models/${id}`, { method: 'PATCH', body });
        await util().refresh?.();
      } catch (err) {
        vToast(err.message, true);
      }
    }, 450),
  );
}

const V_MODEL_TEXT_FIELDS = ['name', 'baseUrl', 'model', 'extraParams'];

v$('#vdModelManage').addEventListener('click', () => {
  applyVideoModelFormDefaults();
  v$('#modalVideoModels').hidden = false;
  renderModelList();
});

v$('#modalVideoModels').addEventListener('click', (event) => {
  if (event.target.id === 'modalVideoModels' || event.target.closest('[data-close]')) {
    v$('#modalVideoModels').hidden = true;
  }
});

v$('#videoModelForm').addEventListener('change', (event) => {
  if (event.target.name !== 'provider') return;
  applyProviderBaseUrl(event.target, v$('#videoModelForm').elements.namedItem('baseUrl'));
  syncFormParamOptions();
});

v$('#videoModelForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const value = (role) => String(form.elements.namedItem(role).value || '').trim();
  try {
    await vApi('/api/video-models', {
      method: 'POST',
      body: {
        name: value('name'),
        provider: form.elements.namedItem('provider').value,
        baseUrl: value('baseUrl'),
        apiKey: value('apiKey'),
        model: value('model'),
        defaultDuration: value('defaultDuration'),
        defaultResolution: value('defaultResolution'),
        defaultRatio: value('defaultRatio'),
        extraParams: value('extraParams'),
      },
    });
    form.reset();
    form.dataset.ready = '';
    applyVideoModelFormDefaults();
    vToast('视频模型已添加');
    await util().refresh?.();
    renderModelList();
  } catch (err) {
    vToast(err.message, true);
  }
});

v$('#videoModelList').addEventListener('input', (event) => {
  const row = event.target.closest('[data-model-id]');
  if (!row) return;
  const role = event.target.dataset.role;
  if (role === 'apiKey') {
    const apiKey = event.target.value.trim();
    if (apiKey) queueVideoModelSave(row.dataset.modelId, { apiKey }); // 留空 = 不修改
    return;
  }
  if (V_MODEL_TEXT_FIELDS.includes(role)) {
    queueVideoModelSave(row.dataset.modelId, { [role]: event.target.value });
  }
});

v$('#videoModelList').addEventListener('change', (event) => {
  const row = event.target.closest('[data-model-id]');
  if (!row) return;
  const role = event.target.dataset.role;

  if (role === 'provider') {
    queueVideoModelSave(row.dataset.modelId, { provider: event.target.value });
    // 立刻把这一行的参数候选换成新服务商的，不用等下一轮刷新
    const preset = vProviders()[event.target.value] || {};
    fillSelect(row.querySelector('[data-role="defaultDuration"]'), preset.durations, '');
    fillSelect(row.querySelector('[data-role="defaultResolution"]'), preset.resolutions, '', preset.sizeLabels);
    fillSelect(row.querySelector('[data-role="defaultRatio"]'), preset.ratios, '');
    const resolution = row.querySelector('[data-role="defaultResolution"]');
    if (resolution) resolution.title = preset.sizeLabels ? '默认比例' : '默认分辨率';
    const ratio = row.querySelector('[data-role="defaultRatio"]');
    if (ratio) ratio.hidden = !(preset.ratios || []).length;
    const hint = row.querySelector('.im-ark-hint');
    if (hint) hint.textContent = preset.note || '';
    applyLocalBridgeHints(row, event.target.value);
    return;
  }
  if (role === 'enabled') {
    queueVideoModelSave(row.dataset.modelId, { enabled: event.target.checked });
    return;
  }
  if (['defaultDuration', 'defaultResolution', 'defaultRatio'].includes(role)) {
    queueVideoModelSave(row.dataset.modelId, { [role]: event.target.value });
  }
});

v$('#videoModelList').addEventListener('click', async (event) => {
  if (event.target.closest('[data-act]')?.dataset.act !== 'delete-video-model') return;
  const row = event.target.closest('[data-model-id]');
  if (!row) return;
  if (!(await confirmDialog('删除该视频模型？已生成的视频不受影响。'))) return;
  try {
    await vApi(`/api/video-models/${row.dataset.modelId}`, { method: 'DELETE' });
    vToast('视频模型已删除');
    await util().refresh?.();
    renderModelList();
  } catch (err) {
    vToast(err.message, true);
  }
});

/* --------------------------------- 对外接口 --------------------------------- */

window.RHVideo = {
  /** app.js 每次刷新状态都会调这里 */
  sync(state) {
    VS.models = state.videoModels || [];
    VS.tasks = state.videoTasks || [];
    VS.inputs = state.videoInputs || [];
    VS.defaults = state.videoDefaults || {};
    // 输入图可能被别处删掉了，清掉本地选中
    if (VS.currentInputId && !VS.inputs.some((i) => i.id === VS.currentInputId)) {
      VS.currentInputId = '';
    }
    renderModelOptions();
    renderInputPreview();
    renderResult();
    renderHistory();
    syncFormStatus();
    if (!v$('#modalVideoModels').hidden && !v$('#videoModelList').contains(document.activeElement)) {
      renderModelList();
    }
  },

  /** 自用模块的结果图点「送去视频」时调用；成功后由调用方切到视频 Tab */
  async receiveImage(taskId, imageId) {
    const input = await vApi(`/api/image-tasks/${taskId}/to-video`, {
      method: 'POST',
      body: { imageId },
    });
    if (!VS.inputs.some((i) => i.id === input.id)) VS.inputs.unshift(input);
    VS.currentInputId = input.id;
    renderInputPreview();
    return input;
  },

  /** 切到视频模块时调一次，保证界面上是最新状态 */
  onShow() {
    renderModelOptions();
    renderInputPreview();
    renderResult();
    renderHistory();
    syncFormStatus();
  },
};