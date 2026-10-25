"""手写标注层：周页面右下角的笔记工具栏 + 盖在正文上的笔迹图层（CSS 与 JS 字符串，由 html_parts.page 嵌进每个周页面）。

用法（给讲的人）：iPad Safari 上打开周页面，点右下角「✎ 笔记」→「标注 开」，用 Apple Pencil 写（手指由浏览器原生滚动，带惯性），
每一笔自动存在本机；点「导出」得到 <页面名>_notes_<日期>.json，AirDrop 到 Mac；Mac 上打开同一页 →「导入」选这个文件，
投屏时笔迹显示在页面上。标注关掉时页面一切照常（点击、播放视频、展开折叠框），笔迹仍显示但不拦截点击。

设计要点：
- 版式固定：有笔迹或标注打开时，正文卡片固定 920px 宽（桌面版宽度），并把窄屏 media query 改过的字号、内边距改回桌面值；
  窗口放不下目录就隐藏目录，再放不下就用 CSS zoom 等比缩小整列。这样各设备上正文排版一致。
- 锚点跟内容走（参考 W3C Web Annotation 的 TextQuoteSelector、Hypothesis 的 text-quote 锚定）：每一笔按起笔处的内容存锚点——
  图片/视频按 src 文件名、陈列公式按 TeX 源码（这两种坐标按元素宽高归一化）、表格按单元格文字 + 表头文字、标题按标题文字、
  正文按起笔处最近的一个字（该字 + 前后各 30 字原文，外加块内字符偏移作后备），坐标存相对该字/该元素左上角的偏移。
  还原时先精确匹配，再模糊匹配，最后块内偏移；都找不到的笔隐藏并计数（任何页面都不显示灰色笔），数据不删。
- 画的与匹配的分开存：每笔的 drawn = 画完（或用户移动）那一刻的原样（页面版本号、pts、frame、anchor、orig、设备 UA、时间），
  之后任何代码都不改它；anchor / frame / orig / pts 是匹配结果（迁移、改名、设备上算的圈住内容），可以重算。
- 笔迹画在正文卡片 <main> 里的一个 SVG 图层上，坐标单位是未缩放的 CSS px（正文卡片内坐标），所以 zoom 不影响存储。
- 打印时把每一笔挪进它所在的块（段落、图、表格），这样分页后笔迹仍跟着内容。
不依赖任何外部库；只用 iPad Safari 支持的 Pointer Events、Blob + a[download]、localStorage（读写都包 try/catch）。
"""

import hashlib
import json
import re
from pathlib import Path

from .config import DRAFTS

# 页面版本号：<script>window.NIPC_PAGE = "…";</script>，插在页面第一个 <script 之前（标注层读 window.NIPC_PAGE）
VERSION_TAG = re.compile(r'<script>window\.NIPC_PAGE = "[0-9a-f]*";</script>\n')


def page_version(html):
    """页面版本号 = 去掉版本号标签后整页 HTML 的 sha256 前 12 位（老页面没有标签，就是整页的哈希）。
    笔画下时写进 drawn.page，服务器按它认出笔是在哪一版页面上画的。"""
    return hashlib.sha256(VERSION_TAG.sub("", html).encode("utf-8")).hexdigest()[:12]


def version_tag(version):
    return f'<script>window.NIPC_PAGE = "{version}";</script>\n'


def inject_before_scripts(html, snippet):
    """把 snippet 插到页面第一个 <script 之前（页面没有 <head>，标注层脚本在后面，先设好的全局量它才读得到）。"""
    i = html.find("<script")
    assert i >= 0, "页面里没有 <script>"
    return html[:i] + snippet + html[i:]


def with_page_version(html):
    """给带标注层的页面嵌入版本号（已有的旧标签先去掉）。"""
    html = VERSION_TAG.sub("", html)
    return inject_before_scripts(html, version_tag(page_version(html)))


