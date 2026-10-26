"""逐笔核对用户的手写笔记还落不落在原来的内容上（只读，只打印，不写任何文件）。

从笔记仓库（gh api，默认 delluluwxy/neural-ipc-notes 的 notes/<页名>.json）取出全部笔，在本地生成好的页面上
按 annotate.py 同样的规则找每一笔的锚点，分四类：
  exact    原样找到（公式 TeX、图 src、标题/表格文字、正文前后文都一字不差）；
  renamed  原样找不到，按页面里嵌的记号改名表（notation_renames_<页>.json）换写法后找到；
  fuzzy    上面都不行，只能靠模糊匹配（标注层会挂到「最像」的地方，可能挂错）；
  orphan   页面上找不到（标注层显示为灰色）。
fuzzy / orphan 就是「笔记没跟着内容走」，逐条打印锚点内容，供补改名表。

用法：
    /bin/python tools/check_notes_anchors.py week3_4.html [--notes <本地 json>]
    /bin/python tools/check_notes_anchors.py week3_4.html --write-quote-map <对应表 json> <改名表 json>
    /bin/python tools/check_notes_anchors.py week3_4.html --browser     （导入真实页面，读标注层自己判出的标灰笔数）
    /bin/python tools/check_notes_anchors.py week3_4.html --compare <旧版页面绝对路径> <旧笔记 json>
        （逐笔比旧页 + 旧笔记与新页 + 当前笔记盖住的内容；「标灰 0」不等于位置对，以这个为准）
第二种：对应表是 [{"old": "旧锚点里带【】的一小段", "new": "新正文里带【】的一小段"}, …]，【】括住一个字：old 用来认出
是哪一笔（在没原样找到的笔里唯一匹配），new 指出它该落到的新字（在页面正文里唯一出现）。按新字取前后各 30 字
（与 annotate.py quoteLen 同）作新锚点，写进改名表的 "quote"（同一旧锚点的旧条目被替换），再照常核对一遍。
依赖 playwright（用户目录安装的 chromium）与已登录的 gh。
"""
import argparse
import json
import math
import pathlib
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = "delluluwxy/neural-ipc-notes"
QUOTE_LEN = 30          # annotate.py CFG.quoteLen
KEYS = ("prefix", "exact", "suffix")

# 与 annotate.py 第 5–7 节同样的正文索引（空白合并、跳过公式/脚本/标注层）、陈列公式 TeX、标题与表格文字
PAGE_JS = """() => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const article = document.querySelector('main article');
  const skip = n => !n.parentElement || !!n.parentElement.closest('mjx-container, script, style, svg, .annot-ui');
  const plain = el => { const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); let s = '';
    for (let n = w.nextNode(); n; n = w.nextNode()) if (!skip(n)) s += n.data; return norm(s); };
  const w = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
  let chars = [], last = true;
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (skip(n)) continue;
    for (const c of n.data) { const sp = /\\s/.test(c); if (sp && last) continue; chars.push(sp ? ' ' : c); last = sp; }
  }
  const math = [];
  for (const m of MathJax.startup.document.math)
    if (m.display && m.typesetRoot && article.contains(m.typesetRoot)) math.push(norm(m.math));
  const src = [...article.querySelectorAll('img, video')].map(e =>
    (e.getAttribute('src') || '').split('#')[0].split('?')[0].split('/').pop());
  const el = document.getElementById('annot-renames');
  return { text: chars.join(''), math, src,
           heads: [...article.querySelectorAll('h1,h2,h3,h4,h5,h6')].map(plain),
           cells: [...article.querySelectorAll('td,th')].map(plain),
           renames: el ? JSON.parse(el.textContent || '{}') : {} };
}"""


