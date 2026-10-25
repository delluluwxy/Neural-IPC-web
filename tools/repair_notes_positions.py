"""把位置跑偏的手写笔记挪回它原来标注的那段内容上。只写出一份修好的笔记 json，不碰笔记仓库、不改页面。

做法（每一笔）：
  1. 原始位置：在笔记仓库（check_notes_anchors.REPO）的历史存档里找这一笔第一次出现的那次存档，取当时的笔画数据；
     当时线上的页面 = 本仓库该页最后一个提交时间不晚于那次存档的版本（git worktree 检出到 --worktrees 下）。
     在那一版页面上导入这一笔、展开全部折叠，截下它四周 MARGIN 像素的干净画面（不画笔记）作模板。
  2. 现在位置：在当前页面上导入当前笔记、展开全部折叠，以这一笔现在的位置为中心，左右各 SEARCH_X、上下各 SEARCH_Y
     像素的干净画面里，用归一化互相关找与模板最像的位置；相关系数 ≥ MIN_SCORE 才采用。
  3. 偏了超过 1 px 的，调标注层 moveBy（与「移动」工具同一段代码）平移过去并重新建锚点；公式上没有符号锚点的笔原地
     重新建锚点，换成按符号挂靠（annotate.py「陈列公式里的符号」），以后公式再改也不会被拉偏。
  4. 写出修好的笔记；打印每一笔挪了多少、相关系数；找不到的（模板是空白或相关系数不够）列出来人工处理。

用法：
    /bin/python tools/repair_notes_positions.py week3_4.html --out <修好的笔记 json> [--shots <对照图目录>]
--shots：给挪动过的笔各存一张左右对照图（左：原版页面 + 原笔画；右：当前页面 + 修好的笔画）。
依赖 playwright（用户目录的 chromium）、numpy、PIL 与已登录的 gh。
"""
import argparse
import datetime
import io
import json
import pathlib
import re
import subprocess
import sys

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

from check_notes_anchors import REPO, ROOT

MARGIN = 40          # 模板：笔迹包围盒四周多截多少像素（带上下文才认得出位置）
SEARCH_X, SEARCH_Y = 300, 700
MIN_SCORE = 0.8
VIEW = {"width": 1440, "height": 900}
WORKTREES = pathlib.Path("/nas/xiaoyingwang/Neural-IPC/outputs/web_annotate_test")
HIDE_CSS = "#annot-layer, .annot-ui, header, nav { visibility: hidden !important; }"
BOX_JS = """ids => Object.fromEntries(ids.map(i => {
  const p = document.querySelector(`#annot-layer path[data-id="${i}"]`);
  if (!p) return [i, null];
  const q = p.getBoundingClientRect();
  return [i, q.width || q.height ? [q.left + scrollX, q.top + scrollY, q.width, q.height] : null];
}))"""


def gh(*args):
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True).stdout


def note_history(stem):
    """这一页的全部笔记存档，从早到晚：[(时间, 笔记 dict)]。"""
    lines = gh(f"repos/{REPO}/commits?path=notes/{stem}.json&per_page=100", "--paginate",
               "--jq", '.[] | .sha + " " + .commit.committer.date').split()
    from concurrent.futures import ThreadPoolExecutor
    fetch = lambda sha: json.loads(gh("-H", "Accept: application/vnd.github.raw",
                                      f"repos/{REPO}/contents/notes/{stem}.json?ref={sha}"))
    with ThreadPoolExecutor(8) as pool:      # 各存档互不相关，并行拉
        notes = list(pool.map(fetch, lines[::2]))
    times = [datetime.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in lines[1::2]]
    return sorted(zip(times, notes), key=lambda x: x[0])


def page_versions(page_file):
    """本仓库该页的提交，从新到旧：[(sha, 时间)]。"""
    out = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H %cI", "--", page_file],
                         capture_output=True, text=True, check=True).stdout.split()
    return [(s, datetime.datetime.fromisoformat(t)) for s, t in zip(out[::2], out[1::2])]


def checkout(sha, root):
    """把页面的某个版本检出成 worktree（已有就复用），返回页面所在目录。"""
    d = root / f"site_{sha[:7]}"
    if not d.exists():
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "-f", str(d), sha],
                       capture_output=True, text=True, check=True)
    return d


def open_with_notes(browser, path, notes):
    """打开页面、导入笔记（替换）、展开全部折叠、等图片加载完。"""
    page = browser.new_page(viewport=VIEW)
    page.goto(pathlib.Path(path).resolve().as_uri(), wait_until="domcontentloaded", timeout=120000)
    page.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise",
                           timeout=120000)
    page.evaluate("MathJax.startup.promise")
    page.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : "
                  "new Promise(r => { i.onload = i.onerror = r; }))))")
    if page.is_visible("#annot-fab"):
        page.click("#annot-fab")
    notes = dict(notes, page=pathlib.Path(path).name)   # 页名不同会弹确认框（无头浏览器默认取消），导入前对齐
    page.set_input_files("#annot-file", files=[{"name": "notes.json", "mimeType": "application/json",
                                                "buffer": json.dumps(notes, ensure_ascii=False).encode()}])
    page.click('[data-import="replace"]')
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_timeout(1500)
    return page


def grab(page, x, y, w, h):
    """页面坐标里一块区域的灰度图（调用前已加 HIDE_CSS，不画笔记和页面浮层）。返回 (数组, 实际左上角)。"""
    x, y = max(0, int(x)), max(0, int(y))
    png = page.screenshot(full_page=True, clip=dict(x=x, y=y, width=int(w), height=int(h)))
    return np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=float), (x, y)


