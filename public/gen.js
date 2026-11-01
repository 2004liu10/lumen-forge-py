/**
 * 图片生成（大模块）—— 内置浏览器去各生图平台薅免费额度。
 *
 * 背景：用户放弃了「调 API 生图」作为主路径，改成在内嵌网页里用各平台的免费额度出图，
 * 额度用完就用一次性邮箱换号重新登录。所以这个模块里：
 *   · 默认页「网页生图」= 站点书签 + 内嵌 <webview>（每个平台一个持久化分区，登录态互不干扰）
 *   · 二级页「高级 · 模型」= 原有的 API 生图面板（整棵子树搬过来，id 不变，本文件不碰）
 *
 * 抓到图的通道有三条，最终都走 POST /api/image-import 登记成一条 db.imageTasks 成功记录
 * （这样「送入画布 / 送去视频」这些现成能力天然可用）：
 *   ① 主进程拦各分区的 will-download（平台自己下载时）→ 推 gen:grabbed 给这里
 *   ② 「抓图」按钮调平台适配器（Leonardo / Ideogram / Copilot）把最新生成图交出来
 *   ③ 「截当前画面」通用截图兜底（所见即所得）
 *
 * 本文件必须在 app.js 之前加载（defer 顺序）：app.js 启动时 switchModule 里的
 * RHGen?.onShow() 要能立刻取到。取名 RHGen 是为了和 RHMaterial / RHVideo / RHPublish 一致。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const bridge = () => (window.rhDesktop && window.rhDesktop.gen) || null;
  const util = () => window.RHUtil || {};

  /** 主进程给的平台清单（slug / name / url / partition），首次进入时拉一次 */
  let sites = [];
  let active = ''; // 当前平台 slug
  const views = {}; // slug -> <webview>，懒建；同时刻只显示当前这个（外观仍是单窗口）
  let booting = false;
  let bound = false;

  function siteOf(slug) {
    return sites.find((s) => s.slug === slug) || null;
  }

  function setHint(text) {
    const el = $('genSiteHint');
    if (el) el.textContent = text || '';
  }

  function say(message, bad) {
    const fn = util().toast;
    if (typeof fn === 'function') fn(message, Boolean(bad));
    else setHint(message);
  }

  /* ------------------------------ 子 Tab 切换 ------------------------------ */

  function setTab(which) {
    const web = which !== 'advanced';
    const webPane = $('genPaneWeb');
    const advPane = $('genPaneAdvanced');
    if (webPane) webPane.hidden = !web;
    if (advPane) advPane.hidden = web;
    for (const b of document.querySelectorAll('#genTabs .gen-tab')) {
      b.classList.toggle('active', (b.dataset.gtab === 'advanced') === !web);
    }
    if (web) ensureActive();
  }

  /* ------------------------------ 站点书签 ------------------------------ */

  function renderChips() {
    const box = $('genSites');
    if (!box) return;
    box.textContent = '';
    for (const site of sites) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'gen-site' + (site.slug === active ? ' active' : '');
      b.textContent = site.name || site.slug;
      b.title = site.url;
      b.addEventListener('click', () => activate(site.slug));
      box.appendChild(b);
    }
  }

  async function ensureActive() {
    if (!sites.length) return;
    if (!active || !siteOf(active)) active = sites[0].slug;
    await activate(active);
  }

  /** 每次切换 +1；连着点两个平台时，先点的那个可能后加载完，靠它保证最后点的说了算 */
  let switchSeq = 0;
  /** 最近用过的平台，越靠后越新（给 trimViews 判断谁该被回收） */
  const recent = [];

  async function activate(slug) {
    const site = siteOf(slug);
    if (!site) return;
    const seq = ++switchSeq;
    active = slug;
    const at = recent.indexOf(slug);
    if (at >= 0) recent.splice(at, 1);
    recent.push(slug);
    const view = await ensureView(site);
    if (seq !== switchSeq) return; // 期间又点了别的平台，这次的结果作废
    for (const [s, v] of Object.entries(views)) {
      if (v) v.style.display = s === slug ? 'flex' : 'none';
    }
    if (view) view.style.display = 'flex';
    renderChips();
    setHint(site.hint || `当前平台：${site.name} — 登录后即可开始生图；额度用完点「清本站登录态」再换号重新登录`);
    renderTranslateBtn();
    if (trOn[slug]) runTranslate(slug).catch(() => {});
    else stopTrTimer();
  }

  /** 懒建某个平台自己的 <webview>：一个分区只属于它，登录态互不干扰 */
  async function ensureView(site) {
    if (views[site.slug]) return views[site.slug];
    const host = $('genWeb');
    if (!host) return null;
    const view = document.createElement('webview');
    view.className = 'gen-view';
    view.setAttribute('partition', site.partition);
    // 海外站登录常走 OAuth 弹窗：不给 allowpopups，window.open 直接返回 null，就登不上
    view.setAttribute('allowpopups', '');
    try {
      const url = await bridge()?.preloadUrl?.();
      if (url) view.setAttribute('preload', url);
    } catch {
      /* 打包进 asar 后预载不一定加载得到，钩子只是可选增强，缺了也能手动操作 */
    }
    view.setAttribute('src', site.url);
    view.style.display = 'none';
    view.addEventListener('did-start-loading', () => {
      setHint(`正在打开「${site.name}」…  海外站常会先停在 Cloudflare 的「正在进行安全验证」页，正常十秒左右会自己通过`);
    });
    // 把页面标题亮出来：卡在验证页时用户能看见是「请稍候…」而不是一块白屏（看着像没反应）
    view.addEventListener('page-title-updated', (event) => {
      const title = String((event && event.title) || '').trim();
      if (title) setHint(`「${site.name}」：${title}`);
    });
    view.addEventListener('did-fail-load', (event) => {
      if (event && event.errorCode === -3) return; // 用户自己中止，不算失败
      if (event && event.isMainFrame === false) return; // 只是某个子资源挂了，不影响整页
      const code = (event && event.errorCode) ?? '?';
      const desc = (event && event.errorDescription) || '未知原因';
      setHint(`「${site.name}」打不开（${code} ${desc}）：海外站一般需要代理；也可改用「高级 · 模型」页的 API 生图`);
    });
    host.appendChild(view);
    views[site.slug] = view;
    trimViews(site.slug);
    // 页面加载完了：如果这个站开着翻译，补翻一遍（新建的网页要等它加载出来才有内容可翻）
    view.addEventListener('did-finish-load', () => {
      if (trOn[site.slug] && active === site.slug) runTranslate(site.slug).catch(() => {});
    });
    return view;
  }

  /**
   * 同时活着的内嵌页面上限。
   *
   * 每个 <webview> 都是一个独立的渲染进程（各自一份完整 Chromium 开销 + JS 堆），
   * 而切平台只是把 display 设成 none —— 建了就永不销毁。7 个生图平台全点一遍，
   * 就常驻 7 个后台渲染进程，是桌面版内存只涨不落的底座。
   * 这里按「最近用过」保留最多 MAX_LIVE_VIEWS 个，超出的从 DOM 摘掉真正释放掉。
   * 登录态存在 persist 分区里（落盘），下次再打开不用重新登录，只是页面重新加载。
   */
  const MAX_LIVE_VIEWS = 4;

  function trimViews(keepSlug) {
    const entries = Object.entries(views).filter(([, v]) => v);
    if (entries.length <= MAX_LIVE_VIEWS) return;
    // 最近用过的排最后（recent 里越靠后越新），保留最后 MAX_LIVE_VIEWS 个
    const order = recent.filter((slug) => views[slug]);
    const rank = (slug) => {
      const i = order.indexOf(slug);
      return i < 0 ? -1 : i;
    };
    const victims = entries
      .map(([slug]) => slug)
      .filter((slug) => slug !== keepSlug && slug !== active)
      .sort((a, b) => rank(a) - rank(b))
      .slice(0, Math.max(0, entries.length - MAX_LIVE_VIEWS));
    for (const slug of victims) {
      const view = views[slug];
      delete views[slug];
      try {
        view.remove();
      } catch {
        /* 已经不在 DOM 里就算了 */
      }
    }
  }

  /** 当前平台那个 <webview> 的 webContents id：主进程要借它自己的登录态做截图 / 抓图 */
  function guestId() {
    const view = views[active];
    try {
      return view && typeof view.getWebContentsId === 'function' ? view.getWebContentsId() : 0;
    } catch {
      return 0;
    }
  }

  /* --------------------------- 落图 → 登记 / 参考图 --------------------------- */

  const baseName = (p) => String(p || '').split(/[\\/]/).pop() || '';

  /** 把主进程落在临时目录的图转成「参考图」（供高级页做图生图） */
  async function importAsRef(localPath) {
    const name = baseName(localPath) || `gen-shot-${Date.now()}.png`;
    const res = await fetch(`/files/douyin-tmp/${encodeURIComponent(name)}`);
    if (!res.ok) throw new Error(`读不到刚落盘的图（HTTP ${res.status}）`);
    const blob = await res.blob();
    const ref = await util().uploadRefBlob?.(blob, name);
    if (!ref) throw new Error('参考图接口还没就绪，请稍后重试');
    if (!util().addRefs?.([ref], { stay: true })) {
      throw new Error('图片生成的参考图已满，先去「高级 · 模型」页移除几张再试');
    }
    return ref;
  }

  /** 把主进程落好的图登记成一条生成结果（→ 自然带「送入画布 / 送去视频」） */
  async function importAsTask(localPath, extra = {}) {
    const put = util().api;
    if (typeof put !== 'function') throw new Error('接口还没就绪，请稍后重试');
    return put('/api/image-import', {
      method: 'POST',
      body: {
        localFile: localPath,
        platform: extra.platform || active || 'web',
        prompt: extra.prompt || '',
        sourceUrl: extra.sourceUrl || '',
      },
    });
  }

  /* -------------------------------- 按钮 -------------------------------- */

  async function onShotRef() {
    const view = views[active];
    if (!view) return say('内嵌网页还没准备好，稍等一下再试', true);
    setHint('正在截当前画面…');
    try {
      const out = await bridge()?.capture?.(guestId());
      if (!out?.ok) throw new Error(out?.error || '没截到画面');
      await importAsRef(out.localPath);
      say('已把当前画面截图加入「高级 · 模型」页的参考图');
    } catch (err) {
      say(err.message || '截图失败', true);
    } finally {
      if (active) setHint(`当前平台：${siteOf(active)?.name || active}`);
    }
  }

  async function onShotResult() {
    const view = views[active];
    if (!view) return say('内嵌网页还没准备好，稍等一下再试', true);
    setHint('正在截当前画面…');
    try {
      const out = await bridge()?.capture?.(guestId());
      if (!out?.ok) throw new Error(out?.error || '没截到画面');
      await importAsTask(out.localPath);
      util().refresh?.();
      say('已把当前画面登记为一条生成结果，可在下方结果区送入画布 / 送去视频');
    } catch (err) {
      say(err.message || '登记失败', true);
    } finally {
      if (active) setHint(`当前平台：${siteOf(active)?.name || active}`);
    }
  }

  async function onGrab() {
    const view = views[active];
    if (!view) return say('内嵌网页还没准备好，稍等一下再试', true);
    setHint('正在让平台把最新生成图交出来…');
    try {
      const out = await bridge()?.grab?.(guestId(), active);
      if (!out?.ok) throw new Error(out?.error || '没抓到图（也可以让平台自己下载，会自动出现在下方结果区）');
      // 抓到的图由主进程下载并推 gen:grabbed 过来自动登记；这里只给个即时反馈
      say('已让平台开始交图，抓到的图会自动出现在下方结果区');
    } catch (err) {
      say(err.message || '抓图失败', true);
    } finally {
      if (active) setHint(`当前平台：${siteOf(active)?.name || active}`);
    }
  }

  async function onClearSite() {
    const site = siteOf(active);
    if (!site) return;
    const ok = await util().confirmDialog?.(
      `清掉「${site.name}」的登录态（Cookie / 本地存储 / 缓存）？清完会刷新页面，重新登录一个新账号即可刷新免费额度。`,
      { okText: '清掉登录态' },
    );
    if (ok === false) return;
    setHint(`正在清「${site.name}」的登录态…`);
    try {
      const out = await bridge()?.clearSite?.(site.slug);
      if (!out?.ok) throw new Error(out?.error || '清除失败');
      reloadView(site.slug);
      say(`已清掉「${site.name}」的登录态，刷新后重新登录即可换号刷额度`);
    } catch (err) {
      say(err.message || '清除失败', true);
    } finally {
      if (active) setHint(`当前平台：${siteOf(active)?.name || active}`);
    }
  }

  /** 重新加载当前平台：卡在 Cloudflare 验证页 / 白屏时的恢复动作 */
  function onReload() {
    const site = siteOf(active);
    if (!site) return;
    const view = views[site.slug];
    if (!view) return say('内嵌网页还没准备好，稍等一下再试', true);
    setHint(`正在重新加载「${site.name}」…`);
    reloadView(site.slug);
  }

  function reloadView(slug) {
    const view = views[slug];
    try {
      if (view && typeof view.reload === 'function') view.reload();
    } catch {
      /* 页面还没就绪，下次进入会重新加载 */
    }
  }

  /* ------------------------------ 整页翻译（双语） ------------------------------ */

  /**
   * 内嵌的 Ideogram / Leonardo 是英文站，Electron 不像 Chrome 自带网页翻译 —— 这里补上。
   * 双语做法：不动原文，只在其后插一行中文（见 electron/gen-preload.cjs 的 __genApply）。
   * 翻译在桌面端主进程里走 Google 免费接口（net.fetch 走系统代理）。
   */

  const trOn = {}; // slug -> 这个站开着翻译没有
  let trTimer = null; // 轮询：SPA 换页 / 出图后新出的文案自动补翻

  function renderTranslateBtn() {
    const b = $('genTranslate');
    if (!b) return;
    const on = Boolean(trOn[active]);
    b.classList.toggle('primary', on);
    b.textContent = on ? '关闭翻译' : '翻译';
  }

  function stopTrTimer() {
    if (trTimer) {
      clearInterval(trTimer);
      trTimer = null;
    }
  }

  /** 开着翻译时每 1.5 秒问一次页面「有没有新内容」；没有就什么都不做（很便宜） */
  function startTrTimer() {
    stopTrTimer();
    trTimer = setInterval(async () => {
      if (!active || !trOn[active]) return;
      const id = guestId();
      if (id) await bridge()?.translate?.(id, 'pending');
    }, 1500);
  }

  /** 给当前平台打开翻译并翻一遍；成功返回主进程给的 out，失败也把 out 交回去（里面有失败原因） */
  async function runTranslate(slug) {
    const site = siteOf(slug);
    if (!site || active !== slug) return null;
    const id = guestId();
    if (!id) return null;
    const out = await bridge()?.translate?.(id, 'on');
    if (!out?.ok) return out || null;
    setHint(
      out.translated
        ? `「${site.name}」已翻出 ${out.translated} 段中文；往下滚动会自动补翻新露出来的部分`
        : `「${site.name}」这一屏暂时没有可翻的英文`,
    );
    startTrTimer();
    return out;
  }

  async function onTranslate() {
    const site = siteOf(active);
    if (!site || !views[site.slug]) return say('内嵌网页还没准备好，稍等一下再试', true);
    if (trOn[site.slug]) {
      trOn[site.slug] = false;
      stopTrTimer();
      renderTranslateBtn();
      try {
        await bridge()?.translate?.(guestId(), 'off');
      } catch {
        /* 撤不掉也不影响继续用 */
      }
      setHint(`已关掉「${site.name}」的翻译`);
      return;
    }
    trOn[site.slug] = true;
    renderTranslateBtn();
    setHint(`正在把「${site.name}」当前画面里的英文翻成中文…  只翻看得见的，往下滚动会自动补翻新露出来的部分`);
    const out = await runTranslate(site.slug);
    if (out?.ok) return;
    trOn[site.slug] = false;
    renderTranslateBtn();
    say(out?.error ? `没翻成：${out.error}` : '没翻成：多半是页面还没加载好。等页面出来再点一次，或点「重新加载」', true);
  }

  /* --------------------------- 主进程推来的抓图 --------------------------- */

  /** 各分区 will-download 被拦下时，主进程推一条过来 → 直接登记成结果 */
  async function onGrabbed(payload) {
    const out = payload || {};
    if (!out.ok || !out.localPath) {
      setHint(`主进程拦到一次下载但没落盘：${out.error || '原因未知'}`);
      return;
    }
    try {
      await importAsTask(out.localPath, { platform: out.platform || '', sourceUrl: out.sourceUrl || '' });
      util().refresh?.();
      say('平台下载了一张图，已自动登记为生成结果');
    } catch (err) {
      setHint(`下载到的图登记失败：${err.message}`);
    }
  }

  /* ------------------------------ 平台管理 ------------------------------ */

  /**
   * 平台不再是写死的常量，而是库里的一份配置（db.webProviders）。
   * 这里负责增删改：页面地址、选择器、每日额度规则。
   *
   * 选择器为什么不自动猜：这些站全是 SPA，class 名是打包时随机生成的，
   * 自动猜出来的九成是错的，还会让用户以为「配好了」但实际每次都失败。
   * 不如给出几格，让他把页面上真实那个粘进来，一眼就知道填的是什么。
   */
  let wpEditing = ''; // 正在编辑的平台 id；'' = 没在编辑，'new' = 新增
  let wpDraft = null;

  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
  const lines = (arr) => (Array.isArray(arr) ? arr : []).join('\n');

  async function wpLoad() {
    const out = await util().api('/api/web-providers');
    return out?.providers || [];
  }

  function wpRow(p) {
    const open = wpEditing === p.id;
    const q = p.quota || {};
    const a = p.adapter || {};
    return `<div class="wp-row${open ? ' open' : ''}" data-wp-id="${esc(p.id)}">
      <div class="wp-head">
        <span class="wp-name">${esc(p.name)}</span>
        <span class="wp-slug">${esc(p.slug)}</span>
        ${p.enabled === false ? '<span class="wp-off">已停用</span>' : ''}
        ${(a.input?.length || a.result?.length) ? '' : '<span class="wp-off" title="没配选择器，只能手动操作">未配选择器</span>'}
        <span class="spacer"></span>
        <span class="wp-quota">${q.daily ? `每日 ${q.daily} ${esc(q.unit)}` : '额度不限'}</span>
        <button class="btn tiny" type="button" data-wp-act="edit">${open ? '收起' : '编辑'}</button>
        <button class="btn tiny danger" type="button" data-wp-act="del">删除</button>
      </div>
      ${open ? wpForm(p) : ''}
    </div>`;
  }

  function wpForm(p) {
    const q = p.quota || {};
    const a = p.adapter || {};
    const field = (label, key, value, hint, area) => `<label class="wp-field${area ? ' wide' : ''}">
        <span>${label}</span>
        ${area
          ? `<textarea data-wp-key="${key}" rows="3" spellcheck="false" placeholder="一行一个候选">${esc(value)}</textarea>`
          : `<input data-wp-key="${key}" value="${esc(value)}" spellcheck="false" />`}
        <em>${hint}</em>
      </label>`;
    return `<div class="wp-form">
      ${field('名称', 'name', p.name, '界面上显示的名字', false)}
      ${field('页面地址', 'url', p.url, '打开这个平台时加载的网址', false)}
      ${field('登录地址', 'loginUrl', p.loginUrl || '', '留空就用页面地址', false)}
      ${field('Cookie 域名', 'hosts', lines(p.hosts), '一行一个，用于判断登录态和隔离分区', true)}
      ${field('输入框选择器', 'adapter.input', lines(a.input), '提示词输入框。一格填一个候选，从上往下试', true)}
      ${field('提交按钮选择器', 'adapter.submit', lines(a.submit), '触发生成的按钮', true)}
      ${field('按钮文字正则', 'adapter.submitText', a.submitText || '', '按按钮上的文字再筛一遍，如「生成|发送」。留空则只用选择器', false)}
      ${field('结果选择器', 'adapter.result', lines(a.result), '生成出来的图/视频在哪，用于「抓图」', true)}
      ${field('额度单位', 'quota.unit', q.unit || '次', '如「次」「点」「积分」', false)}
      ${field('每日额度', 'quota.daily', q.daily ?? 0, '0 = 不限制', false)}
      ${field('单次消耗', 'quota.cost', q.cost ?? 1, '生成一次扣多少', false)}
      <label class="wp-check"><input type="checkbox" data-wp-key="enabled" ${p.enabled === false ? '' : 'checked'} />启用</label>
      <div class="wp-actions">
        <button class="btn tiny primary" type="button" data-wp-act="save">保存</button>
        <button class="btn tiny" type="button" data-wp-act="cancel">取消</button>
      </div>
    </div>`;
  }

  function wpReadForm(row) {
    const out = {};
    for (const el of row.querySelectorAll('[data-wp-key]')) {
      const key = el.dataset.wpKey;
      const raw = el.type === 'checkbox' ? el.checked : String(el.value || '');
      if (key.startsWith('adapter.') || key.startsWith('quota.')) {
        const [group, field] = key.split('.');
        out[group] = out[group] || {};
        if (field === 'input' || field === 'submit' || field === 'result') {
          out[group][field] = String(raw).split('\n').map((s) => s.trim()).filter(Boolean);
        } else if (field === 'daily' || field === 'cost') {
          out[group][field] = Number(raw) || 0;
        } else {
          out[group][field] = raw;
        }
      } else if (key === 'hosts') {
        out.hosts = String(raw).split('\n').map((s) => s.trim()).filter(Boolean);
      } else {
        out[key] = raw;
      }
    }
    return out;
  }

  async function wpRender() {
    const list = await wpLoad();
    const box = $('wpList');
    if (!box) return;
    box.innerHTML = list.map(wpRow).join('')
      || '<p class="hint">还没有平台，点下面「补回内置平台」把豆包、千问、元宝那几家加回来。</p>';
  }

  async function wpSave(id) {
    const row = document.querySelector(`.wp-row[data-wp-id="${id}"]`);
    const body = row ? wpReadForm(row) : {};
    if (id !== 'new') body.id = id;
    else if (!body.slug) {
      // 新增时没有 slug 输入框：用名称生成一个，用户随后可编辑列表里的标识
      body.slug = String(body.name || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      if (!body.slug) body.slug = `site-${Date.now().toString(36)}`;
    }
    await util().api('/api/web-providers', { method: 'POST', body });
    wpEditing = '';
    await wpRender();
    say(`已保存「${body.name}」`);
  }

  async function wpDel(id, name) {
    if (!window.confirm(`删除平台「${name}」？它对应的登录分区不会被删，重新加回来还能接着用。`)) return;
    await util().api(`/api/web-providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
    wpEditing = '';
    await wpRender();
    say(`已删除「${name}」`);
  }

  function wpBind() {
    $('wpClose')?.addEventListener('click', () => { $('modalWebProviders').hidden = true; });
    $('wpList')?.addEventListener('click', async (event) => {
      const btn = event.target.closest('[data-wp-act]');
      if (!btn) return;
      const row = btn.closest('.wp-row');
      const id = row?.dataset.wpId;
      if (!id) return;
      const act = btn.dataset.wpAct;
      try {
        if (act === 'edit') {
          wpEditing = wpEditing === id ? '' : id;
          await wpRender();
        } else if (act === 'cancel') {
          wpEditing = '';
          await wpRender();
        } else if (act === 'save') {
          btn.disabled = true;
          await wpSave(id);
        } else if (act === 'del') {
          await wpDel(id, row.querySelector('.wp-name')?.textContent || id);
        }
      } catch (err) {
        say(err.message || String(err), true);
      }
    });
    $('wpAdd')?.addEventListener('click', async () => {
      wpEditing = 'new';
      const list = await wpLoad();
      const box = $('wpList');
      const draft = {
        id: 'new', name: '新平台', slug: '', url: 'https://', loginUrl: '',
        hosts: [], enabled: true, adapter: { input: [], submit: [], submitText: '', result: [] },
        quota: { unit: '次', daily: 0, cost: 1 },
      };
      box.insertAdjacentHTML('afterbegin', wpRow(draft));
      box.querySelector('.wp-row[data-wp-id="new"] .wp-form')?.scrollIntoView({ block: 'nearest' });
      void list;
    });
    $('wpReset')?.addEventListener('click', async () => {
      try {
        const out = await util().api('/api/web-providers/reset', { method: 'POST', body: {} });
        await wpRender();
        say(out?.added ? `补回 ${out.added} 个内置平台` : '内置平台都在了，没有新增');
      } catch (err) {
        say(err.message || String(err), true);
      }
    });
  }

  async function openProviderManager() {
    wpBind();
    $('modalWebProviders').hidden = false;
    await wpRender();
  }

  /* -------------------------------- 绑定 -------------------------------- */

  /** 只拉平台清单并渲染书签，不建 webview（真正切进来时才建，省资源） */
  async function loadSites() {
    if (booting || sites.length) return;
    booting = true;
    try {
      if (!bound) {
        bound = true;
        bindButtons();
      }
      const list = await bridge()?.sites?.();
      sites = Array.isArray(list) ? list.filter((s) => s && s.slug) : [];
      if (!sites.length) {
        setHint('网页生图只在桌面版可用：请用桌面程序打开这个模块');
        return;
      }
      renderChips();
    } finally {
      booting = false;
    }
  }

  function bindButtons() {
    $('genTabs')?.addEventListener('click', (event) => {
      const tab = event.target.closest('.gen-tab');
      if (tab) setTab(tab.dataset.gtab);
    });
    $('genClearSite')?.addEventListener('click', onClearSite);
    $('genReload')?.addEventListener('click', onReload);
    $('genTranslate')?.addEventListener('click', onTranslate);
    $('genShotRef')?.addEventListener('click', onShotRef);
    $('genShotResult')?.addEventListener('click', onShotResult);
    $('genGrab')?.addEventListener('click', onGrab);
    $('genManageSites')?.addEventListener('click', () => openProviderManager().catch((err) => say(err.message, true)));
    // 主进程推来的下载抓图：一条条登记
    bridge()?.onGrabbed?.((payload) => onGrabbed(payload));
  }

  /* ------------------------------ 对外接口 ------------------------------ */

  window.RHGen = {
    /** 切到「图片生成」模块时被 app.js 调用 */
    async onShow() {
      if (!$('genPaneWeb')) return;
      await loadSites();
      if (!$('genPaneWeb').hidden) await ensureActive();
    },
    /** 从素材截图 / 生成结果「用作参考图」投送过来时：切到高级页并展开表单 */
    showForm() {
      setTab('advanced');
      $('genPanel')?.classList.remove('collapsed');
      setHint('');
    },
    /**
     * 切走「图片生成」：停掉双语补翻的轮询。
     * 它只在「切到没开翻译的站点」和「手动关翻译」时停过，切模块没管 ——
     * 于是开着翻译切到别的模块后，仍然每 1.5 秒 IPC 去改一遍内嵌页的 DOM。
     * 回来时 onShow → activate() 会按 trOn 自己重新拉起。
     */
    onHide() {
      stopTrTimer();
    },
  };

  // defer 顺序保证 DOM 已就绪；只预热书签，不建 webview
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => loadSites(), { once: true });
  } else {
    loadSites();
  }
})();
