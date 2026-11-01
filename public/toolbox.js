/**
 * AI 工具箱：字幕擦除 / 声音克隆 / 人声提取。
 * 这些能力本质都是外部服务，自建要接各家付费 API；
 * 按「内嵌网页薅免费额度」的路子：点卡片后在右侧内嵌窗口打开对应的免费在线工具。
 * Electron 里用 <webview>（每个工具独立分区，登录态各自保存）；
 * 浏览器版没有 webview 标签，退化为新标签页打开。
 */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const TOOLS = {
    subtitle: {
      name: '字幕擦除',
      sites: [
        { name: '傲软在线去水印', url: 'https://www.apowersoft.cn/online-video-watermark-remover' },
        { name: 'Media.io', url: 'https://www.media.io/video-watermark-remover.html' },
      ],
    },
    'voice-clone': {
      name: '声音克隆',
      sites: [
        { name: 'Fish Audio', url: 'https://fish.audio/' },
        { name: 'AnyVoice', url: 'https://anyvoice.net/' },
      ],
    },
    'voice-extract': {
      name: '人声提取',
      sites: [
        { name: 'Vocal Remover', url: 'https://vocalremover.org/' },
        { name: 'LALAL.AI', url: 'https://www.lalal.ai/' },
      ],
    },
  };

  const views = {}; // tool -> webview（一个工具一个，切走再切回不丢登录态和页面状态）
  const siteIdx = {}; // tool -> 当前站点下标
  let current = '';

  function isDesktop() {
    return !!window.rhDesktop;
  }

  function renderStage(tool) {
    const host = $('#tbStage');
    if (!host) return;
    host.textContent = '';
    const conf = TOOLS[tool];

    const bar = document.createElement('div');
    bar.className = 'tb-bar';
    conf.sites.forEach((site, i) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tb-site' + (i === (siteIdx[tool] || 0) ? ' active' : '');
      btn.textContent = site.name;
      btn.addEventListener('click', () => openSite(tool, i));
      bar.appendChild(btn);
    });
    if (isDesktop()) {
      const reload = document.createElement('button');
      reload.type = 'button';
      reload.className = 'btn tiny';
      reload.textContent = '重新加载';
      reload.title = '内嵌网页卡住或白屏时点它';
      reload.addEventListener('click', () => views[tool]?.reload?.());
      bar.appendChild(reload);
    }
    host.appendChild(bar);

    const viewHost = document.createElement('div');
    viewHost.className = 'tb-view-host';
    host.appendChild(viewHost);
  }

  function openSite(tool, idx) {
    const conf = TOOLS[tool];
    const site = conf?.sites[idx];
    if (!site) return;
    siteIdx[tool] = idx;

    // 浏览器版没有 <webview>：新标签页打开，舞台上留个提示
    if (!isDesktop()) {
      window.open(site.url, '_blank', 'noopener');
      renderStage(tool);
      const viewHost = $('.tb-view-host');
      if (viewHost) {
        viewHost.innerHTML = '';
        const tip = document.createElement('div');
        tip.className = 'tb-empty';
        tip.innerHTML = '<p>已在新标签页打开「' + site.name + '」</p><p class="tb-tip">桌面版里这个工具会直接内嵌在窗口中</p>';
        viewHost.appendChild(tip);
      }
      return;
    }

    renderStage(tool);
    let view = views[tool];
    if (!view) {
      view = document.createElement('webview');
      view.className = 'tb-view';
      view.setAttribute('partition', 'persist:toolbox-' + tool);
      // 登录常走 OAuth 弹窗，不给 allowpopups 会登不上
      view.setAttribute('allowpopups', '');
      views[tool] = view;
    }
    if (view.getAttribute('src') !== site.url) view.setAttribute('src', site.url);
    $('.tb-view-host')?.appendChild(view);
  }

  function selectTool(tool) {
    if (!TOOLS[tool]) return;
    current = tool;
    $$('#tbList .tb-card').forEach((card) => {
      card.classList.toggle('active', card.dataset.tool === tool);
    });
    openSite(tool, siteIdx[tool] || 0);
  }

  $('#tbList')?.addEventListener('click', (event) => {
    const card = event.target.closest('.tb-card');
    if (card?.dataset.tool) selectTool(card.dataset.tool);
  });

  window.RHToolbox = {
    onShow() {
      // 切进来时如果还没选过工具，保持空状态；webview 都在后台活着，不用动
    },
    selectTool,
    get current() { return current; },
  };
})();