def renames_script(page_file):
    """记号改名表，嵌成 <script type="application/json" id="annot-renames">，供笔记把旧写法的锚点迁到新写法。
    读讲稿目录里的 notation_renames_<页名>.json 的 "tex"（{旧 TeX: 新 TeX}）与 "text"（{旧文字: 新文字}），由改讲稿的人维护
    （文件里的 "quote" 逐笔对应表不再使用）；没有这个文件时用空表。文件不是合法 JSON 时直接报错停止生成。"""
    path = DRAFTS / f"notation_renames_{Path(page_file).stem}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    table = {k: {o: n for o, n in (data.get(k) or {}).items() if isinstance(o, str) and isinstance(n, str)}
             for k in ("tex", "text")}
    body = json.dumps(table, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="annot-renames">{body}</script>\n'


ANNOT_CSS = """
/* ===== 手写标注：版式固定（有笔迹或标注打开时，各设备正文排版一致） ===== */
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
html.annot-fixed .layout { grid-template-columns: 245px 920px; gap: 50px; padding: 45px 32px 0; display: grid; }
html.annot-fixed .layout.single { grid-template-columns: 920px; }
html.annot-fixed main { width: 920px; padding: 44px 54px 24px; border-radius: 12px; }
html.annot-fixed body { font-size: 16px; }
html.annot-fixed h1 { font-size: 31px; }
html.annot-fixed h2 { font-size: 24px; margin-top: 56px; }
html.annot-fixed h3 { font-size: 19px; }
html.annot-fixed header.top { margin-bottom: 42px; }
html.annot-fixed td, html.annot-fixed th { padding: 11px 14px; }
html.annot-fixed .eyebrow { font-size: 10px; letter-spacing: .17em; }
html.annot-fixed article figcaption { padding: 12px 18px; }
html.annot-fixed article .lead { padding: 8px 20px; }
html.annot-fixed article details { padding: 16px 18px; }
/* 窗口放不下「目录 + 正文」：隐藏目录，正文居中；更窄时 JS 给 main 设 zoom 等比缩小 */
html.annot-noaside .layout { display: block; padding: 45px 16px 0; }
html.annot-noaside aside { display: none; }
html.annot-noaside main { margin: 0 auto; }

/* ===== 笔迹图层与输入层 ===== */
#annot-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; z-index: 5; }
#annot-layer path.hl, svg.annot-print path.hl { mix-blend-mode: multiply; }
#annot-layer path.orphan { display: none; }   /* 圈的内容改了 / 删了：不留标记（用户 10-25），数据仍保留 */
#annot-capture { position: fixed; inset: 0; z-index: 45; display: none; touch-action: pan-x pan-y; cursor: crosshair;
                 -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
html.annot-on #annot-capture { display: block; }
#annot-capture.probe { visibility: hidden; }

/* ===== 工具栏（右下角，可收成一个小按钮） ===== */
.annot-ui { position: fixed; right: 16px; bottom: 16px; z-index: 50; font-size: 13px; line-height: 1.4; color: var(--ink);
            -webkit-user-select: none; user-select: none; }
.annot-ui button { font: inherit; min-height: 36px; padding: 4px 10px; border: 1px solid var(--line); border-radius: 7px;
                   background: #fff; color: var(--ink); cursor: pointer; touch-action: manipulation; }
.annot-ui button[aria-pressed="true"] { background: #eaf1fb; border-color: var(--accent); color: var(--accent); font-weight: 600; }
#annot-fab { box-shadow: 0 4px 16px #1b2a4026; border-radius: 20px; padding: 6px 14px; }
.annot-ui[data-collapsed="1"] #annot-panel, .annot-ui:not([data-collapsed="1"]) #annot-fab { display: none; }
#annot-panel { background: #fffffff2; border: 1px solid var(--line); border-radius: 12px; padding: 10px; width: 300px;
               box-shadow: 0 6px 26px #1b2a402e; backdrop-filter: blur(8px); }
.annot-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.annot-row:last-of-type { margin-bottom: 0; }
.annot-ui button.annot-swatch { width: 36px; padding: 0; }
.annot-swatch::before { content: ""; display: block; width: 16px; height: 16px; margin: auto; border-radius: 50%; background: var(--c); }
.annot-ui button.annot-swatch.hl::before { border-radius: 3px; width: 20px; height: 12px; opacity: .6; }
#annot-status { font-size: 12px; color: var(--muted); margin-top: 6px; }
#annot-status .warn { color: var(--warn); }
#annot-choice { margin-top: 8px; padding: 8px; background: var(--soft); border-radius: 8px; }
#annot-choice p { margin: 0 0 6px; }
#annot-sync-panel { margin-top: 8px; padding: 8px; background: var(--soft); border-radius: 8px; }
#annot-sync-panel label { display: block; margin-bottom: 6px; font-size: 12px; color: var(--muted); }
#annot-sync-panel input { display: block; width: 100%; font: inherit; font-size: 13px; padding: 6px 8px; margin-top: 2px;
                          border: 1px solid var(--line); border-radius: 6px; color: var(--ink); background: #fff; }
/* 只读快照页：不能画、不能改设置，只有「确认位置并保存」和导出 */
.annot-ui:not(.snap) #annot-confirm, .annot-ui.snap #annot-toggle, .annot-ui.snap #annot-tools,
.annot-ui.snap #annot-undo, .annot-ui.snap #annot-clear, .annot-ui.snap #annot-import, .annot-ui.snap #annot-sync { display: none; }
#annot-file { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }

/* ===== 打印 / 生成 PDF：笔迹跟着各自的块走，正文保持 920px 排版再整体缩到纸宽 ===== */
@media print {
  .annot-ui, #annot-capture { display: none !important; }
  html.annot-printing #annot-layer { display: none; }
  .annot-host { position: relative; }
  svg.annot-print { position: absolute; width: 1px; height: 1px; overflow: visible; pointer-events: none; z-index: 5; }
  html.annot-fixed body { font-size: 16px; }
  html.annot-fixed main { width: 920px; padding: 44px 54px 24px; zoom: .76; }
  html.annot-fixed pre, html.annot-fixed mjx-container[display="true"] { overflow: visible; }
}
"""

# 只读探查接口（并进 window.__nipcNotes；repair_notes_positions --apply-snapshot 也把它原样补进快照页里的旧标注层）：
#   contentIndex()：正文文字索引（标注层 buildIndex 的同一串）+ 按文档顺序的全部公式（TeX、陈列与否、符号序列、公式在文字索引里的位置）+ 图 / 视频 src；
#   where(ids)：每笔此刻挂在哪——正文第几个字 / 第几个公式的第几个符号 / 第几个公式整块 / 哪张图（第几个同名），及显示状态。
PROBE_API_JS = r"""
      // 正文里全部公式（陈列 + 行内），按文档先后：[{el, tex, display}]
      mathsInOrder() {
        let out = [];
        try {
          out = [...MathJax.startup.document.math].filter(m => m.typesetRoot && article.contains(m.typesetRoot))
            .map(m => ({ el: m.typesetRoot, tex: norm(m.math), display: !!m.display }));
        } catch (e) { /* MathJax 没加载 */ }
        return out.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
      },
      contentIndex() {
        const ix = ensureIndex();
        return { text: ix.text, maths: this.mathsInOrder().map(m => ({ tex: m.tex, display: !!m.display, ch: glyphList(m.el).ch, pos: posFromElement(m.el) })),
                 media: [...article.querySelectorAll('img, video')].map(srcKey) };
      },
      where(ids) {
        ensureIndex();
        const f = frame(), M = this.mathsInOrder(), media = [...article.querySelectorAll('img, video')];
        return Object.fromEntries(ids.map(id => {
          const s = S.strokes.find(t => t.id === id);
          if (!s) return [id, null];
          if (!S.cache.get(id)) place(s, f);
          const c = S.cache.get(id), t = c && c.target, d = S.drawn.get(id), state = d ? d.state : 'none';
          if (!t) return [id, { state }];
          if (t.el === main) return [id, { state, kind: 'page' }];
          if (t.pos !== undefined) return [id, { state, kind: 'text', pos: t.pos }];
          const k = M.findIndex(m => m.el === t.el), glyph = t.glyph || c.glyph;
          if (k >= 0) return [id, { state, kind: glyph ? 'glyph' : 'math', math: k, gi: glyph ? glyphList(t.el).els.indexOf(glyph) : -1 }];
          const mi = media.indexOf(t.el);
          if (mi >= 0) return [id, { state, kind: 'media', src: srcKey(t.el), nth: media.slice(0, mi).filter(e => srcKey(e) === srcKey(t.el)).length }];
          return [id, { state, kind: 'other' }];
        }));
      },
"""

ANNOT_JS = r"""
(() => {
  'use strict';
  const main = document.querySelector('main');
  const article = main && main.querySelector('article');
  if (!article) return;

  /* ---------- 1. 常量 ---------- */
  const CFG = {
    version: 1,
    colWidth: 920, asideWidth: 245, gap: 50, sidePad: 32, narrowPad: 16,   // 与 html_parts.PAGE_CSS 的桌面版式一致
    quoteLen: 30,        // TextQuote 前后各存多少字
    glyphLen: 8,         // 公式锚点：被锚符号前后各存几个符号
    nearPx: 30,          // 笔没圈住任何内容（下划线、箭头、旁注）时，取离笔画这么近以内最近的那个字 / 符号
    coverCtxAccept: 0.3, // 圈住的关键词在页面上出现好几处时，周围文字与原段落的二元组相似度至少这么高才认
    coverNearAccept: 0.8, // ……或者紧挨着它的前 / 后 10 字与原来的相似度至少这么高（段落别处改了、这一句没改）
    penWidth: 2.4, hlWidth: 16, eraseRadius: 12, undoLimit: 60,
    colors: { red: '#d62828', blue: '#1f5fbf', black: '#1d232b', yellow: '#ffd400' },
  };
  const PAGE_NAME = location.pathname.split('/').pop() || 'index.html';
  // 页面版本号（build_site 嵌入；快照页是笔当初画的那一版的版本号）：笔画下时记进 drawn.page，保存时记进 savedOn
  const PAGE_VERSION = typeof window.NIPC_PAGE === 'string' ? window.NIPC_PAGE : '';
  const STORE_KEY = 'nipc-notes:' + location.pathname;
  const UI_KEY = 'nipc-notes-ui';
  const SVGNS = 'http://www.w3.org/2000/svg';
  const TEXT_BLOCK_SEL = 'p, li, figcaption, blockquote, pre, summary, dt, dd, td, th, h1, h2, h3, h4, h5, h6, .lead, div';
  const TARGET_SEL = 'img, video, mjx-container[display="true"], td, th, h1, h2, h3, h4, h5, h6, ' +
                     'p, li, figcaption, blockquote, pre, summary, dt, dd, .lead';

  const S = {
    strokes: [], on: false, tool: 'pen', color: 'red', undo: [],
    domVersion: 0, index: null, cache: new Map(), drawn: new Map(),
    drawing: null, erasing: null, moving: null, printing: false, orphans: 0, hidden: 0,
    tombs: {}, lastIds: new Set(),
  };

  /* ---------- 2. 小工具 ---------- */
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const round = (v, k) => Math.round(v * k) / k;
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

  /** 两串的编辑距离（Levenshtein），只用于 ≤ 几百字的短串。 */
  function editDistance(a, b) {
    if (a === b) return 0;
    let prev = new Array(b.length + 1), cur = new Array(b.length + 1);
    for (let j = 0; j <= b.length; j++) prev[j] = j;
    for (let i = 1; i <= a.length; i++) {
      cur[0] = i;
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      }
      [prev, cur] = [cur, prev];
    }
    return prev[b.length];
  }
  /** 相似度 0–1：1 - 编辑距离 / 较长串长度；两串都空算 1。 */
  function similarity(a, b) {
    const n = Math.max(a.length, b.length);
    return n === 0 ? 1 : 1 - editDistance(a, b) / n;
  }
  /** text 里 needle 的所有出现位置（最多 limit 个）。 */
  function allIndexes(text, needle, limit = 200) {
    const out = [];
    if (!needle) return out;
    for (let i = text.indexOf(needle); i >= 0 && out.length < limit; i = text.indexOf(needle, i + 1)) out.push(i);
    return out;
  }

  /* ---------- 3. 本机保存（每页一个 localStorage 键） ---------- */
  /** 本页笔记的完整 JSON（本机保存、导出、同步共用）。tombstones = {被删笔画 id: 删除时刻 ms}，让删除能同步到别的设备。 */
  function payload() {
    return { format: 'nipc-notes', version: CFG.version, page: PAGE_NAME, path: location.pathname,
             colWidth: CFG.colWidth, saved: new Date().toISOString(), savedOn: PAGE_VERSION, strokes: S.strokes, tombstones: S.tombs };
  }
  /** 一笔此刻的样子作为「画的」原样（只在收笔、用户移动时调用；深拷贝，之后匹配字段怎么改都碰不到它）。 */
  function drawnOf(s) {
    return JSON.parse(JSON.stringify({ page: PAGE_VERSION, pts: s.pts, frame: s.frame, anchor: s.anchor, orig: s.orig,
                                       ua: navigator.userAgent, at: new Date().toISOString() }));
  }
  function validStroke(s) {
    return !!(s && typeof s.id === 'string' && Array.isArray(s.pts) && s.pts.length && s.anchor && s.orig &&
              (s.frame === 'px' || s.frame === 'norm'));
  }
  function validTombs(t) { return t && typeof t === 'object' && !Array.isArray(t) ? t : {}; }
  /** 本机存的 {strokes, tombs}。 */
  // 只读快照页（snapshot/<n>/）：笔记就是页面里嵌的那份（某次画完时的原样），不读写本机存储、不同步笔记仓库
  const SNAP = window.NIPC_SNAPSHOT && Array.isArray(window.NIPC_SNAPSHOT.strokes) ? window.NIPC_SNAPSHOT : null;
  function loadStored() {
    if (SNAP) return { strokes: SNAP.strokes.filter(validStroke), tombs: validTombs(SNAP.tombstones) };
    try {
      const raw = localStorage.getItem(STORE_KEY);
      const d = raw ? JSON.parse(raw) : null;
      return { strokes: d && Array.isArray(d.strokes) ? d.strokes.filter(validStroke) : [], tombs: validTombs(d && d.tombstones) };
    } catch (e) { return { strokes: [], tombs: {} }; }
  }
  function saveStored() {
    if (SNAP) return;
    try { localStorage.setItem(STORE_KEY, JSON.stringify(payload())); }
    catch (e) { setStatus('本机保存失败（浏览器存储不可用），请及时导出笔记', true); }
  }
  function loadUi() { try { return JSON.parse(localStorage.getItem(UI_KEY) || '{}') || {}; } catch (e) { return {}; } }
  function saveUi(ui) { if (SNAP) return; try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch (e) { /* 只是界面偏好，丢了无妨 */ } }

  /* ---------- 4. 版式固定与坐标换算 ---------- */
  function layoutLocked() { return S.on || S.strokes.length > 0; }
  /** 有笔迹或标注打开时固定正文 920px；放不下目录就隐藏目录；再放不下就 zoom 等比缩小。 */
  function applyLayout() {
    const root = document.documentElement, locked = layoutLocked(), w = root.clientWidth;
    root.classList.toggle('annot-fixed', locked);
    root.classList.toggle('annot-on', S.on);
    root.classList.toggle('annot-noaside', locked && w < CFG.asideWidth + CFG.gap + CFG.colWidth + 2 * CFG.sidePad);
    const z = locked ? Math.min(1, (w - 2 * CFG.narrowPad) / CFG.colWidth) : 1;
    main.style.zoom = z < 1 ? String(round(z, 1000)) : '';
  }
  /** 屏幕坐标 ↔ 正文卡片内未缩放坐标的换算参数：s = 缩放倍数，(x, y) = 图层左上角的屏幕坐标。 */
  function frame() {
    const mr = main.getBoundingClientRect(), lr = layer.getBoundingClientRect();
    return { s: (mr.width / main.offsetWidth) || 1, x: lr.left, y: lr.top };
  }
  const toLocal = (cx, cy, f) => ({ x: (cx - f.x) / f.s, y: (cy - f.y) / f.s });
  const toClient = (x, y, f) => ({ x: f.x + x * f.s, y: f.y + y * f.s });
  const localRect = (r, f) => ({ x: (r.left - f.x) / f.s, y: (r.top - f.y) / f.s, w: r.width / f.s, h: r.height / f.s });
  /** el 是否在某个收起的折叠框里（不算该框自己的标题 summary）。新版 Chromium 里收起内容仍有布局矩形，所以要显式判断。 */
  function inClosedDetails(el) {
    for (let d = el && el.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) {
      if (d.open) continue;
      const sum = el.closest('summary');
      if (!(sum && sum.parentElement === d)) return true;
    }
    return false;
  }
  /** 元素的屏幕矩形；元素没渲染（如在收起的折叠框里）返回 null。 */
  function elementRect(el) {
    if (!el || !el.isConnected || inClosedDetails(el) || el.getClientRects().length === 0) return null;
    const r = el.getBoundingClientRect();
    return r.width || r.height ? r : null;
  }

  /* ---------- 5. 正文文字索引（空白合并成一个空格，跳过公式内部） ---------- */
  function skipText(node) {
    const p = node.parentElement;
    return !p || !!p.closest('mjx-container, script, style, svg, .annot-ui');
  }
  /** 把正文所有文字连成一串 text，并记下每个字来自哪个文本节点的哪个位置。 */
  function buildIndex() {
    const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    const nodes = [], chars = [], posNode = [], posOff = [];
    let lastSpace = true;
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (skipText(n)) continue;
      const t = n.data, ni = nodes.length;
      nodes.push(n);
      for (let i = 0; i < t.length; i++) {
        const space = /\s/.test(t[i]);
        if (space && lastSpace) continue;
        chars.push(space ? ' ' : t[i]);
        lastSpace = space;
        posNode.push(ni); posOff.push(i);
      }
    }
    const nodeIdx = new Map(nodes.map((n, i) => [n, i]));
    return { v: S.domVersion, text: chars.join(''), nodes, nodeIdx,
             posNode: Int32Array.from(posNode), posOff: Int32Array.from(posOff) };
  }
  function ensureIndex() {
    if (!S.index || S.index.v !== S.domVersion) { S.index = buildIndex(); S.cache.clear(); }
    return S.index;
  }
  /** 第一个「节点序号 ≥ ni 且（同节点时）偏移 ≥ off」的字的位置；没有返回 -1。 */
  function firstPos(ni, off) {
    const { posNode, posOff } = S.index;
    let lo = 0, hi = posNode.length;
    while (lo < hi) {
      const m = (lo + hi) >> 1;
      if (posNode[m] < ni || (posNode[m] === ni && posOff[m] < off)) lo = m + 1; else hi = m;
    }
    return lo < posNode.length ? lo : -1;
  }
  /** 文本节点 node 的第 off 个字符对应的索引位置（被合并掉的空白取其后第一个字）。 */
  function posOfCaret(node, off) {
    const ni = S.index.nodeIdx.get(node);
    return ni === undefined ? -1 : firstPos(ni, off);
  }
  /** el 开始处（el 内部或其后）第一个字的位置。 */
  function posFromElement(el) {
    const nodes = S.index.nodes;
    let lo = 0, hi = nodes.length;
    while (lo < hi) {
      const m = (lo + hi) >> 1;
      if (el.compareDocumentPosition(nodes[m]) & Node.DOCUMENT_POSITION_FOLLOWING) hi = m; else lo = m + 1;
    }
    return lo < nodes.length ? firstPos(lo, 0) : -1;
  }
  /** el 前面紧挨着的 quoteLen 个字：用来区分 src / TeX / 文字相同的几处。 */
  function contextBefore(el) {
    const p = posFromElement(el), t = S.index.text;
    return p < 0 ? t.slice(-CFG.quoteLen) : t.slice(Math.max(0, p - CFG.quoteLen), p);
  }
  /** 位置 pos 的那个字的屏幕矩形；没渲染返回 null。 */
  function charRect(pos) {
    const ix = S.index, node = ix.nodes[ix.posNode[pos]];
    if (!node || !node.isConnected || inClosedDetails(node.parentElement)) return null;
    const r = document.createRange(), o = ix.posOff[pos];
    r.setStart(node, o); r.setEnd(node, Math.min(o + 1, node.data.length));
    for (const q of r.getClientRects()) if (q.width || q.height) return q;
    return null;
  }
  /** 位置 pos 所在的文字块（段落、列表项、图注……）及其在 text 里的 [start, end)。 */
  function blockSpan(pos) {
    const ix = S.index, node = ix.nodes[ix.posNode[pos]];
    const blk = (node.parentElement && node.parentElement.closest(TEXT_BLOCK_SEL)) || article;
    const el = article.contains(blk) ? blk : article;
    const start = posFromElement(el);
    let end = start;
    while (end >= 0 && end < ix.text.length && el.contains(ix.nodes[ix.posNode[end]])) end++;
    return { el, start, end };
  }

  /* ---------- 6. 建锚点（起笔处的内容 → 锚点 + 参考矩形） ---------- */
  function srcKey(el) {
    const s = el.getAttribute('src') || el.currentSrc || '';
    return s.split('#')[0].split('?')[0].split('/').pop();
  }
  /** 当前所有陈列公式：[{el, tex}]，TeX 取自 MathJax 的 math 列表。 */
  /** 页面里全部公式（陈列 + 行内），{el, tex}：圈住的内容可以是行内公式里的符号。 */
  function allMath() {
    const out = [];
    try {
      for (const m of MathJax.startup.document.math) {
        if (m.typesetRoot && article.contains(m.typesetRoot)) out.push({ el: m.typesetRoot, tex: norm(m.math) });
      }
    } catch (e) { /* MathJax 没加载 */ }
    return out;
  }
  function displayMath() {
    const out = [];
    try {
      for (const m of MathJax.startup.document.math) {
        if (m.display && m.typesetRoot && article.contains(m.typesetRoot)) out.push({ el: m.typesetRoot, tex: norm(m.math) });
      }
    } catch (e) { /* MathJax 没加载：页面没有可锚的公式 */ }
    return out;
  }
  function plainText(el) {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let s = '';
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (!skipText(n)) s += n.data;
    return norm(s);
  }
  function headerText(cell) {
    const table = cell.closest('table');
    const head = table && table.tHead && table.tHead.rows[0];
    const th = head && head.cells[cell.cellIndex];
    return th ? plainText(th) : '';
  }
  /** 屏幕点下最上面的正文元素（跳过工具栏）；调用前输入层已设为不可点中。 */
  function topElement(cx, cy) {
    for (const el of document.elementsFromPoint(cx, cy)) {
      if (el.closest('.annot-ui')) continue;
      return el;
    }
    return null;
  }
  /** 屏幕点处的字符位置（caretRangeFromPoint / caretPositionFromPoint），取点所在那个字而不是字间缝。 */
  function charAtPoint(cx, cy) {
    let node = null, off = 0;
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(cx, cy);
      if (r) { node = r.startContainer; off = r.startOffset; }
    } else if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(cx, cy);
      if (p) { node = p.offsetNode; off = p.offset; }
    }
    if (!node || node.nodeType !== Node.TEXT_NODE || !article.contains(node) || skipText(node)) return -1;
    const after = posOfCaret(node, off);
    const before = off > 0 ? posOfCaret(node, off - 1) : -1;
    let best = -1, bestD = Infinity;
    for (const p of [before, after]) {
      if (p < 0) continue;
      const r = charRect(p);
      if (!r) continue;
      const dx = Math.max(r.left - cx, 0, cx - r.right), dy = Math.max(r.top - cy, 0, cy - r.bottom);
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = p; }
    }
    return best;
  }
  /** 离屏幕点最近的、当前可见的可锚元素；返回该元素和夹进其矩形内的点。 */
  function nearestTarget(cx, cy) {
    let best = null, bestD = Infinity;
    const vw = innerWidth, vh = innerHeight;
    for (const el of article.querySelectorAll(TARGET_SEL)) {
      const r = elementRect(el);
      if (!r || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
      const dx = Math.max(r.left - cx, 0, cx - r.right), dy = Math.max(r.top - cy, 0, cy - r.bottom);
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        const x = Math.min(Math.max(cx, r.left + 1, 1), r.right - 1, vw - 1);
        const y = Math.min(Math.max(cy, r.top + 1, 1), r.bottom - 1, vh - 1);
        best = { el, x, y };
      }
    }
    return best;
  }
  function textAnchor(pos, f) {
    const t = S.index.text, r = charRect(pos);
    if (!r) return null;
    const { start, end, el } = blockSpan(pos);
    return {
      anchor: { type: 'text', exact: t[pos], prefix: t.slice(Math.max(0, pos - CFG.quoteLen), pos),
                suffix: t.slice(pos + 1, pos + 1 + CFG.quoteLen),
                blk: { tag: el.tagName.toLowerCase(), head: t.slice(start, Math.min(end, start + 40)), off: pos - start } },
      frame: 'px', R: localRect(r, f),
    };
  }
  /* 陈列公式里的符号：MathJax SVG 每个字形是一个带 data-c（Unicode 编码，16 进制）的元素。公式上的笔锚到起笔处
   * 最近的那个符号，存它前后各 glyphLen 个符号（同正文的 TextQuote），笔画坐标相对这个符号；公式别处增删、
   * 整体变宽或拆成几行都不会把笔拉偏。没有符号锚点的老笔照旧按整块公式的比例坐标画。 */
  function glyphList(mj) {
    const els = [...mj.querySelectorAll('[data-c]')];
    // NFKC：数学斜体 𝑡 → t，这样记号改名表（t → q）也能用在公式符号上
    return { els, ch: els.map(e => { const c = parseInt(e.getAttribute('data-c'), 16);
                                     return Number.isFinite(c) ? String.fromCodePoint(c).normalize('NFKC') : '?'; }) };
  }
  /** 符号序列 ch 里 seq（字符数组）出现的所有下标。 */
  function seqIndexes(ch, seq) {
    const out = [];
    for (let i = 0; i + seq.length <= ch.length; i++) if (seq.every((c, k) => ch[i + k] === c)) out.push(i);
    return out;
  }
  function glyphQuote(g, i) {
    return { prefix: g.ch.slice(Math.max(0, i - CFG.glyphLen), i).join(''), exact: g.ch[i],
             suffix: g.ch.slice(i + 1, i + 1 + CFG.glyphLen).join('') };
  }
  /** 公式里离 (cx, cy) 最近的可见符号的下标；没有返回 -1。 */
  function glyphAt(g, cx, cy) {
    let best = -1, bestD = Infinity;
    g.els.forEach((e, i) => {
      const r = e.getBoundingClientRect();
      if (!r.width && !r.height) return;
      const dx = Math.max(r.left - cx, 0, cx - r.right), dy = Math.max(r.top - cy, 0, cy - r.bottom);
      if (dx * dx + dy * dy < bestD) { bestD = dx * dx + dy * dy; best = i; }
    });
    return best;
  }
  /** 按元素类型建锚点：媒体 / 陈列公式 / 表格单元格 / 标题 / 正文字符。返回 {anchor, frame, R} 或 null。 */
  function anchorForElement(el, cx, cy, f) {
    const media = el.closest('img, video');
    if (media && article.contains(media)) {
      return { anchor: { type: 'media', src: srcKey(media), ctx: contextBefore(media) }, frame: 'norm',
               R: localRect(media.getBoundingClientRect(), f) };
    }
    const mj = el.closest('mjx-container');
    if (mj && mj.getAttribute('display') === 'true') {
      const m = displayMath().find(d => d.el === mj);
      if (m) {
        const g = glyphList(mj), i = glyphAt(g, cx, cy);
        if (i >= 0) return { anchor: { type: 'math', tex: m.tex, ctx: contextBefore(mj), g: glyphQuote(g, i) }, frame: 'px',
                             R: localRect(g.els[i].getBoundingClientRect(), f) };
        return { anchor: { type: 'math', tex: m.tex, ctx: contextBefore(mj) }, frame: 'norm',
                 R: localRect(mj.getBoundingClientRect(), f) };
      }
    }
    if (mj) {   // 行内公式：锚到公式后面紧挨着的那个字
      const p = posFromElement(mj);
      return p >= 0 ? textAnchor(p, f) : null;
    }
    const cell = el.closest('td, th');
    if (cell && article.contains(cell)) {
      return { anchor: { type: 'cell', cell: plainText(cell), head: headerText(cell), ctx: contextBefore(cell) },
               frame: 'px', R: localRect(cell.getBoundingClientRect(), f) };
    }
    const h = el.closest('h1, h2, h3, h4, h5, h6');
    if (h && article.contains(h)) {
      return { anchor: { type: 'heading', text: plainText(h), ctx: contextBefore(h) }, frame: 'px',
               R: localRect(h.getBoundingClientRect(), f) };
    }
    const p = charAtPoint(cx, cy);
    return p >= 0 ? textAnchor(p, f) : null;
  }
  /* 一笔圈住了什么（用户 10-25：「我圈画就是圈画在内容上 内容变了没了那就不留那个标记」「这句话是axxxb我圈了ab
   * 然后变成cxxxaxxxb那我圈的还要在ab上」）。圈住的内容 = 中心落在笔画外框里的正文字 / 公式符号；一个都没有（下划线、
   * 箭头、旁注）就取离笔画最近、nearPx 以内的那一个。锚点存这串内容本身（cover）：还原时先在全页找它，几处一样再用
   * 前后文挑，一处都没有就不画（findCover / findCoverGlyph）。主要圈在图上的笔照旧锚整张图。 */
  /** 页面几何（页面坐标，不随滚动变）：陈列公式的外框与符号、正文文字节点的外框，单字矩形按需量、量过就记。
   * 一批笔共用一份（reanchorAll），单独一笔现量；排版变了（DOM 版本或窗口宽度变）就重建。 */
  function pageGeom() {
    const key = `${S.domVersion}:${innerWidth}`;
    if (S.geom && S.geom.key === key) return S.geom;
    const page = q => ({ left: q.left + scrollX, top: q.top + scrollY, right: q.right + scrollX, bottom: q.bottom + scrollY,
                         width: q.width, height: q.height });
    const maths = allMath().filter(m => !inClosedDetails(m.el)).map(m => {
      const g = glyphList(m.el);
      return { m, g, box: page(m.el.getBoundingClientRect()), q: g.els.map(e => page(e.getBoundingClientRect())) };
    });
    const ix = ensureIndex(), rg = document.createRange();
    const nodes = ix.nodes.map((n, ni) => {
      if (!n.isConnected || inClosedDetails(n.parentElement)) return null;
      rg.selectNodeContents(n);
      return { ni, box: page(rg.getBoundingClientRect()) };
    }).filter(Boolean);
    S.geom = { key, page, maths, nodes, chars: new Map() };
    return S.geom;
  }
  function strokeCover(abs, f, padPx = CFG.nearPx) {
    const G = pageGeom();
    const P = abs.map(p => { const c = toClient(p[0], p[1], f); return { x: c.x + scrollX, y: c.y + scrollY }; });
    const l = Math.min(...P.map(p => p.x)), r = Math.max(...P.map(p => p.x));
    const t = Math.min(...P.map(p => p.y)), b = Math.max(...P.map(p => p.y)), pad = padPx * f.s;
    const near = q => q.right >= l - pad && q.left <= r + pad && q.bottom >= t - pad && q.top <= b + pad;
    const inside = q => { const cx = (q.left + q.right) / 2, cy = (q.top + q.bottom) / 2; return cx >= l && cx <= r && cy >= t && cy <= b; };
    const dist = q => Math.min(...P.map(p => Math.hypot(Math.max(q.left - p.x, 0, p.x - q.right), Math.max(q.top - p.y, 0, p.y - q.bottom))));
    const items = [];
    for (const { m, g, box, q } of G.maths) {
      if (!near(box)) continue;
      q.forEach((qi, i) => { if ((qi.width || qi.height) && near(qi)) items.push({ kind: 'g', m, g, i, q: qi }); });
    }
    const ix = S.index;
    for (const { ni, box } of G.nodes) {
      if (!near(box)) continue;
      if (!G.chars.has(ni)) {
        const cs = [];
        for (let p = firstPos(ni, 0); p >= 0 && p < ix.text.length && ix.posNode[p] === ni; p++) {
          const q = ix.text[p] === ' ' ? null : charRect(p);
          if (q) cs.push([p, G.page(q)]);
        }
        G.chars.set(ni, cs);
      }
      for (const [p, q] of G.chars.get(ni)) if (near(q)) items.push({ kind: 't', pos: p, q });
    }
    let hit = items.filter(it => inside(it.q));
    if (!hit.length) {   // 下划线 / 上划线：横向在笔画范围内、竖直方向离笔画最近的那一行
      const over = items.filter(it => { const cx = (it.q.left + it.q.right) / 2; return cx >= l && cx <= r && dist(it.q) <= pad; });
      if (over.length) {
        const dmin = Math.min(...over.map(it => dist(it.q)));
        hit = over.filter(it => dist(it.q) <= dmin + 0.5 * (it.q.bottom - it.q.top));
      }
    }
    if (!hit.length) {   // 箭头、旁注：离笔画最近的那一个
      const best = items.map(it => [dist(it.q), it]).filter(([d]) => d <= pad).sort((x, y) => x[0] - y[0])[0];
      hit = best ? [best[1]] : [];
    }
    if (!hit.length) return null;
    // 核对用：圈住的每个字 / 符号与笔画起点的相对位置（正文卡片 px），按页面上的先后排
    const hitPos = hit.slice().sort((x, y) => (x.kind === 'g' && y.kind === 'g' && x.m === y.m) ? x.i - y.i : (x.q.top - y.q.top) || (x.q.left - y.q.left))
      .map(it => [it.kind === 'g' ? it.g.ch[it.i] : ix.text[it.pos], (P[0].x - it.q.left) / f.s, (P[0].y - it.q.top) / f.s]);
    const got = strokeCoverAnchor(hit, ix, f);
    if (got) got.hit = hitPos;
    return got;
  }
  /** strokeCover 的后半：圈住的那些字 / 符号 → 锚点（主要是公式符号就锚公式里那一串，否则锚正文那一段字）。 */
  function strokeCoverAnchor(hit, ix, f) {
    if (2 * hit.filter(it => it.kind === 'g').length >= hit.length) {   // 主要圈在公式上：符号最多的那个公式里第一个到最后一个
      const by = new Map();
      for (const it of hit) if (it.kind === 'g') by.set(it.m.el, (by.get(it.m.el) || []).concat([it]));
      const gs = [...by.values()].sort((x, y) => y.length - x.length)[0], g = gs[0].g;
      const i0 = Math.min(...gs.map(it => it.i)), i1 = Math.max(...gs.map(it => it.i));
      const bp = posFromElement(gs[0].m.el), bs = bp >= 0 ? blockSpan(bp) : null;
      return { anchor: { type: 'math', tex: gs[0].m.tex, ctx: contextBefore(gs[0].m.el), g: glyphQuote(g, i0),
                         cover: g.ch.slice(i0, i1 + 1).join(''), gi: i0, gn: g.ch.length,
                         block: bs ? ix.text.slice(bs.start, bs.end) : '' },
               frame: 'px', R: localRect(g.els[i0].getBoundingClientRect(), f) };
    }
    const ps = hit.filter(it => it.kind === 't').map(it => it.pos), p0 = Math.min(...ps), p1 = Math.max(...ps);
    const got = textAnchor(p0, f);
    if (got) {
      const { start, end } = blockSpan(p0);
      Object.assign(got.anchor, { cover: ix.text.slice(p0, p1 + 1), block: ix.text.slice(start, end) });
    }
    return got;
  }
  /** 两段文字的字二元组 Jaccard 相似度（改写措辞、调换语序后仍然高，换成别的段落就低）。 */
  function bigramSim(a, b) {
    const grams = s => { const o = new Set(); for (let i = 0; i + 1 < s.length; i++) o.add(s.slice(i, i + 2)); return o; };
    const A = grams(a), B = grams(b);
    let n = 0;
    for (const x of A) if (B.has(x)) n++;
    return A.size + B.size ? n / (A.size + B.size - n) : 1;
  }
  /** 正文里圈住的关键词：全页找这串字（不行再按改名表换写法找），只有一处就是它；几处就挑所在段落与原段落最像的
   * （coverCtxAccept 以上），同样像的取离原位置近的；一处都没有（关键词删了）返回 null，这笔不画。 */
  function findCover(a, orig, f) {
    const t = S.index.text;
    let cover = a.cover, hits = allIndexes(t, cover, 2000);
    if (!hits.length) { const r = renamedText(cover, 0); if (r) { cover = r.s; hits = allIndexes(t, cover, 2000); } }
    if (!hits.length) return null;
    const out = pos => {   // 关键词或它前后的字按改名表换了写法：存的锚点一并改成新写法
      const rn = s => { const r = renamedText(s || '', 0); return r ? r.s : (s || ''); };
      const m = { cover, exact: cover[0], prefix: rn(a.prefix), suffix: rn(a.suffix), block: rn(a.block) };
      return Object.keys(m).every(k => m[k] === (a[k] || '')) ? { pos } : { pos, migrated: Object.assign({}, a, m) };
    };
    if (hits.length === 1 || !a.block) return out(nearestPos(hits, orig, f));
    // 每一处比两样：紧挨着的前后各 10 字（只改了段落别处时原样不变），和周围与原段落等长的一段（改写、换语序、拆段也认得出）。
    // 认它的条件：前或后 10 字几乎原样（≥ coverNearAccept），或段落相似（≥ coverCtxAccept）且前后 10 字也不差（≥ coverCtxAccept）。
    // 几处都认：先挑前后 10 字原样的，再比段落相似度，再比前后 10 字
    const L = 10, pre = a.prefix.slice(-L), suf = a.suffix.slice(cover.length - 1, cover.length - 1 + L);
    const half = Math.max(40, a.block.length >> 1);
    let best = null, key = null;
    for (const p of hits) {
      const sp = similarity(pre, t.slice(Math.max(0, p - L), p)), ss = similarity(suf, t.slice(p + cover.length, p + cover.length + L));
      const sb = bigramSim(a.block, t.slice(Math.max(0, p - half), p + cover.length + half));
      const near = Math.max(sp, ss) >= CFG.coverNearAccept;
      if (!near && !(sb >= CFG.coverCtxAccept && (sp + ss) / 2 >= CFG.coverCtxAccept)) continue;
      const k = [near ? 1 : 0, sb, sp + ss];
      if (!key || k[0] > key[0] || (k[0] === key[0] && (k[1] > key[1] + 1e-9 || (Math.abs(k[1] - key[1]) <= 1e-9 && k[2] > key[2])))) { best = p; key = k; }
    }
    return key ? out(best) : null;
  }
  /** 公式里圈住的符号串：先在原公式（TeX 相同或按改名表换过）里找，找不到再在全页陈列公式里找；几处就挑整个公式与原公式
   * 最像的（符号二元组 Jaccard），一处都没有返回 null。返回 {el: 公式, glyph: 起始符号元素}。 */
  function findCoverGlyph(a) {
    // 圈住的末尾标点（句号、逗号）不算内容：公式里「sin θ.」改成「sin ψ,」仍是同一处
    const all = allMath(), seq = Array.from(a.cover.length > 1 ? a.cover.replace(/[.,;:]+$/, '') : a.cover);
    const renamed = renamedTex(a.tex);
    const own = all.filter(m => m.tex === a.tex || (renamed && m.tex === renamed));
    // 找到的位置（公式 m 的第 i 个符号）；公式换了（改名、并进别的公式）就把存的锚点一并改成新公式里的这一处
    const found = (m, g, i) => {
      const r = { el: m.el, glyph: g.els[i] };
      if (m.tex === a.tex) return r;
      const n = Array.from(a.cover).length, bp = posFromElement(m.el), bs = bp >= 0 ? blockSpan(bp) : null;
      r.migrated = Object.assign({}, a, { tex: m.tex, ctx: contextBefore(m.el), g: glyphQuote(g, i), gi: i, gn: g.ch.length,
                                           cover: g.ch.slice(i, i + n).join(''), block: bs ? S.index.text.slice(bs.start, bs.end) : a.block });
      return r;
    };
    const search = list => {
      const out = [];
      for (const m of list) {
        const g = glyphList(m.el);
        let idx = seqIndexes(g.ch, seq);
        if (!idx.length) { const r = renamedText(a.cover, 0); if (r) idx = seqIndexes(g.ch, Array.from(r.s)); }
        for (const i of idx) out.push({ m, g, i });
      }
      return out;
    };
    let hits = search(own);
    if (!hits.length && own.length) {
      // 原公式还在（TeX 相同或按改名表认出），只是字母改了（t → q）：符号个数没变就按原序号，否则这笔不画；不去别的公式里乱找
      const m = own.find(o => glyphList(o.el).ch.length === a.gn);
      if (m && a.gi !== undefined) { const g = glyphList(m.el); return found(m, g, a.gi); }
      // 符号个数也变了（改名表把它连到改写后的公式）：圈住的符号串按改名表里的字母换名（t → q、θ → ψ，一次用一组）
      // 换过后在这个公式里原样找；找不到就是圈的内容改了，不画
      for (const lm of letterMaps()) {
        const mseq = seq.map(c => lm.get(c) || c);
        if (mseq.every((c, k) => c === seq[k])) continue;
        const hs = own.flatMap(o => { const g = glyphList(o.el); return seqIndexes(g.ch, mseq).map(i => ({ m: o, g, i })); });
        if (hs.length) { hits = hs; break; }
      }
      if (!hits.length) return null;
    }
    // 原公式没了（并进了更长的公式）：原公式短到符号锚点存下了它的全部符号时，全页找这整串，恰好一处就按原序号放
    if (!hits.length && a.g && a.gi !== undefined && a.g.prefix.length === a.gi && a.gi + 1 + a.g.suffix.length === a.gn) {
      const whole = Array.from(a.g.prefix + a.g.exact + a.g.suffix);
      const at = all.flatMap(m => { const g = glyphList(m.el); return seqIndexes(g.ch, whole).map(i => ({ m, g, i: i + a.gi })); });
      if (at.length === 1) return found(at[0].m, at[0].g, at[0].i);
    }
    // 原公式改写了（拆行、加步骤）：全页找这串符号；只圈一个符号的不找（别的公式里同样的单个符号到处都是，认不出是哪个）
    if (!hits.length && seq.length >= 2) hits = search(all);
    if (!hits.length) return null;
    // 几处一样（短的行内公式常见）：依次比所在段落、公式前面的字、公式里前后的符号
    const origG = a.g.prefix + a.g.exact + a.g.suffix, t = S.index.text;
    const score = h => {
      const p = posFromElement(h.m.el), bs = p >= 0 ? blockSpan(p) : null, q = glyphQuote(h.g, h.i);
      return [a.block && bs ? bigramSim(a.block, t.slice(bs.start, bs.end)) : 0,
              similarity(a.ctx || '', contextBefore(h.m.el)), bigramSim(origG, q.prefix + q.exact + q.suffix)];
    };
    const keyed = hits.map(h => [score(h), h]);
    keyed.sort((x, y) => (y[0][0] - x[0][0]) || (y[0][1] - x[0][1]) || (y[0][2] - x[0][2]));
    const h = keyed[0][1];
    return found(h.m, h.g, h.i);
  }
  /** 一整笔（正文卡片坐标的点）的锚点：主要落在图 / 视频里就锚整张图；否则锚圈住的内容；没圈住也不挨着内容（页边旁注）
   * 就逐级放宽到离它最近的那个字 / 符号（按页面几何算，不靠屏幕上点到的元素：笔在视口外时点不到它）；都不行退回起笔点规则。 */
  function anchorForStroke(abs) {
    if (!S.geomBatch) S.geom = null;   // 单独一笔：现量（折叠框开合会改排版）；批量迁移时共用一份
    const f = frame(), P = abs.map(p => toClient(p[0], p[1], f));
    for (const m of article.querySelectorAll('img, video')) {
      const q = elementRect(m);   // 收起的折叠里的图仍有布局矩形，要跳过（否则画在折叠框后面正文上的笔会挂到它上面）
      if (q && 2 * P.filter(p => p.x >= q.left && p.x <= q.right && p.y >= q.top && p.y <= q.bottom).length > P.length) {
        return { anchor: { type: 'media', src: srcKey(m), ctx: contextBefore(m) }, frame: 'norm', R: localRect(q, f) };
      }
    }
    for (const px of [CFG.nearPx, 120, 480, 1920]) { const got = strokeCover(abs, f, px); if (got) return got; }
    return anchorAt(Math.min(Math.max(P[0].x, 1), innerWidth - 1), Math.min(Math.max(P[0].y, 1), innerHeight - 1));
  }
  /** 起笔处（屏幕坐标）的锚点；点不在正文内容上就取最近的可锚元素；实在没有就锚到正文卡片本身。 */
  function anchorAt(cx, cy) {
    ensureIndex();
    const f = frame();
    capture.classList.add('probe');   // 让命中测试穿过输入层
    try {
      let el = topElement(cx, cy), got = null;
      if (el && article.contains(el)) got = anchorForElement(el, cx, cy, f);
      if (!got) {
        const near = nearestTarget(cx, cy);
        if (near) {
          const inner = topElement(near.x, near.y);
          got = anchorForElement(inner && near.el.contains(inner) ? inner : near.el, near.x, near.y, f);
        }
      }
      return got || { anchor: { type: 'page' }, frame: 'px', R: { x: 0, y: 0, w: 1, h: 1 } };
    } finally {
      capture.classList.remove('probe');
    }
  }

  /* ---------- 7. 还原锚点（锚点 → 当前页面上的元素 / 字符） ---------- */
  /** 几个同样匹配的候选里挑一个：先比前文 ctx 的相似度，再比离原位置的竖直距离。 */
  function pickCandidate(cands, a, orig, f) {
    if (cands.length <= 1) return cands[0] || null;
    let best = null, bestKey = null;
    for (const el of cands) {
      const sc = a.ctx ? similarity(a.ctx, contextBefore(el)) : 0;
      const r = elementRect(el), dy = r ? Math.abs(localRect(r, f).y - orig.y) : Infinity;
      if (!best || sc > bestKey[0] + 0.01 || (Math.abs(sc - bestKey[0]) <= 0.01 && dy < bestKey[1])) {
        best = el; bestKey = [sc, dy];
      }
    }
    return best;
  }
  function nearestPos(hits, orig, f) {
    if (hits.length === 1) return hits[0];
    let best = hits[0], bestD = Infinity;
    for (const p of hits) {
      const r = charRect(p), d = r ? Math.abs(localRect(r, f).y - orig.y) : Infinity;
      if (d < bestD) { bestD = d; best = p; }
    }
    return best;
  }
  /* ---------- 7b. 记号改名表：内容只是换了写法（如 d → δ），笔记跟着改名后的内容走 ----------
   * build_site 把 docs/drafts/notation_renames_<页>.json 嵌进页面 <script id="annot-renames">：
   * {"tex": {旧 TeX: 新 TeX}, "text": {旧文字: 新文字}}。只用于有圈住内容（cover）的笔：圈住的那串按原样找不到时，
   * 按改名表（可连续改名）换成新写法再找（findCover / findCoverGlyph），找到就把存的锚点改成新写法；还找不到就隐藏。 */
  const collapse = s => (s || '').replace(/\s+/g, ' ');
  function renameMap() {
    const el = document.getElementById('annot-renames');
    const raw = el ? el.textContent : '';
    if (renameMap.raw !== raw) {
      let m = {};
      try { m = JSON.parse(raw || '{}') || {}; } catch (e) { m = {}; }
      const pick = (o, fn) => new Map(Object.entries(o && typeof o === 'object' ? o : {})
        .filter(([k, v]) => k && typeof v === 'string').map(([k, v]) => [fn(k), fn(v)]));
      renameMap.raw = raw;
      renameMap.val = { tex: pick(m.tex, norm), text: pick(m.text, collapse) };
    }
    return renameMap.val;
  }
  /** 改名表里「只差字母一一换名」的那些公式对，各自换了哪些字母（公式符号字符 → 新字符，如 t → q、θ → ψ）。
   * 两边按 TeX 记号切开、记号数相同、不同处全是字母才算；每一对给一组，按出现顺序去重。 */
  const GREEK = { alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ϵ', varepsilon: 'ε', zeta: 'ζ', eta: 'η', theta: 'θ',
                  vartheta: 'ϑ', kappa: 'κ', lambda: 'λ', mu: 'μ', nu: 'ν', xi: 'ξ', pi: 'π', rho: 'ρ', sigma: 'σ', tau: 'τ',
                  phi: 'ϕ', varphi: 'φ', chi: 'χ', psi: 'ψ', omega: 'ω' };
  function letterMaps() {
    const raw = renameMap();
    if (letterMaps.of === raw) return letterMaps.val;
    const tok = s => s.match(/\\[A-Za-z]+|\S/g) || [];
    const glyph = t => (/^[A-Za-z]$/.test(t) ? t : (t[0] === '\\' && GREEK[t.slice(1)]) || null);
    const out = [], seen = new Set();
    for (const [o, n] of raw.tex) {
      const A = tok(o), B = tok(n);
      if (A.length !== B.length) continue;
      const m = new Map();
      let ok = true;
      A.forEach((x, k) => { if (!ok || x === B[k]) return; const gx = glyph(x), gy = glyph(B[k]);
                            if (!gx || !gy || (m.has(gx) && m.get(gx) !== gy)) ok = false; else m.set(gx, gy); });
      const key = [...m].join(';');
      if (ok && m.size && !seen.has(key)) { seen.add(key); out.push(m); }
    }
    letterMaps.of = raw; letterMaps.val = out;
    return out;
  }
  /** TeX 沿改名表走到底（a→b→c；有环就停）；没改名返回 null。 */
  function renamedTex(tex) {
    const m = renameMap().tex, seen = new Set([tex]);
    let t = tex;
    while (m.has(t) && !seen.has(m.get(t))) { t = m.get(t); seen.add(t); }
    return t === tex ? null : t;
  }
  /** 文字按改名表逐个替换、重复到不再变化（最多 20 轮）；mark = 被锚字在 s 里的位置，随替换平移
   * （被锚字本身被替换时落到替换后的第一个字）。没有任何替换返回 null。 */
  function renamedText(s, mark) {
    const m = renameMap().text;
    let changed = false;
    for (let pass = 0; pass < 20 && m.size; pass++) {
      let any = false;
      for (const [o, n] of m) {
        if (o === n) continue;
        for (let i = s.indexOf(o); i >= 0; i = s.indexOf(o, i + n.length)) {
          if (i + o.length <= mark) mark += n.length - o.length; else if (i <= mark) mark = i;
          s = s.slice(0, i) + n + s.slice(i + o.length);
          any = changed = true;
        }
      }
      if (!any) break;
    }
    return changed ? { s, mark } : null;
  }
  function exactQuote(a, orig, f) {
    const hits = allIndexes(S.index.text, a.prefix + a.exact + a.suffix).map(i => i + a.prefix.length);
    return hits.length ? nearestPos(hits, orig, f) : -1;
  }
  /** 按锚点类型在当前页面找目标：有圈住内容（cover）的跟着这串内容走，其余只按原样精确找。
   * 返回 {el} / {pos} / {el, glyph}（按改名表迁移了的另带 migrated = 新锚点），找不到返回 null。 */
  function findTarget(a, orig, f) {
    if (a.type === 'page') return { el: main };
    if (a.type === 'text' && a.cover) return findCover(a, orig, f);
    if (a.type === 'math' && a.cover) return findCoverGlyph(a);
    return findExact(a, orig, f);
  }
  /** 没有圈住内容（cover）的锚点（用户 10-25 确认过的第 2 次画完的笔，画笔时还没有 cover）：只按原样精确找——正文前后文
   * 一字不差、公式 TeX 相同且符号前后文原样出现、图 src 相同、标题 / 表格文字相同；不用改名表、不模糊匹配。
   * 找不到返回 null（这笔隐藏）。 */
  function findExact(a, orig, f) {
    if (a.gone) return null;   // 迁移时判出内容在新版里没了（repair_notes_positions --apply-snapshot），不画
    if (a.type === 'text') { const p = exactQuote(a, orig, f); return p >= 0 ? { pos: p } : null; }
    let cands = [];
    if (a.type === 'media') {
      cands = [...article.querySelectorAll('img, video')].filter(e => srcKey(e) === a.src);
    } else if (a.type === 'math') {
      const els = displayMath().filter(m => m.tex === a.tex).map(m => m.el);
      if (!a.g) cands = els;
      else {   // 符号锚点：前后各 glyphLen 个符号连同被锚符号原样出现在这个公式里，几处取第一处
        const seq = Array.from(a.g.prefix + a.g.exact + a.g.suffix), k = Array.from(a.g.prefix).length;
        const hits = els.map(el => { const g = glyphList(el), i = seqIndexes(g.ch, seq)[0]; return i === undefined ? null : { el, glyph: g.els[i + k] }; })
                        .filter(Boolean);
        const el = pickCandidate(hits.map(h => h.el), a, orig, f);
        return el ? hits.find(h => h.el === el) : null;
      }
    } else if (a.type === 'cell') {
      cands = [...article.querySelectorAll('td, th')].filter(c => plainText(c) === a.cell && headerText(c) === a.head);
    } else if (a.type === 'heading') {
      cands = [...article.querySelectorAll('h1, h2, h3, h4, h5, h6')].filter(h => plainText(h) === a.text);
    }
    const el = pickCandidate(cands, a, orig, f);
    return el ? { el } : null;
  }
  /** 一笔当前该画在哪：{state: 'ok' | 'hidden' | 'orphan', R（正文卡片内坐标的参考矩形）, el（所在元素）}。 */
  function place(s, f) {
    let c = S.cache.get(s.id);
    if (!c) { c = { target: findTarget(s.anchor, s.orig, f) }; S.cache.set(s.id, c); }
    const t = c.target;
    if (t && t.migrated) {   // 按改名表迁到新写法：改存的锚点，渲染完统一保存并同步
      s.anchor = t.migrated; s.updatedAt = Date.now(); delete t.migrated; S.migrated = true;
    }
    if (!t) return { state: 'orphan', R: s.orig, el: main };
    if (t.el === main) return { state: 'ok', R: s.orig, el: main };
    if (t.glyph) {   // 圈住的公式符号串：画在找到的起始符号上
      const r = elementRect(t.glyph);
      return r ? { state: 'ok', R: localRect(r, f), el: t.el } : { state: 'hidden' };
    }
    if (t.pos !== undefined) {
      const r = charRect(t.pos), node = S.index.nodes[S.index.posNode[t.pos]];
      return r ? { state: 'ok', R: localRect(r, f), el: node.parentElement } : { state: 'hidden' };
    }
    const r = elementRect(t.el);
    return r ? { state: 'ok', R: localRect(r, f), el: t.el } : { state: 'hidden' };
  }

  /* ---------- 8. 笔画几何 ---------- */
  /** 存储坐标 → 正文卡片内坐标 [x, y, 压感]。 */
  function absPoints(s, R) {
    return s.frame === 'norm' ? s.pts.map(p => [R.x + p[0] * R.w, R.y + p[1] * R.h, p[2]])
                              : s.pts.map(p => [R.x + p[0], R.y + p[1], p[2]]);
  }
  /** 正文卡片内坐标 → 存储坐标（norm 存 4 位小数，px 存 2 位）。 */
  function framePoints(abs, R, frameKind) {
    if (frameKind === 'norm') {
      return abs.map(p => [round((p[0] - R.x) / (R.w || 1), 1e4), round((p[1] - R.y) / (R.h || 1), 1e4), round(p[2], 100)]);
    }
    return abs.map(p => [round(p[0] - R.x, 100), round(p[1] - R.y, 100), round(p[2], 100)]);
  }
  /** 一点的半径：笔随压感变粗细（鼠标等无压感记作 -1，用固定粗细）；荧光笔固定。 */
  function radius(s, p) {
    if (s.tool === 'hl') return s.w / 2;
    return p < 0 ? s.w / 2 : (s.w / 2) * (0.35 + 1.3 * Math.max(0.12, Math.min(1, p)));
  }
  const fmt = v => (Math.round(v * 10) / 10).toString();
  /** 笔：每个点一个圆 + 相邻两点间一个梯形，全部同一绕向，nonzero 填充得到粗细可变的连续笔画。 */
  function penPath(s, abs) {
    let d = '';
    for (let i = 0; i < abs.length; i++) {
      const [x, y, p] = abs[i], r = radius(s, p);
      d += `M${fmt(x - r)} ${fmt(y)}a${fmt(r)} ${fmt(r)} 0 1 0 ${fmt(2 * r)} 0a${fmt(r)} ${fmt(r)} 0 1 0 ${fmt(-2 * r)} 0Z`;
      if (i === 0) continue;
      const [x0, y0, p0] = abs[i - 1], r0 = radius(s, p0), len = Math.hypot(x - x0, y - y0);
      if (len < 0.01) continue;
      const nx = -(y - y0) / len, ny = (x - x0) / len;
      let q = [[x0 + nx * r0, y0 + ny * r0], [x + nx * r, y + ny * r], [x - nx * r, y - ny * r], [x0 - nx * r0, y0 - ny * r0]];
      let area = 0;
      for (let k = 0; k < 4; k++) { const a = q[k], b = q[(k + 1) % 4]; area += a[0] * b[1] - b[0] * a[1]; }
      if (area > 0) q = q.reverse();   // 与圆的绕向（面积为负）一致，避免重叠处出现空洞
      d += 'M' + q.map(v => fmt(v[0]) + ' ' + fmt(v[1])).join('L') + 'Z';
    }
    return d;
  }
  /** 荧光笔：一条等宽折线。 */
  function hlPath(abs) {
    return abs.length === 1 ? `M${fmt(abs[0][0])} ${fmt(abs[0][1])}h0.1`
                            : 'M' + abs.map(v => fmt(v[0]) + ' ' + fmt(v[1])).join('L');
  }
  function styleStrokePath(path, s, abs) {
    if (s.tool === 'hl') {
      path.setAttribute('d', hlPath(abs));
      path.setAttribute('fill', 'none'); path.setAttribute('stroke', s.color); path.setAttribute('stroke-width', s.w);
      path.setAttribute('stroke-linecap', 'round'); path.setAttribute('stroke-linejoin', 'round');
      path.setAttribute('opacity', '0.38');
    } else {
      path.setAttribute('d', penPath(s, abs));
      path.setAttribute('fill', s.color); path.setAttribute('fill-rule', 'nonzero');
    }
    path.setAttribute('class', s.tool === 'hl' ? 'hl' : 'pen');
  }
  function bbox(abs, pad) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const [x, y] of abs) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }
  function segDist(px, py, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / l2)) : 0;
    return Math.hypot(px - a[0] - t * dx, py - a[1] - t * dy);
  }
  /** 正文卡片内坐标 (x, y) 半径 rad 内碰到的第一笔（只看当前显示的笔）。 */
  function strokeAt(x, y, rad) {
    for (let i = S.strokes.length - 1; i >= 0; i--) {
      const s = S.strokes[i], d = S.drawn.get(s.id);
      if (!d || !d.abs) continue;
      const pad = rad + s.w;
      const b = d.box;
      if (x < b.x0 - pad || x > b.x1 + pad || y < b.y0 - pad || y > b.y1 + pad) continue;
      const pts = d.abs;
      if (pts.length === 1 && Math.hypot(x - pts[0][0], y - pts[0][1]) <= pad) return s;
      for (let k = 1; k < pts.length; k++) if (segDist(x, y, pts[k - 1], pts[k]) <= rad + s.w / 2) return s;
    }
    return null;
  }

  /* ---------- 9. 渲染（锚点变化或排版变化时重放所有笔；书写中只重画当前这一笔） ---------- */
  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; renderAll(); });
  }
  // 核对页（check/<版本>/）：只显示在这一版上画的笔（NIPC_ONLY = 笔 id 列表），其余笔不画也不动
  const ONLY = Array.isArray(window.NIPC_ONLY) ? new Set(window.NIPC_ONLY) : null;
  function renderAll() {
    ensureIndex();
    const f = frame(), alive = new Set();
    let orphans = 0, hidden = 0;
    for (const s of S.strokes) {
      if (ONLY && !ONLY.has(s.id)) continue;
      alive.add(s.id);
      let d = S.drawn.get(s.id);
      if (!d) {
        const path = document.createElementNS(SVGNS, 'path');
        path.dataset.id = s.id;
        layer.appendChild(path);
        d = { path, key: '' };
        S.drawn.set(s.id, d);
      }
      const pl = place(s, f);
      d.state = pl.state; d.el = pl.el;
      // 圈的内容在本页找不到（orphan）和在收起的折叠里（hidden）一样不画：任何页面都不显示灰色笔，也擦不到、拖不动、打印不出
      d.path.classList.toggle('orphan', pl.state === 'orphan');
      if (pl.state !== 'ok') {
        d.path.style.display = 'none'; d.abs = null; d.key = '';
        if (pl.state === 'orphan') orphans++; else hidden++;
        continue;
      }
      d.path.style.display = '';
      const R = pl.R, key = [R.x, R.y, R.w, R.h].map(v => v.toFixed(2)).join(',');
      if (key !== d.key) {
        d.abs = absPoints(s, R); d.box = bbox(d.abs, 0); d.key = key;
        styleStrokePath(d.path, s, d.abs);
      }
    }
    for (const [id, d] of S.drawn) if (!alive.has(id)) { d.path.remove(); S.drawn.delete(id); }
    S.orphans = orphans; S.hidden = hidden;
    if (ONLY && !S.printing) {
      // 核对页上（笔画的那一版、用户自己的设备和字体）：显示出来的笔就地算出圈住的内容，笔不动，存回笔记
      const todo = S.strokes.filter(s => ONLY.has(s.id) && !s.coverOn && s.anchor.type !== 'media' && s.anchor.type !== 'page');
      if (coverOnDevice(todo, window.NIPC_CHECK_VERSION || PAGE_VERSION).length) { commit(); return; }
    }
    if (S.migrated) { S.migrated = false; saveStored(); schedulePush(); }
    updateStatus();
  }

  /** 在用户设备上（当前页面的排版、字体）把 list 里此刻显示着的笔按它们显示的位置重新算圈住的内容，只改匹配字段
   * （anchor / frame / orig / pts）并记 coverOn = {page: 在哪一版上算的, at, ua}；drawn 不动。返回算了的笔。 */
  function coverOnDevice(list, page) {
    const todo = list.filter(s => { const d = S.drawn.get(s.id); return d && d.state === 'ok' && d.abs; });
    S.geom = null; S.geomBatch = true;
    for (const s of todo) {
      reanchor(s, S.drawn.get(s.id).abs);
      s.coverOn = { page, at: new Date().toISOString(), ua: navigator.userAgent };
    }
    S.geomBatch = false; S.geom = null;
    return todo;
  }

  /* ---------- 10. 操作：书写、橡皮擦、移动、撤销、清空 ---------- */
  function snapshot() { return JSON.stringify(S.strokes); }
  function pushUndo(snap) { S.undo.push(snap); if (S.undo.length > CFG.undoLimit) S.undo.shift(); }
  /** 笔画集合变了：记删除（墓碑）、存本机、更新版式（第一笔会锁定版式）、重画，并安排同步。 */
  function commit() { trackDeletions(); saveStored(); applyLayout(); S.cache.clear(); scheduleRender(); schedulePush(); }
  /** 与上次提交相比消失的笔记墓碑；被撤销恢复、但已有墓碑的笔换新 id（否则同步时会被墓碑删掉）。 */
  function trackDeletions() {
    const now = Date.now(), ids = new Set();
    for (const s of S.strokes) {
      if (S.tombs[s.id]) { s.id = newId(); s.updatedAt = now; }
      ids.add(s.id);
    }
    for (const id of S.lastIds) if (!ids.has(id)) S.tombs[id] = now;
    S.lastIds = ids;
  }
  function forget(id) { S.cache.delete(id); const d = S.drawn.get(id); if (d) d.key = ''; }

  function eventPoints(e) {
    const list = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    return list.length ? list : [e];
  }
  function pressureOf(e) { return e.pointerType === 'pen' ? e.pressure : -1; }

  function startStroke(e) {
    const hl = S.tool === 'hl';
    const s = { tool: hl ? 'hl' : 'pen', color: hl ? CFG.colors.yellow : CFG.colors[S.color], w: hl ? CFG.hlWidth : CFG.penWidth };
    const path = document.createElementNS(SVGNS, 'path');
    layer.appendChild(path);
    const f = frame(), q = toLocal(e.clientX, e.clientY, f);
    S.drawing = { id: e.pointerId, s, path, abs: [[q.x, q.y, pressureOf(e)]], queued: false };
    drawActive();
  }
  function extendStroke(e) {
    const dr = S.drawing, f = frame();
    for (const ev of eventPoints(e)) {
      const q = toLocal(ev.clientX, ev.clientY, f), last = dr.abs[dr.abs.length - 1];
      if (Math.hypot(q.x - last[0], q.y - last[1]) < 0.6 / f.s) continue;   // 太密的点不要
      dr.abs.push([q.x, q.y, pressureOf(ev)]);
    }
    if (!dr.queued) { dr.queued = true; requestAnimationFrame(drawActive); }
  }
  function drawActive() {
    const dr = S.drawing;
    if (!dr) return;
    dr.queued = false;
    styleStrokePath(dr.path, dr.s, dr.abs);
  }
  /** 收笔：按起笔点建锚点，把点换成锚点坐标存起来。 */
  function finishStroke() {
    const dr = S.drawing;
    S.drawing = null;
    dr.path.remove();
    const got = anchorForStroke(dr.abs);
    const s = Object.assign(dr.s, { id: newId(), updatedAt: Date.now(), anchor: got.anchor, frame: got.frame,
                                    orig: roundRect(got.R), pts: framePoints(dr.abs, got.R, got.frame) });
    s.drawn = drawnOf(s);
    pushUndo(snapshot());
    S.strokes.push(s);
    commit();
  }
  const roundRect = R => ({ x: round(R.x, 100), y: round(R.y, 100), w: round(R.w, 100), h: round(R.h, 100) });

  function eraseAt(e) {
    const f = frame(), q = toLocal(e.clientX, e.clientY, f);
    const s = strokeAt(q.x, q.y, CFG.eraseRadius / f.s);
    if (!s) return;
    S.strokes = S.strokes.filter(t => t !== s);
    S.erasing.changed = true;
    scheduleRender();
  }
  function startMove(e) {
    const f = frame(), q = toLocal(e.clientX, e.clientY, f);
    const s = strokeAt(q.x, q.y, CFG.eraseRadius / f.s);
    if (s) S.moving = { id: e.pointerId, s, x: q.x, y: q.y, dx: 0, dy: 0, snap: snapshot() };
  }
  function dragMove(e) {
    const mv = S.moving, q = toLocal(e.clientX, e.clientY, frame());
    mv.dx = q.x - mv.x; mv.dy = q.y - mv.y;
    S.drawn.get(mv.s.id).path.setAttribute('transform', `translate(${fmt(mv.dx)} ${fmt(mv.dy)})`);
  }
  /** 放下被拖动的笔：在新起笔点重新建锚点（灰色的笔拖到对应内容上就重新挂上）。 */
  function finishMove() {
    const mv = S.moving;
    S.moving = null;
    const d = S.drawn.get(mv.s.id);
    d.path.removeAttribute('transform');
    if (Math.hypot(mv.dx, mv.dy) < 0.5 || !d.abs) return;
    reanchor(mv.s, d.abs.map(p => [p[0] + mv.dx, p[1] + mv.dy, p[2]]));
    mv.s.drawn = drawnOf(mv.s);   // 用户移动 = 新的「画的」原样
    pushUndo(mv.snap);
    commit();
  }
  /** 一笔放到新位置 abs（正文卡片坐标）：按它在新位置圈住的内容重新建锚点，点换成新锚点坐标。 */
  function reanchor(s, abs) {
    const got = anchorForStroke(abs);
    Object.assign(s, { updatedAt: Date.now(), anchor: got.anchor, frame: got.frame, orig: roundRect(got.R),
                       pts: framePoints(abs, got.R, got.frame) });
    forget(s.id);
  }
  function undo() {
    if (!S.undo.length) return setStatus('没有可以撤销的操作');
    S.strokes = JSON.parse(S.undo.pop());
    commit();
  }
  function clearPage() {
    if (!S.strokes.length) return;
    if (!confirm(`清空本页全部 ${S.strokes.length} 笔笔记？（之后仍可点「撤销」恢复）`)) return;
    pushUndo(snapshot());
    S.strokes = [];
    commit();
  }

  /* ---------- 11. 输入：笔 / 鼠标画；手指交给浏览器原生滚动（带惯性） ----------
   * 输入层是 touch-action: pan-x pan-y，手指拖动由浏览器自己滚页面，这里不处理手指的指针事件（浏览器开始滚动时会发
   * pointercancel）。Apple Pencil 在 Safari 里的触摸 touchType 是 'stylus'：onTouch 只对笔触（以及正在写字时的任何触摸，
   * 防手掌把页面拖走）调用 preventDefault，页面就不滚，笔的 pointer 事件照常用来画线。 */
  function onDown(e) {
    if (e.pointerType === 'touch') return;
    if (e.button !== 0 || S.drawing || S.erasing || S.moving) return;
    if (S.tool === 'eraser') { S.erasing = { id: e.pointerId, snap: snapshot(), changed: false }; eraseAt(e); }
    else if (S.tool === 'move') { startMove(e); if (!S.moving) return; }
    else startStroke(e);
    e.preventDefault();
    try { capture.setPointerCapture(e.pointerId); } catch (err) { /* 指针已结束 */ }
  }
  function onTouch(e) {
    const stylus = Array.prototype.some.call(e.changedTouches, t => t.touchType === 'stylus');
    if (stylus || S.drawing || S.erasing || S.moving) e.preventDefault();
  }
  function onMove(e) {
    if (S.drawing && e.pointerId === S.drawing.id) extendStroke(e);
    else if (S.erasing && e.pointerId === S.erasing.id) eraseAt(e);
    else if (S.moving && e.pointerId === S.moving.id) dragMove(e);
  }
  function onUp(e) {
    if (S.drawing && e.pointerId === S.drawing.id) finishStroke();
    else if (S.erasing && e.pointerId === S.erasing.id) {
      const er = S.erasing;
      S.erasing = null;
      if (er.changed) { pushUndo(er.snap); commit(); }
    } else if (S.moving && e.pointerId === S.moving.id) finishMove();
  }

  /* ---------- 12. 导出 / 导入 ---------- */
  function dateStamp() {
    const d = new Date(), z = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
  }
  /** 下载 <页面名>_notes_<日期>.json（iPad Safari 上会进「文件」App，可以 AirDrop）。 */
  function exportNotes() {
    const blob = new Blob([JSON.stringify(payload())], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${PAGE_NAME.replace(/\.html?$/, '')}_notes_${dateStamp()}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 30000);
    setStatus(`已导出 ${S.strokes.length} 笔：${a.download}`);
  }
  let pendingImport = null;
  async function readImportFile(file) {
    let data;
    try { data = JSON.parse(await file.text()); } catch (e) { return setStatus('这个文件不是笔记 JSON', true); }
    if (!data || data.format !== 'nipc-notes' || !Array.isArray(data.strokes) || data.version > CFG.version) {
      return setStatus('这个文件不是本网页导出的笔记（或版本更新），没有导入', true);
    }
    if (data.page !== PAGE_NAME && !confirm(`这个笔记来自 ${data.page}，当前是 ${PAGE_NAME}，仍然导入？`)) return;
    pendingImport = data.strokes.filter(validStroke);
    choice.querySelector('p').textContent = `文件里有 ${pendingImport.length} 笔。本页现有 ${S.strokes.length} 笔：`;
    choice.hidden = false;
  }
  /** mode = 'replace'：用文件里的笔替换本页；'merge'：加进本页（同一笔不重复）。 */
  function applyImport(mode) {
    choice.hidden = true;
    const incoming = pendingImport;
    pendingImport = null;
    if (!incoming || mode === 'cancel') return;
    pushUndo(snapshot());
    if (mode === 'replace') S.strokes = incoming;
    else { const have = new Set(S.strokes.map(s => s.id)); S.strokes = S.strokes.concat(incoming.filter(s => !have.has(s.id))); }
    commit();
    setStatus(`已导入 ${incoming.length} 笔（${mode === 'replace' ? '替换' : '合并'}）`);
  }

  /* ---------- 12b. 同步到 GitHub 私有仓库（填了 token 才启用；没填一切照旧只存本机） ----------
   * 每页一个文件 notes/<页名>.json，内容 = payload()（含墓碑）。用 GitHub REST Contents API：
   * GET /repos/{owner}/{repo}/contents/{path} 取 base64 内容和 sha；PUT 同一路径（message、base64 content、sha；新建不带 sha）。
   * https://docs.github.com/en/rest/repos/contents
   * 合并而不覆盖：按 id 取并集，同一 id 取 updatedAt 新的；墓碑（id → 删除时刻）胜过同 id 的笔。
   * token 只存在 localStorage 的 SYNC_KEY 下，不写进日志、不进导出文件。 */
  const SYNC_KEY = 'nipc-notes-sync';
  const DEFAULT_REPO = 'delluluwxy/neural-ipc-notes';
  const PUSH_DELAY_MS = 3000;
  const SYNC_RETRIES = 3;
  const SY = { sha: null, timer: null, busy: false, again: false, dirty: false, state: '', warn: false };

  function syncCfg() {
    if (SNAP) return { token: '', repo: DEFAULT_REPO };   // 快照页从不同步
    try { const c = JSON.parse(localStorage.getItem(SYNC_KEY) || '{}') || {}; return { token: c.token || '', repo: c.repo || DEFAULT_REPO }; }
    catch (e) { return { token: '', repo: DEFAULT_REPO }; }
  }
  function saveSyncCfg(cfg) {
    try { localStorage.setItem(SYNC_KEY, JSON.stringify(cfg)); return true; } catch (e) { return false; }
  }
  const notesPath = () => 'notes/' + PAGE_NAME.replace(/\.html?$/, '') + '.json';
  /** UTF-8 安全的 base64（笔记里有中文锚点文字，btoa 不能直接处理）。 */
  function b64EncodeUtf8(str) {
    const bytes = new TextEncoder().encode(str);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }
  function b64DecodeUtf8(b64) {
    const bin = atob(String(b64).replace(/\s/g, ''));
    return new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0)));
  }
  function ghRequest(cfg, init) {
    return fetch(`https://api.github.com/repos/${cfg.repo}/contents/${notesPath()}`, Object.assign({
      cache: 'no-store',
      headers: { Authorization: 'Bearer ' + cfg.token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
    }, init));
  }
  /** 远端文件：{data（解析后的 JSON 或 null）, sha（不存在为 null）}。 */
  async function ghGet(cfg) {
    const r = await ghRequest(cfg, { method: 'GET' });
    if (r.status === 404) return { data: null, sha: null };
    if (!r.ok) throw new Error('读取 ' + r.status);
    const j = await r.json();
    return { data: JSON.parse(b64DecodeUtf8(j.content)), sha: j.sha };
  }
  /** 写远端文件；sha 过期（409 / 422）返回 {conflict: true}。 */
  async function ghPut(cfg, data, sha, keepalive) {
    const body = { message: `notes: ${PAGE_NAME} ${new Date().toISOString()}`, content: b64EncodeUtf8(JSON.stringify(data)) };
    if (sha) body.sha = sha;
    const r = await ghRequest(cfg, { method: 'PUT', body: JSON.stringify(body), keepalive: !!keepalive });
    if (r.status === 409 || r.status === 422) return { conflict: true };
    if (!r.ok) throw new Error('写入 ' + r.status);
    const j = await r.json();
    return { sha: j.content && j.content.sha };
  }
  /** 合并本机与远端：并集；同 id 取 updatedAt 新的；墓碑取较晚的删除时刻且胜过同 id 的笔。 */
  function mergeNotes(local, remote) {
    const tombs = Object.assign({}, validTombs(remote && remote.tombstones));
    for (const [id, t] of Object.entries(local.tombs)) tombs[id] = Math.max(tombs[id] || 0, t);
    const byId = new Map();
    const remoteStrokes = remote && Array.isArray(remote.strokes) ? remote.strokes.filter(validStroke) : [];
    for (const s of local.strokes.concat(remoteStrokes)) {
      const o = byId.get(s.id);
      // drawn 只随用户画 / 移动产生：选中的那份没有 drawn（老格式）而另一份有，就带上另一份的，绝不丢
      if (!o || (s.updatedAt || 0) > (o.updatedAt || 0)) byId.set(s.id, !s.drawn && o && o.drawn ? Object.assign({}, s, { drawn: o.drawn }) : s);
      else if (!o.drawn && s.drawn) byId.set(s.id, Object.assign({}, o, { drawn: s.drawn }));
    }
    return { strokes: [...byId.values()].filter(s => !tombs[s.id]), tombs };
  }
  /** 只比笔画和墓碑（忽略保存时间），判断远端是否已经是这个内容。 */
  function sameNotes(a, b) {
    const key = d => JSON.stringify([d.strokes.map(s => s.id + ':' + (s.updatedAt || 0)).sort(),
                                     Object.keys(validTombs(d.tombstones || d.tombs)).sort()]);
    return key(a) === key(b);
  }
  /** 把合并结果装进页面（不触发再次同步）。 */
  function applyMerged(m) {
    S.strokes = m.strokes; S.tombs = m.tombs; S.lastIds = new Set(m.strokes.map(s => s.id));
    saveStored(); applyLayout(); S.cache.clear(); scheduleRender();
  }
  function setSync(state, warn) { SY.state = state; SY.warn = !!warn; updateStatus(); }
  /** 拉取 → 合并 → 需要时推送；sha 冲突时重新拉取合并再推，最多重试 SYNC_RETRIES 次。 */
  async function syncNow() {
    const cfg = syncCfg();
    if (!cfg.token) return;
    if (SY.busy) { SY.again = true; return; }
    clearTimeout(SY.timer);   // 这次同步已包含所有本机改动，取消还没到点的防抖推送
    SY.busy = true; SY.dirty = false;
    setSync('同步中');
    try {
      for (let attempt = 0; attempt <= SYNC_RETRIES; attempt++) {
        const remote = await ghGet(cfg);
        const merged = mergeNotes({ strokes: S.strokes, tombs: S.tombs }, remote.data);
        applyMerged(merged);
        if (remote.data && sameNotes(remote.data, merged)) { SY.sha = remote.sha; setSync('已同步'); return; }
        const put = await ghPut(cfg, payload(), remote.sha, false);
        if (!put.conflict) { SY.sha = put.sha; setSync('已同步'); return; }
      }
      throw new Error(`冲突重试 ${SYNC_RETRIES} 次仍失败`);
    } catch (e) {
      SY.dirty = true;
      setSync('失败（' + (e && e.message ? e.message : '网络错误') + '）', true);
    } finally {
      SY.busy = false;
      if (SY.again) { SY.again = false; schedulePush(); }
    }
  }
  /** 本机改动后约 3 s 推送（连续改动只推一次）。 */
  function schedulePush() {
    if (!syncCfg().token) return;
    SY.dirty = true;
    clearTimeout(SY.timer);
    SY.timer = setTimeout(syncNow, PUSH_DELAY_MS);
  }
  /** 页面切到后台：有没推的改动就立刻用 keepalive 推一次（按已知 sha；冲突留给下次打开时合并）。 */
  function pushOnHide() {
    const cfg = syncCfg();
    if (document.visibilityState !== 'hidden' || !cfg.token || !SY.dirty || SY.busy) return;
    clearTimeout(SY.timer);
    ghPut(cfg, payload(), SY.sha, true).then(r => { if (!r.conflict) { SY.dirty = false; SY.sha = r.sha; } }, () => {});
  }
  function wireSync() {
    const cfg = syncCfg();
    $('#annot-token').value = cfg.token;
    $('#annot-repo').value = cfg.repo;
    $('#annot-sync').addEventListener('click', () => { $('#annot-sync-panel').hidden = !$('#annot-sync-panel').hidden; });
    $('#annot-sync-save').addEventListener('click', () => {
      const ok = saveSyncCfg({ token: $('#annot-token').value.trim(), repo: $('#annot-repo').value.trim() || DEFAULT_REPO });
      setSync(ok ? (syncCfg().token ? '设置已保存' : '已清除 token，只存本机') : '设置保存失败（浏览器存储不可用）', !ok);
      if (syncCfg().token) syncNow();
    });
    $('#annot-sync-now').addEventListener('click', syncNow);
    document.addEventListener('visibilitychange', pushOnHide);
  }

  /* ---------- 13. 打印：每一笔挪进它所在的块，分页后仍跟着内容 ---------- */
  function hostFor(el) {
    if (!el || el === main) return main;
    const wrap = el.closest('.tablewrap');
    if (wrap) return wrap;
    const h = el.closest('figure, p, li, h1, h2, h3, h4, h5, h6, blockquote, pre, summary, dt, dd, .demo, .lead');
    return h && article.contains(h) ? h : main;
  }
  function enterPrint() {
    if (S.printing) return;
    renderAll();
    S.printing = true;
    const f = frame();
    for (const s of S.strokes) {
      const d = S.drawn.get(s.id);
      if (!d || !d.abs) continue;
      const host = hostFor(d.el);
      const hr = localRect(host.getBoundingClientRect(), f);
      const svg = document.createElementNS(SVGNS, 'svg');
      svg.setAttribute('class', 'annot-print');
      svg.style.left = fmt(-(hr.x + host.clientLeft)) + 'px';
      svg.style.top = fmt(-(hr.y + host.clientTop)) + 'px';
      svg.appendChild(d.path.cloneNode(true));
      host.classList.add('annot-host');
      host.appendChild(svg);
    }
    document.documentElement.classList.add('annot-printing');
  }
  function exitPrint() {
    if (!S.printing) return;
    document.querySelectorAll('svg.annot-print').forEach(n => n.remove());
    document.querySelectorAll('.annot-host').forEach(n => n.classList.remove('annot-host'));
    document.documentElement.classList.remove('annot-printing');
    S.printing = false;
    scheduleRender();
  }

  /* ---------- 14. 工具栏 ---------- */
  const ui = document.createElement('div');
  ui.className = 'annot-ui';
  ui.id = 'annot-ui';
  ui.innerHTML = `
    <button type="button" id="annot-fab" aria-label="展开笔记工具栏">✎ 笔记</button>
    <div id="annot-panel" role="toolbar" aria-label="手写笔记">
      <div class="annot-row">
        <button type="button" id="annot-toggle" aria-pressed="false">标注 关</button>
        <button type="button" id="annot-collapse" aria-label="收起工具栏">收起</button>
      </div>
      <div class="annot-row" id="annot-tools">
        <button type="button" class="annot-swatch" data-tool="pen" data-color="red" style="--c:${CFG.colors.red}" aria-label="红笔"></button>
        <button type="button" class="annot-swatch" data-tool="pen" data-color="blue" style="--c:${CFG.colors.blue}" aria-label="蓝笔"></button>
        <button type="button" class="annot-swatch" data-tool="pen" data-color="black" style="--c:${CFG.colors.black}" aria-label="黑笔"></button>
        <button type="button" class="annot-swatch hl" data-tool="hl" style="--c:${CFG.colors.yellow}" aria-label="荧光笔"></button>
        <button type="button" data-tool="eraser">橡皮</button>
        <button type="button" data-tool="move">移动</button>
      </div>
      <div class="annot-row">
        <button type="button" id="annot-undo">撤销</button>
        <button type="button" id="annot-clear">清空本页</button>
        <button type="button" id="annot-export">导出</button>
        <button type="button" id="annot-import">导入</button>
        <button type="button" id="annot-sync">同步</button>
        <input type="file" id="annot-file" accept=".json,application/json" tabindex="-1" aria-hidden="true">
      </div>
      <div id="annot-sync-panel" hidden>
        <label>GitHub token <input type="password" id="annot-token" autocomplete="off" spellcheck="false"></label>
        <label>仓库 <input type="text" id="annot-repo" autocomplete="off" spellcheck="false"></label>
        <button type="button" id="annot-sync-save">保存</button>
        <button type="button" id="annot-sync-now">立即同步</button>
      </div>
      <div id="annot-choice" hidden><p></p>
        <button type="button" data-import="replace">替换</button>
        <button type="button" data-import="merge">合并</button>
        <button type="button" data-import="cancel">取消</button>
      </div>
      <div id="annot-status" aria-live="polite"></div>
    </div>`;
  const $ = sel => ui.querySelector(sel);
  const choice = $('#annot-choice');
  let statusMsg = '', statusWarn = false;

  function setStatus(msg, warn) { statusMsg = msg; statusWarn = !!warn; updateStatus(); }
  function updateStatus() {
    const box = $('#annot-status');
    box.textContent = '';
    const line = document.createElement('div');
    line.textContent = `本页 ${S.strokes.length} 笔` + (S.hidden ? `（${S.hidden} 笔在收起的折叠框里）` : '');
    box.appendChild(line);
    if (SY.state) {
      const s = document.createElement('div');
      s.id = 'annot-sync-state';
      if (SY.warn) s.className = 'warn';
      s.textContent = SY.state;
      box.appendChild(s);
    }
    if (S.orphans) {
      const w = document.createElement('div');
      w.className = 'warn';
      w.textContent = `有 ${S.orphans} 笔圈的内容已改或删，已隐藏`;
      box.appendChild(w);
    }
    if (statusMsg) {
      const m = document.createElement('div');
      if (statusWarn) m.className = 'warn';
      m.textContent = statusMsg;
      box.appendChild(m);
    }
    $('#annot-fab').textContent = S.orphans ? `✎ 笔记 · ${S.orphans} 笔已隐藏` : '✎ 笔记';
  }
  function setOn(on) {
    S.on = on;
    const t = $('#annot-toggle');
    t.setAttribute('aria-pressed', String(on));
    t.textContent = on ? '标注 开' : '标注 关';
    applyLayout();
    scheduleRender();
  }
  function selectTool(tool, color) {
    S.tool = tool;
    if (color) S.color = color;
    ui.querySelectorAll('[data-tool]').forEach(b => {
      b.setAttribute('aria-pressed', String(b.dataset.tool === tool && (tool !== 'pen' || b.dataset.color === S.color)));
    });
    if (!S.on) setOn(true);
  }
  function setCollapsed(c) {
    ui.dataset.collapsed = c ? '1' : '0';
    const pref = loadUi(); pref.collapsed = c; saveUi(pref);
  }
  function wireToolbar() {
    $('#annot-fab').addEventListener('click', () => setCollapsed(false));
    $('#annot-collapse').addEventListener('click', () => setCollapsed(true));
    $('#annot-toggle').addEventListener('click', () => setOn(!S.on));
    $('#annot-tools').addEventListener('click', e => {
      const b = e.target.closest('[data-tool]');
      if (b) selectTool(b.dataset.tool, b.dataset.color);
    });
    $('#annot-undo').addEventListener('click', undo);
    $('#annot-clear').addEventListener('click', clearPage);
    $('#annot-export').addEventListener('click', exportNotes);
    $('#annot-import').addEventListener('click', () => $('#annot-file').click());
    $('#annot-file').addEventListener('change', e => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      if (file) readImportFile(file);
    });
    choice.addEventListener('click', e => { const b = e.target.closest('[data-import]'); if (b) applyImport(b.dataset.import); });
  }

  /* ---------- 15. 启动：等 MathJax 排完公式再挂图层（公式会改变正文文字和高度） ---------- */
  const layer = document.createElementNS(SVGNS, 'svg');
  layer.id = 'annot-layer';
  layer.setAttribute('aria-hidden', 'true');
  const capture = document.createElement('div');
  capture.id = 'annot-capture';

  function whenMathReady(cb) {
    const t0 = Date.now();
    let done = false;
    const go = () => { if (!done) { done = true; cb(); } };
    (function wait() {
      const mj = window.MathJax;
      if (mj && mj.startup && mj.startup.promise) mj.startup.promise.then(go, go);
      else if (Date.now() - t0 > 15000) go();          // MathJax 没加载上（离线）：照样启用，只是没有公式锚点
      else setTimeout(wait, 100);
    })();
  }
  function start() {
    main.appendChild(layer);
    document.body.appendChild(capture);
    document.body.appendChild(ui);
    wireToolbar();
    const pref = loadUi();
    ui.dataset.collapsed = pref.collapsed === false ? '0' : '1';
    selectTool('pen', 'red');
    setOn(false);
    const stored = loadStored();
    S.strokes = stored.strokes; S.tombs = stored.tombs; S.lastIds = new Set(S.strokes.map(s => s.id));
    wireSync();
    applyLayout();
    renderAll();
    syncNow();   // 填了 token 才会联网：打开页面先拉取合并

    capture.addEventListener('pointerdown', onDown);
    capture.addEventListener('pointermove', onMove);
    capture.addEventListener('pointerup', onUp);
    capture.addEventListener('pointercancel', onUp);
    capture.addEventListener('touchstart', onTouch, { passive: false });   // 非被动：preventDefault 才能阻止滚动
    capture.addEventListener('touchmove', onTouch, { passive: false });
    addEventListener('resize', () => { applyLayout(); scheduleRender(); });
    new ResizeObserver(scheduleRender).observe(article);
    article.addEventListener('toggle', scheduleRender, true);
    new MutationObserver(() => { if (!S.printing) { S.domVersion++; scheduleRender(); } })
      .observe(article, { childList: true, subtree: true, characterData: true });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleRender);
    addEventListener('load', scheduleRender);
    addEventListener('beforeprint', enterPrint);
    addEventListener('afterprint', exitPrint);
    const printMq = matchMedia('print');
    const onPrintMq = () => (printMq.matches ? enterPrint() : exitPrint());
    if (printMq.addEventListener) printMq.addEventListener('change', onPrintMq); else printMq.addListener(onPrintMq);

    /** 核对旁注用：这笔此刻挂在正文哪个字上——那个字前后各 10 字，和笔画起点相对它的位置（px）；不是挂在正文字上返回 null。 */
    /** 核对用：这笔此刻挂在哪——从挂靠点起、和圈住内容一样长的那串字 / 公式符号，和笔画起点相对第一个的位置（px）。 */
    function targetOf(id, d, a) {
      const c = S.cache.get(id), t = c && c.target, n = Array.from(a.cover || '').length;
      if (!t || !n) return null;
      const f = frame(), off = r => { const R = localRect(r, f); return [Math.round((d.abs[0][0] - R.x) * 2) / 2, Math.round((d.abs[0][1] - R.y) * 2) / 2]; };
      if (t.glyph) {
        const g = glyphList(t.el), i = g.els.indexOf(t.glyph), r = elementRect(t.glyph);
        return i >= 0 && r ? { ch: g.ch.slice(i, i + n).join(''), off: off(r) } : null;
      }
      if (t.pos === undefined) return null;
      const r = charRect(t.pos);
      return r ? { ch: S.index.text.slice(t.pos, t.pos + n), off: off(r) } : null;
    }
    function anchorCtx(id, d) {
      const c = S.cache.get(id), t = c && c.target;
      if (!t || t.pos === undefined) return null;
      const r = charRect(t.pos), f = frame();
      if (!r) return null;
      const R = localRect(r, f), tx = S.index.text;
      return { ctx: tx.slice(Math.max(0, t.pos - 10), t.pos + 11),
               off: [Math.round((d.abs[0][0] - R.x) * 2) / 2, Math.round((d.abs[0][1] - R.y) * 2) / 2] };
    }
    // 给自动化测试读状态用（tools/test_annotate.py）；页面本身不依赖它
    window.__nipcNotes = {/*PROBE_API*/
      strokes: () => S.strokes, render: renderAll, frame,
      placed: id => { const d = S.drawn.get(id); return d ? { state: d.state, abs: d.abs } : null; },
      status: () => ({ orphans: S.orphans, hidden: S.hidden, on: S.on, sync: SY.state, tombs: Object.keys(S.tombs).length }),
      // 修位置用（tools/check_notes_anchors.py --repair）：同「移动」工具，把一笔平移 (dx, dy)（正文卡片坐标）后重新建锚点；
      // 调用前要把新起笔点滚进视口。返回新锚点类型，找不到这一笔返回 null。
      // 核对用（只读，不改笔记）：这些笔此刻各圈住了什么——锚点类型、圈住的内容串、笔画起点相对内容起点的位置（px）
      coverOf: ids => {
        S.geom = null; S.geomBatch = true;
        const out = {};
        for (const id of ids) {
          const d = S.drawn.get(id);
          if (!d || !d.abs || d.state !== 'ok') { out[id] = { state: d ? d.state : 'none' }; continue; }
          const got = anchorForStroke(d.abs), a = got.anchor;
          out[id] = { state: 'ok', type: a.type, cover: a.cover || a.src || a.text || a.cell || '',
                      hit: (got.hit || []).map(([c, x, y]) => [c, Math.round(x * 2) / 2, Math.round(y * 2) / 2]),
                      exact: a.cover ? '' : (a.exact || ''), tex: a.tex || '', at: anchorCtx(id, d),
                      target: targetOf(id, d, (S.strokes.find(s => s.id === id) || {}).anchor || {}),
                      off: [Math.round((d.abs[0][0] - got.R.x) * 2) / 2, Math.round((d.abs[0][1] - got.R.y) * 2) / 2],
                      rel: got.frame === 'norm' ? [Math.round((d.abs[0][0] - got.R.x) / got.R.w * 1000) / 1000,
                                                   Math.round((d.abs[0][1] - got.R.y) / got.R.h * 1000) / 1000] : null };
        }
        S.geomBatch = false; S.geom = null;
        return out;
      },
      texList: () => allMath().map(m => m.tex),
      // 核对字体排版用（只读）：每笔存的锚点此刻在本机排版下的参考矩形（正文卡片 px），与笔里存的画笔设备上的 orig 比
      anchorRects: ids => Object.fromEntries(ids.map(id => {
        const s = S.strokes.find(t => t.id === id);
        if (!s) return [id, null];
        const pl = place(s, frame());
        return [id, pl.state === 'ok' ? { x: pl.R.x, y: pl.R.y, w: pl.R.w, h: pl.R.h, type: s.anchor.type } : null];
      })),
      // 查原因用（只读）：一笔圈住的关键词在当前页面每一处出现的位置、所在段落相似度、是否在收起的折叠里
      explain: id => {
        const s = S.strokes.find(t => t.id === id);
        if (!s || !s.anchor.cover) return null;
        ensureIndex();
        const a = s.anchor, t = S.index.text, half = Math.max(40, (a.block || '').length >> 1);
        if (a.type === 'math') {
          const seq = Array.from(a.cover);
          return { type: 'math', tex: a.tex, cover: a.cover, renamed: renamedTex(a.tex),
                   own: allMath().filter(m => m.tex === a.tex).length,
                   hits: allMath().filter(m => seqIndexes(glyphList(m.el).ch, seq).length).map(m => m.tex) };
        }
        return { type: 'text', cover: a.cover, hits: allIndexes(t, a.cover, 2000).map(p => ({
          pos: p, sb: Math.round(bigramSim(a.block || '', t.slice(Math.max(0, p - half), p + a.cover.length + half)) * 1000) / 1000,
          closed: !charRect(p), around: t.slice(Math.max(0, p - 15), p + a.cover.length + 15) })) };
      },
      // 旧笔迁移用（repair_notes_positions.py --reanchor-origin，在它当初画的那版页面上调）：这些笔原地不动，
      // 按它们此刻圈住的内容重新建锚点。返回实际改了的笔的 id（此刻不在页面上显示的笔不动）。
      reanchorAll: ids => {
        const done = [];
        S.geom = null; S.geomBatch = true;   // 这一批笔逐个处理时排版不变，页面几何只量一次
        const byId = new Map(S.strokes.map(s => [s.id, s]));
        for (const id of ids) {
          const s = byId.get(id), d = S.drawn.get(id);
          if (s && d && d.abs && d.state === 'ok') { reanchor(s, d.abs); done.push(id); }
        }
        S.geomBatch = false; S.geom = null;
        commit();
        return done;
      },
      moveBy: (id, dx, dy) => {
        const s = S.strokes.find(t => t.id === id), d = S.drawn.get(id);
        if (!s || !d || !d.abs) return null;
        reanchor(s, d.abs.map(p => [p[0] + dx, p[1] + dy, p[2]]));
        commit();
        return s.anchor.type;
      },
      syncNow,
    };
  }
  whenMathReady(start);
})();
""".replace("/*PROBE_API*/", PROBE_API_JS)
