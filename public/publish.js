/**
 * 发布抖音（桌面版专属）—— 半自动发作品。
 *
 * 布局：左边是我们自己的表单（不依赖抖音 DOM），右边内嵌「抖音创作者中心」的发布页
 * （<webview partition="persist:douyin">，和素材模块的抖音登录态共用，免二次登录）。
 * 流程：选视频 → 点「自动上传并填写」→ 主进程用 CDP 把本地 mp4 塞进网页的 file input、
 * 再自动填描述/话题/封面/定时 —— 最后一步「发布」永远由用户自己在右边页面点。
 *
 * 只暴露 window.RHPublish = { onShow() }，app.js 切模块时调一下。
 * 本文件在 app.js 之前加载，工具函数统一用 window.RHUtil 现取（不在加载时缓存）。
 */
(() => {
  const d$ = (sel) => document.querySelector(sel);
  const UPLOAD_URL = 'https://creator.douyin.com/creator-micro/content/upload';
  const DES_KEY = 'rh.publish.desc'; // 上次填的描述，切走再切回来还在

  let web = null; // 内嵌的 <webview>，建一次后常驻
  let webReady = false; // 这一轮页面加载完了没有
  let unsubscribeLog = null;
  let simItems = []; // 「搜相似视频」抓回来的同类视频

  const util = () => window.RHUtil || {};
  const toast = (msg, isError) => (util().toast || (() => {}))(msg, isError);
  const bridge = () => window.rhDesktop || null;

  /* --------------------------------- 日志 --------------------------------- */

  function logLine(ok, text) {
    const box = d$('#pubLog');
    if (!box) return;
    const line = document.createElement('p');
    line.className = `pub-log-line ${ok === true ? 'ok' : ok === false ? 'fail' : ''}`;
    line.textContent = `${ok === true ? '✓' : ok === false ? '✗' : '·'} ${text}`;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    // 日志别无限长：留最近 80 条够排查了
    while (box.children.length > 80) box.firstChild.remove();
  }

  function bindLog() {
    if (unsubscribeLog || !bridge()?.publish?.onLog) return;
    unsubscribeLog = bridge().publish.onLog((entry) => {
      logLine(entry?.ok, `[${entry?.step || '步骤'}] ${entry?.detail || ''}`);
    });
  }

  /* ------------------------------- 内嵌创作者页 ------------------------------- */

  function ensureWeb() {
    if (web) return;
    const host = d$('#pubWeb');
    if (!host) return;
    web = document.createElement('webview');
    web.setAttribute('partition', 'persist:douyin');
    web.setAttribute('src', UPLOAD_URL);
    web.setAttribute('allowpopups', '');
    web.addEventListener('dom-ready', () => {
      webReady = true;
      setStatus('页面已就绪：登录后点「自动上传并填写」');
      // 校验登录态：没登录时创作者中心只有扫码页，先提示
      bridge()?.publish?.session?.(guestId()).then((out) => {
        if (!out) return;
        if (out.ok && !out.loggedIn) {
          setStatus('还没登录抖音：请在右边页面扫码登录（和「素材 → 抖音采集」共用一个登录态）');
          logLine(false, '还没登录抖音，请先在右边页面扫码登录');
        } else if (!out.ok) {
          logLine(false, `登录校验失败：${out.error}`);
        }
      }).catch(() => {});
    });
    web.addEventListener('did-navigate', (event) => {
      webReady = true;
      detectPublished(event?.url);
    });
    // 创作者中心是单页应用，站内跳转不会触发 did-navigate，只会触发这个
    web.addEventListener('did-navigate-in-page', (event) => {
      webReady = true;
      detectPublished(event?.url);
    });
    // 创作者中心 window.open 出去的链接会跳系统浏览器、流程就丢了：拦住并提示
    web.addEventListener('new-window', (event) => {
      logLine(null, '页面想开新窗口，已拦下（请在当前页内继续操作）');
      toast('这个操作想开新窗口，已被拦下：请在当前页内完成', true);
    });
    host.textContent = '';
    host.appendChild(web);
  }

  function guestId() {
    try {
      return web && typeof web.getWebContentsId === 'function' ? web.getWebContentsId() : 0;
    } catch {
      return 0;
    }
  }

  function setStatus(text) {
    const el = d$('#pubStatus');
    if (el) el.textContent = text || '';
  }

  /**
   * 发布完成的自动检测：抖音发布成功后会跳到「作品管理」页。
   *
   * 只在「刚填完表、正等着发布」这个状态下才认这个跳转 ——
   * 否则用户自己点进作品管理页闲逛，也会被误记成「刚发布了一条」。
   */
  async function detectPublished(url) {
    if (!awaitingPublish) return;
    const u = String(url || '');
    if (!/creator\.douyin\.com/.test(u)) return;
    // 作品管理页 / 内容管理页：发布成功后抖音会跳到这里
    if (!/content\/(manage|video)/.test(u)) return;
    const p = pickedPath();
    if (!p) return;
    awaitingPublish = false;
    logLine(true, '检测到页面已跳到作品管理，判定发布完成');
    await markPublishedNow();
  }

  /* ------------------------------- 选本地视频 ------------------------------- */

  /** 当前选中的本地视频绝对路径；没选好返回 '' */
  function currentVideoPath() {
    return d$('#pubLocalPath')?.value || '';
  }

  async function pickLocalVideo() {
    const b = bridge();
    if (!b?.storage?.pickFile) return;
    const file = await b.storage.pickFile({
      title: '选择要发布的视频',
      filters: [{ name: '视频', extensions: ['mp4', 'mov', 'webm', 'mkv'] }],
    });
    if (!file) return;
    d$('#pubLocalPath').value = file;
    const name = file.split(/[\\/]/).pop();
    d$('#pubLocalName').textContent = name;
    // 系统文件框给的是绝对路径，网页这边没法播（也不该让后端去读任意路径）
    setPicked('', '本地文件');
    // 顺手把文件名（去掉扩展名和分隔符）填进话题搜索框，省得再打一遍字
    const kw = d$('#pubSearchKeyword');
    if (kw && !kw.value.trim()) {
      kw.value = name.replace(/\.[^.]+$/, '').replace(/[_\-.]+/g, ' ').trim();
    }
    logLine(true, `已选本地视频：${name}`);
    // 防重发第一道：选中就查，发过立刻贴红条
    pendingBlocked = null;
    checkDuplicate();
  }

  async function pickCoverImage() {
    const b = bridge();
    if (!b?.storage?.pickFile) return;
    const file = await b.storage.pickFile({
      title: '选择封面图',
      filters: [{ name: '图片', extensions: ['jpg', 'jpeg', 'png', 'webp'] }],
    });
    if (!file) return;
    d$('#pubCoverPath').value = file;
    d$('#pubCoverName').textContent = file.split(/[\\/]/).pop();
  }

  /* -------------------------------- 表单内容 -------------------------------- */

  /** 从抖音页面抓取推荐话题（基于已上传视频的内容匹配），展示为可勾选的芯片 */
  async function scrapeTopics() {
    const b = bridge();
    if (!b?.publish?.scrapeTopics || !guestId()) {
      toast('右边的页面还没加载好，稍等再试', true);
      return;
    }
    setStatus('正在从抖音页面抓取推荐话题…');
    const out = await b.publish.scrapeTopics(guestId());
    if (!out?.ok) {
      toast(out?.error || '抓取失败', true);
      logLine(false, `抓取话题失败：${out?.error || ''}`);
      return;
    }
    const topics = out.topics || [];
    if (!topics.length) {
      toast('页面还没推荐话题：先把视频上传到抖音，页面会基于内容生成推荐话题');
      logLine(null, '页面没有推荐话题，可能视频还没上传或页面改版了');
      return;
    }
    renderTopicChips(topics);
    logLine(true, `抓到 ${topics.length} 个推荐话题`);
    setStatus(`抓到 ${topics.length} 个推荐话题，勾选要用的，再点「自动上传并填写」`);
  }

  /** 用关键词在抖音搜同类视频，把每条的「文案 + 话题」列成卡片给用户勾 */
  async function searchSimilar() {
    const keyword = d$('#pubSearchKeyword')?.value?.trim();
    if (!keyword) {
      toast('先用一句话描述这条视频（如：二次元 紫发 跳舞）', true);
      return;
    }
    const b = bridge();
    if (!b?.publish?.searchSimilar) {
      toast('这个功能只在桌面版可用', true);
      return;
    }
    // 搜索走的是独立隐藏窗口，不依赖右边创作者页，所以不用等它加载好
    setStatus(`正在抖音搜索「${keyword}」的同类视频…（约 20 秒）`);
    logLine(null, `开始搜索「${keyword}」…`);
    const list = d$('#pubTopicList');
    if (list) {
      list.hidden = true;
      list.textContent = '';
    }
    const apply = d$('#pubSimActions');
    if (apply) apply.hidden = true;
    const out = await b.publish.searchSimilar(guestId(), keyword);
    if (!out?.ok) {
      toast(out?.error || '搜索失败', true);
      logLine(false, `搜索失败：${out?.error || ''}`);
      setStatus(`搜索中断：${out?.error || '搜索失败'}`);
      return;
    }
    const items = out.items || [];
    if (!items.length) {
      toast('没搜到带话题的同类视频，换个更具体的说法再搜', true);
      logLine(null, '搜索结果里没有带话题的视频，换个关键词');
      setStatus('没搜到带话题的同类视频，换个更具体的说法再搜');
      return;
    }
    renderSimList(items);
    logLine(true, `抓到 ${items.length} 条同类视频`);
    setStatus(`抓到 ${items.length} 条同类视频：勾选要用的，再点「把勾选的填进描述 + 话题」`);
  }

  const esc = (text) => (util().esc || ((s) => String(s ?? '')))(text);

  /** 把抓回来的同类视频列成卡片（复用「抖音标签」的 sim-card 样式） */
  function renderSimList(items) {
    const box = d$('#pubTopicList');
    if (!box) return;
    box.textContent = '';
    simItems = items;
    items.forEach((item, i) => {
      const card = document.createElement('label');
      card.className = 'sim-card';
      const tags = (item.tags || []).map((t) => `<span class="sim-tag">#${esc(t)}</span>`).join('');
      card.innerHTML =
        `<input type="checkbox" class="sim-pick" data-i="${i}" />` +
        '<div class="sim-body">' +
        `<p class="sim-desc">${item.desc ? esc(item.desc) : '（这条没有文案，只用话题）'}</p>` +
        `<div class="sim-tags">${tags}</div>` +
        '</div>';
      box.appendChild(card);
    });
    box.hidden = false;
    const apply = d$('#pubSimActions');
    if (apply) apply.hidden = false;
    box.querySelectorAll('.sim-pick').forEach((cb) => cb.addEventListener('change', updateSimCount));
    updateSimCount();
  }

  function updateSimCount() {
    const box = d$('#pubTopicList');
    const picked = box ? box.querySelectorAll('.sim-pick:checked').length : 0;
    const el = d$('#pubSimCount');
    if (el) el.textContent = `已选 ${picked} 条 / 共 ${simItems.length} 条`;
  }

  /** 把勾选的同类视频的文案 + 话题填进表单 */
  function applySimPicked() {
    const box = d$('#pubTopicList');
    if (!box) return;
    const picked = [...box.querySelectorAll('.sim-pick:checked')].map((cb) => simItems[Number(cb.dataset.i)]);
    if (!picked.length) {
      toast('先勾几条要用的', true);
      return;
    }
    // 话题去重、最多 8 个（堆太多会被抖音判砌）
    const tags = [];
    for (const item of picked) {
      for (const t of item.tags || []) {
        if (!tags.includes(t) && tags.length < 8) tags.push(t);
      }
    }
    // 文案取勾选里的第一条（同类作品的文案本就同题材，拿来当模板最省事）
    const desc = (picked.find((it) => it.desc)?.desc || '').slice(0, 150);
    if (desc && d$('#pubDesc')) d$('#pubDesc').value = desc;
    renderTopicChips(tags);
    logLine(true, `已填入：文案 1 条 + ${tags.length} 个话题`);
    setStatus('已填进描述和话题：核对后点「自动上传并填写」');
    toast('文案和话题已填好');
  }

  /** 把抓到的话题渲染成可勾选的芯片 */
  function renderTopicChips(topics) {
    const box = d$('#pubTopicChips');
    if (!box) return;
    box.textContent = '';
    box.dataset.topics = JSON.stringify(topics);
    for (const name of topics) {
      const chip = document.createElement('label');
      chip.className = 'pub-topic-chip';
      // 话题名来自抖音页面爬取 / 搜索结果 / 用户手输，全是外部内容：
      // 拼进 innerHTML 的话，一个带引号尖括号的名字就能闭合属性注入脚本，
      // 轻则把 checkbox 的 value 截断（后面 getSelectedTopics 读到错的话题）。
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = name;
      input.checked = true;
      const label = document.createElement('span');
      label.textContent = `#${name}`;
      chip.append(input, label);
      box.appendChild(chip);
    }
    box.hidden = false;
  }

  /** 获取用户勾选的话题名列表 */
  function getSelectedTopics() {
    const box = d$('#pubTopicChips');
    if (!box || box.hidden) return [];
    return [...box.querySelectorAll('input[type="checkbox"]:checked')].map((i) => i.value);
  }

  function readForm() {
    const tags = (d$('#pubTags')?.value || '')
      .split(/[\s，,]+/)
      .map((w) => w.trim().replace(/^#+/, ''))
      .filter(Boolean)
      .slice(0, 10);
    const coverMode = d$('#pubCoverPick')?.checked ? 'pick' : 'default';
    const scheduleOn = Boolean(d$('#pubScheduleOn')?.checked);
    let scheduleAt = '';
    if (scheduleOn) {
      const raw = d$('#pubSchedule')?.value || '';
      if (!raw) {
        toast('开了定时发布就要选时间', true);
        return null;
      }
      const when = new Date(raw);
      const diff = when.getTime() - Date.now();
      if (!(diff > 0)) {
        toast('定时时间必须是将来的时间', true);
        return null;
      }
      // 抖音允许 2 小时 ~ 14 天，提前在这里挡一下，别等网页报错
      if (diff < 2 * 3600 * 1000 || diff > 14 * 24 * 3600 * 1000) {
        toast('抖音只接受 2 小时后 ~ 14 天内的定时', true);
        return null;
      }
      const p = (n) => String(n).padStart(2, '0');
      scheduleAt = `${when.getFullYear()}-${p(when.getMonth() + 1)}-${p(when.getDate())} ${p(when.getHours())}:${p(when.getMinutes())}`;
    }
    return {
      desc: (d$('#pubDesc')?.value || '').trim(),
      tags,
      topicNames: getSelectedTopics(),
      coverMode,
      coverPath: coverMode === 'pick' ? d$('#pubCoverPath')?.value || '' : '',
      scheduleAt,
    };
  }

  /* ---------------------------- 发布记录（防重发） ---------------------------- */

  /**
   * 用户的问题：发完一条，下次回来表单里还是那条视频，顺手一点就重发了。
   *
   * 三道防线：
   *   ① 选中视频时查一次：发过就贴红条警告
   *   ② 点「自动上传并填写」时再查一次：发过就先问一句，确认了才继续
   *   ③ 发布完成后**把表单清空**（这条最关键 —— 表单空了就不可能顺手重发）
   *
   * 「发布完成」有两个来源：用户自己点「✓ 已发布，记一笔」，
   * 或者页面自己跳到了作品管理页（说明发布成功跳转了）。两个都认。
   */
  let logState = { records: [], loaded: false };
  let awaitingPublish = false; // 已经填好表、正等着用户在右边点发布
  let pendingBlocked = null; // 被拦下的那条（用户选择「仍然继续」后放行一次）

  const api = (p, opts) => (window.RHUtil || {}).api(p, opts);

  async function loadRecords() {
    try {
      const out = await api('/api/publish/records', { timeoutMs: 10000 });
      logState.records = out?.records || [];
      logState.loaded = true;
    } catch {
      logState.loaded = true;
    }
    renderHistory();
    return logState.records;
  }

  /** 当前选中的视频路径 */
  const pickedPath = () => d$('#pubLocalPath')?.value || '';

  /**
   * 选视频后 / 点上传前查这条发过没有。
   * force=false 时只更新提示条；force=true 时把结果返回给调用方决定拦不拦。
   */
  async function checkDuplicate({ silent = false } = {}) {
    const p = pickedPath();
    if (!p) {
      showDup(null);
      return null;
    }
    let out = null;
    try {
      out = await api('/api/publish/check', { method: 'POST', body: { path: p }, timeoutMs: 10000 });
    } catch {
      return null; // 查不了就别拦人，宁可不提示也不能挡住正常发布
    }
    if (!silent) showDup(out);
    return out;
  }

  /** 提示条：blocked 用红色（发过了），warn 用黄色（刚填过 / 内容改过） */
  function showDup(info) {
    const box = d$('#pubDup');
    if (!box) return;
    if (!info || (!info.blocked && !info.warn)) {
      box.hidden = true;
      box.textContent = '';
      box.className = 'pub-dup';
      return;
    }
    const when = info.record?.publishedAt || info.record?.filledAt || 0;
    const timeText = when ? new Date(when).toLocaleString('zh-CN', { hour12: false }) : '';
    box.hidden = false;
    box.className = `pub-dup ${info.blocked ? 'is-block' : 'is-warn'}`;
    box.textContent = `${info.blocked ? '⚠ ' : '· '}${info.reason}${timeText ? `（${timeText}）` : ''}`;
    if (info.blocked) {
      // 「仍然继续」：确实想重发（比如上次发失败又删了作品）时给条路
      const go = document.createElement('button');
      go.className = 'btn tiny';
      go.type = 'button';
      go.textContent = '仍然继续发布';
      go.addEventListener('click', () => {
        pendingBlocked = info.record?.id || null;
        box.hidden = true;
        toast('已确认：这次仍然上传同一条视频');
      });
      box.appendChild(go);
    }
  }

  /** 发布完成后清空表单 —— 防重发的关键一步 */
  function clearForm({ keepDesc = false } = {}) {
    const path = d$('#pubLocalPath');
    if (path) path.value = '';
    const name = d$('#pubLocalName');
    if (name) name.textContent = '未选择';
    setPicked('');
    const chips = d$('#pubTopicChips');
    if (chips) {
      chips.textContent = '';
      chips.hidden = true;
    }
    if (!keepDesc) {
      const desc = d$('#pubDesc');
      if (desc) desc.value = '';
      try { localStorage.removeItem(DES_KEY); } catch { /* 记不住就算了 */ }
    }
    const cov = d$('#pubCoverPath');
    if (cov) cov.value = '';
    const covName = d$('#pubCoverName');
    if (covName) covName.textContent = '';
    awaitingPublish = false;
    pendingBlocked = null;
    showDup(null);
  }

  /** 记一笔「已发布」，并清空表单 */
  async function markPublishedNow({ quiet = false } = {}) {
    const p = pickedPath();
    if (!p) {
      toast('还没有选中的视频：这条记录可能已经记过了', true);
      return;
    }
    try {
      const out = await api('/api/publish/published-by-path', {
        method: 'POST',
        body: { path: p },
        timeoutMs: 10000,
      });
      logState.records = out?.records || logState.records;
      renderHistory();
      const nm = p.split(/[\\/]/).pop();
      // record 为空 = 库里没有这条的登记（比如填表那步的请求失败了）。
      // 这时不能假装记上了 —— 否则下次照样认不出来、还会重发。
      if (!out?.record) {
        toast(`没找到「${nm}」的填表记录，这次没能记上：请确认它确实发布成功了`, true);
        logLine(false, `标记失败：库里没有 ${nm} 的填表记录`);
        return;
      }
      if (!quiet) {
        toast(`已记下「${nm}」发布完成，表单已清空，可以发下一条了`);
        logLine(true, `发布记录：${nm} 已标记为已发布，表单已清空`);
      }
      clearForm();
      setStatus('已记一笔，表单已清空 —— 选下一个视频继续');
    } catch (err) {
      toast(`记录失败：${err.message}`, true);
    }
  }

  /* ------------------------- 已选视频的预览 ------------------------- */

  /**
   * 选完视频只显示一个文件名，认不出是哪条 —— 填表前想确认一下都没辙。
   * 这里记一下「当前选中那条的可播放地址」：从作品里挑的时候我们有 URL；
   * 从系统文件框选的就只有本地绝对路径，网页这边播不了（也不能让后端去读任意路径），
   * 那就如实说明，别给个坏掉的播放器。
   */
  let pickedVideoUrl = '';
  let pickedVideoNote = '';

  function setPicked(url, note = '') {
    pickedVideoUrl = url || '';
    pickedVideoNote = note || '';
    renderPicked();
  }

  function renderPicked() {
    const box = d$('#pubPicked');
    if (!box) return;
    const p = pickedPath();
    if (!p) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    box.hidden = false;
    box.textContent = '';

    if (pickedVideoUrl) {
      const v = document.createElement('video');
      v.src = pickedVideoUrl;
      v.controls = true;
      v.preload = 'metadata';
      v.playsInline = true;
      box.appendChild(v);
    } else {
      const ph = document.createElement('div');
      ph.className = 'pub-picked-no';
      ph.textContent = '本地文件\n无法预览';
      ph.style.whiteSpace = 'pre-line';
      box.appendChild(ph);
    }

    const info = document.createElement('div');
    info.className = 'pub-picked-info';
    if (pickedVideoNote) {
      const n = document.createElement('span');
      n.className = 'hint';
      n.textContent = pickedVideoNote;
      info.appendChild(n);
    }
    const pth = document.createElement('span');
    pth.className = 'pub-picked-path';
    pth.textContent = p;
    info.appendChild(pth);
    box.appendChild(info);
  }

  /* ---------------------- 从「我的作品」里挑视频 ---------------------- */

  /**
   * 发布页原来只有「选择本地视频…」——弹系统文件框，靠手动翻文件夹。
   * 而程序自己刚生成的视频（视频生成模块的成品、自用模块的画布结果）明明就在库里，
   * 却选不到。这个挑选用它补上：列出来点一下就选中。
   *
   * 顺带把两个状态标出来，省得点开才发现：已提升画质 / 已发布过。
   */
  let workList = [];
  let workHidden = 0; // 文件已被删除、没列出来的条数（任务记录还在，片子没了）

  async function openWorkPicker() {
    const modal = d$('#modalPubWork');
    const list = d$('#pubWorkList');
    if (!modal || !list) return;
    list.textContent = '正在读取…';
    modal.hidden = false;
    try {
      const out = await api('/api/publish/works', { timeoutMs: 20000 });
      workList = out?.works || [];
      workHidden = Number(out?.hidden) || 0;
    } catch (err) {
      list.textContent = `读取失败：${err.message}`;
      return;
    }
    renderWorkPicker();
  }

  function renderWorkPicker() {
    const list = d$('#pubWorkList');
    const foot = d$('#pubWorkFoot');
    if (!list) return;
    list.textContent = '';
    if (foot) {
      const parts = [];
      if (workList.length) parts.push(`共 ${workList.length} 条`);
      if (workHidden) parts.push(`另有 ${workHidden} 条视频文件已删除，未列出`);
      foot.textContent = parts.join(' · ');
    }
    if (!workList.length) {
      const empty = document.createElement('p');
      empty.className = 'pub-work-empty';
      empty.textContent =
        '还没有可以发布的视频。去「视频生成」模块做一条，或者到「自用」模块跑一次画布，这里就会出现。';
      list.appendChild(empty);
      return;
    }

    for (const w of workList) {
      const row = document.createElement('div');
      row.className = 'pub-work-row';

      // 缩略图：拿第一帧当封面。光看文件名根本认不出是哪条片子，必须给画面。
      const thumb = document.createElement('span');
      thumb.className = 'pub-work-thumb';
      let vid = null;
      if (w.url) {
        vid = document.createElement('video');
        // #t=0.5 让它去取第 0.5 秒那一帧当封面（0 秒常是黑场）
        vid.src = `${w.url}#t=0.5`;
        vid.muted = true;
        vid.preload = 'metadata';
        vid.playsInline = true;
        thumb.appendChild(vid);
        // 鼠标移上去就播（静音），移开暂停回到封面 —— 光看一帧有时还是分不清
        row.addEventListener('mouseenter', () => {
          vid.play().catch(() => {});
        });
        row.addEventListener('mouseleave', () => {
          try {
            vid.pause();
            vid.currentTime = 0.5;
          } catch {
            /* 还没加载好就算了 */
          }
        });
      } else {
        const ph = document.createElement('span');
        ph.className = 'pub-work-thumb-ph';
        ph.textContent = '无预览';
        thumb.appendChild(ph);
      }
      row.appendChild(thumb);

      const main = document.createElement('div');
      main.className = 'pub-work-main';
      const nm = document.createElement('span');
      nm.className = 'pub-work-name';
      nm.textContent = w.name || w.path;
      nm.title = w.path;
      main.appendChild(nm);
      const meta = document.createElement('span');
      meta.className = 'pub-work-meta';
      meta.textContent = [
        w.origin,
        w.size ? fmtMB(w.size) : '',
        w.createdAt ? new Date(w.createdAt).toLocaleString('zh-CN', { hour12: false }) : '',
        w.desc || '',
      ]
        .filter(Boolean)
        .join(' · ');
      main.appendChild(meta);
      row.appendChild(main);

      if (w.enhanced) {
        const t = document.createElement('span');
        t.className = 'pub-work-tag is-enhanced';
        t.textContent = `已提升${w.enhancedLabel ? ` · ${w.enhancedLabel}` : ''}`;
        row.appendChild(t);
      }
      if (w.published) {
        const t = document.createElement('span');
        t.className = 'pub-work-tag is-published';
        t.textContent = '已发布过';
        row.appendChild(t);
      }
      // 文件已被删除的条目不在这里出现（服务端已经滤掉，只送还在的文件）

      row.addEventListener('click', () => {
        const inp = d$('#pubLocalPath');
        if (inp) inp.value = w.path;
        const lbl = d$('#pubLocalName');
        if (lbl) lbl.textContent = w.name || w.path.split(/[\\/]/).pop();
        // 记下可播放地址，选中后能直接预览确认
        setPicked(w.url || '', `${w.origin}${w.enhanced ? ' · 已提升画质' : ''}`);
        // 顺手把文件名填进话题搜索框（跟本地选视频时一致）
        const kw = d$('#pubSearchKeyword');
        if (kw && !kw.value.trim()) {
          kw.value = String(w.name || '')
            .replace(/\.[^.]+$/, '')
            .replace(/[_\-.]+/g, ' ')
            .trim();
        }
        const modal = d$('#modalPubWork');
        if (modal) modal.hidden = true;
        logLine(true, `已选中作品：${w.name}`);
        pendingBlocked = null;
        // 选完立刻查重（发过的话贴红条）
        checkDuplicate();
      });

      list.appendChild(row);
    }
  }

  /* ------------------------ 删已发布视频的本地文件 ------------------------ */

  /**
   * 用户要求：能把「发布过的视频」一键从本地清掉，腾磁盘空间。
   *
   * 这是**删用户成品**的操作，所以刻意做得啰嗦：
   *   ① 先跟后端要清单：能删的 / 被别处用着不能删的 / 文件已经不在了的，分三组摊开
   *   ② 弹窗把三组都列出来，让用户看清到底删什么、跳过什么
   *   ③ 确认后交给 **Electron 主进程移进系统回收站**（不是永久删）——
   *      删错了能从回收站捡回来；真要腾空间，清空回收站也就一下
   *   ④ 删完只给记录打一个 deletedAt 时间戳，**记录本身保留**：
   *      以后还要靠它认出「这条发过了」，不能因为文件删了就忘掉
   */
  let cleanPlan = null; // 当前弹窗里这份清单

  const fmtMB = (bytes) => {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  };

  /** 拉清单 + 渲染弹窗 */
  async function openCleanDialog() {
    const modal = d$('#modalPubClean');
    const body = d$('#pubCleanBody');
    if (!modal || !body) return;
    body.textContent = '正在统计…';
    modal.hidden = false;
    let plan = null;
    try {
      plan = await api('/api/publish/deletable', { method: 'POST', body: {}, timeoutMs: 20000 });
    } catch (err) {
      body.textContent = `统计失败：${err.message}`;
      return;
    }
    cleanPlan = plan;
    renderClean(plan);
  }

  function renderClean(plan) {
    const body = d$('#pubCleanBody');
    const foot = d$('#pubCleanFoot');
    const okBtn = d$('#pubCleanOk');
    if (!body) return;
    body.textContent = '';

    if (!plan.totalCount) {
      const p = document.createElement('p');
      p.className = 'pub-clean-note';
      p.textContent = '还没有「已发布」的记录，没有可清理的文件。发完视频点「✓ 已发布，记一笔」就会记进来。';
      body.appendChild(p);
      if (okBtn) okBtn.disabled = true;
      if (foot) foot.textContent = '';
      return;
    }

    /** 一组：标题 + 若干行 */
    const section = (title, rows, hint) => {
      if (!rows.length) return;
      const sec = document.createElement('div');
      sec.className = 'pub-clean-sec';
      const h = document.createElement('h4');
      h.textContent = `${title}（${rows.length}）`;
      sec.appendChild(h);
      if (hint) {
        const hp = document.createElement('p');
        hp.className = 'pub-clean-note';
        hp.textContent = hint;
        sec.appendChild(hp);
      }
      const list = document.createElement('div');
      list.className = 'pub-clean-list';
      for (const it of rows) {
        const row = document.createElement('div');
        row.className = 'pub-clean-row';
        const nm = document.createElement('span');
        nm.className = 'nm';
        nm.textContent = it.name;
        nm.title = it.path;
        row.appendChild(nm);
        const sz = document.createElement('span');
        sz.className = 'sz';
        sz.textContent = it.size ? fmtMB(it.size) : '';
        row.appendChild(sz);
        if (it.why) {
          const why = document.createElement('span');
          why.className = 'why';
          why.textContent = it.why;
          row.appendChild(why);
        }
        list.appendChild(row);
      }
      sec.appendChild(list);
      body.appendChild(sec);
    };

    section(
      '会移入回收站',
      (plan.deletable || []).map((x) => ({ ...x })),
      '这些是「已发布」且没有被画布 / 视频模块占用的视频，可以安全清掉。',
    );
    section(
      '跳过：还被别的模块用着',
      (plan.inUse || []).map((x) => ({ ...x, why: `被引用 ${x.refs} 处` })),
      '这些文件同时是画布素材或视频模块的输入 / 结果，删了那边就废了，所以跳过。想删的话先去对应模块把它移除。',
    );
    section(
      '跳过：文件已经不在了',
      (plan.missing || []).map((x) => ({ ...x, why: '找不到' })),
      '记录指向的文件已经被移走或删掉了，没什么可清理的。',
    );

    if (okBtn) okBtn.disabled = !plan.deletableCount;
    if (foot) {
      foot.textContent = plan.deletableCount
        ? `可清理 ${plan.deletableCount} 个，共 ${fmtMB(plan.deletableBytes)}${
            plan.inUseCount ? `；另有 ${plan.inUseCount} 个被占用会跳过` : ''
          }`
        : '没有可清理的文件';
    }
  }

  /** 确认后：主进程移进回收站 → 给记录打时间戳 → 刷新列表 */
  async function runClean() {
    const plan = cleanPlan;
    if (!plan?.deletableCount) return;
    const b = bridge();
    if (!b?.publish?.trashFiles) {
      toast('这个功能只在桌面版可用', true);
      return;
    }
    const okBtn = d$('#pubCleanOk');
    if (okBtn) {
      okBtn.disabled = true;
      okBtn.textContent = '正在移入回收站…';
    }
    try {
      const out = await b.publish.trashFiles(plan.deletable.map((x) => x.path));
      const done = out?.trashed || 0;
      const failed = out?.failed || 0;
      // 成功的那些才去给记录打时间戳
      const okIds = plan.deletable
        .filter((x) => (out?.results || []).some((r) => r.ok && r.path === x.path))
        .map((x) => x.id);
      if (okIds.length) {
        try {
          const res = await api('/api/publish/mark-deleted', {
            method: 'POST',
            body: { ids: okIds },
            timeoutMs: 15000,
          });
          logState.records = res?.records || logState.records;
        } catch { /* 记不上不影响文件已经删掉的事实 */ }
      }
      renderHistory();
      const modal = d$('#modalPubClean');
      if (modal) modal.hidden = true;
      const sizeText = fmtMB(out?.bytes || 0);
      if (failed) {
        toast(`已移入回收站 ${done} 个（${sizeText}），${failed} 个失败：看日志`, true);
        logLine(false, `清理发布视频：成功 ${done} 个，失败 ${failed} 个`);
      } else {
        toast(`已把 ${done} 个视频（${sizeText}）移入回收站，可随时清空回收站腾空间`);
        logLine(true, `清理发布视频：${done} 个，共 ${sizeText}，已移入回收站`);
      }
      for (const r of out?.results || []) {
        if (!r.ok && r.reason) logLine(false, `跳过/失败：${r.name} —— ${r.reason}`);
      }
    } catch (err) {
      toast(`清理失败：${err.message}`, true);
      logLine(false, `清理发布视频失败：${err.message}`);
    } finally {
      if (okBtn) {
        okBtn.disabled = false;
        okBtn.textContent = '移入回收站';
      }
    }
  }

  /* ------------------------------ 发布记录列表 ------------------------------ */

  function renderHistory() {
    const box = d$('#pubHistList');
    const sum = d$('#pubHistSummary');
    if (!box) return;
    const list = logState.records || [];
    const published = list.filter((r) => r.status === 'published').length;
    if (sum) {
      sum.textContent = list.length
        ? `共 ${list.length} 条，已发布 ${published} 条`
        : '还没有记录：发完点「✓ 已发布，记一笔」，以后就不会重复发同一条';
    }
    box.textContent = '';
    if (!list.length) {
      const empty = document.createElement('p');
      empty.className = 'pub-hist-empty';
      empty.textContent = '（空）';
      box.appendChild(empty);
      return;
    }
    for (const rec of list.slice(0, 50)) {
      const item = document.createElement('div');
      item.className = `pub-hist-item ${rec.status === 'published' ? 'is-published' : ''}`;

      const name = document.createElement('span');
      name.className = 'pub-hist-name';
      name.textContent = rec.name || rec.path;
      name.title = rec.path;
      item.appendChild(name);

      if (rec.fileExists === false) {
        const gone = document.createElement('span');
        gone.className = 'pub-hist-tag is-gone';
        gone.textContent = '文件已不在';
        item.appendChild(gone);
      }

      const tag = document.createElement('span');
      tag.className = `pub-hist-tag ${rec.status === 'published' ? 'is-published' : 'is-filled'}`;
      tag.textContent = rec.status === 'published' ? '已发布' : '填过表未确认';
      item.appendChild(tag);

      const when = rec.publishedAt || rec.filledAt || rec.createdAt;
      const time = document.createElement('span');
      time.className = 'pub-hist-time';
      time.textContent = when
        ? new Date(when).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : '';
      item.appendChild(time);

      // 「重发这条」：明确地把这条视频填回表单（用户主动要重发，不算误操作）
      if (rec.fileExists !== false) {
        const again = document.createElement('button');
        again.className = 'btn tiny';
        again.type = 'button';
        again.textContent = '重发这条';
        again.title = '把这条视频填回表单（你主动要重发）';
        again.addEventListener('click', () => {
          const inp = d$('#pubLocalPath');
          if (inp) inp.value = rec.path;
          const nm = d$('#pubLocalName');
          if (nm) nm.textContent = rec.name || rec.path.split(/[\\/]/).pop();
          // 发布记录里存的是本地路径，没有可播地址；若这条正好是「我的作品」里的，
          // 顺手把地址找出来，能预览就预览
          const hit = workList.find((w) => w.path === rec.path);
          setPicked(hit?.url || '', '从发布记录填回');
          toast('已把这条填回表单，记得确认是要重发');
          checkDuplicate();
        });
        item.appendChild(again);
      }

      // 已发布的可以撤销（误点了 / 其实没发成功）
      if (rec.status === 'published') {
        const undo = document.createElement('button');
        undo.className = 'btn tiny';
        undo.type = 'button';
        undo.textContent = '撤销';
        undo.title = '标记错了：改回「填过表未确认」';
        undo.addEventListener('click', async () => {
          try {
            const out = await api(`/api/publish/records/${rec.id}/unpublish`, { method: 'POST', timeoutMs: 10000 });
            logState.records = out?.records || logState.records;
            renderHistory();
            checkDuplicate();
          } catch (err) {
            toast(err.message, true);
          }
        });
        item.appendChild(undo);
      }

      const del = document.createElement('button');
      del.className = 'btn tiny';
      del.type = 'button';
      del.textContent = '删';
      del.title = '删掉这条记录（删了就不再认它发过）';
      del.addEventListener('click', async () => {
        try {
          const out = await api(`/api/publish/records/${rec.id}`, { method: 'DELETE', timeoutMs: 10000 });
          logState.records = out?.records || logState.records;
          renderHistory();
          checkDuplicate();
        } catch (err) {
          toast(err.message, true);
        }
      });
      item.appendChild(del);

      box.appendChild(item);
    }
  }

  /* -------------------------------- 主流程编排 -------------------------------- */

  function setBusy(busy) {
    for (const id of ['#pubGo', '#pubDryRun', '#pubRetry', '#pubDetach']) {
      const btn = d$(id);
      if (btn) btn.disabled = busy && btn.id !== 'pubDetach';
    }
  }

  /** 把当前页面状态同步给主进程，让它决定从哪一步接着来（重试不重复上传） */
  async function syncStatus() {
    const b = bridge();
    if (!b?.publish?.status || !guestId()) return null;
    return b.publish.status(guestId()).catch(() => null);
  }

  async function run({ dryRun = false, retry = false } = {}) {
    const b = bridge();
    if (!b?.publish) {
      toast('这个功能只在桌面版可用', true);
      return;
    }
    const videoPath = currentVideoPath();
    if (!videoPath) {
      toast('先选一个要发布的视频', true);
      return;
    }
    const form = readForm();
    if (!form) return;
    if (!guestId()) {
      toast('右边的创作者页面还没加载好，稍等再点', true);
      return;
    }
    // 防重发第二道：真要点上传了再查一次。发过就先问一句，
    // 用户没明确点过「仍然继续发布」的话，这一步直接拦下。
    if (!dryRun) {
      const dup = await checkDuplicate({ silent: true });
      if (dup?.blocked && !pendingBlocked) {
        showDup(dup);
        const when = dup.record?.publishedAt
          ? new Date(dup.record.publishedAt).toLocaleString('zh-CN', { hour12: false })
          : '';
        setStatus('这条视频已经发布过了，已拦下：确认要重发就点红条上的「仍然继续发布」');
        toast(`这条视频${when ? ` ${when} ` : ''}已经发布过了。真要重发，点上面红条里的「仍然继续发布」`, true);
        logLine(false, `拦下重复发布：${videoPath.split(/[\\/]/).pop()} 已发布过`);
        return;
      }
    }
    try {
      localStorage.setItem(DES_KEY, form.desc);
    } catch { /* 记不住就算了 */ }

    setBusy(true);
    setStatus(dryRun ? '演练中：只填表，不会碰发布…' : '自动上传并填写中…');
    logLine(null, dryRun ? '—— 开始演练（只填不发）——' : '—— 开始自动上传并填写 ——');
    try {
      // 重试时先问页面到哪一步了，已经上传过就把 setFile 省掉
      let state = null;
      if (retry) state = await syncStatus();

      if (!state?.uploaded) {
        setStatus('正在把视频塞进上传框…');
        const up = await b.publish.setFile(guestId(), videoPath);
        if (!up?.ok) throw new Error(up?.error || '上传失败');
      } else {
        logLine(true, '页面里已有上传好的视频，跳过重新上传');
      }

      setStatus('正在填写描述 / 话题 / 封面 / 定时…');
      const fill = await b.publish.fill(guestId(), { ...form, dryRun });
      if (!fill?.ok) throw new Error(fill?.error || '填写失败');

      // 记一次「用过」：这些话题在素材话题库里会排到前面，下次更好挑
      markLibUsed([...form.tags, ...(form.topicNames || [])]).catch(() => {});

      if (dryRun) {
        setStatus('演练完成：表单已按真实流程填了一遍，未碰「发布」');
        toast('演练完成，去右边页面看看填得对不对');
      } else {
        // 记一笔「已填表」：这样即便这次没发成、或程序被关掉，
        // 下次再选到同一条视频时也能认出「这条刚填过」。
        awaitingPublish = true;
        pendingBlocked = null;
        try {
          const out = await api('/api/publish/filled', {
            method: 'POST',
            body: {
              path: videoPath,
              name: videoPath.split(/[\\/]/).pop(),
              desc: form.desc,
              topics: [...form.tags, ...(form.topicNames || [])],
            },
            timeoutMs: 10000,
          });
          logState.records = out?.records || logState.records;
          renderHistory();
        } catch { /* 记不上不影响发布本身 */ }

        setStatus('已填好：请在右边页面检查一遍，确认后自己点「发布」；点完回来按「✓ 已发布，记一笔」');
        toast('已自动填好，最后一步请在右边页面自己点「发布」；发完回来点「✓ 已发布，记一笔」');
        logLine(null, '提示：发布完请点「✓ 已发布，记一笔」，否则下一条可能又发成它');
      }
    } catch (err) {
      setStatus(`中断了：${err.message}；可在页面手动完成后点「重试」`);
      logLine(false, err.message);
      toast(err.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function detach() {
    const b = bridge();
    if (!b?.publish?.detach || !guestId()) return;
    const out = await b.publish.detach(guestId()).catch(() => null);
    logLine(null, out?.ok ? '已脱离自动模式' : out?.error || '脱离失败');
  }

  /* ------------------------------ 素材话题库 ------------------------------ */

  /**
   * 素材话题库：从「素材 → 抖音采集」加视频时自动攒下来的话题。
   * 这是用户最初那个问题的解法 —— 抖音页面上明明有话题，加进画布就没了，
   * 等生成完自己的视频要发布时，那些话题早找不到了。
   */
  const libState = { topics: [], loaded: false };

  /** 拉一份话题库（后端每次都回全量，按「用过的优先」排好序） */
  async function loadLib() {
    const util = window.RHUtil || {};
    try {
      const out = await util.api('/api/topics', { timeoutMs: 15000 });
      libState.topics = out?.topics || [];
      libState.loaded = true;
    } catch (err) {
      libState.topics = [];
      libState.loaded = true;
      toast(`话题库读取失败：${err.message}`, true);
    }
    return libState.topics;
  }

  /** 渲染话题库列表：每个话题一个可点的芯片 */
  function renderLib(keyword = '') {
    const box = d$('#pubLibList');
    if (!box) return;
    const kw = String(keyword || '').replace(/^#+/, '').trim();
    const list = kw ? libState.topics.filter((t) => t.name.includes(kw)) : libState.topics;
    box.textContent = '';

    if (!libState.topics.length) {
      const empty = document.createElement('p');
      empty.className = 'hint pub-lib-empty';
      empty.textContent =
        '话题库还是空的。去「素材」模块选到「抖音采集」分类，在抖音页里给想要的那条视频点「＋ 当前视频加画布」——它带的话题会自动收进来。';
      box.appendChild(empty);
      setLibCount(0, 0);
      return;
    }
    if (!list.length) {
      const empty = document.createElement('p');
      empty.className = 'hint pub-lib-empty';
      empty.textContent = `没有含「${kw}」的话题`;
      box.appendChild(empty);
      setLibCount(0, libState.topics.length);
      return;
    }

    for (const topic of list) {
      const chip = document.createElement('label');
      chip.className = 'pub-topic-chip pub-lib-chip';
      // 用过的标一下，方便优先挑验证过的
      if (topic.usedCount > 0) chip.classList.add('is-used');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = topic.name;
      const span = document.createElement('span');
      span.textContent = `#${topic.name}`;
      if (topic.usedCount > 0) span.title = `用过 ${topic.usedCount} 次`;
      if (topic.sourceDesc) chip.title = `来自：${topic.sourceDesc}`;
      chip.append(input, span);

      // 单个删除：库里的废话题要能清掉
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'pub-lib-del';
      del.textContent = '×';
      del.title = '从话题库删除';
      del.addEventListener('click', async (event) => {
        event.preventDefault();
        event.stopPropagation();
        try {
          const util = window.RHUtil || {};
          const out = await util.api(`/api/topics/${topic.id}`, { method: 'DELETE' });
          libState.topics = out?.topics || libState.topics.filter((t) => t.id !== topic.id);
          renderLib(d$('#pubLibSearch')?.value || '');
        } catch (err) {
          toast(err.message, true);
        }
      });
      chip.appendChild(del);
      box.appendChild(chip);
    }
    setLibCount(box.querySelectorAll('input:checked').length, libState.topics.length);
  }

  function setLibCount(picked, total) {
    const el = d$('#pubLibCount');
    if (!el) return;
    el.textContent = total ? `已勾 ${picked} / 共 ${total} 个话题` : '';
  }

  /** 打开/收起素材话题库 */
  async function toggleLib() {
    const box = d$('#pubLibBox');
    if (!box) return;
    if (!box.hidden) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    if (!libState.loaded) {
      const list = d$('#pubLibList');
      if (list) list.textContent = '正在读取话题库…';
      await loadLib();
    }
    renderLib(d$('#pubLibSearch')?.value || '');
  }

  /** 把勾选的话题填进「话题芯片」那一排（和搜相似视频、抓推荐话题共用同一套渲染） */
  function applyLib() {
    const box = d$('#pubLibList');
    if (!box) return;
    const picked = [...box.querySelectorAll('input[type="checkbox"]:checked')].map((i) => i.value);
    if (!picked.length) {
      toast('先勾几个话题', true);
      return;
    }
    // 合并进现有芯片：已经有的不重复加
    const cur = Array.isArray(readChipTopics()) ? readChipTopics() : [];
    const merged = [...cur];
    for (const name of picked) if (!merged.includes(name)) merged.push(name);
    renderTopicChips(merged.slice(0, 10));
    d$('#pubLibBox').hidden = true;
    logLine(true, `从素材话题库填入 ${picked.length} 个话题`);
    setStatus(`已填入 ${merged.length} 个话题：核对后点「自动上传并填写」`);
    toast(`已填入 ${picked.length} 个话题`);
  }

  /** 读当前已渲染的话题芯片（用于合并，不覆盖用户之前挑的） */
  function readChipTopics() {
    const box = d$('#pubTopicChips');
    if (!box || box.hidden) return [];
    return [...box.querySelectorAll('input[type="checkbox"]')].map((i) => i.value);
  }

  /** 发布成功后把用过的话题记一次，让它们排到库前面 */
  async function markLibUsed(names) {
    if (!names?.length) return;
    try {
      const util = window.RHUtil || {};
      const out = await util.api('/api/topics/used', { method: 'POST', body: { names } });
      if (out?.topics) libState.topics = out.topics;
    } catch {
      /* 记账失败不影响发布 */
    }
  }

  /* -------------------------------- 事件绑定 -------------------------------- */

  function bind() {
    d$('#pubPickFile')?.addEventListener('click', pickLocalVideo);
    d$('#pubPickCover')?.addEventListener('click', pickCoverImage);
    d$('#pubScrapeTopics')?.addEventListener('click', scrapeTopics);
    d$('#pubSearchSimilar')?.addEventListener('click', searchSimilar);
    d$('#pubSimApply')?.addEventListener('click', applySimPicked);
    // 素材话题库
    d$('#pubPickLibTopics')?.addEventListener('click', () => toggleLib());
    d$('#pubLibSearch')?.addEventListener('input', (event) => renderLib(event.target.value));
    d$('#pubLibApply')?.addEventListener('click', applyLib);
    d$('#pubLibAll')?.addEventListener('click', () => {
      for (const i of d$('#pubLibList')?.querySelectorAll('input[type="checkbox"]') || []) i.checked = true;
      setLibCount(d$('#pubLibList')?.querySelectorAll('input:checked').length || 0, libState.topics.length);
    });
    d$('#pubLibNone')?.addEventListener('click', () => {
      for (const i of d$('#pubLibList')?.querySelectorAll('input[type="checkbox"]') || []) i.checked = false;
      setLibCount(0, libState.topics.length);
    });
    d$('#pubLibClear')?.addEventListener('click', async () => {
      const util = window.RHUtil || {};
      const n = libState.topics.length;
      if (!n) return toast('话题库本来就是空的');
      const ask = util.confirmDialog;
      const yes = ask
        ? await ask(`清空整个话题库（${n} 个）？已经发布出去的视频不受影响。`, { title: '清空话题库', okText: '清空' })
        : true;
      if (!yes) return;
      try {
        await util.api('/api/topics', { method: 'DELETE' });
        libState.topics = [];
        renderLib();
        toast('话题库已清空');
      } catch (err) {
        toast(err.message, true);
      }
    });
    d$('#pubGo')?.addEventListener('click', () => run());
    d$('#pubDryRun')?.addEventListener('click', () => run({ dryRun: true }));
    d$('#pubRetry')?.addEventListener('click', () => run({ retry: true }));
    d$('#pubDetach')?.addEventListener('click', detach);
    // 发布记录 / 防重发
    d$('#pubMarkDone')?.addEventListener('click', () => markPublishedNow());
    d$('#pubHistToggle')?.addEventListener('click', () => {
      const list = d$('#pubHistList');
      const btn = d$('#pubHistToggle');
      if (!list) return;
      list.hidden = !list.hidden;
      if (btn) btn.textContent = list.hidden ? '展开' : '收起';
    });
    // 从「我的作品」里挑（自己生成的视频，不用翻文件夹）
    d$('#pubPickWork')?.addEventListener('click', openWorkPicker);
    d$('#pubWorkCancel')?.addEventListener('click', () => {
      const m = d$('#modalPubWork');
      if (m) m.hidden = true;
    });
    d$('#modalPubWork')?.addEventListener('click', (event) => {
      const m = d$('#modalPubWork');
      if (!m) return;
      if (event.target === m || event.target.closest('[data-close]')) m.hidden = true;
    });
    // 删已发布视频的本地文件（破坏性操作，走独立弹窗确认）
    d$('#pubHistClean')?.addEventListener('click', openCleanDialog);
    d$('#pubCleanCancel')?.addEventListener('click', () => {
      const m = d$('#modalPubClean');
      if (m) m.hidden = true;
    });
    d$('#pubCleanOk')?.addEventListener('click', runClean);
    // 弹窗右上角的 ✕ 和点遮罩关闭，都要跟「取消」一样收尾
    d$('#modalPubClean')?.addEventListener('click', (event) => {
      const m = d$('#modalPubClean');
      if (!m) return;
      if (event.target === m || event.target.closest('[data-close]')) m.hidden = true;
    });
    d$('#pubHistClear')?.addEventListener('click', async () => {
      const n = (logState.records || []).length;
      if (!n) return toast('发布记录本来就是空的');
      const util = window.RHUtil || {};
      const yes = util.confirmDialog
        ? await util.confirmDialog(
            `清空 ${n} 条发布记录？清空后就认不出哪条发过了，重复发布不会再被拦。已经发出去的视频不受影响。`,
            { title: '清空发布记录', okText: '清空' },
          )
        : true;
      if (!yes) return;
      try {
        const out = await api('/api/publish/records', { method: 'DELETE', timeoutMs: 10000 });
        logState.records = out?.records || [];
        renderHistory();
        showDup(null);
        toast('发布记录已清空');
      } catch (err) {
        toast(err.message, true);
      }
    });
    d$('#pubManualCopy')?.addEventListener('click', async () => {
      const form = readForm();
      if (!form) return;
      const text = `${form.desc} ${form.tags.map((t) => `#${t}`).join(' ')}`.trim();
      const ok = await (window.rhDesktop?.douyin?.copy
        ? window.rhDesktop.douyin.copy(text).then(() => true).catch(() => false)
        : false);
      if (!ok) {
        // 网页版兜底
        try {
          await navigator.clipboard.writeText(text);
        } catch { /* 尽力了 */ }
      }
      toast('「文案 + 话题」已复制，去右边页面粘贴');
    });
  }

  window.RHPublish = {
    onShow() {
      bindLog();
      ensureWeb();
      // 发布记录：进模块就拉一次，防重发要靠它
      loadRecords().catch(() => {});
      // ⚠ 防重发的关键一步：表单里可能还留着上次那条视频（切走再回来 DOM 不会重置），
      // 一进来就查一次，发过的话立刻贴红条 —— 这样用户不可能「顺手」重发。
      if (pickedPath()) {
        renderPicked();
        checkDuplicate()
          .then((info) => {
            if (info?.blocked) {
              setStatus('注意：当前选中的这条视频已经发布过了。要发新的请点「选择本地视频…」重选');
            }
          })
          .catch(() => {});
      }
      // 话题库预加载：进这个模块就拉一次，点「从素材话题挑」时不用等
      if (!libState.loaded) loadLib().then(() => {
        // 库里有话题就顺手提示一下，省得用户不知道有这个功能
        if (libState.topics.length && d$('#pubTopicHint')) {
          d$('#pubTopicHint').textContent =
            `素材话题库里有 ${libState.topics.length} 个话题（点「从素材话题挑」选），也可以点「搜相似视频出话题」去抖音搜同类作品`;
        }
      }).catch(() => {});
      // 上次的描述带回来，少打一遍字
      try {
        const saved = localStorage.getItem(DES_KEY);
        if (saved && d$('#pubDesc') && !d$('#pubDesc').value) d$('#pubDesc').value = saved;
      } catch { /* 忽略 */ }
    },
  };

  bind();
})();