def load_notes(page_file, path):
    """本地 json 或笔记仓库里的 notes/<页名>.json。"""
    if path:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    out = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                          f"repos/{REPO}/contents/notes/{pathlib.Path(page_file).stem}.json"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def page_data(rel):
    """打开生成好的页面，等 MathJax 排完，取出锚点要用的全部内容。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto((ROOT / rel).as_uri(), wait_until="domcontentloaded", timeout=120000)
        page.wait_for_function("window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
        page.evaluate("MathJax.startup.promise")
        data = page.evaluate(PAGE_JS)
        browser.close()
    return data


def norm(s):
    return " ".join((s or "").split())


def follow(table, key):
    """沿改名表走到底（同 annotate.py renamedTex，有环就停）；没改名返回 None。"""
    seen, k = {key}, key
    while k in table and table[k] not in seen:
        k = table[k]
        seen.add(k)
    return None if k == key else k


def rename_text(table, s):
    """文字按改名表逐个替换到不再变化（同 annotate.py renamedText，最多 20 轮）；没替换返回 None。"""
    changed = False
    for _ in range(20):
        hit = False
        for o, n in table.items():
            if o != n and o in s:
                s, hit, changed = s.replace(o, n), True, True
        if not hit:
            break
    return s if changed else None


def classify(a, d):
    """一笔的锚点在当前页上的去向：(类别, 给人看的锚点内容)。"""
    tex = {norm(k): norm(v) for k, v in d["renames"].get("tex", {}).items()}
    txt = {norm(k): norm(v) for k, v in d["renames"].get("text", {}).items()}
    t = a.get("type")
    if t == "page":
        return "exact", "（整页）"
    if t == "media":
        return ("exact" if a["src"] in d["src"] else "orphan"), f"图 {a['src']}"
    if t == "math":
        if a["tex"] in d["math"]:
            return "exact", a["tex"]
        t2 = follow(tex, a["tex"])
        return ("renamed" if t2 in d["math"] else "orphan"), a["tex"]
    if t in ("heading", "cell"):
        key, pool = (a["text"], d["heads"]) if t == "heading" else (a["cell"], d["cells"])
        if key in pool:
            return "exact", key
        r = rename_text(txt, key)
        return ("renamed" if r in pool else "orphan"), key
    quote = a["prefix"] + a["exact"] + a["suffix"]
    shown = f"{a['prefix']}【{a['exact']}】{a['suffix']}"          # 【】= 这一笔锚定的那个字
    if quote in d["text"]:
        return "exact", shown
    for e in d["renames"].get("quote", []):                       # 逐笔迁移表（同 annotate.py findQuote）
        if all(e["from"][k] == a[k] for k in KEYS) and "".join(e["to"][k] for k in KEYS) in d["text"]:
            return "renamed", shown
    r = rename_text(txt, quote)
    if r and r in d["text"]:
        return "renamed", shown
    seed_p, seed_s = a["prefix"][-4:] + a["exact"], a["exact"] + a["suffix"][:4]
    return ("fuzzy" if seed_p in d["text"] or seed_s in d["text"] else "orphan"), shown


def bracketed(s):
    """「…【x】…」→ (去掉括号的整段, x 在其中的位置)。"""
    i = s.index("【")
    assert s[i + 2] == "】", f"【】里必须正好一个字：{s}"
    return s[:i] + s[i + 1] + s[i + 3:], i


def write_quote_map(notes, d, map_path, renames_path):
    """对应表 → 改名表的 quote 条目（见模块说明）。返回写入条数。"""
    entries = json.loads(pathlib.Path(map_path).read_text(encoding="utf-8"))
    loose = [s["anchor"] for s in notes["strokes"] if s["anchor"].get("type") == "text"
             and s["anchor"]["prefix"] + s["anchor"]["exact"] + s["anchor"]["suffix"] not in d["text"]]
    out = []
    for e in entries:
        hits = {tuple(a[k] for k in KEYS) for a in loose if e["old"] in f"{a['prefix']}【{a['exact']}】{a['suffix']}"}
        assert len(hits) == 1, f"old 应唯一对应一个旧锚点，实得 {len(hits)}：{e['old']}"
        needle, k = bracketed(e["new"])
        at = d["text"].find(needle)
        assert at >= 0 and d["text"].find(needle, at + 1) < 0, f"new 在页面正文里应唯一出现：{e['new']}"
        p, t = at + k, d["text"]
        out.append({"from": dict(zip(KEYS, hits.pop())),
                    "to": {"prefix": t[max(0, p - QUOTE_LEN):p], "exact": t[p], "suffix": t[p + 1:p + 1 + QUOTE_LEN]}})
    table = json.loads(pathlib.Path(renames_path).read_text(encoding="utf-8"))
    keep = [q for q in table.get("quote", []) if all(q["from"] != o["from"] for o in out)]
    table["quote"] = keep + out
    pathlib.Path(renames_path).write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(out)


# 每笔在页面上实际盖住了什么：笔迹包围盒与正文逐字、公式、图 / 视频的包围盒求交（页面坐标）
COVER_JS = """() => {
  const art = document.querySelector('main article');
  const skip = n => !n.parentElement || !!n.parentElement.closest('mjx-container, script, style, svg, .annot-ui');
  const box = q => [q.left + scrollX, q.top + scrollY, q.right + scrollX, q.bottom + scrollY];
  const chars = [], rg = document.createRange(), w = document.createTreeWalker(art, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (skip(n)) continue;
    for (let i = 0; i < n.data.length; i++) {
      if (/\\s/.test(n.data[i])) continue;
      rg.setStart(n, i); rg.setEnd(n, i + 1);
      const q = rg.getBoundingClientRect();
      if (q.width || q.height) chars.push([n.data[i], ...box(q)]);
    }
  }
  const maths = [...MathJax.startup.document.math].filter(m => m.typesetRoot && art.contains(m.typesetRoot))
    .map(m => [m.math.replace(/\\s+/g, ' ').trim(), ...box(m.typesetRoot.getBoundingClientRect())]);
  const media = [...art.querySelectorAll('img, video')]
    .map(e => [(e.getAttribute('src') || '').split('/').pop(), ...box(e.getBoundingClientRect())]);
  const out = {};
  for (const p of document.querySelectorAll('#annot-layer path[data-id]')) {
    const q = p.getBoundingClientRect(), b = box(q);
    const hit = r => r[1] < b[2] && r[3] > b[0] && r[2] < b[3] && r[4] > b[1];
    out[p.dataset.id] = {shown: !!(q.width || q.height) && getComputedStyle(p).display !== 'none',
                         gray: p.classList.contains('orphan'),
                         text: chars.filter(hit).map(c => c[0]).join(''),
                         math: maths.filter(hit).map(m => m[0]), media: media.filter(hit).map(m => m[0])};
  }
  return out;
}"""


def covered(page_path, notes, shots=None):
    """把笔记导入页面，返回 {笔 id: {closed_shown, gray, text, math, media}}：closed_shown 是折叠全收起时
    这一笔看不看得见；其余是折叠全展开后它盖住的正文字、公式 TeX、图 / 视频文件名。
    shots = {笔 id: png 路径}：折叠全展开后，把这些笔连同四周 150 px 截图。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(pathlib.Path(page_path).resolve().as_uri() if pathlib.Path(page_path).is_absolute()
                  else (ROOT / page_path).as_uri(), wait_until="domcontentloaded", timeout=120000)
        page.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise",
                               timeout=120000)
        page.evaluate("MathJax.startup.promise")
        page.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : "
                      "new Promise(r => { i.onload = i.onerror = r; }))))")
        if page.is_visible("#annot-fab"):
            page.click("#annot-fab")
        page.set_input_files("#annot-file", files=[{"name": "notes.json", "mimeType": "application/json",
                                                    "buffer": json.dumps(notes, ensure_ascii=False).encode()}])
        page.click('[data-import="replace"]')
        page.wait_for_timeout(1500)
        closed = page.evaluate(COVER_JS)
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.wait_for_timeout(1500)
        opened = page.evaluate(COVER_JS)
        for i, path in (shots or {}).items():
            el = page.locator(f'#annot-layer path[data-id="{i}"]')
            if not el.count():
                continue
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            q = el.bounding_box()
            if q and q["width"]:
                m = 150
                page.screenshot(path=path, clip=dict(x=max(0, q["x"] - m), y=max(0, q["y"] - m),
                                                    width=q["width"] + 2 * m, height=q["height"] + 2 * m))
        browser.close()
    return {i: dict(opened[i], closed_shown=closed[i]["shown"]) for i in opened}