def measure(job):
    """一个进程里量一批笔：原版页面上截模板，当前页面上找最像的位置。不挪笔，只返回
    {笔 id: ("move", dx, dy, 相关系数) | ("keep", 相关系数) | ("lost", 原因)} 与各段耗时。"""
    import time
    t0, res, tmpl, tm = time.time(), {}, {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for sha, strokes in job["groups"].items():
            old = open_with_notes(browser, checkout(sha, job["worktrees"]) / job["page"], dict(job["current"], strokes=strokes))
            boxes = old.evaluate(BOX_JS, [s["id"] for s in strokes])
            old.add_style_tag(content=HIDE_CSS)
            for s in strokes:
                b = boxes.get(s["id"])
                if b:
                    tmpl[s["id"]] = grab(old, b[0] - MARGIN, b[1] - MARGIN, b[2] + 2 * MARGIN, b[3] + 2 * MARGIN)[0]
            old.close()
        tm["templates"] = time.time() - t0
        new = open_with_notes(browser, job["root"] / job["page"], job["current"])
        ids = sorted(s["id"] for g in job["groups"].values() for s in g)
        boxes = new.evaluate(BOX_JS, ids)
        new.add_style_tag(content=HIDE_CSS)
        tm["new_page"] = time.time() - t0 - tm["templates"]
        for i in ids:
            b, t = boxes.get(i), tmpl.get(i)
            if b is None or t is None:
                res[i] = ("lost", "原位置或现位置看不到")
                continue
            search, (sx, sy) = grab(new, b[0] - MARGIN - SEARCH_X, b[1] - MARGIN - SEARCH_Y,
                                    b[2] + 2 * (MARGIN + SEARCH_X), b[3] + 2 * (MARGIN + SEARCH_Y))
            m = best_match(search, t)
            if m is None:
                res[i] = ("lost", "原位置下面是空白，认不出")
            elif m[2] < MIN_SCORE:
                res[i] = ("lost", f"最像处相关系数只有 {m[2]:.2f}")
            else:
                dx, dy = sx + m[1] + MARGIN - b[0], sy + m[0] + MARGIN - b[1]
                res[i] = ("keep", m[2]) if abs(dx) <= 1 and abs(dy) <= 1 else ("move", dx, dy, m[2])
        browser.close()
    tm["match"] = time.time() - t0 - tm["templates"] - tm["new_page"]
    return res, tm


def shoot(job):
    """一个进程里给一批挪动过的笔出左右对照图（左：原版页面 + 原笔画；右：当前页面 + 修好的笔画）。"""
    clip = lambda b: dict(x=max(0, b[0] - 150), y=max(0, b[1] - 150), width=b[2] + 300, height=b[3] + 300)
    before = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for sha, strokes in job["groups"].items():
            old = open_with_notes(browser, checkout(sha, job["worktrees"]) / job["page"], dict(job["current"], strokes=strokes))
            boxes = old.evaluate(BOX_JS, [s["id"] for s in strokes])
            for s in strokes:
                if boxes.get(s["id"]):
                    before[s["id"]] = Image.open(io.BytesIO(old.screenshot(full_page=True, clip=clip(boxes[s["id"]])))).convert("RGB")
            old.close()
        new = open_with_notes(browser, job["root"] / job["page"], job["repaired"])
        boxes = new.evaluate(BOX_JS, list(before))
        for i, img in before.items():
            if boxes.get(i):
                after = Image.open(io.BytesIO(new.screenshot(full_page=True, clip=clip(boxes[i])))).convert("RGB")
                side_by_side(img, after, f"{job['shots']}/{i}.png")
        browser.close()


# 在原版页面上把一笔公式笔换成符号锚点：读它当时画在哪（placed().abs，正文卡片坐标），取起笔点所在（或最近）的
# 陈列公式里离起笔点最近的符号，点改存成相对该符号的像素坐标（同 annotate.py「陈列公式里的符号」，glyphLen = 8）
GLYPH_JS = """ids => {
  const N = window.__nipcNotes, f = N.frame(), L = 8, r2 = v => Math.round(v * 100) / 100;
  const maths = [...MathJax.startup.document.math].filter(m => m.display && m.typesetRoot && m.typesetRoot.isConnected);
  const dist = (q, x, y) => Math.hypot(Math.max(q.left - x, 0, x - q.right), Math.max(q.top - y, 0, y - q.bottom));
  const out = {};
  for (const id of ids) {
    const pl = N.placed(id);
    if (!pl || pl.state !== 'ok' || !pl.abs || !maths.length) { out[id] = null; continue; }
    const x = f.x + pl.abs[0][0] * f.s, y = f.y + pl.abs[0][1] * f.s;
    const m = maths.reduce((a, b) => dist(b.typesetRoot.getBoundingClientRect(), x, y) <
                                    dist(a.typesetRoot.getBoundingClientRect(), x, y) ? b : a);
    const els = [...m.typesetRoot.querySelectorAll('[data-c]')];
    const ch = els.map(e => { const c = parseInt(e.getAttribute('data-c'), 16); return Number.isFinite(c) ? String.fromCodePoint(c) : '?'; });
    let k = -1, bd = Infinity;
    els.forEach((e, i) => { const q = e.getBoundingClientRect(); if ((q.width || q.height) && dist(q, x, y) < bd) { bd = dist(q, x, y); k = i; } });
    if (k < 0) { out[id] = null; continue; }
    const q = els[k].getBoundingClientRect();
    const R = {x: (q.left - f.x) / f.s, y: (q.top - f.y) / f.s, w: q.width / f.s, h: q.height / f.s};
    out[id] = {tex: m.math.replace(/\\s+/g, ' ').trim(),
               g: {prefix: ch.slice(Math.max(0, k - L), k).join(''), exact: ch[k], suffix: ch.slice(k + 1, k + 1 + L).join('')},
               orig: {x: r2(R.x), y: r2(R.y), w: r2(R.w), h: r2(R.h)},
               pts: pl.abs.map(p => [r2(p[0] - R.x), r2(p[1] - R.y), r2(p[2])])};
  }
  return out;
}"""


# 每一笔精确压在哪些内容上：笔画每个点（按笔宽的一半作半径）落进哪些正文字、公式符号（data-c 字形）、图 / 视频。
# 先按笔画外框筛候选，再逐点判断。文字按文档顺序连成串；图记文件名和笔画点在图内的平均位置（0–1，取 3 位）。
HIT_JS = """ids => {
  const N = window.__nipcNotes, f = N.frame(), art = document.querySelector('main article');
  const skip = n => !n.parentElement || !!n.parentElement.closest('mjx-container, script, style, svg, .annot-ui');
  const items = [], rg = document.createRange(), w = document.createTreeWalker(art, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) {
    if (skip(n)) continue;
    for (let i = 0; i < n.data.length; i++) {
      if (/\\s/.test(n.data[i])) continue;
      rg.setStart(n, i); rg.setEnd(n, i + 1);
      const q = rg.getBoundingClientRect();
      if (q.width || q.height) items.push(['t', n.data[i], q.left, q.top, q.right, q.bottom]);
    }
  }
  for (const e of art.querySelectorAll('mjx-container [data-c]')) {
    const q = e.getBoundingClientRect(), c = parseInt(e.getAttribute('data-c'), 16);
    if (q.width || q.height) items.push(['g', Number.isFinite(c) ? String.fromCodePoint(c) : '?', q.left, q.top, q.right, q.bottom]);
  }
  const media = [...art.querySelectorAll('img, video')].map(e => [(e.getAttribute('src') || '').split('/').pop(), e.getBoundingClientRect()]);
  const byId = new Map(N.strokes().map(s => [s.id, s]));
  const out = {};
  for (const id of ids) {
    const pl = N.placed(id), s = byId.get(id);
    if (!pl || pl.state !== 'ok' || !pl.abs || !s) { out[id] = {state: pl ? pl.state : 'none'}; continue; }
    const r = (s.w || 2) / 2 * f.s, P = pl.abs.map(p => [f.x + p[0] * f.s, f.y + p[1] * f.s]);
    const bx = [Math.min(...P.map(p => p[0])) - r, Math.min(...P.map(p => p[1])) - r,
                Math.max(...P.map(p => p[0])) + r, Math.max(...P.map(p => p[1])) + r];
    const hit = (l, t, rr, b) => l <= bx[2] && rr >= bx[0] && t <= bx[3] && b >= bx[1] &&
      P.some(([x, y]) => x >= l - r && x <= rr + r && y >= t - r && y <= b + r);
    const text = items.filter(it => it[0] === 't' && hit(it[2], it[3], it[4], it[5])).map(it => it[1]).join('');
    const glyph = items.filter(it => it[0] === 'g' && hit(it[2], it[3], it[4], it[5])).map(it => it[1]).join('');
    const med = media.filter(([, q]) => hit(q.left, q.top, q.right, q.bottom)).map(([src, q]) => {
      const inside = P.filter(([x, y]) => x >= q.left && x <= q.right && y >= q.top && y <= q.bottom);
      const mx = inside.reduce((a, p) => a + (p[0] - q.left) / q.width, 0) / (inside.length || 1);
      const my = inside.reduce((a, p) => a + (p[1] - q.top) / q.height, 0) / (inside.length || 1);
      return `${src}@${mx.toFixed(3)},${my.toFixed(3)}`;
    });
    // 每个点最近的字 / 符号（笔画外框外扩 NEAR px 内找）和点相对它左上角的偏移：画在内容旁边的笔（下划线、圈、箭头）也能精确比
    const NEAR = 80, cand = items.filter(it => it[2] <= bx[2] + NEAR && it[4] >= bx[0] - NEAR && it[3] <= bx[3] + NEAR && it[5] >= bx[1] - NEAR);
    const near = P.map(([x, y]) => {
      let best = null, bd = Infinity;
      for (const it of cand) {
        const d = Math.hypot(Math.max(it[2] - x, 0, x - it[4]), Math.max(it[3] - y, 0, y - it[5]));
        if (d < bd) { bd = d; best = it; }
      }
      return best ? [best[1], Math.round((x - best[2]) / f.s * 2) / 2, Math.round((y - best[3]) / f.s * 2) / 2] : null;
    });
    // 影响范围：笔画任一点周围 ZONE px 以内的字 / 符号，记它中心相对起笔点的偏移（未缩放 px，取 0.5 px）
    const ZONE = 40 * f.s, [x0, y0] = P[0];
    const zone = cand.filter(it => P.some(([x, y]) =>
        Math.hypot(Math.max(it[2] - x, 0, x - it[4]), Math.max(it[3] - y, 0, y - it[5])) <= ZONE))
      .map(it => [it[1], Math.round(((it[2] + it[4]) / 2 - x0) / f.s * 2) / 2, Math.round(((it[3] + it[5]) / 2 - y0) / f.s * 2) / 2]);
    // 圈住的内容：中心落在笔画外框里的字 / 符号（按文档顺序，NFKC 归一）；一个都没有就取离笔画最近（30 px 内）的那一个。
    // 同时记第一个的中心相对起笔点的位置（未缩放 px）
    const ctr = it => [(it[2] + it[4]) / 2, (it[3] + it[5]) / 2];
    let cov = cand.filter(it => { const [cx, cy] = ctr(it); return cx >= bx[0] + r && cx <= bx[2] - r && cy >= bx[1] + r && cy <= bx[3] - r; });
    if (!cov.length) {
      const d = it => Math.min(...P.map(([x, y]) => Math.hypot(Math.max(it[2] - x, 0, x - it[4]), Math.max(it[3] - y, 0, y - it[5]))));
      const best = cand.map(it => [d(it), it]).filter(([dd]) => dd <= 30 * f.s).sort((p, q) => p[0] - q[0])[0];
      cov = best ? [best[1]] : [];
    }
    const cover = cov.map(it => it[1].normalize('NFKC')).join('');
    const off = cov.length ? [Math.round((ctr(cov[0])[0] - x0) / f.s * 2) / 2, Math.round((ctr(cov[0])[1] - y0) / f.s * 2) / 2] : null;
    out[id] = {state: 'ok', text, glyph, media: med, near, zone, cover, off};
  }
  return out;
}"""


def hits(job):
    """一个进程在一个页面上算一批笔压住的内容：job 里 sha 为 None 表示当前页面。
    job["cover"]：改用标注层自己的 coverOf（原版页面先换上当前标注层），与标注层挂靠用同一套「圈住的内容」定义。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        path = job["root"] / job["page"] if job["sha"] is None else checkout(job["sha"], job["worktrees"]) / job["page"]
        if job.get("cover") and job["sha"] is not None:
            path = with_new_layer(path)
        page = open_with_notes(browser, path, dict(job["current"], strokes=job["strokes"]))
        ids = [s["id"] for s in job["strokes"]]
        got = page.evaluate("ids => window.__nipcNotes.coverOf(ids)", ids) if job.get("cover") else page.evaluate(HIT_JS, ids)
        browser.close()
    return got


def with_new_layer(page_path):
    """旧版页面换上当前的标注层（annotate.py 的 JS 与 CSS 各一整块替换），写到同目录 <页名>.newlayer.html 并返回路径。"""
    from webgen.annotate import ANNOT_CSS, ANNOT_JS
    h = page_path.read_text(encoding="utf-8")

    import re

    def swap(h, marker, tag, new):
        """把内容里含 marker 的那一个 <tag>…</tag> 块（逐块找，不跨块）的内容换成 new。"""
        for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", h, flags=re.S):
            if marker in m.group(1):
                return h[:m.start(1)] + new + h[m.end(1):]
        raise ValueError(f"页面里没有内容含 {marker!r} 的 <{tag}>")
    h = swap(swap(h, "#annot-layer", "style", ANNOT_CSS), "window.__nipcNotes = {", "script", ANNOT_JS)
    out = page_path.with_name(page_path.stem + ".newlayer.html")
    out.write_text(h, encoding="utf-8")
    return out


def reanchor_on_origin(job):
    """一个进程处理一个原版页面：换上新标注层、导入当初的笔、展开折叠，按每笔此刻圈住的内容重新建锚点，返回这些笔。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = open_with_notes(browser, with_new_layer(checkout(job["sha"], job["worktrees"]) / job["page"]),
                               dict(job["current"], strokes=job["strokes"]))
        ids = [s["id"] for s in job["strokes"]]
        done = set(page.evaluate("ids => window.__nipcNotes.reanchorAll(ids)", ids))
        got = {s["id"]: s for s in page.evaluate("window.__nipcNotes.strokes()") if s["id"] in done}
        browser.close()
    return got, len(done)


def glyph_anchors(job):
    """一个进程处理一个原版页面上的公式笔：返回 {笔 id: 新的 tex / g / orig / pts 或 None}。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = open_with_notes(browser, checkout(job["sha"], job["worktrees"]) / job["page"],
                               dict(job["current"], strokes=job["strokes"]))
        got = page.evaluate(GLYPH_JS, [s["id"] for s in job["strokes"]])
        browser.close()
    return got


def split(groups, ids, n):
    """把要处理的笔轮流分成 n 份，每份仍按原始版本分组。"""
    by_id = {s["id"]: (sha, s) for sha, g in groups.items() for s in g}
    out = [dict() for _ in range(n)]
    for k, i in enumerate(sorted(ids)):
        if i in by_id:
            sha, s = by_id[i]
            out[k % n].setdefault(sha, []).append(s)
    return [g for g in out if g]


def best_match(search, tmpl):
    """归一化互相关（FFT 求互相关 + 积分图求窗口方差）：返回 (行, 列, 相关系数)；模板是空白返回 None。"""
    th, tw = tmpl.shape
    sh, sw = search.shape
    t = tmpl - tmpl.mean()
    tn = np.sqrt((t * t).sum())
    if th > sh or tw > sw or tn < 1e-3 * th * tw:
        return None
    shape = (sh + th, sw + tw)
    corr = np.fft.irfft2(np.fft.rfft2(search, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)
    corr = corr[th - 1:sh, tw - 1:sw]
    pad = lambda a: np.pad(a.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    win = lambda I: I[th:, tw:] - I[:-th, tw:] - I[th:, :-tw] + I[:-th, :-tw]
    s1, s2 = win(pad(search)), win(pad(search * search))
    score = corr / (np.sqrt(np.maximum(s2 - s1 * s1 / (th * tw), 1e-6)) * tn)
    k = int(np.argmax(score))
    return k // score.shape[1], k % score.shape[1], float(score.flat[k])


def letter_maps(table):
    """改名表里只差字母一一换名的公式对各自换了哪些 TeX 记号（同 annotate.py letterMaps），每对一组。"""
    out = []
    for o, n in table.items():
        A, B = TEX_TOKEN.findall(o), TEX_TOKEN.findall(n)
        if len(A) != len(B):
            continue
        m, ok = {}, True
        for x, y in zip(A, B):
            if x != y and (not (LETTER.fullmatch(x) and LETTER.fullmatch(y)) or m.setdefault(x, y) != y):
                ok = False
                break
        if ok and m and m not in out:
            out.append(m)
    return out


GREEK_CHAR = {"\\" + k: v for k, v in dict(  # 同 annotate.py GREEK（MathJax 符号经 NFKC 后的字符）
    alpha="α", beta="β", gamma="γ", delta="δ", epsilon="ϵ", varepsilon="ε", zeta="ζ", eta="η", theta="θ", vartheta="ϑ",
    kappa="κ", lambda_="λ", mu="μ", nu="ν", xi="ξ", pi="π", rho="ρ", sigma="σ", tau="τ", phi="ϕ", varphi="φ", chi="χ",
    psi="ψ", omega="ω").items()}
GREEK_CHAR["\\lambda"] = GREEK_CHAR.pop("\\lambda_")


def same_label(old, new, maps):
    """图里两个文字标注是不是同一个：原样相同，或按改名表的一组字母换名（TeX 记号与对应的希腊字母一起换）后相同。"""
    if old == new:
        return True
    tok = re.findall(r"\\[A-Za-z]+|.", old, flags=re.S)
    for m in maps:
        mm = dict(m, **{GREEK_CHAR[k]: GREEK_CHAR[v] for k, v in m.items() if k in GREEK_CHAR and v in GREEK_CHAR})
        if "".join(mm.get(t, t) for t in tok) == new:
            return True
    return False


def remap_media(base, version, worktrees, root, fig_labels, maps):
    """图上的笔：画笔时那版（version[笔 id]）的图与现在的图像素不同，就跟着离笔最近的那个文字标注走。
    fig_labels = sandbox tools/figs/fig_label_boxes.py 的输出（各提交重画的图：像素哈希 + 每个标注在最终 PNG 上的框）；
    两版图都按像素哈希认出是哪个提交画的（认不出就不动并报出来）。两版里都只出现一次的同名标注（按改名表换名后相同也算）
    逐轴线性拟合出整张图的缩放；笔 = 新标注中心 + 缩放 ×（笔 − 旧标注中心）。最近的标注在新图里没了就标 gone。
    改 base 里的笔，返回 [(笔 id, 图, 结果)]。"""
    sys.path.insert(0, str(root.parent / "Neural-IPC-sandbox/tools/figs"))
    from fig_label_boxes import pixel_hash
    by_hash = {v["pixels"]: v for d in fig_labels.values() for v in d.values()}
    report = []
    by_id = {s["id"]: s for s in base["strokes"]}
    for i, sha in version.items():
        s = by_id.get(i)
        if not s or s["anchor"].get("type") != "media" or s.get("frame") != "norm":
            continue
        src = s["anchor"]["src"]
        old_p, new_p = checkout(sha, worktrees) / "assets/images" / src, root / "assets/images" / src
        if not old_p.exists() or not new_p.exists():
            continue
        ho, hn = pixel_hash(old_p), pixel_hash(new_p)
        if ho == hn:
            continue
        if ho not in by_hash or hn not in by_hash:
            report.append((i, src, "图改过，但认不出是哪个提交画的（fig_labels 里没有同像素的图），没动"))
            continue
        old, new = by_hash[ho], by_hash[hn]
        r = follow_label(s["pts"], old, new, maps)
        if r["gone"]:
            s["anchor"]["gone"] = True
            report.append((i, src, f"最近的标注「{r['label']}」（距笔 {r['dist']:.0f} px）在新图里没了，隐藏"))
            continue
        s["pts"] = [q + [p[2]] for q, p in zip(r["pts"], s["pts"])]
        s["anchor"].pop("gone", None)
        report.append((i, src, f"跟着标注「{r['label']}」（距笔 {r['dist']:.0f} px）走：缩放 {r['sc'][0]:.4f} / {r['sc'][1]:.4f}，"
                               f"{r['pairs']} 对同名标注拟合"))
    return report


def label_same(a, b, old_png, new_png, fig_labels, maps):
    """图重画过的笔：原版图上的起笔点按 follow_label 跟着最近的标注搬到新图，与现在页面上量到的起笔点（图内归一化坐标）
    差 ≤ 0.005 就返回 "label"；两版图像素相同、认不出版本或对不上返回 None。"""
    sys.path.insert(0, str(ROOT.parent / "Neural-IPC-sandbox/tools/figs"))
    from fig_label_boxes import pixel_hash
    by_hash = {v["pixels"]: v for d in fig_labels.values() for v in d.values()}
    ho, hn = pixel_hash(old_png), pixel_hash(new_png)
    if ho == hn or ho not in by_hash or hn not in by_hash:
        return None
    r = follow_label([a["rel"]], by_hash[ho], by_hash[hn], maps)
    return "label" if not r["gone"] and all(abs(x - y) <= 0.005 for x, y in zip(r["pts"][0], b["rel"])) else None


def follow_label(pts, old, new, maps):
    """一笔（旧图归一化坐标的点 pts）跟着离它最近的文字标注从旧图搬到新图（old / new = fig_label_boxes 的一张图）。
    两版里都只出现一次的同名标注逐轴线性拟合出整张图的缩放 sc；笔 = 新标注中心 + sc ×（笔 − 旧标注中心）。
    返回 {label, dist（旧图 px）, gone（最近的标注新图里没了）, sc, pairs, pts（新图归一化坐标，四位小数）}。"""
    inside = lambda L: [l for l in L if l["box"][0] >= 0 and l["box"][1] >= 0 and l["box"][2] <= 1 and l["box"][3] <= 1]
    cen = lambda l, W, H: np.array([(l["box"][0] + l["box"][2]) / 2 * W, (l["box"][1] + l["box"][3]) / 2 * H])
    (W0, H0), (W1, H1) = old["size"], new["size"]
    lo, ln = inside(old["labels"]), inside(new["labels"])
    pairs = []
    for l in lo:
        m = [k for k in ln if same_label(l["text"], k["text"], maps)]
        if len(m) == 1 and sum(x["text"] == l["text"] for x in lo) == 1:
            pairs.append((cen(l, W0, H0), cen(m[0], W1, H1)))
    A, B = np.array([p for p, _ in pairs]), np.array([q for _, q in pairs])
    sc = np.array([np.polyfit(A[:, k], B[:, k], 1)[0] for k in (0, 1)]) if len(pairs) >= 2 else np.array([W1 / W0, H1 / H0])
    P = np.array([[p[0] * W0, p[1] * H0] for p in pts])
    dist = lambda l: np.hypot(np.maximum(0, np.maximum(l["box"][0] * W0 - P[:, 0], P[:, 0] - l["box"][2] * W0)),
                              np.maximum(0, np.maximum(l["box"][1] * H0 - P[:, 1], P[:, 1] - l["box"][3] * H0))).min()
    near = min(lo, key=dist)
    out = dict(label=near["text"], dist=float(dist(near)), sc=sc.tolist(), pairs=len(pairs), gone=False, pts=None)
    cands = [k for k in ln if same_label(near["text"], k["text"], maps)]
    if not cands:
        return dict(out, gone=True)
    c0 = cen(near, W0, H0)
    guess = B.mean(0) + sc * (c0 - A.mean(0)) if len(pairs) >= 2 else c0 * [W1 / W0, H1 / H0]
    tgt = min(cands, key=lambda k: np.hypot(*(cen(k, W1, H1) - guess)))
    Q = cen(tgt, W1, H1) + sc * (P - c0)
    return dict(out, pts=[[round(q[0] / W1, 4), round(q[1] / H1, 4)] for q in Q])


def side_by_side(left, right, path):
    h = max(left.height, right.height)
    out = Image.new("RGB", (left.width + right.width + 12, h), "white")
    out.paste(left, (0, 0))
    out.paste(right, (left.width + 12, 0))
    out.save(path)


def cover_same(a, b, tex_renames, char_maps=()):
    """一笔在原版页面（a）与当前页面（b）上圈住的是不是同一处内容（两边都由标注层 coverOf 算）。是就返回判据名，否则 None。
    图上的笔：同一张图、图内相对位置差 ≤ 0.005。其余（off / hit 都是笔画起点相对内容的 px）：
      target   现在挂靠点起那串字 / 符号就是原来圈住的内容（或按改名表一组字母换名后的），起点相对它第一个差 ≤ 1 px
               ——圈仍在关键词上；现在笔画范围里另有别的内容挤进来不算错（排版变紧）；
      same     圈住的内容串、锚点类型相同，起点相对内容起点差 ≤ 1 px；
      superset 原来圈住的那串字 / 符号原样连续出现在现在圈住的里面，且起点相对它们第一个的位置差 ≤ 1 px（新排版里别的字挪近了）；
      renamed  公式按改名表换了写法（t → q），圈住的符号个数相同、起点相对第一个符号差 ≤ 1 px；
      anchor   原来没圈住任何内容（旁注、箭头）：起笔处挂的那个字相同，相对它的位置差 ≤ 1 px。"""
    near = lambda p, q, tol=1: all(abs(x - y) <= tol for x, y in zip(p, q))
    if a["rel"] is not None or b["rel"] is not None:
        ok = a["rel"] is not None and b["rel"] is not None and a["cover"] == b["cover"] and near(a["rel"], b["rel"], 0.005)
        return "same" if ok else None
    if not a["cover"]:
        ok = a["at"] and b["at"] and a["at"]["ctx"] == b["at"]["ctx"] and near(a["at"]["off"], b["at"]["off"])
        return "anchor" if ok else None
    if a["type"] == b["type"] and a["cover"] == b["cover"] and near(a["off"], b["off"]):
        return "same"
    t = b.get("target")
    if t and near(a["off"], t["off"]) and any("".join(m.get(c, c) for c in a["cover"]) == t["ch"] for m in ({}, *char_maps)):
        return "target"
    ha, hb = a["hit"], b["hit"]
    for k in range(len(hb) - len(ha) + 1):
        if ha and all(x[0] == y[0] for x, y in zip(ha, hb[k:])) and near(ha[0][1:], hb[k][1:]):
            return "superset"
    # 换名后字宽变了，压在笔画框边上的符号可能多一个少一个，所以只核起点相对第一个符号的位置
    if (a["type"] == b["type"] == "math" and tex_renames.get(a.get("tex")) == b.get("tex") and ha and hb
            and near(ha[0][1:], hb[0][1:])):
        return "renamed"
    return None


TEX_TOKEN = re.compile(r"\\[A-Za-z]+|\S")
LETTER = re.compile(r"[A-Za-z]|\\(alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|"
                    r"pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega)$")


def letter_renames(notes, page_tex):
    """笔挂的公式在当前页面上已找不到时，找当前页面里只差「字母一一换名」（如 θ→ψ、t→q）的那个公式。
    两边按 TeX 记号切开，记号数相同、不同的记号全是字母且换名一一对应（同一个旧字母只换成同一个新字母）才算。
    返回 {旧 TeX: 新 TeX}；一个旧公式对上多个新公式的不算（歧义），留给人看。"""
    have = set(page_tex)
    out = {}
    for tex in sorted({s["anchor"]["tex"] for s in notes["strokes"] if s["anchor"].get("type") == "math"} - have):
        a = TEX_TOKEN.findall(tex)
        found = []
        for new in have:
            b = TEX_TOKEN.findall(new)
            if len(a) != len(b):
                continue
            fwd, back = {}, {}
            ok = any(x != y for x, y in zip(a, b))
            for x, y in zip(a, b):
                if x == y:
                    continue
                if not (LETTER.fullmatch(x) and LETTER.fullmatch(y)) or fwd.setdefault(x, y) != y or back.setdefault(y, x) != x:
                    ok = False
                    break
            # 换了名的旧字母在别处原样没换（如一半 t 换成 q、一半还是 t）也不算一一换名
            if ok and not any(x == y and x in fwd for x, y in zip(a, b)):
                found.append(new)
        if len(found) == 1:
            out[tex] = found[0]
    return out


def publish(args):
    """--publish NEW_JSON BASE_JSON：把修好的笔记（NEW_JSON）写回笔记仓库 notes/<页>.json。
    先拉线上那份，和迁移用的底稿 BASE_JSON 比：笔迹、墓碑有任何不同（用户之后又画过或删过）就不写、列出差异退出 1；
    相同才用 Contents API 带 sha 覆盖写（期间又被改了 GitHub 返回 409，也不会覆盖）。"""
    import base64
    import tempfile
    stem = pathlib.Path(args.page).stem
    path = f"repos/{REPO}/contents/notes/{stem}.json"
    meta = json.loads(gh(path))
    live = json.loads(base64.b64decode(meta["content"]))
    base = json.loads(pathlib.Path(args.publish[1]).read_text(encoding="utf-8"))
    key = lambda n: (json.dumps(n["strokes"], sort_keys=True), json.dumps(n.get("tombstones"), sort_keys=True))
    if key(live) != key(base):
        lb, bb = {s["id"]: s for s in live["strokes"]}, {s["id"]: s for s in base["strokes"]}
        diff = sorted(set(lb) ^ set(bb)) + sorted(i for i in set(lb) & set(bb) if lb[i] != bb[i])
        print(f"== 线上笔记和底稿不同（之后又画过 / 删过），不写入：差异笔 {diff[:20]}"
              f"{'…' if len(diff) > 20 else ''}；墓碑{'相同' if key(live)[1] == key(base)[1] else '不同'}")
        sys.exit(1)
    new = json.loads(pathlib.Path(args.publish[0]).read_text(encoding="utf-8"))
    new["saved"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {"message": f"notes {stem}: anchor every stroke to the content it circles (repair_notes_positions)",
            "content": base64.b64encode(json.dumps(new, ensure_ascii=False).encode()).decode(), "sha": meta["sha"]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir=WORKTREES, delete=False) as fh:
        json.dump(body, fh)
    try:
        out = json.loads(gh("--method", "PUT", path, "--input", fh.name))
    finally:
        pathlib.Path(fh.name).unlink()
    print(f"== 写入 notes/{stem}.json：{len(new['strokes'])} 笔，提交 {out['commit']['sha'][:7]}")


def live_versions():
    """GitHub Pages 每次部署：[(上线时间, 提交 sha)]，从早到晚（用部署记录的完成时间 updated_at，没完成的不算）。"""
    out = gh("repos/delluluwxy/Neural-IPC-web/pages/builds?per_page=100", "--paginate",
             "--jq", '.[] | select(.status == "built") | .commit + " " + .updated_at').split()
    rows = [(datetime.datetime.fromisoformat(t.replace("Z", "+00:00")), s) for s, t in zip(out[::2], out[1::2])]
    return sorted(rows)


def origin_report(args):
    """--origin-report：每笔第一次出现在笔记仓库的时间、那时线上部署的是哪一版、和按提交时间推的版本页面是否相同。"""
    stem = pathlib.Path(args.page).stem
    history = note_history(stem)
    first = {}
    for t, notes in history:
        for s in notes["strokes"]:
            first.setdefault(s["id"], t)
    live = live_versions()
    commits = page_versions(args.page)
    blob = lambda sha: subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"{sha}:{args.page}"],
                                      capture_output=True, text=True).stdout.strip()
    rows = []
    for i, t in sorted(first.items(), key=lambda x: x[1]):
        dep = next((s for lt, s in reversed(live) if lt <= t), None)
        guess = next((v for v, vt in commits if vt <= t), commits[-1][0])
        rows.append(dict(id=i, first_seen=t.isoformat(), live=dep, guessed=guess,
                         same_page=bool(dep) and blob(dep) == blob(guess)))
    pathlib.Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    import collections
    c = collections.Counter((r["live"][:7] if r["live"] else None, r["guessed"][:7], r["same_page"]) for r in rows)
    print(f"== {len(rows)} 笔：（画时线上版本, 之前按提交时间推的版本, 两版页面相同）→ 笔数")
    for k, n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {k}  {n}")


def restore(args):
    """--restore SHA：笔记仓库 notes/<页>.json 原样恢复成提交 SHA 时的内容（字节不改），带当前 sha 覆盖写。"""
    import base64
    import tempfile
    stem = pathlib.Path(args.page).stem
    path = f"repos/{REPO}/contents/notes/{stem}.json"
    old = json.loads(gh(f"{path}?ref={args.restore}"))
    cur = json.loads(gh(path))
    body = {"message": f"notes {stem}: restore to {args.restore[:7]}", "content": old["content"].replace("\n", ""),
            "sha": cur["sha"]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir=WORKTREES, delete=False) as fh:
        json.dump(body, fh)
    try:
        out = json.loads(gh("--method", "PUT", path, "--input", fh.name))
    finally:
        pathlib.Path(fh.name).unlink()
    print(f"== notes/{stem}.json 恢复成 {args.restore[:7]} 的内容，提交 {out['commit']['sha'][:7]}；"
          f"内容一致：{base64.b64decode(old['content']) == base64.b64decode(json.loads(gh(path))['content'])}")


def explain(args):
    """--explain：当前页面换上当前标注层，导入 NOTES_JSON，对 IDS_TXT 里每笔调 __nipcNotes.explain，结果写 --out。"""
    ids = pathlib.Path(args.explain[0]).read_text(encoding="utf-8").split()
    notes = json.loads(pathlib.Path(args.explain[1]).read_text(encoding="utf-8"))
    path = with_new_layer(ROOT / args.page)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = open_with_notes(browser, path, notes)
            got = {i: page.evaluate("id => window.__nipcNotes.explain(id)", i) for i in ids}
            got["_letter_renames"] = letter_renames(notes, page.evaluate("window.__nipcNotes.texList()"))
            browser.close()
    finally:
        path.unlink()
    pathlib.Path(args.out).write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"== {len(ids)} 笔的查原因结果写到 {args.out}")
    if args.write_renames:   # 只差字母一一换名的公式补进讲稿目录的改名表（已有的键不动）
        from webgen.annotate import DRAFTS
        path = DRAFTS / f"notation_renames_{pathlib.Path(args.page).stem}.json"
        table = json.loads(path.read_text(encoding="utf-8"))
        new = {k: v for k, v in got["_letter_renames"].items() if k not in table["tex"]}
        table["tex"].update(new)
        path.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"== 改名表补了 {len(new)} 条字母换名：{path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page")
    ap.add_argument("--out", required=True, help="修好的笔记 json")
    ap.add_argument("--shots", help="挪动过的笔的左右对照图目录")
    ap.add_argument("--worktrees", default=str(WORKTREES), help="旧版页面 worktree 放哪")
    ap.add_argument("--only", nargs="+", help="只修这几笔（冒烟用；其余笔原样写出）")
    ap.add_argument("--workers", type=int, default=8, help="并行进程数（每个进程一个 chromium）")
    ap.add_argument("--verify", metavar="NOTES_JSON",
                    help="精确核对：每笔在原版页面与「当前页面 + NOTES_JSON」上压住的字 / 公式符号 / 图位置；明细写到 --out")
    ap.add_argument("--verify-cover", action="store_true",
                    help="--verify 用新判据：圈住的内容串相同、它相对起笔点的位置差 ≤ 1 px")
    ap.add_argument("--shots-for", nargs=2, metavar=("IDS_TXT", "NOTES_JSON"),
                    help="只出对照图（要配 --shots）：这些笔在原版页面与「当前页面 + NOTES_JSON」上的样子")
    ap.add_argument("--reanchor-origin", metavar="BASE_JSON",
                    help="每笔在当初画的那版页面上（换上当前标注层）按圈住的内容重新建锚点，写进 BASE_JSON 的副本（--out）")
    ap.add_argument("--math-from-origin", metavar="BASE_JSON",
                    help="只做一件事：当初挂在公式上的笔，在各自原版页面上换成符号锚点，写进 BASE_JSON 的副本（--out）")
    ap.add_argument("--explain", nargs=2, metavar=("IDS_TXT", "NOTES_JSON"),
                    help="只读查原因：这些笔圈住的关键词在当前页面（换上当前标注层）每一处出现的位置与段落相似度，写到 --out")
    ap.add_argument("--fig-labels", default=str(WORKTREES / "fig_labels/labels.json"),
                    help="配 --reanchor-origin：sandbox tools/figs/fig_label_boxes.py 输出的各版图文字标注框（图上的笔跟着标注走）")
    ap.add_argument("--gate", action="store_true",
                    help="推送前闸门（tools/githooks/pre-push 调）：用户现在的笔记在线上版本与工作区页面上逐笔比圈住的内容，"
                         "位置变了的有一笔就退出码 1；内容删了而隐藏的只列出不拦")
    ap.add_argument("--write-renames", action="store_true",
                    help="配 --explain：把只差字母一一换名（θ→ψ、t→q）的公式对补进讲稿目录的 notation_renames_<页>.json")
    ap.add_argument("--publish", nargs=2, metavar=("NEW_JSON", "BASE_JSON"),
                    help="把修好的笔记写回笔记仓库；线上那份和底稿 BASE_JSON 不同（用户又画过）就不写")
    ap.add_argument("--restore", metavar="SHA", help="笔记仓库这一页的笔记原样恢复成某个提交时的内容")
    ap.add_argument("--origin-report", action="store_true",
                    help="每笔画的时候线上部署的是哪一版（Pages 部署记录），和按提交时间推的版本比，写到 --out")
    args = ap.parse_args()
    if args.origin_report:
        origin_report(args)
        return
    if args.restore:
        restore(args)
        return
    if args.publish:
        publish(args)
        return
    if args.explain:
        explain(args)
        return
    stem = pathlib.Path(args.page).stem
    if args.shots:
        pathlib.Path(args.shots).mkdir(parents=True, exist_ok=True)
    if args.gate:   # 闸门只要现在的笔记，不拉历史
        history = [(datetime.datetime.now(datetime.timezone.utc),
                    json.loads(gh("-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/notes/{stem}.json")))]
    else:
        history = note_history(stem)
    current = history[-1][1]
    ids = {s["id"] for s in current["strokes"]} & set(args.only or [s["id"] for s in current["strokes"]])
    origin = {}
    for t, notes in history:
        for s in notes["strokes"]:
            if s["id"] in ids and s["id"] not in origin:
                origin[s["id"]] = (t, s)
    versions = page_versions(args.page)
    groups, version = {}, {}
    for i, (t, s) in origin.items():
        sha = next((v for v, vt in versions if vt <= t), versions[-1][0])
        groups.setdefault(sha, []).append(s)
        version[i] = sha
    print(f"== {len(ids)} 笔，原始版本分布：" + "，".join(f"{v[:7]} {len(g)}" for v, g in groups.items()), flush=True)

    import multiprocessing as mp
    import time
    t0 = time.time()
    common = dict(page=args.page, worktrees=pathlib.Path(args.worktrees), root=ROOT, current=current)
    if args.gate:
        # 推送前闸门：用户现在的笔记，在线上版本（origin/main）与要推的版本（工作区）上各圈住什么，逐笔比（同 --verify-cover）
        live = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "origin/main"], capture_output=True, text=True,
                              check=True).stdout.strip()
        groups = {live: [s for s in current["strokes"] if s["id"] in ids]}
        version = {s["id"]: live for s in groups[live]}
        args.verify_cover = True
        print(f"== 闸门：线上 {live[:7]} 对 工作区，{len(ids)} 笔", flush=True)
    for sha in groups:
        checkout(sha, common["worktrees"])          # 先检出，免得几个进程同时 git worktree add
    if args.verify or args.gate:
        # 精确核对：每笔在原版页面（原始笔画）与当前页面（VERIFY_JSON 的笔画）上压住的字 / 公式符号 / 图位置是否相同
        shown = current if args.gate else json.loads(pathlib.Path(args.verify).read_text(encoding="utf-8"))
        vfile = pathlib.Path(f"{args.verify}.versions.json")
        if not args.gate and vfile.exists():   # 迁移时实际用的原版（换过版本的笔按它分组）
            version = json.loads(vfile.read_text(encoding="utf-8"))
            by = {s["id"]: s for g in groups.values() for s in g}
            groups = {}
            for i, sha in version.items():
                if i in by:
                    groups.setdefault(sha, []).append(by[i])
        from webgen.annotate import DRAFTS
        table = json.loads((DRAFTS / f"notation_renames_{stem}.json").read_text(encoding="utf-8")).get("tex", {})
        tex_renames = {}
        for k in table:   # 沿改名表走到底（同 annotate.py renamedTex）
            t, seen = k, {k}
            while t in table and table[t] not in seen:
                t = table[t]
                seen.add(t)
            tex_renames[k] = t
        fig_labels = json.loads(pathlib.Path(args.fig_labels).read_text(encoding="utf-8"))
        # 字母换名换成公式符号字符（t → q、\theta → θ → ψ），和标注层 glyphList 的 NFKC 字符一致
        glyph = lambda tok: GREEK_CHAR.get(tok, tok)
        char_maps = [{glyph(x): glyph(y) for x, y in m.items()} for m in letter_maps(table)]
        jobs = [dict(common, sha=sha, strokes=g, cover=args.verify_cover) for sha, g in groups.items()]
        jobs.append(dict(common, sha=None, current=shown, strokes=[s for s in shown["strokes"] if s["id"] in ids],
                         cover=args.verify_cover))
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            outs = pool.map(hits, jobs)
        old = {k: v for r in outs[:-1] for k, v in r.items()}
        new = outs[-1]
        rows = []
        for i in sorted(ids):
            a, b = old.get(i, {"state": "none"}), new.get(i, {"state": "none"})
            if a["state"] != "ok" or b["state"] != "ok":
                rows.append(dict(id=i, same=a["state"] == b["state"], match=None, old=a, new=b))
                continue
            if args.verify_cover:
                # 新判据（标注层 coverOf 算）：圈住的内容串与锚点类型相同；笔画起点相对内容起点差 ≤ 1 px，图上的笔比图内相对位置差 ≤ 0.005
                how = cover_same(a, b, tex_renames, char_maps)
                if how is None and a["rel"] is not None and b["rel"] is not None and a["cover"] == b["cover"]:
                    how = label_same(a, b, checkout(version[i], common["worktrees"]) / "assets/images" / a["cover"],
                                     ROOT / "assets/images" / b["cover"], fig_labels, letter_maps(table))
                rows.append(dict(id=i, same=False, match=None, old=a, new=b, how=how))
                rows[-1]["same"] = rows[-1]["how"] is not None
                continue
            # 影响范围内的内容一一对应：原版范围里每个字 / 符号，在现在的范围里有同一个字、相对起笔点的位置差 ≤ 1 px
            pool, lost = list(b["zone"]), []
            for it in a["zone"]:
                k = next((j for j, q in enumerate(pool) if q[0] == it[0] and abs(q[1] - it[1]) <= 1 and abs(q[2] - it[2]) <= 1), None)
                if k is None:
                    lost.append(it[0])
                else:
                    pool.pop(k)
            match = 1 - len(lost) / len(a["zone"]) if a["zone"] else (1.0 if not b["zone"] else 0.0)
            a["lost"], b["extra"] = "".join(lost), "".join(q[0] for q in pool)
            same_media = [m.split("@")[0] for m in a["media"]] == [m.split("@")[0] for m in b["media"]] and all(
                abs(float(x) - float(y)) <= 0.01 for ma, mb in zip(a["media"], b["media"])
                for x, y in zip(ma.split("@")[1].split(","), mb.split("@")[1].split(",")))
            rows.append(dict(id=i, same=(match >= 0.95 if not a["media"] else same_media), match=match, old=a, new=b))
        bad = [r for r in rows if not r["same"]]
        report = pathlib.Path(args.out)
        report.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.verify_cover:
            import collections
            how = collections.Counter(r.get("how") for r in rows if r["same"])
            hidden = sum(1 for r in bad if r["old"]["state"] == "ok" and r["new"]["state"] == "orphan")
            print(f"== 精确核对 {len(rows)} 笔（判据见 cover_same）：对上 {len(rows) - len(bad)} 笔 {dict(how)}；"
                  f"对不上 {len(bad)} 笔，其中现在隐藏（圈的内容找不到）{hidden} 笔；用 {time.time() - t0:.0f} s；逐笔明细 {report}")
        else:
            print(f"== 精确核对 {len(rows)} 笔：位置对上（影响范围 40 px 内的字 / 符号 ≥ 95% 一一对应、相对位置差 ≤ 1 px；"
                  f"图上的笔图内位置差 ≤ 0.01）"
                  f"{len(rows) - len(bad)} 笔，对不上 {len(bad)} 笔；"
                  f"用 {time.time() - t0:.0f} s；逐笔明细 {report}")
        for r in bad:
            a, b = r["old"], r["new"]
            fmt = lambda h: (h["state"] if h["state"] != "ok" else
                             f"{h['type']} 圈住「{h['cover']}」 相对位置 {h['rel'] or h['off']}" if args.verify_cover else
                             f"范围内「{''.join(z[0] for z in h['zone'])}」 压住「{h['text']}{h['glyph']}」 图{h['media']}")
            pct = "" if r["match"] is None else (f"（范围内对上 {100 * r['match']:.0f}%；原来有、现在对不上的「{a.get('lost', '')}」，"
                                                f"现在多出来的「{b.get('extra', '')}」）")
            print(f"  {r['id']}{pct}\n      原来：{fmt(a)}\n      现在：{fmt(b)}")
        if args.gate:
            moved = [r for r in bad if not (r["old"]["state"] == "ok" and r["new"]["state"] == "orphan")]
            print(f"== 闸门{'不通过' if moved else '通过'}：圈的位置变了 {len(moved)} 笔；"
                  f"圈的内容删了、已隐藏 {len(bad) - len(moved)} 笔（不拦，见上面明细）")
            sys.exit(1 if moved else 0)
        return
    if args.shots_for:
        # 只出对照图：给定的笔（id 列表文件），左原版页面 + 原笔画，右当前页面 + NOTES_JSON 里的笔画
        want = set(pathlib.Path(args.shots_for[0]).read_text().split())
        shown = json.loads(pathlib.Path(args.shots_for[1]).read_text(encoding="utf-8"))
        chunks = split(groups, want, args.workers)
        with mp.get_context("spawn").Pool(len(chunks)) as pool:
            pool.map(shoot, [dict(common, groups=g, repaired=shown, shots=args.shots) for g in chunks])
        print(f"== 对照图 {len(want)} 笔，用 {time.time() - t0:.0f} s，写到 {args.shots}")
        return
    if args.reanchor_origin:
        # 每笔在它当初画的那版页面上、按它圈住的内容重新建锚点（每个版本一个进程），写进底稿笔记
        jobs = [dict(common, sha=sha, strokes=g) for sha, g in groups.items()]
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            outs = pool.map(reanchor_on_origin, jobs)
        got = {k: v for r, _ in outs for k, v in r.items()}
        # 推定的原版上挂不上的笔（实际是在别的版本上画的）：在其余各版上再试，取第一版能挂上的
        miss = [s for g in groups.values() for s in g if s["id"] not in got]
        if miss:
            with mp.get_context("spawn").Pool(len(groups)) as pool:
                more = pool.map(reanchor_on_origin, [dict(common, sha=sha, strokes=miss) for sha in groups])
            for sha, (r, _) in zip(groups, more):
                for k, v in r.items():
                    if k not in got:
                        got[k], version[k] = v, sha
            outs = outs + more
            print(f"  推定原版上挂不上 {len(miss)} 笔，换版本后挂上 {sum(1 for s in miss if s['id'] in got)} 笔", flush=True)
        base = json.loads(pathlib.Path(args.reanchor_origin).read_text(encoding="utf-8"))
        kinds = {}
        for s in base["strokes"]:
            if s["id"] in got:
                s.update({k: got[s["id"]][k] for k in ("anchor", "frame", "orig", "pts", "updatedAt")})
                key = s["anchor"]["type"] + ("+cover" if s["anchor"].get("cover") else "")
                kinds[key] = kinds.get(key, 0) + 1
        from webgen.annotate import DRAFTS
        table = json.loads((DRAFTS / f"notation_renames_{stem}.json").read_text(encoding="utf-8")).get("tex", {})
        fig_labels = json.loads(pathlib.Path(args.fig_labels).read_text(encoding="utf-8"))
        media = remap_media(base, version, common["worktrees"], ROOT, fig_labels, letter_maps(table))
        pathlib.Path(args.out + ".versions.json").write_text(json.dumps(version), encoding="utf-8")   # 核对按同一张表取原版
        pathlib.Path(args.out).write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        print(f"== 在原版页面上按圈住的内容重新建锚点 {sum(n for _, n in outs)} 笔（锚点类型 {kinds}）；"
              f"图换过的图上笔 {len(media)} 笔；用 {time.time() - t0:.0f} s；写出 {args.out}")
        for i, src, what in media:
            print(f"  图 {src}  {i}  {what}")
        return
    if args.math_from_origin:
        # 当初挂在公式上的笔：在各自原版页面上换成符号锚点（每个版本一个进程），写进底稿笔记
        jobs = [dict(common, sha=sha, strokes=[s for s in g if s["anchor"].get("type") == "math"]) for sha, g in groups.items()]
        jobs = [j for j in jobs if j["strokes"]]
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            got = {k: v for r in pool.map(glyph_anchors, jobs) for k, v in r.items()}
        base = json.loads(pathlib.Path(args.math_from_origin).read_text(encoding="utf-8"))
        now, done, miss = int(time.time() * 1000), 0, []
        for s in base["strokes"]:
            if s["id"] not in got:
                continue
            r = got[s["id"]]
            if r is None:
                miss.append(s["id"])
                continue
            s.update(anchor={"type": "math", "tex": r["tex"], "ctx": s["anchor"].get("ctx", ""), "g": r["g"]},
                     frame="px", orig=r["orig"], pts=r["pts"], updatedAt=now)
            done += 1
        pathlib.Path(args.out).write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        print(f"== 公式笔换成原版页面上的符号锚点 {done} 笔，原版上看不到 {len(miss)} 笔 {miss}；用 {time.time() - t0:.0f} s；"
              f"写出 {args.out}")
        return
    chunks = split(groups, ids, args.workers)
    with mp.get_context("spawn").Pool(len(chunks)) as pool:   # 各笔互不相关：每个进程自己的浏览器量一批
        outs = pool.map(measure, [dict(common, groups=g) for g in chunks])
    res = {k: v for r, _ in outs for k, v in r.items()}
    print(f"  测量 {len(res)} 笔用 {time.time() - t0:.0f} s（{len(chunks)} 个进程；各进程 模板 / 当前页 / 匹配 秒："
          + "，".join("/".join(f"{tm[k]:.0f}" for k in ("templates", "new_page", "match")) for _, tm in outs) + "）",
          flush=True)
    moved = [(i, v[1], v[2], v[3]) for i, v in sorted(res.items()) if v[0] == "move"]
    kept = [i for i, v in res.items() if v[0] == "keep"]
    lost = [(i, v[1]) for i, v in sorted(res.items()) if v[0] == "lost"]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        new = open_with_notes(browser, ROOT / args.page, current)
        scale = new.evaluate("window.__nipcNotes.frame().s")
        boxes = new.evaluate(BOX_JS, [m[0] for m in moved])
        for i, dx, dy, _ in moved:
            b = boxes[i]
            new.evaluate(f"window.scrollTo(0, {max(0, b[1] + dy - 300)})")
            new.wait_for_timeout(100)
            new.evaluate("([i, dx, dy]) => window.__nipcNotes.moveBy(i, dx, dy)", [i, dx / scale, dy / scale])
        # 公式上还没有符号锚点的笔：原地重新建锚点，换成按符号挂靠
        legacy = [i for i in new.evaluate("window.__nipcNotes.strokes().filter(s => s.anchor.type === 'math' && "
                                          "!s.anchor.g).map(s => s.id)") if i in ids]
        boxes = new.evaluate(BOX_JS, legacy)
        for i in legacy:
            if boxes.get(i):
                new.evaluate(f"window.scrollTo(0, {max(0, boxes[i][1] - 300)})")
                new.wait_for_timeout(100)
                new.evaluate("i => window.__nipcNotes.moveBy(i, 0, 0)", i)
        repaired = dict(current, strokes=new.evaluate("window.__nipcNotes.strokes()"),
                        saved=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"))
        browser.close()
    pathlib.Path(args.out).write_text(json.dumps(repaired, ensure_ascii=False), encoding="utf-8")
    print(f"  挪笔并导出：到 {time.time() - t0:.0f} s", flush=True)
    if args.shots and moved:
        shot_chunks = split(groups, {m[0] for m in moved}, args.workers)
        with mp.get_context("spawn").Pool(len(shot_chunks)) as pool:
            pool.map(shoot, [dict(common, groups=g, repaired=repaired, shots=args.shots) for g in shot_chunks])
        print(f"  对照图：到 {time.time() - t0:.0f} s", flush=True)
    print(f"== 挪动 {len(moved)} 笔，原位不动 {len(kept)} 笔，认不出 {len(lost)} 笔；公式笔改符号锚点 {len(legacy)} 笔；"
          f"写出 {args.out}")
    for i, dx, dy, sc in moved:
        print(f"  挪动 {i}  dx {dx:+.0f}  dy {dy:+.0f}  相关系数 {sc:.3f}")
    for i, why in lost:
        print(f"  认不出 {i}  {why}")


if __name__ == "__main__":
    main()
