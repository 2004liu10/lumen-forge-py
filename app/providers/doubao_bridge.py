"""本地豆包桥（Doubao desktop bridge）—— 复刻 `server/providers/doubao-bridge.js`。

## 为什么要有这一层

豆包桌面版没有对外 API，但它自己是 Chromium 内核，用 `--remote-debugging-port` 起来之后
可以走 CDP 驱动：开新对话、塞参考图、打字、回车、等图、把图抓成 base64。生成的图再交回
imagegen 的统一管线，走 GEN_DIR 落盘，于是「画布生图节点 / 图片生成模块」一行前端代码都不用改。

**前置条件（用户侧）**：
  1. 豆包已登录；
  2. 豆包是以 `--remote-debugging-port=9555` 启动的。

没开调试端口时 `probe_bridge()` 返回 `{ok: False, reason: 'no-cdp'}`，上层可以提示用户一键重启
（对应 `POST /api/doubao/restart`）。

## 已知取舍（与原版完全一致）
  - 全靠界面选择器（`.ProseMirror` 编辑器、`新对话` 文本按钮、`input[type=file]`），
    豆包改版就可能失效 —— 所以每处选择器都写了多个候选；
  - 出图带「豆包 AI 生成」水印，是图床服务端烧进右下角的（模板 `cgen_lwm3_hflow`）。
    换任何 tplv 参数都会被签名拒掉（403），但它的接口响应里另给了一条 `image_ori_raw`
    （模板 `image_raw_hflow`）—— 同一张图的干净原版，同尺寸不降质。
    现在走「响应钩子记录原图地址 → 直接取原图」，取不到才回退本地抹水印；
  - 一次提示词会出多张，要 count>1 就串行多跑几轮，每轮取分辨率最高的那张。

## 移植说明
原版用 Node 的 `WebSocket` 手写了一个 CDP 小客户端；这里用 `websockets` 的 asyncio 客户端，
`send` / `evaluate` / `wait_event` 的语义与超时行为一一对应（每条命令都带超时，
否则豆包窗口崩掉时那条 Promise 永远不 settle，任务会一直挂在 submitting 上还会堵死队列）。
所有注入页面的脚本都保持**原样 JS 字符串**，不做任何改写 —— 它们是在 Chromium 里跑的。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .imagegen import ImageGenError

log = logging.getLogger(__name__)

#: 豆包默认调试端口，可在模型配置里用 baseUrl 覆盖
DOUBAO_DEFAULT_ENDPOINT = "http://127.0.0.1:9555"

CONNECT_TIMEOUT_MS = 3000
NEW_CHAT_SETTLE_MS = 1500
UPLOAD_SETTLE_MS = 2800
TYPE_SETTLE_MS = 500
POLL_STEP_MS = 2000
#: 单条 CDP 命令的超时。抓图/抓视频那次 evaluate 要编码整张图，给得宽一点
CDP_CALL_TIMEOUT_MS = 90 * 1000
GENERATION_STABLE_MS = 6000
GENERATION_TIMEOUT_MS = 5 * 60 * 1000
RESTART_TIMEOUT_MS = 45 * 1000

VIDEO_TIMEOUT_MS = 15 * 60 * 1000
#: 提交后等它「上车」的窗口：要留出点掉安全确认、以及它反问一句参数再补答「确认」的时间
VIDEO_KICKOFF_MS = 45 * 1000
VIDEO_DOWNLOAD_WAIT_MS = 45 * 1000

VIDEO_TASK_PREFIX = "dbv2."
VIDEO_TASK_PREFIX_V1 = "dbv1."
#: 视频地址前缀：加了它的走本地桥下载，其余按普通 https 处理
DOUBAO_VIDEO_URL_PREFIX = "doubao-bridge:"

GRAB_CHUNK = 256 * 1024


async def sleep(ms: int | float) -> None:
    await asyncio.sleep(ms / 1000)


def now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================ 页面脚本（原样 JS）

#: 页面里找可见的可点击元素（按 innerText 精确匹配）
_CLICK_BY_TEXT = r"""(() => {
  const label = %s;
  const nodes = [...document.querySelectorAll('div[role="button"], button, [role="menuitem"], div, span')];
  const hit = nodes.find((el) => {
    if ((el.innerText || '').trim() !== label) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!hit) return false;
  hit.click();
  return true;
})()"""


def click_by_text_script(text: str) -> str:
    return _CLICK_BY_TEXT % json.dumps(text, ensure_ascii=False)


#: 找提示词编辑器（豆包是 tiptap/ProseMirror，不是 textarea）
EDITOR_SCRIPT = r"""document.querySelector('.ProseMirror[contenteditable="true"]')
  || document.querySelector('div[contenteditable="true"][role="textbox"]')
  || [...document.querySelectorAll('div[contenteditable="true"]')].find((el) => el.getBoundingClientRect().width > 200)"""

#: 当前页面上所有生成图（按豆包 CDN 的 rc_gen_image 特征识别），带分辨率
GEN_IMAGES_SCRIPT = r"""[...document.querySelectorAll('img')]
  .map((i) => ({ src: i.currentSrc || i.src || '', w: i.naturalWidth || 0, h: i.naturalHeight || 0 }))
  .filter((o) => typeof o.src === 'string' && o.src.includes('rc_gen_image'))"""

#: 页面是否还在生成中
BUSY_SCRIPT = r"""(() => {
  if (/停止生成|正在生成|生成中/.test(document.body.innerText || '')) return true;
  return !!document.querySelector('[class*="loading"]:not([hidden]),[class*="generating"]:not([hidden])');
})()"""

#: 视频这条链路的页面状态（只看页面最后一段文字，别拿整页判）
VIDEO_STATE_SCRIPT = r"""(() => {
  const t = String(document.body.innerText || '').replace(/\s+/g, '');
  const tail = t.slice(-600);
  const lastOf = (re) => {
    const g = new RegExp(re.source, 'g');
    let last = -1;
    let m;
    while ((m = g.exec(tail)) !== null) last = m.index + m[0].length;
    return last;
  };
  const busy = lastOf(/预计等待|生成好后|正在生成|生成中|排队中/);
  const quota = lastOf(/额度不足|额度已用完|额度用完|暂无可用额度|没有可用额度|额度已耗尽|今日额度|次数已用完|免费额度/);
  const fail = lastOf(/生成失败|无法生成|不支持生成|内容违规/);
  if (busy >= 0 && busy > quota && busy > fail) return 'busy';
  if (quota >= 0 && quota > fail) return 'quota';
  if (fail >= 0) return 'fail';
  if (busy >= 0) return 'busy';
  return '';
})()"""

#: 数一下页面上出现过几次「额度用完」类的提示
VIDEO_QUOTA_COUNT_SCRIPT = r"""(() => {
  const t = String(document.body.innerText || '').replace(/\s+/g, '');
  const g = /额度不足|额度已用完|额度用完|暂无可用额度|没有可用额度|额度已耗尽|今日额度|次数已用完|免费额度/g;
  let n = 0;
  while (g.exec(t) !== null) n += 1;
  return n;
})()"""

#: 输入区是不是处在「视频生成」技能态
VIDEO_MODE_SCRIPT = (
    r"""(() => {
  const ed = """
    + EDITOR_SCRIPT
    + r""";
  if (!ed) return { ok: false, has: false, why: '页面上没有输入框' };
  const attrOf = (el) => String((el && el.getAttribute && el.getAttribute('data-placeholder')) || '');
  let ph = attrOf(ed);
  if (!ph) ph = attrOf(ed.querySelector('[data-placeholder]'));
  if (!ph && ed.innerHTML) {
    const m = /data-placeholder="([^"]*)"/.exec(ed.innerHTML);
    if (m) ph = m[1];
  }
  const ok = /描述你想要的视频/.test(ph);
  return { ok, has: true, ph: ph.slice(0, 24) };
})()"""
)

#: 把编辑器聚焦好（图片 / 视频两条流程都用）
FOCUS_EDITOR_SCRIPT = (
    r"""(() => {
  const ed = """
    + EDITOR_SCRIPT
    + r""";
  if (!ed) return false;
  ed.focus();
  return true;
})()"""
)

#: 输入框是不是已经空了（提交成功的标志）
EDITOR_EMPTY_SCRIPT = (
    r"""(() => {
  const ed = """
    + EDITOR_SCRIPT
    + r""";
  return !ed || !String(ed.innerText || '').trim();
})()"""
)

#: 点输入框容器里的发送按钮（豆包 2.33 起回车不再提交）
CLICK_SEND_SCRIPT = (
    r"""(() => {
  const ed = """
    + EDITOR_SCRIPT
    + r""";
  if (!ed) return false;
  let box = ed;
  for (let i = 0; i < 6 && box.parentElement; i += 1) {
    box = box.parentElement;
    if (box.querySelectorAll('button').length > 2) break;
  }
  const btn = [...box.querySelectorAll('button')].pop();
  if (!btn) return false;
  btn.click();
  return true;
})()"""
)

#: 「安全确认」弹窗
SAFETY_DIALOG_SCRIPT = r"""(() => {
  const tail = String(document.body.innerText || '').replace(/\s+/g, '').slice(-500);
  if (tail.indexOf('安全确认') === -1) return '';
  const btn = [...document.querySelectorAll('button')].filter((b) => (b.innerText || '').trim() === '确认' && b.getBoundingClientRect().width > 0).pop();
  return btn ? 'open' : 'text';
})()"""

#: 豆包有时先把参数复述一遍问一句「确认后我再开始生成视频」
VIDEO_CONFIRM_ASK_SCRIPT = r"""(() => {
  const tail = String(document.body.innerText || '').replace(/\s+/g, '').slice(-500);
  if (/确认后我再开始|确认后开始|确认后即开始|是否开始生成|要我开始生成|参数确认/.test(tail)) return 'ask';
  return '';
})()"""

#: 左下角账号菜单（靠 testid 而不是文字）
ACCOUNT_MENU_OPEN_SCRIPT = r"""(() => {
  const set = document.querySelector('[data-testid="sidebar_footer_setting_entry"]');
  if (!set) return { ok: false, why: 'no-footer' };
  let box = set;
  for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
  const btn = [...box.querySelectorAll('button')].find((b) => b !== set && !b.contains(set));
  if (!btn) return { ok: false, why: 'no-account-bar' };
  if (btn.getAttribute('aria-expanded') === 'true') return { ok: true, already: true };
  const r = btn.getBoundingClientRect();
  if (!(r.width > 0 && r.height > 0)) return { ok: false, why: 'bar-hidden' };
  return { ok: true, already: false, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
})()"""

#: 读账号菜单里的账号行（只在菜单已打开时有用）
ACCOUNT_MENU_READ_SCRIPT = r"""(() => {
  const menu = document.querySelector('[data-testid="chat_header_menu"]');
  if (!menu) return { open: false };
  const rows = [...menu.querySelectorAll('[data-testid="avatar_menu_account_row"]')].map((m, i) => {
    const r = m.getBoundingClientRect();
    const img = m.querySelector('img');
    return {
      i,
      name: String((m.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 40),
      logged: !!img,
      current: !!m.querySelector('svg'),
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
    };
  });
  const quotaLeaf = [...menu.querySelectorAll('*')].filter((e) => e.children.length === 0 && /额度用量/.test(e.innerText || ''))[0];
  let quota = '';
  if (quotaLeaf && quotaLeaf.parentElement) {
    quota = String((quotaLeaf.parentElement.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 60);
  }
  const add = menu.querySelector('[data-testid="avatar_menu_add_account"]');
  return { open: true, rows, quota, addY: add ? Math.round(add.getBoundingClientRect().y) : -1 };
})()"""

#: 去水印函数（会被 toString() 注入到豆包页面里执行，所以不能引用任何外部变量）
ERASE_FN_NAME = "__ncEraseWatermark"
ERASE_FN_SCRIPT = r"""window.__ncEraseWatermark = function eraseWatermarkInPage(cx, W, H) {
  const R = { w: 0.1563, h: 0.0366, right: 0.021, bottom: 0.0186 };
  const pad = Math.max(6, Math.round(W * 0.004));
  const wmW = Math.round(W * R.w);
  const wmH = Math.round(H * R.h);
  const rw = wmW + pad * 2;
  const rh = wmH + pad * 2;
  const x0 = W - Math.round(W * R.right) - wmW - pad;
  const y0 = H - Math.round(H * R.bottom) - wmH - pad;
  if (x0 < 1 || y0 - rh < 1 || x0 + rw >= W - 1 || y0 + rh >= H - 1) {
    return { ok: false, reason: 'region-out-of-range' };
  }

  const box = cx.getImageData(x0 - 1, y0 - 1, rw + 2, rh + 2);
  const S = rw + 2;
  const bd = box.data;
  const at = (x, y) => (y * S + x) * 4;

  const fill = new Float32Array(rw * rh * 3);
  for (let y = 0; y < rh; y += 1) {
    const wt = 1 / (y + 1);
    const wb = 1 / (rh - y);
    for (let x = 0; x < rw; x += 1) {
      const wl = 1 / (x + 1);
      const wr = 1 / (rw - x);
      const den = wt + wb + wl + wr;
      const t = at(x + 1, 0);
      const b = at(x + 1, rh + 1);
      const l = at(0, y + 1);
      const r = at(rw + 1, y + 1);
      const o = (y * rw + x) * 3;
      for (let c = 0; c < 3; c += 1) {
        fill[o + c] = (bd[t + c] * wt + bd[b + c] * wb + bd[l + c] * wl + bd[r + c] * wr) / den;
      }
    }
  }

  const strip = cx.getImageData(x0, y0 - rh, rw, rh).data;
  const blur = new Float32Array(rw * rh * 3);
  const tmp = new Float32Array(rw * rh * 3);
  const radius = Math.max(3, Math.round(rw * 0.018));
  for (let y = 0; y < rh; y += 1) {
    for (let x = 0; x < rw; x += 1) {
      const o = (y * rw + x) * 3;
      let n = 0;
      for (let c = 0; c < 3; c += 1) tmp[o + c] = 0;
      for (let k = -radius; k <= radius; k += 1) {
        const xx = Math.min(rw - 1, Math.max(0, x + k));
        const p = (y * rw + xx) * 4;
        tmp[o] += strip[p];
        tmp[o + 1] += strip[p + 1];
        tmp[o + 2] += strip[p + 2];
        n += 1;
      }
      for (let c = 0; c < 3; c += 1) tmp[o + c] /= n;
    }
  }
  for (let y = 0; y < rh; y += 1) {
    for (let x = 0; x < rw; x += 1) {
      const o = (y * rw + x) * 3;
      let n = 0;
      for (let c = 0; c < 3; c += 1) blur[o + c] = 0;
      for (let k = -radius; k <= radius; k += 1) {
        const yy = Math.min(rh - 1, Math.max(0, y + k));
        const p = (yy * rw + x) * 3;
        blur[o] += tmp[p];
        blur[o + 1] += tmp[p + 1];
        blur[o + 2] += tmp[p + 2];
        n += 1;
      }
      for (let c = 0; c < 3; c += 1) blur[o + c] /= n;
    }
  }
  for (let y = 0; y < rh; y += 1) {
    const weight = Math.sin((Math.PI * y) / rh) * 0.85;
    for (let x = 0; x < rw; x += 1) {
      const o = (y * rw + x) * 3;
      const p = (y * rw + x) * 4;
      for (let c = 0; c < 3; c += 1) {
        fill[o + c] += (strip[p + c] - blur[o + c]) * weight;
      }
    }
  }

  const dst = cx.createImageData(rw, rh);
  for (let i = 0; i < rw * rh; i += 1) {
    for (let c = 0; c < 3; c += 1) {
      dst.data[i * 4 + c] = Math.max(0, Math.min(255, Math.round(fill[i * 3 + c])));
    }
    dst.data[i * 4 + 3] = 255;
  }
  cx.putImageData(dst, x0, y0);
  return { ok: true, box: [x0, y0, rw, rh] };
}; void 0;"""

#: 无水印原图的钩子（必须在发送之前挂上，出图那波接口响应才会被记下来）
RAW_URLS_KEY = "__ncRawUrls"
RAW_HOOK_SCRIPT = r"""(() => {
  const KEY = '__ncRawUrls';
  if (window.__ncRawHooked) return (window[KEY] || []).length;
  window.__ncRawHooked = true;
  const list = (window[KEY] = window[KEY] || []);
  const RE = /https?:\/\/[^"'\s\\]+image_raw_hflow[^"'\s\\]*/g;
  const push = (text) => {
    if (typeof text !== 'string' || text.indexOf('image_raw_hflow') === -1) return;
    const hits = text.match(RE);
    if (!hits) return;
    for (let i = 0; i < hits.length; i += 1) {
      const url = hits[i].replace(/[)\]},;]+$/, '');
      if (/\.heic(\?|$)/i.test(url)) continue;
      if (list.indexOf(url) === -1) list.push(url);
    }
    while (list.length > 80) list.shift();
  };
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (...args) {
      const out = origFetch.apply(this, args);
      try {
        out.then((res) => {
          try {
            const ct = res.headers.get('content-type') || '';
            if (!/json|event-stream|text/i.test(ct)) return;
            res.clone().text().then(push).catch(() => {});
          } catch (e) { }
        }).catch(() => {});
      } catch (e) { }
      return out;
    };
  }
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype && XHR.prototype.send) {
    const origSend = XHR.prototype.send;
    XHR.prototype.send = function (...args) {
      try {
        this.addEventListener('load', () => {
          try {
            const ct = this.getResponseHeader('content-type') || '';
            if (/json|event-stream|text/i.test(ct) && typeof this.responseText === 'string') push(this.responseText);
          } catch (e) { }
        });
      } catch (e) { }
      return origSend.apply(this, args);
    };
  }
  return list.length;
})()"""

#: 视频钩子：把响应里出现过的视频对象收进 window.__ncVideoList
VIDEO_LIST_KEY = "__ncVideoList"
VIDEO_HOOK_SCRIPT = r"""(() => {
  const KEY = '__ncVideoList';
  if (window.__ncVideoHooked) return (window[KEY] || []).length;
  window.__ncVideoHooked = true;
  const list = (window[KEY] = window[KEY] || []);

  const b64 = (s) => {
    try { return atob(String(s || '')); } catch (e) { return ''; }
  };

  const bornOf = (u) => {
    const m = /[?&]l=(\d{14})/.exec(String(u || ''));
    if (m) {
      const s = m[1];
      const t = new Date(
        +s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8),
        +s.slice(8, 10), +s.slice(10, 12), +s.slice(12, 14),
      ).getTime();
      if (!isNaN(t)) return t;
    }
    const q = /[?&]dy_q=(\d{10})/.exec(String(u || ''));
    if (q) return Number(q[1]) * 1000;
    return 0;
  };

  const bestOf = (vm) => {
    const vl = vm && vm.video_list;
    if (!vl || typeof vl !== 'object') return null;
    let best = null;
    for (const k of Object.keys(vl)) {
      const one = vl[k] || {};
      const url = b64(one.main_url || one.backup_url_1 || '');
      if (!url) continue;
      const w = Number(one.vwidth) || 0;
      if (!best || w > best.w) best = { url, w, h: Number(one.vheight) || 0, definition: String(one.definition || '') };
    }
    return best;
  };

  const add = (v) => {
    if (!v || typeof v.vid !== 'string' || !/^v[0-9a-z]/i.test(v.vid)) return;
    if (!v.url) return;
    for (let i = 0; i < list.length; i += 1) {
      if (list[i].vid === v.vid) {
        const merged = (v.w || 0) > (list[i].w || 0) ? v : list[i];
        if (!merged.born && (list[i].born || v.born)) merged.born = list[i].born || v.born;
        list[i] = merged;
        return;
      }
    }
    list.push(v);
    while (list.length > 60) list.shift();
  };

  const scan = (obj, depth) => {
    if (!obj || typeof obj !== 'object' || depth > 12) return;
    if (Array.isArray(obj)) {
      for (let i = 0; i < obj.length; i += 1) scan(obj[i], depth + 1);
      return;
    }
    if (typeof obj.vid === 'string' && (obj.download_url || obj.video_model)) {
      let vm = null;
      if (typeof obj.video_model === 'string') { try { vm = JSON.parse(obj.video_model); } catch (e) { vm = null; } }
      else if (obj.video_model && typeof obj.video_model === 'object') vm = obj.video_model;
      const best = bestOf(vm);
      add({
        vid: obj.vid,
        url: (best && best.url) || String(obj.download_url || ''),
        w: (best && best.w) || Number(obj.width) || 0,
        h: (best && best.h) || Number(obj.height) || 0,
        definition: (best && best.definition) || '',
        duration: Number(obj.duration) || 0,
        born: bornOf((best && best.url) || String(obj.download_url || '')),
        ts: Date.now(),
      });
    }
    const keys = Object.keys(obj);
    for (let i = 0; i < keys.length; i += 1) {
      const v = obj[keys[i]];
      if (v && typeof v === 'object') scan(v, depth + 1);
    }
  };

  const parse = (text) => {
    if (typeof text !== 'string' || text.length < 40) return;
    if (text.indexOf('video_model') === -1 && text.indexOf('download_url') === -1) return;
    try { scan(JSON.parse(text), 0); return; } catch (e) { }
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
      let line = lines[i].trim();
      if (line.indexOf('{') !== 0) {
        const cut = line.indexOf('data:');
        if (cut !== 0) continue;
        line = line.slice(5).trim();
      }
      if (line.indexOf('{') !== 0) continue;
      try { scan(JSON.parse(line), 0); } catch (e) { }
    }
  };

  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (...args) {
      const out = origFetch.apply(this, args);
      try {
        out.then((res) => {
          try {
            const ct = res.headers.get('content-type') || '';
            if (!/json|event-stream|text/i.test(ct)) return;
            res.clone().text().then(parse).catch(() => {});
          } catch (e) { }
        }).catch(() => {});
      } catch (e) { }
      return out;
    };
  }
  const XHR = window.XMLHttpRequest;
  if (XHR && XHR.prototype && XHR.prototype.send) {
    const origSend = XHR.prototype.send;
    XHR.prototype.send = function (...args) {
      try {
        this.addEventListener('load', () => {
          try {
            const ct = this.getResponseHeader('content-type') || '';
            if (/json|event-stream|text/i.test(ct)) parse(this.responseText);
          } catch (e) { }
        });
      } catch (e) { }
      return origSend.apply(this, args);
    };
  }
  return list.length;
})()"""

#: 读钩子收到的视频清单（按出现顺序，最后一条最新）
VIDEO_LIST_SCRIPT = r"""(() => {
  const list = window.__ncVideoList || [];
  return list.filter((v) => v && v.vid && v.url).map((v) => ({ vid: v.vid, url: v.url, w: v.w || 0, h: v.h || 0, definition: v.definition || '', duration: v.duration || 0, born: v.born || 0, ts: v.ts || 0 }));
})()"""

#: 钩子还在不在（页面被重载 / 换了文档就没了）
VIDEO_HOOKED_SCRIPT = "Boolean(window.__ncVideoHooked)"

#: 页面里所有 video（blob: 或 https 都有），作为接口钩子失效时的兜底
GEN_VIDEOS_SCRIPT = r"""[...document.querySelectorAll('video')]
  .map((v) => {
    const src = v.currentSrc || v.src || (v.querySelector('source') && v.querySelector('source').src) || '';
    return { src: String(src || ''), w: v.videoWidth || 0, h: v.videoHeight || 0, dur: v.duration || 0 };
  })
  .filter((o) => o.src.length > 0)"""

#: 输入区是不是「普通对话」态（不是视频模式）
CHAT_MODE_OK_SCRIPT = (
    r"""(() => {
  const ed = """
    + EDITOR_SCRIPT
    + r""";
  if (!ed) return { ok: false, has: false };
  const attrOf = (el) => String((el && el.getAttribute && el.getAttribute('data-placeholder')) || '');
  let ph = attrOf(ed);
  if (!ph) ph = attrOf(ed.querySelector('[data-placeholder]'));
  if (!ph && ed.innerHTML) {
    const m = /data-placeholder="([^"]*)"/.exec(ed.innerHTML);
    if (m) ph = m[1];
  }
  return { ok: !/描述你想要的视频/.test(ph), has: true, ph: ph.slice(0, 24) };
})()"""
)

#: 消息列表里最后一条是什么
CHAT_REPLY_SCRIPT = r"""(() => {
  const list = document.querySelector('[data-testid="message-list"]') || document.body;
  const items = [...list.querySelectorAll('[data-testid="union_message"]')];
  if (!items.length) return { ok: false, why: 'no-message', count: 0 };
  const last = items[items.length - 1];
  const isUser = !!last.querySelector('[data-testid="send_message"]') && !last.querySelector('[data-testid="receive_message"]');
  const text = [...last.querySelectorAll('[data-testid="message_text_content"]')]
    .map((e) => String(e.innerText || '')).join('\n').trim();
  const busy = /停止生成|正在生成|生成中/.test(String(document.body.innerText || ''));
  return { ok: true, isUser, text, busy, count: items.length };
})()"""

RATIO_SETTLE_MS = 700

#: 只找「文本正好等于目标比例」的可见元素，取最深的那个点掉
_RATIO_CLICK = r"""(() => {
  const want = %s;
  const seen = (el) => { const r = el.getBoundingClientRect(); return r.width > 6 && r.height > 6; };
  const label = (el) => String(el.innerText || el.textContent || '').replace(/\s+/g, '');
  const nodes = [...document.querySelectorAll('button,[role="button"],[role="radio"],li,label,div,span')];
  const exact = nodes.filter((el) => seen(el) && label(el) === want).pop();
  if (exact) { exact.click(); return { ok: true, how: 'exact' }; }
  const loose = nodes.filter((el) => seen(el) && label(el).startsWith(want) && label(el).length <= want.length + 3)[0];
  if (loose) { loose.click(); return { ok: true, how: 'loose' }; }
  return { ok: false, how: 'missing' };
})()"""


def ratio_click_script(target: str) -> str:
    return _RATIO_CLICK % json.dumps(target, ensure_ascii=False)


#: 点开比例面板（只在第一步没找到时才用）
RATIO_OPEN_SCRIPT = r"""(() => {
  const seen = (el) => { const r = el.getBoundingClientRect(); return r.width > 6 && r.height > 6; };
  const label = (el) => String(el.innerText || el.textContent || '').replace(/\s+/g, '');
  const nodes = [...document.querySelectorAll('button,[role="button"],[role="radio"],li,label,div,span')];
  const hit = nodes.find((el) => seen(el) && label(el).length <= 12 && /^(比例|画幅|画面比例|图片比例|尺寸|比例设置|更多设置)$/.test(label(el)));
  if (!hit) return { ok: false };
  hit.click();
  return { ok: true };
})()"""

#: 视频参数面板（时长 / 比例 / 清晰度 / 模型档）
VIDEO_PARAM_TRIGGER = (
    "[...document.querySelectorAll('button[aria-haspopup=menu]')]"
    ".find((b) => /·\\s*\\d+s/.test(b.textContent || ''))"
)
VIDEO_MODEL_TRIGGER = (
    "[...document.querySelectorAll('button[aria-haspopup=menu]')]"
    ".find((b) => /模型/.test(b.textContent || '') && !/·\\s*\\d+s/.test(b.textContent || ''))"
)
VIDEO_PANEL_ANY = (
    "[...document.querySelectorAll('[role=menu]')].find((m) => m.querySelector('[role=slider]'))"
)
VIDEO_PANEL_SCRIPT = (
    r"""(() => {
  const m = """
    + VIDEO_PANEL_ANY
    + r""";
  if (!m) return false;
  const t = m.querySelector('[role=slider]');
  const r = t.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && getComputedStyle(t).visibility !== 'hidden';
})()"""
)

#: 打开一个 Radix 下拉（trigger 只认 pointerdown）
_SOFT_OPEN = r"""(() => {
  const el = (%s);
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (!(r.width > 0 && r.height > 0)) return false;
  el.dispatchEvent(new PointerEvent('pointerdown', {
    bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
    button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true,
  }));
  return true;
})()"""

#: 点面板里的普通按钮（比例 / 清晰度）：这类用完整点击序列
_SOFT_CLICK = r"""(() => {
  const el = (%s);
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (!(r.width > 0 && r.height > 0)) return false;
  const at = {
    bubbles: true, cancelable: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
    button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse', isPrimary: true,
  };
  el.dispatchEvent(new PointerEvent('pointerdown', at));
  el.dispatchEvent(new MouseEvent('mousedown', at));
  el.dispatchEvent(new PointerEvent('pointerup', { ...at, buttons: 0 }));
  el.dispatchEvent(new MouseEvent('mouseup', { ...at, buttons: 0 }));
  el.dispatchEvent(new MouseEvent('click', { ...at, buttons: 0 }));
  return true;
})()"""


def soft_open_script(find_expr: str) -> str:
    return _SOFT_OPEN % find_expr


def soft_click_script(find_expr: str) -> str:
    return _SOFT_CLICK % find_expr


KEY_CODES = {"Home": 36, "End": 35, "ArrowLeft": 37, "ArrowRight": 39, "Escape": 27}

#: 模型档位 → 菜单项文字的特征
TIER_PATTERNS = {
    "2.5": r"2\.5",
    "2.0": r"2\.0(?!\s*(?:Fast|Mini))",
    "fast": r"Fast",
    "mini": r"Mini",
}

NO_CDP_TIP = (
    "连不上豆包（调试端口未开）。请完全退出豆包后用 "
    "Doubao.exe --remote-debugging-port=9555 重新打开，或在本应用里点「一键启动豆包」"
)


# ============================================================ CDP 小客户端


class CdpError(Exception):
    def __init__(self, message: str, *, bridge_down: bool = False) -> None:
        super().__init__(message)
        self.bridge_down = bridge_down


class CdpTab:
    """一条 CDP 连接 + 一个 attached session。用完必须 `close()`。"""

    def __init__(self, ws: Any, session_id: str, browser: str, target_url: str) -> None:
        self.ws = ws
        self.session_id = session_id
        self.browser = browser
        self.target_url = target_url
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._reader: asyncio.Task | None = None
        self.hook_script_id: str | None = None

    async def _read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                mid = msg.get("id")
                if mid and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if fut.done():
                        continue
                    if msg.get("error"):
                        err = msg["error"]
                        fut.set_exception(CdpError(str(err.get("message") or json.dumps(err)))
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
                    continue
                # CDP event，交给等它的人
                method = msg.get("method")
                waiters = self._waiters.get(method)
                if waiters:
                    fut = waiters.pop(0)
                    if not fut.done():
                        fut.set_result(msg.get("params") or {})
        except (asyncio.CancelledError, Exception):
            # 窗口被关掉时把还在等的人叫醒，别让他们干等到超时
            for mid, fut in list(self._pending.items()):
                self._pending.pop(mid, None)
                if not fut.done():
                    fut.set_exception(CdpError("豆包窗口已关闭"))
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CdpError("豆包窗口已关闭"))
            self._pending.clear()

    def _start_reader(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        """每条命令都带超时。

        没有它的话，豆包窗口崩了（或页面卡住）时这条 await 永远不返回，
        任务会一直挂在 submitting 上，还会把整条视频队列堵死 —— 踩过一次。
        """
        self._start_reader()
        self._seq += 1
        mid = self._seq
        payload: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as err:  # noqa: BLE001 - 连接已断
            self._pending.pop(mid, None)
            raise CdpError(f"豆包连接已断开：{err}") from err
        try:
            return await asyncio.wait_for(fut, timeout=CDP_CALL_TIMEOUT_MS / 1000)
        except asyncio.TimeoutError as err:
            self._pending.pop(mid, None)
            raise CdpError(
                f"豆包无响应（CDP {method} 超过 {round(CDP_CALL_TIMEOUT_MS / 1000)} 秒）"
            ) from err

    async def wait_event(self, method: str, timeout_ms: int = 5000) -> dict | None:
        self._start_reader()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(method, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            return None
        finally:
            waiters = self._waiters.get(method) or []
            if fut in waiters:
                waiters.remove(fut)

    async def evaluate(self, expression: str) -> Any:
        res = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                # 页面自己抛错时不炸桥，交给调用方判断
                "userGesture": True,
            },
            self.session_id,
        )
        exc = res.get("exceptionDetails")
        if exc:
            detail = (exc.get("exception") or {}).get("description") or exc.get("text") or "页面脚本异常"
            raise CdpError(str(detail).split("\n")[0][:200])
        return (res.get("result") or {}).get("value")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001 - 关不掉也无所谓
            pass


async def open_target(endpoint: str) -> dict:
    """连上豆包的调试端口，attach 到聊天页。返回 {ok} 或 {ok: False, reason, detail}。"""
    from websockets.asyncio.client import connect

    try:
        async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_MS / 1000) as client:
            res = await client.get(f"{endpoint}/json/version")
        if res.status_code >= 400:
            raise RuntimeError(f"HTTP {res.status_code}")
        version = res.json()
    except Exception as err:  # noqa: BLE001 - 连不上就是没开端口
        return {"ok": False, "reason": "no-cdp", "detail": f"{endpoint} 连不上（{err}）"}

    ws_url = version.get("webSocketDebuggerUrl") or ""
    try:
        ws = await asyncio.wait_for(
            connect(ws_url, max_size=None, open_timeout=CONNECT_TIMEOUT_MS / 1000), timeout=CONNECT_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "no-cdp", "detail": "CDP WebSocket 打开超时"}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "reason": "no-cdp", "detail": f"CDP WebSocket 打开失败：{err}"}

    tab = CdpTab(ws, "", str(version.get("Browser") or ""), "")
    try:
        targets = (await tab.send("Target.getTargets")).get("targetInfos") or []
        # 豆包有三个 target：launcher / background / chat。真正聊天界面是 doubao-chat。
        chat = next(
            (t for t in targets if t.get("type") == "page" and "doubao-chat" in str(t.get("url") or "")),
            None,
        ) or next(
            (
                t
                for t in targets
                if t.get("type") == "page"
                and "background" not in str(t.get("url") or "")
                and "launcher" not in str(t.get("url") or "")
            ),
            None,
        )
        if not chat:
            await tab.close()
            return {"ok": False, "reason": "no-page", "detail": "豆包没有聊天页面，请先在豆包里打开一个对话"}

        attached = await tab.send(
            "Target.attachToTarget", {"targetId": chat["targetId"], "flatten": True}
        )
        tab.session_id = attached.get("sessionId") or ""
        tab.target_url = str(chat.get("url") or "")
        # Page 域要开着，才能用 addScriptToEvaluateOnNewDocument 把视频钩子注册成文档级脚本
        try:
            await tab.send("Page.enable", {}, tab.session_id)
        except CdpError:
            pass
        tab._start_reader()
        return {"ok": True, "tab": tab, "browser": tab.browser, "targetUrl": tab.target_url}
    except Exception as err:  # noqa: BLE001
        await tab.close()
        return {"ok": False, "reason": "no-cdp", "detail": f"CDP 初始化失败：{err}"}


async def require_tab(endpoint: str) -> CdpTab:
    """打开一次豆包（调用方负责关 ws）"""
    opened = await open_target(endpoint)
    if opened.get("ok"):
        return opened["tab"]
    raise CdpError(
        NO_CDP_TIP if opened.get("reason") == "no-cdp" else (opened.get("detail") or "豆包页面不可用"),
        bridge_down=opened.get("reason") == "no-cdp",
    )


# ============================================================ 探测 / 一键重启


async def probe_bridge(endpoint: str = DOUBAO_DEFAULT_ENDPOINT) -> dict:
    """探测当前机器上的豆包是否开着调试端口"""
    try:
        async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_MS / 1000) as client:
            res = await client.get(f"{endpoint}/json/version")
        if res.status_code >= 400:
            return {"ok": False, "reason": "no-cdp", "detail": f"HTTP {res.status_code}", "endpoint": endpoint}
        info = res.json()
        return {"ok": True, "browser": info.get("Browser") or "", "endpoint": endpoint}
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "reason": "no-cdp", "detail": str(err), "endpoint": endpoint}


def doubao_exe_candidates() -> list[str]:
    env = os.environ
    local = env.get("LOCALAPPDATA") or ""
    pf = env.get("ProgramFiles") or ""
    pf86 = env.get("ProgramFiles(x86)") or ""
    roots = [
        env.get("DOUBAO_EXE") or "",
        str(Path(local) / "Doubao" / "app" / "Doubao.exe") if local else "",
        str(Path(local) / "Programs" / "Doubao" / "app" / "Doubao.exe") if local else "",
        str(Path(pf) / "Doubao" / "app" / "Doubao.exe") if pf else "",
        str(Path(pf86) / "Doubao" / "app" / "Doubao.exe") if pf86 else "",
        "D:\\豆包\\Doubao\\app\\Doubao.exe",
        "C:\\豆包\\Doubao\\app\\Doubao.exe",
    ]
    return [p for p in roots if p]


def find_doubao_exe() -> str:
    """找出豆包 exe；找不到返回空串"""
    for path in doubao_exe_candidates():
        try:
            if os.path.exists(path):
                return path
        except OSError:
            continue
    return ""


def _tasklist_has_doubao() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Doubao.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "Doubao.exe" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


async def kill_doubao() -> bool:
    """杀掉所有豆包进程（用镜像名，不依赖 PID）"""
    try:
        subprocess.run(
            ["taskkill", "/IM", "Doubao.exe", "/F", "/T"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        # 本来就没在跑时 taskkill 会返回 128，属于正常情况
        pass
    # 等端口释放：进程退出是异步的，立刻重启会和旧实例抢单例锁
    for _ in range(20):
        await sleep(250)
        if not _tasklist_has_doubao():
            return True
    return False


async def restart_doubao(endpoint: str = DOUBAO_DEFAULT_ENDPOINT) -> dict:
    """一键重启豆包到「调试端口已开」的状态。用户在设置里点一下就走这里。"""
    already = await probe_bridge(endpoint)
    if already.get("ok"):
        return {
            "ok": True,
            "already": True,
            "endpoint": endpoint,
            "browser": already.get("browser") or "",
            "detail": "",
        }

    exe = find_doubao_exe()
    if not exe:
        raise ImageGenError(
            "没找到豆包（Doubao.exe）。请确认装了豆包桌面版；装在非默认目录时，"
            "可以用环境变量 DOUBAO_EXE 指定 Doubao.exe 的完整路径。"
        )

    port = urlparse(endpoint).port or 9555
    await kill_doubao()

    flags = 0
    if sys.platform == "win32":
        # detached + 独立进程组：主进程退出时别把豆包带走
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}"],
        cwd=str(Path(exe).parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )

    deadline = now_ms() + RESTART_TIMEOUT_MS
    while now_ms() < deadline:
        await sleep(1200)
        probe = await probe_bridge(endpoint)
        if probe.get("ok"):
            return {
                "ok": True,
                "already": False,
                "exe": exe,
                "endpoint": endpoint,
                "browser": probe.get("browser") or "",
                "detail": "",
            }
    return {
        "ok": False,
        "already": False,
        "exe": exe,
        "endpoint": endpoint,
        "detail": (
            f"豆包已重启，但 {round(RESTART_TIMEOUT_MS / 1000)} 秒内没等到调试端口 {port}。"
            '可能是启动时被安全软件拦住了，手动用 "Doubao.exe --remote-debugging-port=9555" 试试。'
        ),
    }


# ============================================================ 页面基础操作


async def press_enter(tab: CdpTab) -> None:
    """敲一次回车：keyDown / char / keyUp 三件套缺一个，React 受控组件就不认"""
    for type_ in ("keyDown", "char", "keyUp"):
        params: dict[str, Any] = {
            "type": type_,
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode": 13,
        }
        if type_ == "char":
            params["text"] = "\r"
        await tab.send("Input.dispatchKeyEvent", params, tab.session_id)


async def press_key(tab: CdpTab, name: str) -> None:
    vk = KEY_CODES[name]
    await tab.send(
        "Input.dispatchKeyEvent",
        {
            "type": "rawKeyDown",
            "key": name,
            "code": name,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        },
        tab.session_id,
    )
    await tab.send(
        "Input.dispatchKeyEvent",
        {
            "type": "keyUp",
            "key": name,
            "code": name,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
        },
        tab.session_id,
    )
    await sleep(35)


async def send_prompt(tab: CdpTab) -> str:
    """把话发出去：先敲回车（老版本认），没发出去就点发送按钮（新版本认）。

    返回 'enter' / 'button' / '' —— 空串表示这一轮没发出去，调用方要尽快报错，
    别让任务在「等出图」里白等五分钟。
    """
    await press_enter(tab)
    await sleep(600)
    try:
        if await tab.evaluate(EDITOR_EMPTY_SCRIPT):
            return "enter"
    except CdpError:
        return "enter"
    try:
        clicked = bool(await tab.evaluate(CLICK_SEND_SCRIPT))
    except CdpError:
        clicked = False
    await sleep(900)
    try:
        empty = bool(await tab.evaluate(EDITOR_EMPTY_SCRIPT))
    except CdpError:
        empty = True
    if empty:
        return "button" if clicked else ""
    return ""


async def tap_point(tab: CdpTab, point: dict) -> None:
    """真实鼠标点一下（菜单项这种浮层里最靠上的元素用这个最稳）"""
    at = {"x": round(point["x"]), "y": round(point["y"])}
    try:
        await tab.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", **at, "button": "none", "buttons": 0},
            tab.session_id,
        )
    except CdpError:
        pass
    await sleep(60)
    await tab.send(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", **at, "button": "left", "buttons": 1, "clickCount": 1},
        tab.session_id,
    )
    await sleep(50)
    await tab.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", **at, "button": "left", "buttons": 0, "clickCount": 1},
        tab.session_id,
    )


async def click_by_script(tab: CdpTab, find_expr: str) -> bool:
    """在页面里找一个元素，用真实鼠标点它。

    为什么要绕这一圈：豆包有些面板项（比如「更多技能」里那个「视频生成」）挂了
    React 合成事件，`el.click()` 调过去没反应 —— 实测点完面板不动、对话也不变；
    换成 CDP 注入 mousePressed / mouseReleased 才生效。所以所有关键点击都走这里。
    """
    expr = (
        r"""(() => {
  try {
    const el = (%s);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  } catch (e) { return null; }
})()"""
        % find_expr
    )
    try:
        point = await tab.evaluate(expr)
    except CdpError:
        return False
    if not point or not point.get("x"):
        return False
    await tap_point(tab, point)
    return True


async def confirm_safety(tab: CdpTab, report: Callable[[str], None]) -> bool:
    """有安全确认弹窗就点掉，返回是否点到了"""
    try:
        state = await tab.evaluate(SAFETY_DIALOG_SCRIPT)
    except CdpError:
        return False
    if state != "open":
        return False
    clicked = await click_by_script(
        tab,
        r"""[...document.querySelectorAll('button')].filter((b) => (b.innerText || '').trim() === '确认' && b.getBoundingClientRect().width > 0).pop()""",
    )
    if clicked:
        report("豆包桥：已点掉「安全确认」弹窗")
        await sleep(900)
    return clicked


async def answer_confirm_ask(tab: CdpTab, report: Callable[[str], None]) -> bool:
    """回一句「确认」，让它按当前参数开工"""
    try:
        focused = await tab.evaluate(FOCUS_EDITOR_SCRIPT)
    except CdpError:
        return False
    if not focused:
        return False
    await sleep(TYPE_SETTLE_MS)
    await tab.send("Input.insertText", {"text": "确认，按这个参数开始生成"}, tab.session_id)
    await sleep(TYPE_SETTLE_MS)
    if not await send_prompt(tab):
        return False
    report("豆包桥：它先问了一句参数，已替你回「确认」")
    await sleep(1200)
    await confirm_safety(tab, report)
    return True


async def enter_video_mode(tab: CdpTab, report: Callable[[str], None]) -> bool:
    """切到「视频生成」模式。切不进去就别发提示词。"""
    async def check() -> bool:
        try:
            now = await tab.evaluate(VIDEO_MODE_SCRIPT)
        except CdpError:
            return False
        return bool(now and now.get("ok"))

    if await check():
        return True

    pick_video_btn = (
        r"""[...document.querySelectorAll('button')].filter((b) => (b.innerText || '').trim() === '视频生成' && b.getBoundingClientRect().width > 40).pop()"""
    )

    # 1) 底栏直给的「视频生成」
    if await click_by_script(tab, pick_video_btn):
        await sleep(1600)
        if await check():
            report("豆包桥：已切到「视频生成」模式")
            return True

    # 2) 底栏没有就开「更多技能」面板，面板里的条目同样是 button
    opened = await click_by_script(
        tab,
        r"""[...document.querySelectorAll('button')].filter((b) => (b.innerText || '').trim() === '更多技能' && b.getBoundingClientRect().width > 20).pop()""",
    )
    if not opened:
        return False
    await sleep(1200)

    if await click_by_script(tab, pick_video_btn):
        await sleep(1600)
        if await check():
            report("豆包桥：已切到「视频生成」模式")
            return True
    # 面板没点成就收起来，别留着挡下一次
    try:
        await tab.evaluate(click_by_text_script("更多技能"))
    except CdpError:
        pass
    return False


async def read_video_list(tab: CdpTab) -> list[dict]:
    """读钩子收到的视频清单"""
    try:
        found = await tab.evaluate(VIDEO_LIST_SCRIPT)
    except CdpError:
        return []
    return found if isinstance(found, list) else []


async def ensure_video_hook(tab: CdpTab) -> bool:
    """确保视频钩子还在。

    页面被重载 / 豆包自己换了文档，钩子就没了 —— 这时列表是空的，
    光靠「没新视频」判断会一直等到超时。所以重装钩子并重新拉一次会话。

    **关键：用 Page.addScriptToEvaluateOnNewDocument 注册成「文档级」脚本。**
    以前是 Runtime.evaluate 直接跑，那只在「当前这份文档」里有效 ——
    而 query_video_with_doubao 发现钩子丢了就会 Page.reload，刷新把刚装好的钩子清掉，
    于是钩子永远装不上、列表永远是空的，任务只能一路等到 15 分钟超时。
    """
    try:
        alive = bool(await tab.evaluate(VIDEO_HOOKED_SCRIPT))
    except CdpError:
        alive = False
    if alive:
        return True
    # 先注册（幂等：同一个 identifier 只在没注册过时加一次），再在当前文档里补跑一遍
    if not tab.hook_script_id:
        try:
            res = await tab.send(
                "Page.addScriptToEvaluateOnNewDocument", {"source": VIDEO_HOOK_SCRIPT}, tab.session_id
            )
            if res.get("identifier"):
                tab.hook_script_id = res["identifier"]
        except CdpError:
            pass
    try:
        await tab.evaluate(VIDEO_HOOK_SCRIPT)
    except CdpError:
        pass
    return False


async def wait_page_ready(tab: CdpTab, timeout_ms: int = 20000) -> bool:
    """豆包刚被拉起来时，窗口打开了但页面还没渲染完（连输入框都还不存在）。"""
    deadline = now_ms() + timeout_ms
    while now_ms() < deadline:
        try:
            ready = bool(await tab.evaluate(f"!!({EDITOR_SCRIPT})"))
        except CdpError:
            ready = False
        if ready:
            return True
        await sleep(1000)
    return False


async def upload_files(tab: CdpTab, files: list[str]) -> None:
    """把本机文件塞进豆包那个隐藏的 file input。

    走 CDP 的 DOM.setFileInputFiles（不用剪贴板、不用拖拽），文件名和二进制都是真实的。
    上传入口也是渲染出来的，所以找几次再放弃。
    """
    targets = [p for p in (files or []) if p and os.path.exists(p)]
    if not targets:
        raise ImageGenError("要上传的文件已被删除")

    node_ids: list[int] = []
    for _ in range(8):
        # depth: 0 —— 只要根节点就够（下面按选择器全文档搜）
        root = (await tab.send("DOM.getDocument", {"depth": 0, "pierce": True}, tab.session_id)).get(
            "root"
        ) or {}
        found = await tab.send(
            "DOM.querySelectorAll",
            {"nodeId": root.get("nodeId"), "selector": "input[type=file]"},
            tab.session_id,
        )
        ids = found.get("nodeIds") or []
        if ids:
            node_ids = ids
            break
        await sleep(1500)
    if not node_ids:
        raise ImageGenError("豆包里找不到上传入口，文件发不出去")

    await tab.send(
        "DOM.setFileInputFiles", {"nodeId": node_ids[0], "files": targets}, tab.session_id
    )
    await sleep(UPLOAD_SETTLE_MS)


# ============================================================ 画幅比例


async def try_set_ratio(tab: CdpTab, ratio: str) -> bool:
    """尽力把豆包界面的画幅切到 ratio（'9:16'）。返回 True 表示点中了。"""
    want = str(ratio or "").strip()
    if not re.match(r"^\d{1,2}:\d{1,2}$", want):
        return False
    try:
        direct = await tab.evaluate(ratio_click_script(want))
        if direct and direct.get("ok"):
            return True
        opened = await tab.evaluate(RATIO_OPEN_SCRIPT)
        if not (opened and opened.get("ok")):
            return False
        await sleep(RATIO_SETTLE_MS)
        again = await tab.evaluate(ratio_click_script(want))
        return bool(again and again.get("ok"))
    except CdpError:
        # 页面里抛什么都当「没点中」：比例是加分项，不能把这一轮出图带崩
        return False


# ============================================================ 去水印 / 取图


def crop_box_for(w: int, h: int, ratio: str, tolerance: float = 0.04) -> dict:
    """居中裁剪到目标比例：返回要画的源矩形。

    注意：grab_image 里注入页面的那段脚本有一份同样的算式（页面里用不了模块函数）。
    改这里必须同步改那边，否则「Python 侧算的」和「实际裁的」会不一样。
    """
    full = {"sx": 0, "sy": 0, "sw": w, "sh": h}
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", str(ratio or ""))
    if not m or not (w > 0) or not (h > 0):
        return full
    target = int(m.group(1)) / int(m.group(2))
    if not (target > 0) or abs(w / h - target) <= target * tolerance:
        return full
    sw, sh = w, h
    if w / h > target:
        sw = round(h * target)
    else:
        sh = round(w / target)
    sw = max(16, min(w, sw))
    sh = max(16, min(h, sh))
    return {"sx": round((w - sw) / 2), "sy": round((h - sh) / 2), "sw": sw, "sh": sh}


def _raw_pick_script(src: str) -> str:
    return (
        r"""(() => {
  const pathOf = (u) => {
    const s = String(u || '');
    const cut = s.indexOf('~tplv');
    if (cut <= 0) return '';
    const m = /^https?:\/\/[^/]+(\/.*)$/.exec(s.slice(0, cut));
    return m ? m[1] : s.slice(0, cut);
  };
  const want = pathOf(%s);
  if (!want) return '';
  const list = window.__ncRawUrls || [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (/\.heic(\?|$)/i.test(list[i])) continue;
    if (pathOf(list[i]) === want) return list[i];
  }
  return '';
})()"""
        % json.dumps(str(src or ""), ensure_ascii=False)
    )


async def grab_image(
    tab: CdpTab, src: str, *, erase_watermark: bool = True, ratio: str = ""
) -> dict:
    """从页面里把一张图取回来。

    两个坑：
    1. 图床要签名 + 登录态，Python 侧直接下会 403，必须在页面里 fetch；
    2. Runtime.evaluate 一次性回传几 MB 的 base64 字符串会把 CDP 的 WebSocket
       直接打死（1006），所以先存在页面全局里，再按 256KB 分片取。
    """
    # 先问页面有没有这张图的无水印原版（接口里的 image_ori_raw）
    try:
        raw_src = str(await tab.evaluate(_raw_pick_script(src)) or "")
    except CdpError:
        raw_src = ""

    urls = [raw_src, src] if raw_src else [src]
    meta_expr = (
        r"""(() => {
  try {
    const urls = %s;
    const rawUrl = %s;
    let usedUrl = '';
    let blob = null;
    let bmp = null;
    let lastStatus = 0;
    for (let i = 0; i < urls.length; i += 1) {
      try {
        const r = await fetch(urls[i]);
        if (!r.ok) { lastStatus = r.status; continue; }
        const b = await r.blob();
        const bm = await createImageBitmap(b).catch(() => null);
        if (!bm) { lastStatus = -2; continue; }
        usedUrl = urls[i]; blob = b; bmp = bm;
        break;
      } catch (e) { lastStatus = -1; }
    }
    if (!bmp) return { error: '取图失败（' + (lastStatus === -2 ? '图片格式解不开' : 'HTTP ' + lastStatus) + '）' };
    const isRaw = Boolean(rawUrl) && usedUrl === rawUrl;
    let out = blob;
    let mime = blob.type || 'image/png';
    let w = 0; let h = 0; let marked = null; let cropped = null;
    {
      w = bmp.width; h = bmp.height;
      let sx = 0; let sy = 0; let sw = w; let sh = h;
      const want = %s;
      const m = /^(\d{1,2}):(\d{1,2})$/.exec(want);
      if (m && w > 0 && h > 0) {
        const target = Number(m[1]) / Number(m[2]);
        if (target > 0 && Math.abs(w / h - target) > target * 0.04) {
          if (w / h > target) { sw = Math.round(h * target); sx = Math.round((w - sw) / 2); }
          else { sh = Math.round(w / target); sy = Math.round((h - sh) / 2); }
          sw = Math.max(16, Math.min(w, sw));
          sh = Math.max(16, Math.min(h, sh));
        }
      }
      let base = bmp;
      if (%s && !isRaw) {
        try {
          const wc = document.createElement('canvas');
          wc.width = w; wc.height = h;
          const wcx = wc.getContext('2d', { willReadFrequently: true, alpha: false });
          wcx.drawImage(bmp, 0, 0);
          marked = window.__ncEraseWatermark ? window.__ncEraseWatermark(wcx, w, h) : { ok: false, reason: 'no-fn' };
          if (marked && marked.ok) base = wc;
        } catch (e) {
          marked = { ok: false, reason: String((e && e.message) || e).slice(0, 120) };
        }
      }
      try {
        const cv = document.createElement('canvas');
        cv.width = sw; cv.height = sh;
        const cx = cv.getContext('2d', { alpha: false });
        cx.drawImage(base, sx, sy, sw, sh, 0, 0, sw, sh);
        const cleaned = await new Promise((r) => cv.toBlob(r, 'image/jpeg', 0.95));
        const mustSwap = sw !== w || sh !== h || Boolean(marked && marked.ok);
        if (cleaned && mustSwap) {
          out = cleaned;
          mime = 'image/jpeg';
          if (sw !== w || sh !== h) cropped = { from: [w, h], to: [sw, sh] };
          w = sw; h = sh;
        }
      } catch (e) {
        cropped = null;
      }
      if (bmp.close) bmp.close();
    }
    const bytes = new Uint8Array(await out.arrayBuffer());
    let bin = '';
    for (let k = 0; k < bytes.length; k += 8192) {
      bin += String.fromCharCode.apply(null, bytes.subarray(k, k + 8192));
    }
    window.__ncGrab = btoa(bin);
    return {
      len: window.__ncGrab.length,
      size: out.size,
      type: mime,
      w,
      h,
      marked,
      cropped,
      cleaned: isRaw ? 'raw' : (marked && marked.ok ? 'erased' : 'none'),
      usedRaw: isRaw,
    };
  } catch (e) {
    return { error: String((e && e.message) || e).slice(0, 200) };
  }
})()"""
        % (
            json.dumps(urls, ensure_ascii=False),
            json.dumps(raw_src, ensure_ascii=False),
            json.dumps(str(ratio or ""), ensure_ascii=False),
            "true" if erase_watermark else "false",
        )
    )
    # 页面里用了 await，必须包成 async IIFE
    meta_expr = "(async () => {" + meta_expr[len("(() => {") :].rstrip()[:-len("})()")] + "})()"
    meta = await tab.evaluate(meta_expr)
    if not isinstance(meta, dict):
        raise ImageGenError("取图失败（页面没有返回结果）")
    if meta.get("error"):
        raise ImageGenError(meta["error"])

    total = int(meta.get("len") or 0)
    parts: list[str] = []
    offset = 0
    while offset < total:
        end = min(offset + GRAB_CHUNK, total)
        piece = await tab.evaluate(f"window.__ncGrab.slice({offset}, {end})")
        if not isinstance(piece, str) or len(piece) != end - offset:
            raise ImageGenError("取图数据不完整")
        parts.append(piece)
        offset = end
    try:
        await tab.evaluate("window.__ncGrab = null; 1")
    except CdpError:
        pass

    b64 = "".join(parts)
    if not b64:
        raise ImageGenError("取图数据为空")
    return {
        "b64": b64,
        "size": meta.get("size") or 0,
        "mimeType": meta.get("type") or "image/png",
        "width": meta.get("w") or 0,
        "height": meta.get("h") or 0,
        "marked": meta.get("marked"),
        "cropped": meta.get("cropped"),
        "cleaned": meta.get("cleaned") or "none",
    }


def best_per_image(items: list[dict]) -> list[dict]:
    """同一张图的多个尺寸归成一组，保留分辨率最大的那个"""
    groups: dict[str, dict] = {}
    for item in items:
        m = re.search(r"rc_gen_image/([0-9a-f]{16,})", str(item.get("src") or ""), re.I)
        key = m.group(1) if m else str(item.get("src") or "")
        cur = groups.get(key)
        if cur is None or int(item.get("w") or 0) > int(cur.get("w") or 0):
            groups[key] = item
    return list(groups.values())


# ============================================================ 账号


async def read_doubao_accounts(endpoint: str = DOUBAO_DEFAULT_ENDPOINT) -> dict:
    """读出豆包当前登录了哪些账号、额度用了多少。"""
    try:
        tab = await require_tab(endpoint)
    except CdpError as err:
        if err.bridge_down:
            return {"ok": False, "reason": "no-cdp", "accounts": [], "quota": ""}
        raise
    try:
        try:
            st = await tab.evaluate(ACCOUNT_MENU_OPEN_SCRIPT)
        except CdpError:
            st = {"ok": False}
        if not st or not st.get("ok"):
            return {"ok": False, "reason": (st or {}).get("why") or "unknown", "accounts": [], "quota": ""}
        if not st.get("already"):
            await tap_point(tab, {"x": st["x"], "y": st["y"]})
            await sleep(1200)
        try:
            read = await tab.evaluate(ACCOUNT_MENU_READ_SCRIPT)
        except CdpError:
            read = {"open": False}
        await close_account_menu(tab)
        if not read or not read.get("open"):
            return {"ok": False, "reason": "menu-not-open", "accounts": [], "quota": ""}
        rows = read.get("rows") or []
        return {
            "ok": True,
            "accounts": rows,
            # 已登录的号才算「能拿来生成」，没头像的那行是还没扫码的占位
            "usable": len([r for r in rows if r.get("logged")]),
            "quota": read.get("quota") or "",
            "endpoint": endpoint,
        }
    finally:
        await tab.close()


async def close_account_menu(tab: CdpTab) -> None:
    """菜单开着就按 Esc 收起来，免得挡住后面的点击"""
    for type_ in ("keyDown", "keyUp"):
        try:
            await tab.send(
                "Input.dispatchKeyEvent",
                {"type": type_, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
                tab.session_id,
            )
        except CdpError:
            pass
    await sleep(400)


async def switch_doubao_account(
    *, endpoint: str = DOUBAO_DEFAULT_ENDPOINT, to: str = "", avoid: list[str] | None = None
) -> dict:
    """换到下一个已登录账号。**只切已登录的号** —— 没头像那行点下去会弹扫码登录框。"""
    try:
        tab = await require_tab(endpoint)
    except CdpError as err:
        if err.bridge_down:
            return {"switched": False, "reason": "no-cdp", "accounts": []}
        raise
    try:
        try:
            st = await tab.evaluate(ACCOUNT_MENU_OPEN_SCRIPT)
        except CdpError:
            st = {"ok": False}
        if not st or not st.get("ok"):
            return {"switched": False, "reason": (st or {}).get("why") or "unknown", "accounts": []}
        if not st.get("already"):
            await tap_point(tab, {"x": st["x"], "y": st["y"]})
            await sleep(1200)
        try:
            read = await tab.evaluate(ACCOUNT_MENU_READ_SCRIPT)
        except CdpError:
            read = {"open": False}
        if not read or not read.get("open"):
            return {"switched": False, "reason": "menu-not-open", "accounts": []}

        rows = read.get("rows") or []
        logged = [r for r in rows if r.get("logged")]
        cur = next((r for r in rows if r.get("current")), None)
        # 已经试过、确认没额度的账号 —— 别再切回去，否则两个号之间来回踢皮球
        tried = {str(s or "") for s in (avoid or [])}
        target = None
        if to:
            target = next((r for r in logged if to in str(r.get("name") or "")), None)
        if target is None:
            idx = next((i for i, r in enumerate(logged) if cur and r.get("i") == cur.get("i")), -1)
            pool = [r for r in logged if not (cur and r.get("i") == cur.get("i")) and str(r.get("name")) not in tried]
            if pool:
                # 从当前账号的位置往后轮，轮到底再回到开头
                after = [r for r in pool if idx < 0 or int(r.get("i") or 0) > int(logged[idx].get("i") or -1)]
                target = after[0] if after else pool[0]
        if target is None:
            await close_account_menu(tab)
            return {"switched": False, "reason": "no-other-account", "from": (cur or {}).get("name") or "", "accounts": rows}

        await tap_point(tab, {"x": target["x"], "y": target["y"]})
        # 换号要重连会话、重拉历史，比切个菜单慢，给足时间
        await sleep(2600)
        try:
            after = await tab.evaluate(ACCOUNT_MENU_READ_SCRIPT)
        except CdpError:
            after = {"open": False}
        if after and after.get("open"):
            await close_account_menu(tab)
        now_cur = next((r for r in (after or {}).get("rows") or [] if r.get("current")), None) if (after or {}).get("open") else None
        return {
            "switched": True,
            "from": (cur or {}).get("name") or "",
            "to": target.get("name") or "",
            # 读得到就以页面为准，读不到（菜单已关）就先信自己的动作
            "confirmed": (now_cur.get("name") == target.get("name")) if now_cur else None,
            "accounts": (after or {}).get("rows") or rows if (after or {}).get("open") else rows,
        }
    finally:
        await tab.close()


# ============================================================ 文字问答


async def reset_to_chat_mode(
    tab: CdpTab, report: Callable[[str], None], *, new_chat: bool = True
) -> bool:
    """切回「普通对话」模式。

    用户可能把豆包停在「视频生成」模式。实测点「新对话」会把模式一起重置回「对话」——
    这是唯一可靠的复位手段，因为进了视频模式之后技能栏会被参数条替换，页面上根本没有「对话」入口。
    """
    try:
        state = await tab.evaluate(CHAT_MODE_OK_SCRIPT)
    except CdpError:
        state = None
    # 已经在对话模式、而且这一轮要接着上一轮追问（补镜 / 重拆），就别点「新对话」
    if state and state.get("ok") and not new_chat:
        return True

    report("豆包桥：先点「新对话」清掉上一轮上下文" if new_chat else "豆包桥：当前停在视频模式，先点「新对话」切回对话")
    clicked = await click_by_script(
        tab,
        r"""document.querySelector('[data-testid="create_conversation_button"]')
      || [...document.querySelectorAll('div[role="button"], button, div')]
        .find((el) => (el.innerText || '').trim().startsWith('新对话') && el.getBoundingClientRect().width > 0)""",
    )
    if clicked:
        await sleep(NEW_CHAT_SETTLE_MS)

    try:
        after = await tab.evaluate(CHAT_MODE_OK_SCRIPT)
    except CdpError:
        after = None
    if after and after.get("ok"):
        if not (state and state.get("ok")):
            report("豆包桥：已切回对话模式")
        return True
    return False


async def ask_doubao(
    *,
    prompt: str,
    endpoint: str = DOUBAO_DEFAULT_ENDPOINT,
    timeout_ms: int = 180000,
    on_progress: Callable[[str], None] | None = None,
    new_chat: bool = True,
) -> str:
    """问豆包一句话，把它的文字回复取回来。

    走的是和生图/生视频完全一样的那条白嫖链路：CDP 打字 → 回车 → 轮询页面。
    实测豆包写分镜的质量比免费对话模型好得多，而且它天然守在「只输出 JSON」这种指令上。

    注意：会先点「新对话」，把上一轮的上下文清掉。这是刻意的 ——
    编导要的是「一段剧本进去、一份分镜出来」，带上历史对话只会让它跑偏。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("提示词不能为空")

    def report(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001 - 回调出错不影响取回复
                pass

    tab = await require_tab(endpoint)
    try:
        await wait_page_ready(tab)

        # 1) 新对话：既清上下文，也把「视频模式」复位回「对话」
        reset = await reset_to_chat_mode(tab, report, new_chat=new_chat)
        if not reset:
            try:
                now = await tab.evaluate(CHAT_MODE_OK_SCRIPT)
            except CdpError:
                now = None
            if not (now and now.get("ok")):
                raise ImageGenError(
                    "豆包还停在「视频生成」模式，点「新对话」也没切回对话。"
                    "请在豆包窗口里手动点一次「新对话」，再重试"
                )

        # 2) 打字发送
        focused = await tab.evaluate(FOCUS_EDITOR_SCRIPT)
        if not focused:
            raise ImageGenError("豆包里找不到输入框，可能页面还没加载好")
        await sleep(TYPE_SETTLE_MS)
        await tab.send("Input.insertText", {"text": text}, tab.session_id)
        await sleep(TYPE_SETTLE_MS)
        if not await send_prompt(tab):
            raise ImageGenError("提示词没发出去：豆包输入框还留着文字（界面可能改版了，先看一眼前台窗口再试）")
        report("豆包桥：已发送，等豆包回复")

        # 3) 轮询最后一条消息，直到它不再变
        deadline = now_ms() + timeout_ms
        last_text = ""
        stable = 0
        while now_ms() < deadline:
            await sleep(POLL_STEP_MS)
            try:
                now = await tab.evaluate(CHAT_REPLY_SCRIPT)
            except CdpError:
                continue
            if not now or not now.get("ok"):
                continue
            # 最后一条还是刚发出去的用户消息，说明它还没开始回
            if now.get("isUser"):
                continue
            body = str(now.get("text") or "").strip()
            if not body:
                continue
            if body == last_text:
                stable += 1
            else:
                last_text = body
                stable = 0
            # 页面上没有「停止生成」+ 内容两轮没变 → 说完了
            if not now.get("busy") and stable >= 1:
                return body
            # 兜底：有些版本不显示停止按钮，连续四轮（约 8 秒）没变化也认它说完了
            if stable >= 3:
                return body
        if last_text:
            return last_text
        raise ImageGenError(
            f"豆包没有回复（等了 {round(timeout_ms / 1000)} 秒还没吐字，看一眼前台窗口）"
        )
    finally:
        await tab.close()


# ============================================================ 生图主流程


async def generate_with_doubao(
    *,
    prompt: str,
    ref_files: list[str] | None = None,
    count: int = 1,
    endpoint: str = DOUBAO_DEFAULT_ENDPOINT,
    erase_watermark: bool = True,
    ratio: str = "",
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """驱动豆包出一次图。返回 [{b64, width, height, mimeType, source}]。"""
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("提示词不能为空")
    times = max(1, min(4, int(count or 1)))
    want_ratio = str(ratio or "") if re.match(r"^\d{1,2}:\d{1,2}$", str(ratio or "")) else ""
    ref_files = ref_files or []

    def report(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    opened = await open_target(endpoint)
    if not opened.get("ok"):
        if opened.get("reason") == "no-cdp":
            raise ImageGenError(
                f"连不上豆包（{endpoint}）。豆包要以调试模式启动才行：先完全退出豆包，再用 "
                "Doubao.exe --remote-debugging-port=9555 打开它。本应用可以帮你一键重启豆包。"
            )
        raise ImageGenError(opened.get("detail") or "豆包页面不可用")

    tab: CdpTab = opened["tab"]
    results: list[dict] = []
    try:
        # 豆包可能刚被一键重启拉起来，页面还没画出输入框，先等它渲染
        await wait_page_ready(tab)

        # 抹水印的函数体先注入一次（幂等），抓图时直接调。注入失败不能连累出图
        if erase_watermark:
            try:
                await tab.evaluate(ERASE_FN_SCRIPT)
            except CdpError as err:
                report(f"豆包桥：去水印脚本注入失败（{str(err)[:60]}），本轮返回原图")

        # 无水印原图的钩子：必须在发送之前挂上，出图那波接口响应才会被记下来
        try:
            await tab.evaluate(RAW_HOOK_SCRIPT)
        except CdpError as err:
            report(f"豆包桥：原图钩子注入失败（{str(err)[:60]}），本轮回退到本地抹水印")

        for round_ in range(times):
            if len(results) >= times:
                break
            report(f"豆包桥：第 {round_ + 1}/{times} 轮")

            # 1) 新对话，避免上一轮的上下文影响这一轮的画面
            try:
                started = await tab.evaluate(click_by_text_script("新对话"))
            except CdpError:
                started = False
            if started:
                await sleep(NEW_CHAT_SETTLE_MS)

            # 2) 画幅：豆包默认方图，先试着在它界面上点比例。
            #    点不中也没关系，抓图时会按 want_ratio 居中裁
            if want_ratio:
                ok = await try_set_ratio(tab, want_ratio)
                report(
                    f"豆包桥：画幅已切到 {want_ratio}"
                    if ok
                    else f"豆包桥：界面上没找到 {want_ratio}，出图后按比例裁"
                )

            try:
                base_items = await tab.evaluate(GEN_IMAGES_SCRIPT) or []
            except CdpError:
                base_items = []
            baseline = [str(o.get("src") or "") for o in base_items if isinstance(o, dict)]

            # 2) 参考图：塞进页面里那个隐藏的 file input
            if ref_files:
                await upload_files(tab, ref_files)
                report(f"豆包桥：已上传 {len(ref_files)} 张参考图")

            # 3) 打字
            focused = await tab.evaluate(FOCUS_EDITOR_SCRIPT)
            if not focused:
                raise ImageGenError("豆包里找不到输入框，可能页面还没加载好")
            await sleep(TYPE_SETTLE_MS)
            await tab.send("Input.insertText", {"text": text}, tab.session_id)
            await sleep(TYPE_SETTLE_MS)

            # 4) 发送（新版豆包回车不提交，所以还得兜一手点按钮）
            if not await send_prompt(tab):
                raise ImageGenError(
                    "提示词没发出去：豆包输入框还留着文字。可能界面又改版了，把豆包窗口切到前台看一眼前再试"
                )
            report("豆包桥：已发送，等待出图")

            # 5) 等新图出现并稳定
            deadline = now_ms() + GENERATION_TIMEOUT_MS
            fresh: list[dict] = []
            fresh_sig = ""
            last_change = 0
            saw_busy = False
            while now_ms() < deadline:
                await sleep(POLL_STEP_MS)
                try:
                    now_items = await tab.evaluate(GEN_IMAGES_SCRIPT) or []
                except CdpError:
                    now_items = []
                dedup = best_per_image(
                    [o for o in now_items if isinstance(o, dict) and str(o.get("src") or "") not in baseline]
                )
                # 比较的必须是「去重后的地址串」，之前拿对象数组 join 去比，永远不等
                signature = "|".join(str(o.get("src") or "") for o in dedup)
                if signature and signature != fresh_sig:
                    fresh = dedup
                    fresh_sig = signature
                    last_change = now_ms()
                    report(f"豆包桥：看到 {len(dedup)} 张新图")
                try:
                    busy = bool(await tab.evaluate(BUSY_SCRIPT))
                except CdpError:
                    busy = False
                if busy:
                    saw_busy = True
                    last_change = now_ms()
                if fresh and now_ms() - last_change > GENERATION_STABLE_MS:
                    break
                if saw_busy and not busy and fresh and now_ms() - last_change > 2500:
                    break
            if not fresh:
                raise ImageGenError(
                    "豆包没有出图（5 分钟内没检测到新图片）。可能这一轮它只回了文字，换个更明确的提示词试试"
                )

            # 6) 抓图：一轮豆包会给好几个变体，要几张就从这一轮里取几张
            wanted = sorted(
                fresh,
                key=lambda o: int(o.get("w") or 0) * int(o.get("h") or 0),
                reverse=True,
            )
            need = max(1, times - len(results))
            picks = wanted[: min(len(wanted), need)]
            if picks:
                report(
                    f"豆包桥：本轮出 {len(wanted)} 张"
                    f"（{picks[0].get('w') or '?'}x{picks[0].get('h') or '?'}），取 {len(picks)} 张"
                )

            raw_reported = False
            for item in picks:
                try:
                    one = await grab_image(
                        tab, str(item.get("src") or ""), erase_watermark=erase_watermark, ratio=want_ratio
                    )
                    if one.get("cropped"):
                        cropped = one["cropped"]
                        report(
                            f"豆包桥：原图 {'x'.join(str(v) for v in cropped['from'])} 不是 {want_ratio}，"
                            f"已居中裁到 {'x'.join(str(v) for v in cropped['to'])}"
                        )
                    if one.get("cleaned") == "raw" and not raw_reported:
                        raw_reported = True
                        report("豆包桥：已取到无水印原图（image_ori_raw，未做任何涂抹）")
                    if one.get("cleaned") != "raw" and erase_watermark:
                        marked = one.get("marked") or {}
                        report(
                            "豆包桥：这张没拿到无水印原图，已本地抹掉右下角水印"
                            if marked.get("ok")
                            else f"豆包桥：这张没拿到无水印原图，水印也没抹掉（{marked.get('reason') or '未知'}），先按原图给你"
                        )
                    results.append({**one, "source": item.get("src")})
                except (ImageGenError, CdpError) as err:
                    report(f"豆包桥：一张图抓取失败（{str(err)[:80]}）")
            if not results:
                raise ImageGenError("豆包出图了但抓不下来，请稍后重试")
    finally:
        await tab.close()

    return [
        {
            "b64": item["b64"],
            "width": item["width"],
            "height": item["height"],
            "mimeType": item["mimeType"],
            "source": item["source"],
            # 给上层/日志用：raw = 原图直取，erased = 本地抹的，none = 带水印
            "cleaned": item.get("cleaned") or "none",
        }
        for item in results
    ]


# ============================================================ 视频参数面板


def parse_param_summary(text: str) -> dict:
    """把摘要按钮上的「自动 · 10s · 720P」拆成结构化参数（纯函数，可单测）。"""
    out = {"ratio": "", "seconds": 0, "resolution": ""}
    for part in [p.strip() for p in str(text or "").split("·") if p.strip()]:
        sec = re.match(r"^(\d+(?:\.\d+)?)\s*s$", part, re.I)
        if sec:
            out["seconds"] = round(float(sec.group(1)))
            continue
        if re.match(r"^\d+\s*p$", part, re.I):
            out["resolution"] = part.upper()
            continue
        if part == "自动" or re.match(r"^\d{1,2}:\d{1,2}$", part):
            out["ratio"] = part
    return out


async def read_video_options(tab: CdpTab) -> dict:
    """读当前摘要（比例 · 秒数 · 清晰度）"""
    try:
        text = await tab.evaluate(
            r"""(() => {
  const b = %s;
  return b ? String(b.textContent || '') : '';
})()"""
            % VIDEO_PARAM_TRIGGER
        )
    except CdpError:
        text = ""
    return parse_param_summary(str(text or ""))


async def open_video_options(tab: CdpTab) -> bool:
    """展开参数面板；已经展开就直接返回 True。试三次（React 重渲染后触发器可能还没挂回来）。"""
    for _ in range(3):
        try:
            if await tab.evaluate(VIDEO_PANEL_SCRIPT):
                return True
        except CdpError:
            pass
        try:
            sent = bool(await tab.evaluate(soft_open_script(VIDEO_PARAM_TRIGGER)))
        except CdpError:
            sent = False
        if not sent:
            await sleep(450)
            continue
        await sleep(700)
    try:
        return bool(await tab.evaluate(VIDEO_PANEL_SCRIPT))
    except CdpError:
        return False


async def close_video_options(tab: CdpTab) -> None:
    await press_key(tab, "Escape")
    await sleep(200)
    # 有些版本一次 Escape 只关一层（菜单套菜单），关不干净下一次就点不开
    try:
        still = bool(await tab.evaluate(VIDEO_PANEL_SCRIPT))
    except CdpError:
        still = False
    if still:
        await press_key(tab, "Escape")
        await sleep(250)


async def pick_panel_button(tab: CdpTab, label: str) -> bool:
    """面板里按文字点一个选项（比例 / 清晰度），返回有没有点中"""
    expr = soft_click_script(
        "[...(%s || document).querySelectorAll('button')]"
        ".find((b) => (b.textContent || '').trim() === %s)"
        % (VIDEO_PANEL_ANY, json.dumps(label, ensure_ascii=False))
    )
    try:
        clicked = bool(await tab.evaluate(expr))
    except CdpError:
        clicked = False
    if clicked:
        await sleep(420)
    return clicked


async def set_video_seconds(tab: CdpTab, seconds: int) -> dict | None:
    """拖时长滑块。滑块是 Radix Slider：用键盘而不是拖鼠标 —— Home/End 一步到位。"""
    expr = (
        r"""(() => {
  const panel = %s;
  const thumb = panel && panel.querySelector('[role=slider]');
  if (!thumb) return null;
  thumb.focus();
  const ticks = (panel.textContent.match(/(\d+)s/g) || [])
    .map((s) => parseInt(s, 10)).filter((n) => n > 0);
  if (!ticks.length) return null;
  return { lo: Math.min(...ticks), hi: Math.max(...ticks) };
})()"""
        % VIDEO_PANEL_ANY
    )
    try:
        info = await tab.evaluate(expr)
    except CdpError:
        info = None
    if not info:
        return None

    asked = round(float(seconds or 0))
    lo, hi = int(info["lo"]), int(info["hi"])
    want = max(lo, min(hi, asked))
    # 从更近的那一端走，按键次数最少（30 秒档最多也就是 13 下）
    from_low = want - lo
    from_high = hi - want
    if from_low <= from_high:
        await press_key(tab, "Home")
        for _ in range(from_low):
            await press_key(tab, "ArrowRight")
    else:
        await press_key(tab, "End")
        for _ in range(from_high):
            await press_key(tab, "ArrowLeft")
    await sleep(250)
    return {"seconds": want, "clamped": want != asked, "lo": lo, "hi": hi}


async def set_video_tier(tab: CdpTab, tier: str) -> str:
    """切豆包内部的模型档（可选）。返回切成了哪一项（没切返回空串）。"""
    key = str(tier or "").strip().lower()
    pattern = TIER_PATTERNS.get(key)
    if not pattern:
        return ""

    def model_text() -> str:
        return r"""(() => {
  const b = %s;
  return b ? String(b.textContent || '') : '';
})()""" % VIDEO_MODEL_TRIGGER

    try:
        current = str(await tab.evaluate(model_text()) or "")
    except CdpError:
        current = ""
    if re.search(pattern, current, re.I):
        return ""  # 已经是这一档，点了反而多余

    try:
        opened = bool(await tab.evaluate(soft_open_script(VIDEO_MODEL_TRIGGER)))
    except CdpError:
        opened = False
    if not opened:
        return ""
    await sleep(650)

    # 菜单项是真实的下拉项：先试指针事件，不关菜单就退回真实鼠标点
    pick_expr = (
        r"""(() => {
  const hit = [...document.querySelectorAll('[role=menu][data-state=open] [role=menuitem]')]
    .find((el) => new RegExp(%s, 'i').test(el.textContent || ''));
  if (!hit) return null;
  const r = hit.getBoundingClientRect();
  return { text: String(hit.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24), x: r.x + r.width / 2, y: r.y + r.height / 2 };
})()"""
        % json.dumps(pattern, ensure_ascii=False)
    )
    try:
        picked = await tab.evaluate(pick_expr)
    except CdpError:
        picked = None
    if not picked:
        await press_key(tab, "Escape")
        return ""
    await tap_point(tab, picked)
    # 切档后整块输入区会重渲染（比例 / 清晰度 / 时长上限都跟着变），等它换完再动参数
    for _ in range(4):
        await sleep(450)
        try:
            now = str(await tab.evaluate(model_text()) or "")
        except CdpError:
            now = ""
        if re.search(pattern, now, re.I):
            break
    # 菜单没关说明没选中，补一次 Escape，别让它挡着后面的发送
    try:
        still_open = bool(await tab.evaluate("Boolean(document.querySelector('[role=menu][data-state=open]'))"))
    except CdpError:
        still_open = False
    if still_open:
        await press_key(tab, "Escape")
        await sleep(350)
    return str(picked.get("text") or "")


async def apply_video_options(
    tab: CdpTab, want: dict | None = None, report: Callable[[str], None] = lambda _m: None
) -> dict | None:
    """把用户选的参数真正设进豆包面板：模型档 → 比例 → 清晰度 → 时长（顺序有讲究）。

    返回**实际生效**的参数（读摘要按钮上的字），设不上返回 None ——
    调用方据此在进度里说实话（而不是嘴上说 15 秒、出来 10 秒）。
    """
    want = want or {}
    ratio = str(want.get("ratio") or "").strip()
    resolution = str(want.get("resolution") or "").strip().upper()
    seconds = round(float(want.get("seconds") or 0))
    tier = str(want.get("tier") or "").strip()
    if not ratio and not resolution and not seconds and not tier:
        return None

    try:
        # 参数按钮只在「视频生成」模式下才渲染（切回普通对话就没了）
        try:
            mode = await tab.evaluate(VIDEO_MODE_SCRIPT)
        except CdpError:
            mode = None
        if not (mode and mode.get("ok")):
            await enter_video_mode(tab, report)
            await sleep(900)
        if tier:
            picked = await set_video_tier(tab, tier)
            if picked:
                report(f"豆包桥：模型档已切到 {picked.split('·')[0].strip()}")
            # 切档后输入区整块重建，上一轮的下拉可能还挂着。这里清一遍场
            try:
                await close_video_options(tab)
            except CdpError:
                pass
            await sleep(600)
        if not await open_video_options(tab):
            report("豆包桥：参数面板没打开，时长 / 清晰度按豆包当前的设置走")
            return None
        try:
            if ratio and not await pick_panel_button(tab, ratio):
                report(f"豆包桥：比例「{ratio}」没找到，保持原样")
            if resolution and not await pick_panel_button(tab, resolution):
                report(f"豆包桥：清晰度「{resolution}」没找到，保持原样")
            if seconds:
                dur = await set_video_seconds(tab, seconds)
                if not dur:
                    report("豆包桥：时长滑块没找到，按豆包默认时长")
                elif dur.get("clamped"):
                    report(f"豆包桥：这一档最长 {dur['hi']} 秒，已按 {dur['seconds']} 秒生成")
            applied = await read_video_options(tab)
            report(
                f"豆包桥：参数 {applied.get('ratio') or '自动'} · "
                f"{applied.get('seconds') or '?'}s · {applied.get('resolution') or '默认'}"
            )
            return applied
        finally:
            try:
                await close_video_options(tab)
            except CdpError:
                pass
    except (CdpError, ImageGenError) as err:
        # 参数是加分项：设不上就按豆包当前设置出片，别因此让整条任务失败
        report(f"豆包桥：参数没设上（{err}），按豆包当前设置生成")
        return None


# ============================================================ 视频：提交 / 查询 / 取回

#: 已经替它回过「确认」的任务，别每轮都回一遍
_answered_asks: set[str] = set()
#: 钩子丢了之后已经刷过一次页面的任务（刷新一次就够，别轮轮刷）
_hook_revived: set[str] = set()


def sig_of(value: Any) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:12]


def encode_video_task(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return VIDEO_TASK_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_video_task(task_id: str) -> dict | None:
    raw = str(task_id or "")
    legacy = raw.startswith(VIDEO_TASK_PREFIX_V1)
    head = VIDEO_TASK_PREFIX_V1 if legacy else VIDEO_TASK_PREFIX
    if not raw.startswith(head):
        return None
    body = raw[len(head) :]
    padding = "=" * (-len(body) % 4)
    try:
        info = json.loads(base64.urlsafe_b64decode(body + padding).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    # dbv1 的 b 存的是页面 video 元素地址的 sha1，dbv2 存 vid。两套不能混着比
    return {**info, "legacy": legacy}


async def submit_video_with_doubao(
    *,
    prompt: str,
    image_file: str = "",
    end_image_file: str = "",
    options: dict | None = None,
    endpoint: str = DOUBAO_DEFAULT_ENDPOINT,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """提交一次视频生成。只做到「豆包接了活」就返回，剩下的交给轮询。

    生成要好几分钟（界面提示「预计等待 5 分钟」），卡在这里会把整条视频队列堵死。
    """
    text = str(prompt or "").strip()
    if not text:
        raise ImageGenError("提示词不能为空")

    def report(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:  # noqa: BLE001
                pass

    tab = await require_tab(endpoint)
    try:
        await wait_page_ready(tab)

        # 1) 新对话：不让上一轮的上下文影响这一轮
        try:
            await tab.evaluate(click_by_text_script("新对话"))
        except CdpError:
            pass
        await sleep(NEW_CHAT_SETTLE_MS)

        # 2) 先下钩子再记基线：钩子得赶在会话接口返回前装上，否则这一轮的视频漏收。
        #    用 ensure_video_hook 而不是裸 evaluate —— 它会把钩子注册成文档级脚本。
        try:
            await ensure_video_hook(tab)
        except CdpError:
            pass
        baseline = [str(v.get("vid") or "") for v in await read_video_list(tab)]
        # 额度提示的次数基线：换号后旧对话里那句「今日额度已用完」还在页面上
        try:
            quota_base = int(await tab.evaluate(VIDEO_QUOTA_COUNT_SCRIPT) or 0)
        except (CdpError, TypeError, ValueError):
            quota_base = 0

        # 3) 切「视频生成」模式。**这一步失败就必须报错** ——
        #    不在视频模式下，豆包会把提示词改写成一段「拿去生成视频用」的文字回给你，
        #    页面上永远等不到片子，任务只能等到 15 分钟超时。
        if not await enter_video_mode(tab, report):
            raise ImageGenError(
                "豆包没进「视频生成」模式（输入区上方没出现「视频生成」标签）。"
                "请在豆包里手动点一次输入区左边的「更多技能 → 视频生成」，再重试这条任务"
            )

        # 4) 参数：模型档 → 比例 → 清晰度 → 时长，直接点面板。
        #    必须在上传图之前：上传会弹安全确认，弹窗一开面板就点不动了。
        applied = await apply_video_options(tab, options or {}, report)

        # 5) 首帧图（图生视频）：上传要在切模式之后，否则切模式会把附件清掉。
        files = [p for p in (image_file, end_image_file) if p and os.path.exists(p)]
        if files:
            await upload_files(tab, files)
            report("豆包桥：首帧图 + 目标画面已上传" if len(files) > 1 else "豆包桥：首帧图已上传")
            # 传了素材就会弹「安全确认」，不点它后面全白等
            await confirm_safety(tab, report)

        # 6) 打字并发送
        focused = await tab.evaluate(FOCUS_EDITOR_SCRIPT)
        if not focused:
            raise ImageGenError("豆包里找不到输入框，可能页面还没加载好")
        await sleep(TYPE_SETTLE_MS)
        await tab.send("Input.insertText", {"text": text}, tab.session_id)
        await sleep(TYPE_SETTLE_MS)
        if not await send_prompt(tab):
            raise ImageGenError("提示词没发出去：豆包输入框还留着文字（界面可能改版了，先手动看一眼前台窗口再试）")
        report("豆包桥：已发送，等待豆包出片")

        # 7) 确认它真的接了活
        deadline = now_ms() + VIDEO_KICKOFF_MS
        kicked = False
        why = ""
        nudges = 0
        while now_ms() < deadline:
            await sleep(POLL_STEP_MS)
            # 有些版本是发出去之后才弹安全确认，所以每一轮都顺手点一次
            await confirm_safety(tab, report)
            try:
                ask = await tab.evaluate(VIDEO_CONFIRM_ASK_SCRIPT) or ""
            except CdpError:
                ask = ""
            # 它要是回头问「确认后再开始」，替用户答一句就开工（最多答两次）
            if ask == "ask" and nudges < 2:
                nudges += 1
                if await answer_confirm_ask(tab, report):
                    continue
            try:
                state = await tab.evaluate(VIDEO_STATE_SCRIPT) or ""
            except CdpError:
                state = ""
            if state in ("quota", "fail"):
                if state == "quota":
                    try:
                        now_quota = int(await tab.evaluate(VIDEO_QUOTA_COUNT_SCRIPT) or 0)
                    except (CdpError, TypeError, ValueError):
                        now_quota = 0
                    if now_quota > quota_base:
                        why = state
                        break
                else:
                    why = state
                    break
            listed = await read_video_list(tab)
            try:
                dom_videos = await tab.evaluate(GEN_VIDEOS_SCRIPT) or []
            except CdpError:
                dom_videos = []
            if state == "busy" or len(listed) > len(baseline) or dom_videos:
                kicked = True
                break
        if not kicked:
            if why == "quota":
                # 额度不足是「换号就能救」的唯一一类，单独打标记让上层去轮换账号
                err = ImageGenError("豆包的免费额度用完了（这个账号今天没次数了），可以换一个豆包账号继续")
                setattr(err, "quota", True)
                setattr(err, "endpoint", endpoint)
                raise err
            raise ImageGenError(
                "豆包拒绝了这次生成（内容违规 / 不支持），具体原因请看豆包窗口里的提示"
                if why == "fail"
                else (
                    "豆包没有开始生成视频（20 秒内既没进入生成状态、也没出片）。"
                    "多半是豆包没登录，或者「视频生成」模式没切上 —— 请在豆包窗口里看一眼再重试"
                )
            )
        report("豆包桥：生成已开始")

        return {
            "remoteTaskId": encode_video_task({"ep": endpoint, "t": now_ms(), "b": baseline, "q": quota_base}),
            "applied": applied,
        }
    finally:
        await tab.close()


async def query_video_with_doubao(
    *, endpoint: str = DOUBAO_DEFAULT_ENDPOINT, remote_task_id: str
) -> dict:
    """查一次：接口钩子有没有收到「提交时还不存在」的视频。

    只看 DOM 是不够的 —— 新版豆包出片后卡片只放一张封面图，`<video>` 要点播放才创建。
    连不上豆包时返回 running 而不是 failed —— 豆包可能被关掉了，用户开回来还能接着等。
    """
    info = decode_video_task(remote_task_id)
    if not info:
        raise ImageGenError("豆包视频任务标识无效，请重新提交这条任务")

    try:
        tab = await require_tab(str(info.get("ep") or endpoint))
    except CdpError as err:
        if err.bridge_down:
            return {"status": "running", "videoUrl": "", "error": "豆包没开着调试端口，等它开回来继续等", "raw": "豆包未开"}
        raise

    try:
        # 钩子丢了（豆包被重载 / 换过文档）就重装。重装之后这份文档不会再补发旧响应，
        # 所以再刷一次页面，让会话接口重新跑一遍。每个任务最多刷一次
        try:
            alive = await ensure_video_hook(tab)
        except CdpError:
            alive = True
        if not alive and remote_task_id not in _hook_revived:
            _hook_revived.add(remote_task_id)
            try:
                await tab.send("Page.reload", {}, tab.session_id)
            except CdpError:
                pass
            await sleep(8000)

        # 图生视频的「安全确认」、以及它回头问的那句「确认后再开始」，都可能拖到轮询阶段才出现
        await confirm_safety(tab, lambda _m: None)
        if remote_task_id not in _answered_asks and now_ms() - int(info.get("t") or 0) < 3 * 60 * 1000:
            try:
                ask = await tab.evaluate(VIDEO_CONFIRM_ASK_SCRIPT) or ""
            except CdpError:
                ask = ""
            if ask == "ask":
                _answered_asks.add(remote_task_id)
                try:
                    await answer_confirm_ask(tab, lambda _m: None)
                except CdpError:
                    pass

        base = set(info.get("b") or [])
        hooked = [] if info.get("legacy") else await read_video_list(tab)
        # 除了「不在基线里」，还要求它是提交之后才生成的 ——
        # 这里必须用视频自己的出生时间（url 里的 l=），不能用钩子的 ts：
        # 页面一刷新，会话接口会把全部历史视频重新吐一遍，钩子给它们全打上「现在」的 ts。
        # 两套时钟要分开留余量：born 是服务端时间（秒级），给 2 分钟宽限；ts 是本机的，只留 5 秒
        since = int(info.get("t") or 0) - 5000
        born_since = int(info.get("t") or 0) - 120000
        fresh_vids = []
        for v in hooked:
            vid = v.get("vid")
            if not vid or vid in base:
                continue
            born = int(v.get("born") or 0)
            if born:
                if born >= born_since:
                    fresh_vids.append(v)
            elif int(v.get("ts") or 0) >= since:
                fresh_vids.append(v)
        if fresh_vids:
            # 多段候选时取出生时间最新的那段，而不是列表里最后出现的那个
            pick = max(
                fresh_vids,
                key=lambda v: int(v.get("born") or 0) or int(v.get("ts") or 0),
            )
            return {
                "status": "success",
                # 直链自带签名，Python 侧能直接下，不用再经 CDP 传 base64
                "videoUrl": f"{DOUBAO_VIDEO_URL_PREFIX}{pick['url']}",
                "error": "",
                "raw": f"已出片 {pick['definition']}" if pick.get("definition") else "已出片",
            }

        # 兜底：只给老任务（dbv1. 里存的是 video 元素地址的 sha1）用
        if info.get("legacy"):
            try:
                dom_list = await tab.evaluate(GEN_VIDEOS_SCRIPT) or []
            except CdpError:
                dom_list = []
            fresh_dom = [o for o in dom_list if sig_of(o.get("src")) not in base]
            if fresh_dom:
                src = str(fresh_dom[-1].get("src") or "")
                video_url = f"{DOUBAO_VIDEO_URL_PREFIX}{src}" if src.startswith("blob:") else src
                return {"status": "success", "videoUrl": video_url, "error": "", "raw": "已出片"}

        if now_ms() - int(info.get("t") or 0) > VIDEO_TIMEOUT_MS:
            return {
                "status": "failed",
                "videoUrl": "",
                "error": "豆包 15 分钟没出片，这次生成多半失败了",
                "raw": "超时",
            }

        try:
            state = await tab.evaluate(VIDEO_STATE_SCRIPT) or ""
        except CdpError:
            state = ""
        if state == "quota":
            # 只有「提交之后新冒出来的额度提示」才算这次真没额度
            try:
                now_quota = int(await tab.evaluate(VIDEO_QUOTA_COUNT_SCRIPT) or 0)
            except (CdpError, TypeError, ValueError):
                now_quota = 0
            if now_quota > int(info.get("q") or 0):
                return {
                    "status": "failed",
                    "videoUrl": "",
                    "error": "豆包的免费额度用完了（这个账号今天没次数了），可以换一个豆包账号继续",
                    "quota": True,
                    "raw": "额度不足",
                }
            # 是历史残留：当没看见，继续等
        if state == "fail":
            return {
                "status": "failed",
                "videoUrl": "",
                "error": "豆包那边报错了（内容违规 / 不支持），具体原因看豆包窗口里的提示",
                "raw": "失败",
            }
        return {
            "status": "running",
            "videoUrl": "",
            "error": "" if state == "busy" else "还没看到视频，继续等",
            # raw 会直接显示在任务列表上，给中文比给 generating / pending 好认
            "raw": "生成中" if state == "busy" else "等待中",
        }
    finally:
        await tab.close()


async def fetch_video_with_doubao(
    *, endpoint: str = DOUBAO_DEFAULT_ENDPOINT, src: str
) -> dict:
    """把页面里的视频取回本地，返回 {buffer, ext}。

    主路径：页面里 fetch(blob) → base64 → 分片传回（和抓图同一套路）。
    兜底：blob 是 MediaSource 那种 fetch 不到时，改用 `<a download>` + CDP 下载目录落盘。
    """
    target = str(src or "")
    if not target:
        raise ImageGenError("没有视频地址")

    # 接口给的直链（v26-vdl.doubao.com/...mp4）自带签名，Python 侧直接下就行
    if re.match(r"^https?:", target, re.I):
        try:
            async with httpx.AsyncClient(timeout=VIDEO_DOWNLOAD_WAIT_MS / 1000, follow_redirects=True) as client:
                res = await client.get(target)
            if res.status_code >= 400:
                raise ImageGenError(f"下载视频失败：HTTP {res.status_code}")
            if res.content:
                return {"buffer": res.content, "ext": ".mp4"}
        except ImageGenError:
            raise
        except Exception:  # noqa: BLE001 - 下不到就退回页面内取
            pass

    tab = await require_tab(endpoint)
    try:
        meta = await tab.evaluate(
            r"""(() => {
  const src = %s;
  const busy = document.createElement('a');
  return { ok: true, len: 0, src };
})()"""
            % json.dumps(target, ensure_ascii=False)
        )
        # 页面里 fetch → base64 → 存全局，再分片取（一次性回传几 MB 会把 CDP 打死）
        try:
            grabbed = await tab.evaluate(
                r"""(async () => {
  try {
    const r = await fetch(%s);
    if (!r.ok) return { error: 'HTTP ' + r.status };
    const b = await r.blob();
    const bytes = new Uint8Array(await b.arrayBuffer());
    let bin = '';
    for (let k = 0; k < bytes.length; k += 8192) {
      bin += String.fromCharCode.apply(null, bytes.subarray(k, k + 8192));
    }
    window.__ncGrabVideo = btoa(bin);
    return { len: window.__ncGrabVideo.length, size: b.size, type: b.type || 'video/mp4' };
  } catch (e) {
    return { error: String((e && e.message) || e).slice(0, 200) };
  }
})()"""
                % json.dumps(target, ensure_ascii=False)
            )
        except CdpError as err:
            grabbed = {"error": str(err)}
        if not isinstance(grabbed, dict) or grabbed.get("error"):
            return await _download_video_via_browser(tab, target, (meta or {}).get("error"))

        total = int(grabbed.get("len") or 0)
        parts: list[str] = []
        offset = 0
        while offset < total:
            end = min(offset + GRAB_CHUNK, total)
            piece = await tab.evaluate(f"window.__ncGrabVideo.slice({offset}, {end})")
            if not isinstance(piece, str) or len(piece) != end - offset:
                raise ImageGenError("视频取不回来（分片数据不完整）")
            parts.append(piece)
            offset = end
        try:
            await tab.evaluate("window.__ncGrabVideo = null; 1")
        except CdpError:
            pass
        data = "".join(parts)
        if not data:
            # 兜底：借浏览器的下载通道把 blob 落盘
            return await _download_video_via_browser(tab, target, (grabbed or {}).get("error"))
        return {"buffer": base64.b64decode(data), "ext": ".mp4"}
    finally:
        await tab.close()


async def _download_video_via_browser(tab: CdpTab, src: str, reason: str | None) -> dict:
    """兜底取片：让浏览器把 blob 下到指定目录，再从磁盘读。

    只在页面内 fetch 拿不到时走这里（比如视频是 MediaSource 流，fetch 直接报错）。
    """
    tmp_root = Path(tempfile.gettempdir()) / "rh-canvas-studio"
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmp_root = Path.cwd()
    download_dir = tmp_root / f"nc-doubao-video-{now_ms()}"
    download_dir.mkdir(parents=True, exist_ok=True)
    try:
        await tab.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir), "eventsEnabled": True},
            tab.session_id,
        )
        await tab.evaluate(
            r"""(() => {
  const a = document.createElement('a');
  a.href = %s;
  a.download = 'doubao-video.mp4';
  document.body.appendChild(a);
  a.click();
  a.remove();
  return 1;
})()"""
            % json.dumps(src, ensure_ascii=False)
        )

        # 等文件出现且大小不再变化
        deadline = now_ms() + VIDEO_DOWNLOAD_WAIT_MS
        last = -1
        stable = 0
        while now_ms() < deadline:
            await sleep(1000)
            files = [f for f in download_dir.iterdir() if not f.name.endswith(".crdownload")]
            if not files:
                continue
            file = files[0]
            size = file.stat().st_size
            if size > 0 and size == last:
                stable += 1
                if stable >= 2:
                    return {"buffer": file.read_bytes(), "ext": file.suffix or ".mp4"}
            else:
                stable = 0
                last = size
        raise ImageGenError(
            f"视频取不回来（页面内读取失败：{reason or '未知'}，下载通道也没拿到文件）"
        )
    finally:
        try:
            for f in download_dir.iterdir():
                f.unlink()
            download_dir.rmdir()
        except OSError:
            pass