def compare(old_page, old_notes, new_page, new_notes, shot_dir=None):
    """逐笔比：旧页 + 旧笔记（用户当时看到的）与新页 + 新笔记（现在看到的）盖住的内容是否相同。
    shot_dir：给位置变了的每一笔截 <id>_old.png / <id>_new.png（折叠全展开，笔迹四周 150 px）。"""
    from difflib import SequenceMatcher
    old, new = covered(old_page, old_notes), covered(new_page, new_notes)
    rows = []
    for s in old_notes["strokes"]:
        i = s["id"]
        a, b = old.get(i), new.get(i)
        if a is None or b is None:
            rows.append((i, "缺笔", a, b))
            continue
        why = []
        if b["gray"]:
            why.append("标灰")
        if a["closed_shown"] and not b["closed_shown"]:
            why.append("原来不展开就看得到，现在被收进折叠")
        if set(a["math"]) != set(b["math"]):
            why.append("盖住的公式变了")
        if set(a["media"]) != set(b["media"]):
            why.append("盖住的图变了")
        r = SequenceMatcher(None, a["text"], b["text"]).ratio() if (a["text"] or b["text"]) else 1.0
        if r < 0.6:
            why.append(f"盖住的文字变了（相似度 {r:.2f}）")
        rows.append((i, "；".join(why), a, b))
    bad = [x for x in rows if x[1]]
    if shot_dir:
        ids = [x[0] for x in bad]
        covered(old_page, old_notes, {i: f"{shot_dir}/{i}_old.png" for i in ids})
        covered(new_page, new_notes, {i: f"{shot_dir}/{i}_new.png" for i in ids})
    print(f"== 对比 {old_page} → {new_page}：{len(rows)} 笔，位置变了 {len(bad)} 笔")
    for i, why, a, b in bad:
        print(f"  {i}  {why}")
        if a and b:
            print(f"      原来盖住：{a['text'][:40]!r} {a['math'][:1]} {a['media']}")
            print(f"      现在盖住：{b['text'][:40]!r} {b['math'][:1]} {b['media']}")
    return not bad


def browser_check(rel, notes):
    """把笔记导入真实页面（标注层「导入 → 替换」），读标注层自己判出的标灰笔数，并列出这些笔的锚点。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto((ROOT / rel).as_uri(), wait_until="domcontentloaded", timeout=120000)
        page.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise",
                               timeout=120000)
        page.evaluate("MathJax.startup.promise")
        if page.is_visible("#annot-fab"):
            page.click("#annot-fab")
        page.set_input_files("#annot-file", files=[{"name": "notes.json", "mimeType": "application/json",
                                                    "buffer": json.dumps(notes, ensure_ascii=False).encode()}])
        page.click('[data-import="replace"]')
        page.wait_for_timeout(1500)
        st = page.evaluate("window.__nipcNotes.status()")
        n = page.evaluate("window.__nipcNotes.strokes().length")
        gray = page.evaluate("[...document.querySelectorAll('#annot-layer path.orphan')].map(e => e.dataset.id)")
        browser.close()
    print(f"== 浏览器实测 {rel}：导入 {len(notes.get('strokes', []))} 笔，页面上 {n} 笔，标灰 {st['orphans']}，"
          f"折叠中隐藏 {st['hidden']}")
    byid = {s["id"]: s["anchor"] for s in notes.get("strokes", [])}
    for i in gray:
        a = byid.get(i, {})
        print(f"  标灰 {a.get('type')}  {a.get('tex') or a.get('src') or a.get('exact')}")
    return st["orphans"] == 0 and n == len(notes.get("strokes", []))


# ---------------------------------------------------------------- 笔迹跑偏诊断（用户 10-26：「写好了然后跑到边上去了」）
VIEW_H = {820: 1180, 1180: 820, 1440: 900}
DRIFT_TOL_PX = 0.05      # 落笔 = 重绘的容差（正文卡片 px）
# 落笔位置（页面里按固定规则挑元素，滚到视口中间，返回起笔屏幕点）：正文、行内公式上 / 旁、陈列公式、图、卡片内左边距、
# 卡片外右侧、展开的折叠块里
SPOT_JS = r"""(name) => {
  const art = document.querySelector('main article'), main = document.querySelector('main');
  const center = el => { el.scrollIntoView({block: 'center', behavior: 'instant'}); const r = el.getBoundingClientRect(); return [r, (r.left + r.right) / 2, (r.top + r.bottom) / 2]; };
  const charMid = p => { const w = document.createTreeWalker(p, NodeFilter.SHOW_TEXT); let n = w.nextNode();
    while (n && (n.data.trim().length < 6 || n.parentElement.closest('mjx-container'))) n = w.nextNode();
    p.scrollIntoView({block: 'center', behavior: 'instant'}); const rg = document.createRange(), k = n.data.length >> 1; rg.setStart(n, k); rg.setEnd(n, k + 1);
    const r = rg.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  const paras = [...art.querySelectorAll('p')].filter(p => !p.closest('details') && p.textContent.trim().length > 40);
  if (name === '正文') return charMid(paras[10]);
  const inl = [...art.querySelectorAll('p mjx-container:not([display="true"])')].filter(m => !m.closest('details'))[5];
  if (name === '行内公式上') { const [, x, y] = center(inl); return [x, y]; }
  if (name === '行内公式旁') { const [r, , y] = center(inl); return [r.right + 6, y]; }
  if (name === '陈列公式') { const [, x, y] = center([...art.querySelectorAll('mjx-container[display="true"]')].filter(m => !m.closest('details'))[3]); return [x, y]; }
  if (name === '图') { const [, x, y] = center(art.querySelectorAll('img')[2]); return [x, y]; }
  if (name === '卡片内左边距') { const [, y] = charMid(paras[14]); return [main.getBoundingClientRect().left + 12, y]; }
  if (name === '卡片外右侧') { const [, y] = charMid(paras[18]); const r = main.getBoundingClientRect(); return [Math.min(r.right + 30, innerWidth - 4), y]; }
  if (name === '表格重复内容') {   // 同一张表里内容相同的第二个单元格（如两行都写「1 / 0」），圈这一格
    const cells = [...art.querySelectorAll('td')].filter(c => !c.closest('details'));
    const seen = new Map();
    for (const c of cells) { const t = c.textContent.trim(); if (t.length >= 3 && seen.has(t) && seen.get(t).closest('table') === c.closest('table')) { const [, x, y] = center(c); return [x, y]; } seen.set(t, c); }
    return null;
  }
  if (name === '折叠块内') { const d = art.querySelector('details'); d.open = true; return charMid(d.querySelector('p') || d); }
  return null;
}"""
SPOTS = ("正文", "行内公式上", "行内公式旁", "陈列公式", "图", "卡片内左边距", "卡片外右侧", "折叠块内", "表格重复内容")


def _stroke_shape(x, y, tool):
    """一笔的屏幕点：笔 = 横向 40 px 的小波浪（13 点）；荧光笔 = 横向 60 px 的直线（16 点）。点距 ≥ 3 px（标注层丢弃过密点）。"""
    if tool == "hl":
        return [(x - 30 + 4 * k, y) for k in range(16)]
    return [(x - 20 + 3.4 * k, y + (4 if k % 2 else -4)) for k in range(13)]


def _max_dev(a, b):
    """两串正文卡片坐标点的最大偏差（px）：同长度逐点比，否则每点到另一串最近点。"""
    if len(a) == len(b):
        return max(math.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(a, b))
    return max(min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in b) for p in a)


def drift_synth(page_html, widths, out):
    """--drift-synth：无头 Chromium（触摸设备、2 倍像素）按宽度打开 page_html（file://，本机存储为空），打开标注，在 SPOTS 每处先用笔、
    再在正文 / 行内公式 / 陈列公式用荧光笔各画一笔（鼠标指针，标注层对非手指指针一视同仁）。落笔坐标 = 每个鼠标点当场经
    __nipcNotes.frame() 换成正文卡片坐标；重绘坐标 = __nipcNotes.placed(id).abs。每笔比三次：① 画完所有笔后（每次收笔都会
    全页重放，annotate.py commit）；② 滚到页顶、页底各调一次 render()；③ reload（本机存储保留）后。打印逐笔表，写 out。"""
    res = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for w in widths:
            ctx = browser.new_context(viewport={"width": w, "height": VIEW_H.get(w, 900)}, device_scale_factor=2, has_touch=True)
            ctx.route("https://api.github.com/**", lambda route, req: route.abort())
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)[:300]))
            pg.on("console", lambda m: m.type == "error" and errs.append(m.text[:300]))

            def ready():
                pg.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
                pg.evaluate("MathJax.startup.promise")
                pg.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; }))))")
                if pg.is_visible("#annot-fab"):
                    pg.click("#annot-fab")
                # 「折叠块内」要展开第一个折叠块，它会把下面的正文整体推下去（笔跟着内容走，属正常）；所以落笔前、重载后都先展开，
                # 三次比较时排版相同
                pg.evaluate("document.querySelector('main article details').open = true")
                pg.wait_for_timeout(500)
            pg.goto(pathlib.Path(page_html).resolve().as_uri(), wait_until="domcontentloaded", timeout=120000)
            ready()
            rows = []
            for tool in ("pen", "hl"):
                pg.click('[data-tool="hl"]' if tool == "hl" else '[data-tool="pen"][data-color="red"]')
                for name in (SPOTS if tool == "pen" else ("正文", "行内公式上", "陈列公式", "表格重复内容")):
                    xy = pg.evaluate(SPOT_JS, name)
                    pg.wait_for_timeout(300)
                    xy = pg.evaluate(SPOT_JS, name)          # 滚动停稳后再取一次
                    pts = _stroke_shape(xy[0], xy[1], tool)
                    f = pg.evaluate("window.__nipcNotes.frame()")
                    local = [((x - f["x"]) / f["s"], (y - f["y"]) / f["s"]) for x, y in pts]
                    pg.mouse.move(*pts[0])
                    pg.mouse.down()
                    for q in pts[1:]:
                        pg.mouse.move(*q)
                    pg.mouse.up()
                    pg.wait_for_timeout(150)
                    got = pg.evaluate("(() => { const s = window.__nipcNotes.strokes(); const t = s[s.length - 1]; return t ? [t.id, t.anchor] : null; })()")
                    assert got and got[0] not in {r["id"] for r in rows}, (
                        f"{w} {tool} {name}：这一笔没有存下（标注 {pg.evaluate('window.__nipcNotes.status()')}，起笔点 {pts[0]}，页面报错 {errs[-3:]}）")
                    sid, anchor = got
                    rows.append(dict(spot=name, tool=tool, id=sid, type=anchor.get("type"),
                                     cover=anchor.get("cover") or anchor.get("src") or anchor.get("tex") or anchor.get("exact") or "",
                                     local=local, scale=f["s"]))
                    print(f"[drift synth {w}] {tool} {name}: 锚 {anchor.get('type')} 「{rows[-1]['cover'][:20]}」", flush=True)

            def measure(tag):
                for r in rows:
                    d = pg.evaluate("id => window.__nipcNotes.placed(id)", r["id"])
                    r[tag] = None if not d or not d["abs"] else round(_max_dev(r["local"], [(a[0], a[1]) for a in d["abs"]]), 3)
            pg.evaluate("window.__nipcNotes.render()")
            pg.wait_for_timeout(200)
            measure("after_all")
            for y in ("0", "document.body.scrollHeight"):
                pg.evaluate(f"window.scrollTo({{left: 0, top: {y}, behavior: 'instant'}})")
                pg.wait_for_timeout(200)
                pg.evaluate("window.__nipcNotes.render()")
                pg.wait_for_timeout(200)
            measure("after_scroll")
            pg.reload(wait_until="domcontentloaded", timeout=120000)
            ready()
            pg.evaluate("window.__nipcNotes.render()")
            pg.wait_for_timeout(300)
            measure("after_reload")
            for r in rows:
                print(f"[drift synth {w}] {r['tool']:3s} {r['spot']:6s} {r['type']:6s} 「{r['cover'][:16]}」 偏差 px：收完全部笔 {r['after_all']}  "
                      f"滚动重放 {r['after_scroll']}  重载 {r['after_reload']}", flush=True)
            res[w] = rows
            ctx.close()
        browser.close()
    pathlib.Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return res


def _quote_ok(text, p, a):
    """正文第 p 个字是否就是锚点 a 落笔时那个字：前后文一字不差。"""
    pre, suf = a.get("prefix", ""), a.get("suffix", "")
    return text[max(0, p - len(pre)):p] == pre and text[p:p + 1] == a.get("exact") and text[p + 1:p + 1 + len(suf)] == suf


def _is_margin(s, near=30.0):
    """落笔时这笔离它的锚（字 / 符号）的参考矩形超过 near px（annotate.py CFG.nearPx）= 页边旁注（锚是放宽搜索找来的）。"""
    if s["drawn"]["frame"] != "px":
        return False
    R = s["drawn"]["orig"]
    d = min(math.hypot(max(-x, 0, x - R["w"]), max(-y, 0, y - R["h"])) for x, y, *_ in s["drawn"]["pts"])
    return d > near


ANNOT_SCRIPT = re.compile(r"<script>(?:(?!</script>).)*?window\.__nipcNotes = \{.*?</script>", re.S)


def with_annot_from(html_path, annot_from):
    """html_path 那一版页面的正文不动，只把标注层 <script>（含 window.__nipcNotes = {）换成 annot_from 页面里的那一份，
    写到同目录 week3_4.annot_fix.html（资源相对路径照旧可用）并返回它的路径——回归用：新标注层在笔当初画的那一版内容上。"""
    new = ANNOT_SCRIPT.findall(pathlib.Path(annot_from).read_text(encoding="utf-8"))
    old_html = pathlib.Path(html_path).read_text(encoding="utf-8")
    assert len(new) == 1 and len(ANNOT_SCRIPT.findall(old_html)) == 1, "标注层脚本不是恰好一个"
    out = pathlib.Path(html_path).with_name("week3_4.annot_fix.html")
    out.write_text(ANNOT_SCRIPT.sub(lambda m: new[0], old_html, count=1), encoding="utf-8")
    return out


def drift_data(notes, pages_dir, out, annot_from=None):
    """--drift-data：每笔在它画的那一版页面上（pages_dir/<drawn.page>/week3_4.html，笔记以只读快照嵌入）被挂到了哪
    （__nipcNotes.where），与落笔时的锚点（drawn.anchor）比：正文 = 那个字前后文一字不差；公式 = 同 TeX、符号前后 8 个与 g 相同、
    公式前 30 字与 ctx 相同；图 = 同 src。不一致 = 挂到了别处（跑掉）。分类：② 落笔时锚在内容上（重找时挑错了同样的另一处）、
    ③ 页边旁注（_is_margin）；另记锚点参考矩形与落笔时 orig 的差（px，受本机字体影响，只作 ④ 的旁证）。写 out。"""
    strokes = notes["strokes"]
    by_ver = {}
    for s in strokes:
        by_ver.setdefault(s["drawn"]["page"], []).append(s)
    snap = json.dumps(notes, ensure_ascii=False).replace("</", "<\\/")
    res = {"versions": {}, "moved": []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for ver, ss in sorted(by_ver.items()):
            html = pathlib.Path(pages_dir) / ver / "week3_4.html"
            assert html.exists(), f"缺 {ver} 版页面 {html}"
            if annot_from:
                html = with_annot_from(html, annot_from)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            ctx.route("https://api.github.com/**", lambda route, req: route.abort())
            ctx.add_init_script(f"window.NIPC_SNAPSHOT = {snap};")
            pg = ctx.new_page()
            pg.goto(html.resolve().as_uri(), wait_until="domcontentloaded", timeout=120000)
            pg.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
            pg.evaluate("MathJax.startup.promise")
            pg.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; }))))")
            pg.wait_for_timeout(800)     # 折叠块保持收起（她打开页面时的默认排版），不然 anchorRects 与落笔时的 orig 不可比
            pg.evaluate("window.__nipcNotes.render()")
            assert pg.evaluate("window.NIPC_PAGE") == ver, "页面版本号与笔的 drawn.page 不符"
            ids = [s["id"] for s in ss]
            where = pg.evaluate("ids => window.__nipcNotes.where(ids)", ids)
            rects = pg.evaluate("ids => window.__nipcNotes.anchorRects(ids)", ids)
            ci = pg.evaluate("window.__nipcNotes.contentIndex()")
            text, maths = ci["text"], ci["maths"]
            n_bad = 0
            for s in ss:
                a, w_ = s["drawn"]["anchor"], where.get(s["id"]) or {}
                kind, ok, now = w_.get("kind"), None, ""
                if a["type"] == "text" and kind == "text":
                    ok, pp = _quote_ok(text, w_["pos"], a), w_["pos"]
                    now = f"{text[max(0, pp - 10):pp]}【{text[pp]}】{text[pp + 1:pp + 11]}"
                elif a["type"] == "math" and kind in ("glyph", "math"):
                    m = maths[w_["math"]]
                    gi = w_.get("gi", -1)
                    q = ("".join(m["ch"][max(0, gi - 8):gi]), m["ch"][gi] if gi >= 0 else "", "".join(m["ch"][gi + 1:gi + 9]))
                    g = a.get("g") or {}
                    ctx_now = text[max(0, m["pos"] - 30):m["pos"]] if m["pos"] >= 0 else text[-30:]
                    ok = m["tex"] == a["tex"] and (not g or q == (g.get("prefix"), g.get("exact"), g.get("suffix"))) and ctx_now == a.get("ctx")
                    now = f"${m['tex'][:40]}$ 第 {gi} 个符号 {q[0][-4:]}【{q[1]}】{q[2][:4]}，公式前文「{ctx_now[-12:]}」"
                elif a["type"] == "media" and kind == "media":
                    ok, now = w_["src"] == a["src"], f"图 {w_['src']} 第 {w_.get('nth')} 个"
                elif a["type"] == "page":
                    ok, now = True, "整页"
                else:
                    ok, now = False, f"状态 {w_.get('state')}，挂到 {kind}"
                R, O = rects.get(s["id"]), s["drawn"]["orig"]
                dR = None if not R else round(math.hypot(R["x"] - O["x"], R["y"] - O["y"]), 2)
                if not ok:
                    n_bad += 1
                    was = (f"{a.get('prefix', '')[-10:]}【{a.get('exact', '')}】{a.get('suffix', '')[:10]}" if a["type"] == "text" else
                           f"${a.get('tex', '')[:40]}$ 符号 {(a.get('g') or {}).get('prefix', '')[-4:]}【{(a.get('g') or {}).get('exact', '')}】"
                           f"{(a.get('g') or {}).get('suffix', '')[:4]}，公式前文「{a.get('ctx', '')[-12:]}」" if a["type"] == "math" else a.get("src", ""))
                    res["moved"].append(dict(id=s["id"], version=ver, type=a["type"], cover=a.get("cover", ""), margin=_is_margin(s),
                                             was=was, now=now, rect_minus_orig_px=dR, at=s["drawn"]["at"]))
            res["versions"][ver] = dict(strokes=len(ss), moved=n_bad,
                                        rect_minus_orig_px=sorted(round(math.hypot(rects[i]["x"] - s["drawn"]["orig"]["x"],
                                                                                   rects[i]["y"] - s["drawn"]["orig"]["y"]), 2)
                                                                  for i, s in zip(ids, ss) if rects.get(i)))
            print(f"[drift data] 版本 {ver}：{len(ss)} 笔，挂到别处 {n_bad}", flush=True)
            ctx.close()
        browser.close()
    # ④ 的旁证（只用笔记数据、不用本机排版）：同一版页面上挂在同一处（同一个字 / 符号 / 图）的几笔，落笔时在 iPad 上量到的
    # 参考矩形 orig 应该完全一样；不一样 = 那台设备上同一处在不同时刻量出不同位置（折叠开合、图片加载、或缩放下量法不一致）
    groups = {}
    for s in strokes:
        a = s["drawn"]["anchor"]
        key = (s["drawn"]["page"], a["type"], a.get("prefix"), a.get("exact"), a.get("suffix"), a.get("tex"),
               json.dumps(a.get("g"), ensure_ascii=False), a.get("ctx"), a.get("src"))
        groups.setdefault(key, []).append(s)
    same = []
    for key, ss in groups.items():
        if len(ss) < 2:
            continue
        o = [s["drawn"]["orig"] for s in ss]
        dx = max(v["x"] for v in o) - min(v["x"] for v in o)
        dy = max(v["y"] for v in o) - min(v["y"] for v in o)
        dw = max(v["w"] for v in o) - min(v["w"] for v in o)
        same.append(dict(page=key[0], type=key[1], n=len(ss), spread_x=round(dx, 2), spread_y=round(dy, 2), spread_w=round(dw, 2),
                         what=(key[3] or key[5] or key[8] or "")[:30], ats=sorted(s["drawn"]["at"] for s in ss)))
    res["same_anchor_orig_spread"] = same
    bad = [g for g in same if max(g["spread_x"], g["spread_y"], g["spread_w"]) > 0.5]
    print(f"[drift data] ④ 旁证：同一版页面上挂在同一处的笔 {len(same)} 组；落笔时参考矩形不一致（> 0.5 px）的 {len(bad)} 组", flush=True)
    for g in sorted(bad, key=lambda g: -max(g["spread_x"], g["spread_y"]))[:12]:
        print(f"[drift data]   {g['page']} {g['type']:5s} ×{g['n']} 「{g['what']}」 x 差 {g['spread_x']} y 差 {g['spread_y']} 宽差 {g['spread_w']}  "
              f"落笔时刻 {g['ats'][0][11:19]}–{g['ats'][-1][11:19]}", flush=True)
    mv = res["moved"]
    print(f"[drift data] 合计 {len(strokes)} 笔，挂到别处 {len(mv)}：页边旁注 {sum(m['margin'] for m in mv)}，落在内容上 {sum(not m['margin'] for m in mv)}；"
          f"按类型 " + "，".join(f"{t} {sum(m['type'] == t for m in mv)}" for t in ("text", "math", "media")))
    for m in mv:
        print(f"[drift data]   {m['id']} {m['type']:5s} {'旁注' if m['margin'] else '内容'} 圈住「{m['cover'][:12]}」\n"
              f"      落笔：{m['was']}\n      现在：{m['now']}", flush=True)
    pathlib.Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {out}")
    return res


def deleted_from_history(page_file, notes, out):
    """--deleted-from-history：笔记里被擦掉的笔（tombstones：id → 删除时刻 ms）从笔记仓库历史里找回原样（只读）：
    每个墓碑取删除时刻之前最近的那次提交里的这一笔（gh api 列提交 + git blobs 取 >1 MB 的文件）。输出与笔记同格式的 json
    （strokes = 找回的笔，每笔加 deleted_at、lifetime_s = 删除时刻 − drawn.at），供 --drift-data 判它们当时是否挂到了别处；
    并打印寿命分布（画完很快就擦的笔最可能是「跑掉了」被擦重画的）。"""
    import datetime as dt
    path = f"notes/{pathlib.Path(page_file).stem}.json"
    commits = []
    for pg in range(1, 20):
        got = json.loads(subprocess.run(["gh", "api", f"repos/{REPO}/commits?path={path}&per_page=100&page={pg}"],
                                        capture_output=True, text=True, check=True).stdout)
        commits += [(c["sha"], dt.datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00")).timestamp() * 1000)
                    for c in got]
        if len(got) < 100:
            break
    commits.sort(key=lambda c: c[1])
    cache = {}

    def at(sha):
        if sha not in cache:
            bsha = subprocess.run(["gh", "api", f"repos/{REPO}/contents/{path}?ref={sha}", "--jq", ".sha"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            b64 = subprocess.run(["gh", "api", f"repos/{REPO}/git/blobs/{bsha}", "--jq", ".content"],
                                 capture_output=True, text=True, check=True).stdout
            import base64
            cache[sha] = {s["id"]: s for s in json.loads(base64.b64decode(b64))["strokes"]}
        return cache[sha]
    found, missing = [], []
    for sid, t_del in sorted(notes.get("tombstones", {}).items(), key=lambda kv: kv[1]):
        before = [c for c in commits if c[1] <= t_del]
        s = None
        for sha, _ in reversed(before[-3:]):          # 删除前最近的 3 次提交里找（同步可能慢一拍）
            s = at(sha).get(sid)
            if s:
                break
        if not s:
            missing.append(sid)
            continue
        drawn_ms = dt.datetime.fromisoformat(s["drawn"]["at"].replace("Z", "+00:00")).timestamp() * 1000
        found.append(dict(s, deleted_at=dt.datetime.utcfromtimestamp(t_del / 1000).isoformat() + "Z",
                          lifetime_s=round((t_del - drawn_ms) / 1000, 1)))
    life = sorted(s["lifetime_s"] for s in found)
    print(f"[deleted] 墓碑 {len(notes.get('tombstones', {}))}，找回 {len(found)}，找不到 {len(missing)}（取了 {len(cache)} 个历史版本）")
    if life:
        print(f"[deleted] 画完到擦掉的秒数：≤10 s {sum(x <= 10 for x in life)}，≤60 s {sum(x <= 60 for x in life)}，"
              f"≤600 s {sum(x <= 600 for x in life)}，中位数 {life[len(life) // 2]}")
    pathlib.Path(out).write_text(json.dumps(dict(notes, strokes=found, tombstones={}), ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page")
    ap.add_argument("--notes", help="本地笔记 json（默认从笔记仓库取）")
    ap.add_argument("--write-quote-map", nargs=2, metavar=("MAP_JSON", "RENAMES_JSON"))
    ap.add_argument("--compare", nargs=2, metavar=("OLD_PAGE", "OLD_NOTES_JSON"),
                    help="逐笔比旧页 + 旧笔记与本页 + 当前笔记各盖住什么（文字 / 公式 / 图、折叠收起时是否可见）")
    ap.add_argument("--shot-dir", help="--compare 时给位置变了的笔各截新旧两张图到这个目录")
    ap.add_argument("--browser", action="store_true", help="改为在真实页面里导入笔记，按标注层自己的判定数标灰笔")
    ap.add_argument("--grep", nargs="+", metavar="TEXT", help="只打印正文索引里这些文字前后各 60 字（写对应表时用）")
    ap.add_argument("--drift-synth", action="store_true",
                    help="笔迹跑偏诊断：page 为页面 html 的路径，模拟落笔，比落笔坐标与重绘坐标（drift_synth）")
    ap.add_argument("--widths", type=int, nargs="+", default=[820, 1180, 1440], help="--drift-synth 的视口宽度")
    ap.add_argument("--drift-data", metavar="PAGES_DIR",
                    help="笔迹跑偏诊断：PAGES_DIR/<页面版本号>/week3_4.html 各版页面，比每笔现在挂到哪与落笔时的锚点（drift_data）")
    ap.add_argument("--out", help="--drift-synth / --drift-data / --deleted-from-history 的结果 json")
    ap.add_argument("--annot-from", help="--drift-data：各版页面的标注层换成这个页面里的那一份再查（回归新标注层；with_annot_from）")
    ap.add_argument("--deleted-from-history", action="store_true",
                    help="从笔记仓库历史里找回被擦掉的笔（deleted_from_history），写 --out，可再喂给 --drift-data")
    args = ap.parse_args()
    if args.deleted_from_history:
        deleted_from_history(args.page, load_notes(args.page, args.notes), args.out)
        return
    if args.drift_synth:
        res = drift_synth(args.page, args.widths, args.out)
        bad = [(w, r["tool"], r["spot"]) for w, rows in res.items() for r in rows
               if any(r[k] is None or r[k] > DRIFT_TOL_PX for k in ("after_all", "after_scroll", "after_reload"))]
        print(f"== 落笔 = 重绘（≤ {DRIFT_TOL_PX} px）：{sum(len(v) for v in res.values()) - len(bad)} 笔通过，{len(bad)} 笔不通过 {bad}")
        sys.exit(1 if bad else 0)
    if args.drift_data:
        res = drift_data(load_notes(args.page, args.notes), args.drift_data, args.out, args.annot_from)
        sys.exit(1 if res["moved"] else 0)
    if args.grep:
        t = page_data(args.page)["text"]
        for g in args.grep:
            at = t.find(g)
            print(f"== {g}：{'没找到' if at < 0 else t[max(0, at - 60):at + len(g) + 60]}")
        return
    notes = load_notes(args.page, args.notes)
    if args.compare:
        old_notes = json.loads(pathlib.Path(args.compare[1]).read_text(encoding="utf-8"))
        sys.exit(0 if compare(args.compare[0], old_notes, args.page, notes, args.shot_dir) else 1)
    if args.browser:
        sys.exit(0 if browser_check(args.page, notes) else 1)
    d = page_data(args.page)
    if args.write_quote_map:
        n = write_quote_map(notes, d, *args.write_quote_map)
        print(f"wrote {n} quote entries to {args.write_quote_map[1]}")
        d["renames"]["quote"] = json.loads(pathlib.Path(args.write_quote_map[1]).read_text(encoding="utf-8"))["quote"]
    groups = {}
    for s in notes.get("strokes", []):
        cat, what = classify(s["anchor"], d)
        groups.setdefault(cat, []).append((s["anchor"].get("type"), what))
    total = sum(len(v) for v in groups.values())
    print(f"== {args.page}：笔记 {total} 笔（保存于 {notes.get('saved')}）；" +
          "，".join(f"{k} {len(groups.get(k, []))}" for k in ("exact", "renamed", "fuzzy", "orphan")))
    for cat in ("fuzzy", "orphan"):
        seen = set()
        for typ, what in groups.get(cat, []):
            if (typ, what) in seen:
                continue
            seen.add((typ, what))
            n = sum(1 for x in groups[cat] if x == (typ, what))
            print(f"  {cat:6s} {typ:7s} ×{n}  {what}")
    sys.exit(0 if not groups.get("fuzzy") and not groups.get("orphan") else 1)


if __name__ == "__main__":
    main()
