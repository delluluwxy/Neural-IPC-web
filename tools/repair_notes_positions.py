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
from webgen.annotate import PROBE_API_JS, inject_before_scripts, page_version

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


def swap_block(h, marker, tag, new):
    """把页面里内容含 marker 的那一个 <tag>…</tag> 块（逐块找，不跨块）的内容换成 new。"""
    for m in re.finditer(rf"<{tag}\b[^>]*>(.*?)</{tag}>", h, flags=re.S):
        if marker in m.group(1):
            return h[:m.start(1)] + new + h[m.end(1):]
    raise ValueError(f"页面里没有内容含 {marker!r} 的 <{tag}>")


def swap_layer(h):
    """页面 HTML 换上当前的标注层（annotate.py 的 JS 与 CSS 各一整块替换），返回新 HTML。"""
    from webgen.annotate import ANNOT_CSS, ANNOT_JS
    return swap_block(swap_block(h, "#annot-layer", "style", ANNOT_CSS), "window.__nipcNotes = {", "script", ANNOT_JS)


def with_new_layer(page_path):
    """旧版页面换上当前的标注层，写到同目录 <页名>.newlayer.html 并返回路径。"""
    out = page_path.with_name(page_path.stem + ".newlayer.html")
    out.write_text(swap_layer(page_path.read_text(encoding="utf-8")), encoding="utf-8")
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


def font_check(job):
    """（子进程）一个原版页面换上当前标注层、导入当初的笔：每笔锚点在本机排版下的参考矩形，与笔里存的画笔设备上的 orig 比。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        path = with_new_layer(checkout(job["sha"], job["worktrees"]) / job["page"])
        page = open_with_notes(browser, path, dict(job["current"], strokes=job["strokes"]))
        got = page.evaluate("ids => window.__nipcNotes.anchorRects(ids)", [s["id"] for s in job["strokes"]])
        fonts = page.evaluate("getComputedStyle(document.querySelector('article p')).fontFamily")
        browser.close()
    return got, fonts


STATE_JS = "ids => Object.fromEntries(ids.map(id => [id, (window.__nipcNotes.placed(id) || {state: 'none'}).state]))"


def build_check_page(sha, ids, out_dir, page_file):
    """核对页 check/<sha7>/<页>：那一版页面原样（只换上当前标注层），只显示 ids 这些笔；页面引用的图 / 视频按那一版拷过来。"""
    d = out_dir / sha[:7]
    d.mkdir(parents=True, exist_ok=True)
    src = checkout(sha, WORKTREES) / page_file
    tmp = with_new_layer(src)
    h = tmp.read_text(encoding="utf-8")
    tmp.unlink()
    pre = (f"<script>window.NIPC_ONLY = {json.dumps(sorted(ids))}; window.NIPC_CHECK_VERSION = '{sha[:7]}';</script>\n")
    h = inject_before_scripts(h, pre)
    (d / page_file).write_text(h, encoding="utf-8")
    for ref in sorted(set(re.findall(r'(?:src|href)="(assets/[^"#?]+)"', h))):
        p = d / ref
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{sha}:{ref}"], capture_output=True)
        if blob.returncode == 0:
            p.write_bytes(blob.stdout)
    return d / page_file


def page_states(job):
    """（子进程）打开核对页、导入笔记、展开全部折叠，返回每笔的显示状态（ok / hidden / orphan）。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = open_with_notes(browser, job["path"], job["notes"])
        got = page.evaluate(STATE_JS, job["ids"])
        browser.close()
    return got


def check_pages(args):
    """--check-pages：每笔分到它画的那一版（先取第一次存下时线上那版；在那版上是灰的，就按时间远近试别的上线版本），
    生成 check/<sha7>/ 核对页，最后逐页核：灰色（找不到锚点）必须 0 笔。"""
    import multiprocessing as mp
    stem = pathlib.Path(args.page).stem
    notes = json.loads(gh("-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/notes/{stem}.json"))
    history = note_history(stem)
    first = {}
    for t, n in history:
        for s in n["strokes"]:
            first.setdefault(s["id"], t)
    live = live_versions()
    blob = lambda sha: subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"{sha}:{args.page}"],
                                      capture_output=True, text=True).stdout.strip()
    distinct, seen = [], set()           # 上线过的页面版本（同一页面内容只取最早上线那次），从早到晚
    for t, sha in live:
        b = blob(sha)
        if b and b not in seen:
            seen.add(b)
            distinct.append((t, sha))
    ids = [s["id"] for s in notes["strokes"]]
    home = {}
    for i in ids:
        t = first[i]
        k = max(j for j, (lt, _) in enumerate(distinct) if lt <= t)
        home[i] = [distinct[k][1]] + [s for _, s in sorted(((abs(j - k), s) for j, (_, s) in enumerate(distinct) if j != k))]
    # 不删任何东西：最终核对页写到 check/，各轮试分组的页写到 NAS 测试目录 check_round<n>/（要清理就列清单请用户批准）
    final_dir = ROOT / "check"
    assert not final_dir.exists(), f"{final_dir} 已存在：先按删除流程清掉再重建"
    assign, todo = {}, {i: 0 for i in ids}
    for rnd in range(len(distinct)):
        if not todo:
            break
        by = {}
        for i, k in todo.items():
            if k < len(home[i]):
                by.setdefault(home[i][k], []).append(i)
        jobs = [dict(path=build_check_page(sha, g, WORKTREES / f"check_round{rnd}", args.page), notes=notes, ids=g) for sha, g in by.items()]
        with mp.get_context("spawn").Pool(min(len(jobs), args.workers)) as pool:
            outs = pool.map(page_states, jobs)
        nxt = {}
        for (sha, g), st in zip(by.items(), outs):
            for i in g:
                if st[i] == "ok":
                    assign[i] = sha
                else:
                    nxt[i] = todo[i] + 1
        todo = nxt
        print(f"  第 {rnd + 1} 轮：已定 {len(assign)} 笔，待定 {len(todo)} 笔", flush=True)
    groups = {}
    for i, sha in assign.items():
        groups.setdefault(sha, []).append(i)
    jobs = [dict(path=build_check_page(sha, g, final_dir, args.page), notes=notes, ids=g) for sha, g in groups.items()]
    with mp.get_context("spawn").Pool(min(len(jobs), args.workers)) as pool:
        outs = pool.map(page_states, jobs)
    import collections
    print(f"== 核对页 {len(groups)} 个（{final_dir}），笔 {len(assign)} / {len(ids)}；找不到家的 {sorted(todo)}")
    for (sha, g), st in zip(groups.items(), outs):
        print(f"  check/{sha[:7]}/{args.page}：{len(g)} 笔，状态 {dict(collections.Counter(st.values()))}")
    pathlib.Path(args.out).write_text(json.dumps({sha[:7]: g for sha, g in groups.items()}, indent=1), encoding="utf-8")


def drawing_sessions(history, gap_s=1200):
    """用户主动画笔的那几次：出现新笔的保存，相隔 ≤ gap_s 秒的归为同一次。返回 [{start, end, new, last_notes}]。"""
    seen, out = set(), []
    for t, n in history:
        ids = {s["id"] for s in n["strokes"]}
        new = ids - seen
        seen |= ids
        if not new:
            continue
        if out and (t - out[-1]["end"]).total_seconds() <= gap_s:
            out[-1].update(end=t, last_notes=n)
            out[-1]["new"] += len(new)
        else:
            out.append(dict(start=t, end=t, new=len(new), last_notes=n))
    return out


SNAP_STATE_JS = """() => { const N = window.__nipcNotes;
  return Object.fromEntries(N.strokes().map(s => [s.id, (N.placed(s.id) || {state: 'none'}).state])); }"""


def snapshot_states(path):
    """（子进程）打开快照页（笔记嵌在页里，不导入）、展开全部折叠，返回 (每笔的显示状态, 可见灰色笔数)。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEW)
        page.goto(pathlib.Path(path).resolve().as_uri(), wait_until="domcontentloaded", timeout=120000)
        page.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
        page.evaluate("MathJax.startup.promise")
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.wait_for_timeout(1500)
        got, gray = page.evaluate(SNAP_STATE_JS), page.evaluate(VISIBLE_GRAY_JS)
        browser.close()
    return got, gray


def write_snapshot_page(d, page_file, h, notes, read_asset):
    """只读快照页：页面 HTML h 里嵌入笔记 notes（window.NIPC_SNAPSHOT，标注层据此不读写存储、不同步），写到 d/<页>，
    页面引用的 assets/… 由 read_asset(相对路径) 取字节拷到 d 下同一路径（取不到的跳过）。同名文件覆盖，不删任何东西。"""
    snap = json.dumps(notes, ensure_ascii=False).replace("</", "<\\/")
    h = inject_before_scripts(h, f"<script>window.NIPC_SNAPSHOT = {snap};</script>\n")
    (d / page_file).write_text(h, encoding="utf-8")
    for ref in sorted(set(re.findall(r'(?:src|href)="(assets/[^"#?]+)"', h))):
        blob = read_asset(ref)
        if blob is not None:
            (d / ref).parent.mkdir(parents=True, exist_ok=True)
            (d / ref).write_bytes(blob)


def snapshots(args):
    """--snapshots：每一次主动画笔之后的样子 = 那次最后一次保存的笔记（原样）+ 当时线上的页面，做成只读快照页
    snapshot/<次>/<页>（一次里页面换过版本的，前后各一页 snapshot/<次>a、<次>b）。逐页统计灰色（找不到锚点）笔数。"""
    import multiprocessing as mp
    stem = pathlib.Path(args.page).stem
    history = note_history(stem)
    live = live_versions()
    out_dir = ROOT / "snapshot"
    assert not out_dir.exists(), f"{out_dir} 已存在：先按删除流程清掉再重建（本工具不删任何东西）"
    pages = []
    for k, ses in enumerate(drawing_sessions(history), 1):
        vers = []
        for t in (ses["start"], ses["end"]):
            v = [s for lt, s in live if lt <= t][-1]
            if v not in vers:
                vers.append(v)
        for j, sha in enumerate(vers):
            name = f"{k}{'ab'[j] if len(vers) > 1 else ''}"
            d = out_dir / name
            d.mkdir(parents=True)
            tmp = with_new_layer(checkout(sha, WORKTREES) / args.page)
            h = tmp.read_text(encoding="utf-8")
            tmp.unlink()
            write_snapshot_page(d, args.page, h, ses["last_notes"], lambda ref: git_bytes(f"{sha}:{ref}"))
            pages.append((name, ses, sha, d / args.page))
    with mp.get_context("spawn").Pool(len(pages)) as pool:
        outs = [st for st, _ in pool.map(snapshot_states, [p for *_, p in pages])]
    import collections
    pdt = lambda t: (t - datetime.timedelta(hours=7)).strftime("%m-%d %H:%M")
    rows = []
    for (name, ses, sha, path), st in zip(pages, outs):
        c = collections.Counter(st.values())
        rows.append(dict(name=name, page=sha[:7], start=ses["start"].isoformat(), end=ses["end"].isoformat(),
                         strokes=len(st), states=dict(c), gray=[i for i, v in st.items() if v == "orphan"]))
        print(f"  snapshot/{name}/{args.page}：第 {name[0]} 次画（{pdt(ses['start'])}–{pdt(ses['end'])} 加州时间）画完的笔记 + 页面 {sha[:7]}；"
              f"{len(st)} 笔，状态 {dict(c)}")
    pathlib.Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")


def write_original(args):
    """--write-original BASE_SHA：用户自己画的原始位置。以笔记仓库 BASE_SHA 那份为准（哪些笔还在、墓碑），每一笔换回它
    第一次存进仓库时的样子（画完那一刻设备上建的锚点和坐标，之后的任何迁移都不要），写回笔记仓库，并把写出的内容存 --out。"""
    import base64
    import tempfile
    stem = pathlib.Path(args.page).stem
    path = f"repos/{REPO}/contents/notes/{stem}.json"
    history = note_history(stem)
    first = {}
    for _, notes in history:
        for s in notes["strokes"]:
            first.setdefault(s["id"], s)
    base = json.loads(gh("-H", "Accept: application/vnd.github.raw", f"{path}?ref={args.write_original}"))
    changed = sum(1 for s in base["strokes"] if first[s["id"]] != s)
    base["strokes"] = [first[s["id"]] for s in base["strokes"]]
    base["saved"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pathlib.Path(args.out).write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    cur = json.loads(gh(path))
    body = {"message": f"notes {stem}: every stroke back to its first saved form (as drawn), stroke set of {args.write_original[:7]}",
            "content": base64.b64encode(json.dumps(base, ensure_ascii=False).encode()).decode(), "sha": cur["sha"]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", dir=WORKTREES, delete=False) as fh:
        json.dump(body, fh)
    try:
        out = json.loads(gh("--method", "PUT", path, "--input", fh.name))
    finally:
        pathlib.Path(fh.name).unlink()
    print(f"== {len(base['strokes'])} 笔换回各自第一次存下时的样子（其中 {changed} 笔之后被改过），提交 {out['commit']['sha'][:7]}")


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


# ---------- --apply-snapshot：用户确认过的快照（页面 + 笔记）里每笔挂的内容，按两版内容的 diff 迁到最新版 ----------
# 用户 10-25 确认：线上 snapshot/2/（页面 8288b02 + 笔记 2e32506）上每一笔位置都对。每笔在那一版挂在哪个字 / 符号 / 图上
# 由当时的标注层（快照页里那一份，原样不换）算出，是确定的；再把那一版的内容对应到最新版的内容，笔跟着走：
#   正文：两版正文文字索引（标注层 buildIndex 的同一串）做字级 diff（difflib.SequenceMatcher，isjunk=None, autojunk=False），
#         equal 段逐字一一对应，replace / delete 段里的字没有对应 → 这笔隐藏；对上了就按新位置取新的 prefix / exact / suffix。
#   公式：两版公式（按文档顺序，陈列 + 行内）按 TeX 序列 diff；equal 的原样对上，replace 段里按 TeX 相似度（SequenceMatcher.ratio
#         ≥ MATH_PAIR_MIN）做保序配对；对上的公式内按符号序列 diff（同上参数），锚点符号映射不到 → 隐藏。
#   图：图文件像素相同 → 原样；像素变了 → follow_label（图中文字标注的精确框，sandbox tools/figs/fig_label_boxes.py），
#       不用任何改名表（标注文字原样相同才算同一个）；最近的标注新图里没了 → 隐藏。
# 不用任何手写对应表（改名表的 quote / text / tex 一律不用）。drawn = 快照里这一笔原样 + 页面版本号，不改。
SNAP_REF = "origin/main:snapshot/2"        # 线上快照页（git 里的原样，与 GitHub Pages 上的逐字节相同）
SNAP_NOTES, SNAP_PAGE, TOMBS_AT = "2e32506", "8288b02", "c13089b"
MATH_PAIR_MIN = 0.5
TEXT_SEG_MIN, TEXT_SEG_MARGIN = 8, 4   # 正文：锚点字所在 equal 段 ≥ 8 字，且它前后各 4 字也在这一段里，才接受
GLYPH_SEG_MIN = 3                      # 公式（TeX 改过的）：锚点符号所在 equal 段 ≥ 3 个符号才接受
CROSS_LINE = 1.5                       # 笔画竖向跨度超过锚字高的这么多倍，算跨行（压住哪些字算不清）
SENT_END = "。！？；!?;"
# 可见的灰色笔：找不到圈的内容（orphan）的笔却还显示着（屏幕图层或打印副本里），任何页面任何状态都必须是 0
VISIBLE_GRAY_JS = """() => [...document.querySelectorAll('#annot-layer path.orphan, svg.annot-print path.orphan')]
  .filter(p => getComputedStyle(p).display !== 'none' && p.getBoundingClientRect().width > 0).length"""


def git_bytes(spec):
    """git show <提交>:<路径> 的字节；没有返回 None。"""
    r = subprocess.run(["git", "-C", str(ROOT), "show", spec], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def serve():
    """本机起一个只读 HTTP 服务（127.0.0.1，随机端口）给出本仓库工作区文件（页面本身由 open_served 的 route 直接给，不落盘）。
    返回 (base_url, server)。"""
    import functools
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(ROOT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv


def open_served(browser, base, path, html, git_prefix=None, notes=None):
    """打开 base + path，页面 HTML 由 route 直接给 html；git_prefix 时同目录下的资源取 git 对象（git_prefix/<相对路径>）。
    notes 给了就导入（替换）。等 MathJax 和图片，展开全部折叠后返回 page。"""
    page = browser.new_page(viewport=VIEW)
    url, folder = base + path, base + path.rsplit("/", 1)[0] + "/"

    def handle(route, request):
        if request.url == url:
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        if git_prefix and request.url.startswith(folder):
            blob = git_bytes(f"{git_prefix}/{request.url[len(folder):].split('?')[0]}")
            return route.fulfill(status=200, body=blob) if blob is not None else route.fulfill(status=404, body="")
        return route.continue_()
    page.route(base + "/**", handle)
    page.goto(url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_function("window.__nipcNotes && window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
    page.evaluate("MathJax.startup.promise")
    page.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : "
                  "new Promise(r => { i.onload = i.onerror = r; }))))")
    if notes is not None:
        if page.is_visible("#annot-fab"):
            page.click("#annot-fab")
        page.set_input_files("#annot-file", files=[{"name": "notes.json", "mimeType": "application/json",
                                                    "buffer": json.dumps(dict(notes, page=path.rsplit('/', 1)[-1]), ensure_ascii=False).encode()}])
        page.click('[data-import="replace"]')
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_timeout(1500)
    return page


def opcode_map(a, b):
    """序列 a → b 的逐项对应（difflib.SequenceMatcher(None, a, b, autojunk=False) 的 equal 段）：
    {a 下标: (b 下标, 所在 equal 段在 a 里的 [起, 止))}。"""
    import difflib
    out = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.update((i, (j, i1, i2)) for i, j in zip(range(i1, i2), range(j1, j2)))
    return out


def mapped(m, i, seg_min, margin=0):
    """i 经 opcode_map 结果 m 映射到的新下标；所在 equal 段短于 seg_min、或 i 前后 margin 项不在同一段里返回 None
    （防误配：改写区里碰巧相同的零星单字 / 符号不算对应）。另返回所在 equal 段长度（没对上为 0）。"""
    if i not in m:
        return None, 0
    j, i1, i2 = m[i]
    return (j if i2 - i1 >= seg_min and i - margin >= i1 and i + margin < i2 else None), i2 - i1


def pair_maths(old, new):
    """两版公式（[{tex, ...}] 按文档顺序）的对应 {旧序号: 新序号}：TeX 序列 diff 的 equal 段原样对上；replace 段里按 TeX
    相似度（≥ MATH_PAIR_MIN）做保序配对（动态规划取相似度之和最大）；最后剩下的里 TeX 原样相同且两边各唯一的（挪了位置）对上。
    返回 (对应, 按最后这条对上的旧序号集合)。"""
    import difflib
    out = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [m["tex"] for m in old], [m["tex"] for m in new],
                                                       autojunk=False).get_opcodes():
        if tag == "equal":
            out.update(zip(range(i1, i2), range(j1, j2)))
        elif tag == "replace":
            sim = [[difflib.SequenceMatcher(None, old[i]["tex"], new[j]["tex"], autojunk=False).ratio()
                    for j in range(j1, j2)] for i in range(i1, i2)]
            n, m = i2 - i1, j2 - j1
            best = [[0.0] * (m + 1) for _ in range(n + 1)]
            for i in range(n - 1, -1, -1):
                for j in range(m - 1, -1, -1):
                    take = sim[i][j] + best[i + 1][j + 1] if sim[i][j] >= MATH_PAIR_MIN else -1
                    best[i][j] = max(take, best[i + 1][j], best[i][j + 1])
            i = j = 0
            while i < n and j < m:
                if sim[i][j] >= MATH_PAIR_MIN and best[i][j] == sim[i][j] + best[i + 1][j + 1]:
                    out[i1 + i] = j1 + j
                    i, j = i + 1, j + 1
                elif best[i][j] == best[i + 1][j]:
                    i += 1
                else:
                    j += 1
    # 挪了位置的公式（保序 diff 认不出挪动）：剩下没对上的里，TeX 原样相同、且在两边剩下的里各只有一个的，对上
    left_old = [i for i in range(len(old)) if i not in out]
    left_new = [j for j in range(len(new)) if j not in set(out.values())]
    moved = set()
    count = lambda idx, seq, tex: sum(seq[k]["tex"] == tex for k in idx)
    for i in left_old:
        hit = [j for j in left_new if new[j]["tex"] == old[i]["tex"]]
        if len(hit) == 1 and count(left_old, old, old[i]["tex"]) == 1:
            out[i] = hit[0]
            moved.add(i)
    return out, moved


def sentence_span(text, pos):
    """文字索引 text 里位置 pos 所在整句的 [起, 止)（到前后最近的句末标点，各最多 120 字）。"""
    lo = max(max(text.rfind(c, max(0, pos - 120), pos) for c in SENT_END) + 1, pos - 120, 0)
    ends = [i for i in (text.find(c, pos, pos + 120) for c in SENT_END) if i >= 0]
    return lo, (min(ends) + 1 if ends else min(len(text), pos + 120))


def sentence(text, pos):
    """文字索引 text 里位置 pos 所在的整句。"""
    lo, hi = sentence_span(text, pos)
    return text[lo:hi].strip()


class Tokens:
    """正文 diff 用的记号序列：文字索引的每个字一个记号，每个行内公式按 TeX 原文一个记号（插在它在文字索引里的位置前）。
    行内公式不在文字索引里，单比文字时公式改了也看不出来；当作记号后，公式一改 equal 段就断开。
    toks[i]：字本身或 "\\0" + TeX；pos[i]：记号对应的文字位置（公式为 None）；of[p]：文字位置 p 的记号下标；
    width[i]：公式在本机排版下的宽（正文卡片 px，字为 None）。"""

    def __init__(self, ix):
        at = {}
        for m in ix["maths"]:
            if not m["display"]:
                at.setdefault(m["pos"] if m["pos"] >= 0 else len(ix["text"]), []).append(m)
        self.toks, self.pos, self.width, self.of = [], [], [], []
        for p in range(len(ix["text"]) + 1):
            for m in at.get(p, []):
                self.toks.append("\0" + m["tex"])
                self.pos.append(None)
                self.width.append(m.get("width") or 0)
            if p < len(ix["text"]):
                self.of.append(len(self.toks))
                self.toks.append(ix["text"][p])
                self.pos.append(p)
                self.width.append(None)

    def show(self, i):
        """记号给人看：字原样，公式写成 $TeX$。"""
        return f"${self.toks[i][1:]}$" if self.toks[i].startswith("\0") else self.toks[i]

    def span(self, lo, hi):
        """文字位置 [lo, hi) 覆盖的记号下标 [起, 止)（含其间的行内公式）。"""
        return self.of[lo] if lo < len(self.of) else len(self.toks), self.of[hi - 1] + 1 if hi > lo else self.of[lo]


def pressed_tokens(s, T, ti, anchor_w):
    """这笔从锚字（记号 ti）起压住了哪些记号：[t0, t1]（含两端）。用画笔设备上的量算——orig.w / orig.h 是锚字在她 iPad
    上的宽高，pts 是相对锚字左上角的点（px）；字一个按 orig.w 宽，行内公式按「8288b02 页面上该公式宽 / 锚字宽 anchor_w」
    × orig.w 估。笔画竖向跨度超过 CROSS_LINE × orig.h（跨行）或不是 px 坐标的返回 None（退回段边规则）。"""
    o = s["orig"]
    if s["frame"] != "px" or not o.get("w") or not o.get("h"):
        return None
    xs, ys = [p[0] for p in s["pts"]], [p[1] for p in s["pts"]]
    if max(ys) - min(ys) > CROSS_LINE * o["h"]:
        return None
    wid = lambda i: o["w"] if T.width[i] is None else (T.width[i] / anchor_w * o["w"] if anchor_w else o["w"])
    t0, x = ti, 0.0                                  # 往左：记号 t0-1 占 [x - 宽, x)
    while t0 > 0 and x > min(xs):
        x -= wid(t0 - 1)
        t0 -= 1
    t1, x = ti, wid(ti)                              # 往右：记号 t1+1 占 [x, x + 宽)
    while t1 + 1 < len(T.toks) and x <= max(xs):
        x += wid(t1 + 1)
        t1 += 1
    return t0, t1


def render_tokens(T, lo, hi, mark=(None, None)):
    """记号 [lo, hi) 给人看的样子：[[文字或 $TeX$, 是不是公式, 在不在 mark 范围（含两端）里], …]（hidden.html 用）。"""
    return [[T.show(i), T.toks[i].startswith("\0"), mark[0] is not None and mark[0] <= i <= mark[1]] for i in range(lo, hi)]


def most_similar_sentence(text, s):
    """最新版正文里与句子 s 最像的一句（按句末标点切句，SequenceMatcher.ratio 最大），只给人看。"""
    import difflib
    sents = [x.strip() for x in re.split(f"(?<=[{SENT_END}])", text) if x.strip()]
    return max(sents, key=lambda x: difflib.SequenceMatcher(None, s, x, autojunk=False).ratio(), default="")


def migrate_text(s, w, old, new, To, Tn, tmap, row):
    """正文笔：锚字（旧文字位置 w["pos"]）经记号级映射 tmap（opcode_map(To.toks, Tn.toks)）到新位置 → 新 TextQuote 锚点；
    映射不到返回 (None, 原因)。这笔压住的记号（pressed_tokens）全在同一个 equal 段里、且该段 ≥ TEXT_SEG_MIN 个记号才接受；
    跨行算不清的退回「锚字前后各 TEXT_SEG_MARGIN 个记号在同一段」（row["rule"] 记用的哪条）。row 里另记 seg（锚字所在
    equal 段长）、pressed_old / pressed_new（压住的记号，给人看）、old_tokens（8288b02 里的整句，压住的标出，hidden.html 用）。"""
    ti = To.of[w["pos"]]
    ks = pressed_tokens(s, To, ti, w.get("w") or 0)
    row["rule"] = "pressed" if ks else "margin（跨行，退回段边规则）"
    lo, hi = sentence_span(old["text"], w["pos"])
    a0, a1 = To.span(lo, hi)
    if ks:
        a0, a1 = min(a0, ks[0]), max(a1, ks[1] + 1)
    row["old_tokens"] = render_tokens(To, a0, a1, ks or (ti, ti))
    tj, row["seg"] = mapped(tmap, ti, TEXT_SEG_MIN, 0 if ks else TEXT_SEG_MARGIN)
    if ks:
        row["pressed_old"] = "".join(To.show(i) for i in range(ks[0], ks[1] + 1))
        if tj is not None:
            row["pressed_new"] = "".join(Tn.show(i) for i in range(tj + ks[0] - ti, tj + ks[1] - ti + 1))
        segs = {tmap[i][1:] if i in tmap else None for i in range(ks[0], ks[1] + 1)}
        if tj is None or len(segs) != 1 or None in segs:
            return None, ("圈住的字改了" if row["seg"] else "锚点字在最新版里没有对应（diff 里属于改写 / 删除的那段）")
    if tj is None:
        return None, ("所在原文被改写（锚点字所在 equal 段太短或锚点字离段边不足 "
                      f"{TEXT_SEG_MARGIN} 个记号）" if row["seg"] else "锚点字在最新版里没有对应（diff 里属于改写 / 删除的那段）")
    p, L = Tn.pos[tj], 30   # annotate.py CFG.quoteLen
    a = {"type": "text", "exact": new["text"][p], "prefix": new["text"][max(0, p - L):p], "suffix": new["text"][p + 1:p + 1 + L]}
    return dict(s, anchor=a), p


def migrate_math(s, w, old, new, mpair, row):
    """公式笔：旧公式（序号）经公式对应、旧符号经符号 diff 映射 → 新公式 TeX + 新符号锚点；整块公式锚点（没有符号）
    只对应公式。TeX 原样相同的公式符号逐个对应（row["seg"] = 整个公式的符号数）；TeX 变了的按符号 diff，
    equal 段 ≥ GLYPH_SEG_MIN 个符号才算。映射不到返回 (None, 原因)。"""
    k = mpair.get(w["math"])
    if k is None:
        return None, "所在公式在最新版里没有对应"
    om, nm, L = old["maths"][w["math"]], new["maths"][k], 8   # annotate.py CFG.glyphLen
    ctx = new["text"][max(0, nm["pos"] - 30):nm["pos"]] if nm["pos"] >= 0 else ""
    a = {"type": "math", "tex": nm["tex"], "ctx": ctx}
    if w["kind"] == "glyph":
        if om["tex"] == nm["tex"]:
            gi, row["seg"] = w["gi"], len(om["ch"])
        else:
            gi, row["seg"] = mapped(opcode_map(om["ch"], nm["ch"]), w["gi"], GLYPH_SEG_MIN)
        if gi is None:
            return None, ("所在原文被改写（公式改了，锚点符号所在 equal 段不足 "
                          f"{GLYPH_SEG_MIN} 个符号）" if row["seg"] else "公式对上了，但锚点符号在新公式里没有对应")
        ch = nm["ch"]
        a["g"] = {"prefix": "".join(ch[max(0, gi - L):gi]), "exact": ch[gi], "suffix": "".join(ch[gi + 1:gi + 1 + L])}
    return dict(s, anchor=a), (k, gi if w["kind"] == "glyph" else -1)


def migrate_media(s, w, fig_labels, maps):
    """图上的笔：快照里那张图与最新版同名图像素相同 → 原样；像素变了 → follow_label：先要求标注文字原样相同，
    最近的标注新图里没有时，再按 maps（讲稿改名表 tex 里只差字母一一换名的那些对，letter_maps）认换了字母的同一个标注。
    返回 (新笔 / None, 说明)。"""
    sys.path.insert(0, str(ROOT.parent / "Neural-IPC-sandbox/tools/figs"))
    from fig_label_boxes import pixel_hash
    old_b = git_bytes(f"{SNAP_REF}/assets/images/{w['src']}")
    new_p = ROOT / "assets/images" / w["src"]
    if old_b is None or not new_p.exists():
        return None, "最新版里没有这张图"
    ho, hn = pixel_hash(io.BytesIO(old_b)), pixel_hash(new_p)
    if ho == hn:
        return dict(s), "图像素没变"
    by_hash = {v["pixels"]: v for d in fig_labels.values() for v in d.values()}
    if ho not in by_hash or hn not in by_hash:
        return None, "图重画过，但 fig_labels 里认不出两版是哪个提交画的"
    r, how = follow_label(s["pts"], by_hash[ho], by_hash[hn], []), "原样相同"
    if r["gone"]:
        r, how = follow_label(s["pts"], by_hash[ho], by_hash[hn], maps), "按改名表的字母换名（letter_maps + same_label）认出"
    if r["gone"]:
        return None, f"图重画过，离笔最近的标注「{r['label']}」新图里没了（原样、按改名表字母换名都没有）"
    return (dict(s, pts=[q + [p[2]] for q, p in zip(r["pts"], s["pts"])]),
            f"图重画过，跟着标注「{r['label']}」走（新图里的同一个标注{how}）")


def apply_snapshot(args):
    """--apply-snapshot：快照（SNAP_REF 页面 + SNAP_NOTES 笔记，按 TOMBS_AT 的墓碑去掉之后删的笔）里每笔挂的内容按两版 diff
    迁到最新版（工作区的页面，换上当前标注层）。写 --out（带 drawn 的新笔记）、<out 目录>/apply_snapshot2_report.json / .md，
    最后在最新版上无头浏览器核：迁过去的笔挂在映射到的位置、映射不到的隐藏、可见灰色 0。"""
    stem = pathlib.Path(args.page).stem
    raw = lambda sha: json.loads(gh("-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/notes/{stem}.json?ref={sha}"))
    notes, tombs = raw(SNAP_NOTES), raw(TOMBS_AT).get("tombstones", {})
    at = json.loads(gh(f"repos/{REPO}/commits/{SNAP_NOTES}"))["commit"]["committer"]["date"]
    strokes = [s for s in notes["strokes"] if s["id"] not in tombs]
    ids = [s["id"] for s in strokes]
    drawn_page = page_version(git_bytes(f"{SNAP_PAGE}:{args.page}").decode("utf-8"))
    snap_html = git_bytes(f"{SNAP_REF}/{args.page}").decode("utf-8")
    assert "window.NIPC_SNAPSHOT = " in snap_html, "快照页里没有嵌入的笔记"
    old_html = snap_html.replace("window.__nipcNotes = {", "window.__nipcNotes = {" + PROBE_API_JS, 1)   # 旧标注层原样，只补只读探查接口
    new_html = swap_layer((ROOT / args.page).read_text(encoding="utf-8"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        base, srv = serve()
        pg = open_served(browser, base, f"/_snap/{args.page}", old_html, git_prefix=SNAP_REF)
        old, w_old = pg.evaluate("window.__nipcNotes.contentIndex()"), pg.evaluate("ids => window.__nipcNotes.where(ids)", ids)
        pg.close()
        pg = open_served(browser, base, f"/{args.page}", new_html)
        new = pg.evaluate("window.__nipcNotes.contentIndex()")
        pg.close()
        To, Tn = Tokens(old), Tokens(new)   # 正文 + 行内公式（按 TeX 一个记号）的序列，两版逐记号 diff
        tmap = opcode_map(To.toks, Tn.toks)
        mpair, moved_tex = pair_maths(old["maths"], new["maths"])
        fig_labels = json.loads(pathlib.Path(args.fig_labels).read_text(encoding="utf-8"))
        from webgen.annotate import DRAFTS   # 图中标注的字母换名：只取改名表 tex 里只差字母一一换名的公式对（letter_maps）
        maps = letter_maps(json.loads((DRAFTS / f"notation_renames_{stem}.json").read_text(encoding="utf-8")).get("tex", {}))
        out, rows, expect, now_ms = [], [], {}, int(datetime.datetime.now().timestamp() * 1000)
        for s in strokes:
            w, row = w_old.get(s["id"]) or {"state": "none"}, {"id": s["id"], "type": s["anchor"]["type"]}
            drawn = {"page": drawn_page, "pts": s["pts"], "frame": s["frame"], "anchor": s["anchor"], "orig": s["orig"],
                     "ua": None, "at": at}
            kind = w.get("kind")
            if kind == "text":
                row["old"] = sentence(old["text"], w["pos"])
                got, how = migrate_text(s, w, old, new, To, Tn, tmap, row)
            elif kind in ("glyph", "math"):
                row["old"] = old["maths"][w["math"]]["tex"]
                got, how = migrate_math(s, w, old, new, mpair, row)
                if w["math"] in moved_tex:
                    row["math_pair"] = "挪了位置：TeX 原样相同、两边剩下的里各唯一"
            elif kind == "media":
                row["old"] = w["src"]
                got, how = migrate_media(s, w, fig_labels, maps)
            elif kind == "page":
                got, how = dict(s), "页边（锚到正文卡片本身）"
            else:
                got, how = None, f"快照页上这笔没挂上（状态 {w['state']}）"
            if got is None:
                row.update(result="hidden", why=how)
                if kind == "text":   # 最新版同一位置附近：锚字之前最近一个对上了的字所在的那句
                    q = max((i for i in tmap if i <= To.of[w["pos"]] and Tn.pos[tmap[i][0]] is not None), default=None)
                    if q is not None:
                        lo, hi = sentence_span(new["text"], Tn.pos[tmap[q][0]])
                        row["new_here"] = new["text"][lo:hi].strip()
                        row["new_tokens"] = render_tokens(Tn, *Tn.span(lo, hi))
                    row["new_similar"] = most_similar_sentence(new["text"], row["old"])
                elif kind in ("glyph", "math"):
                    import difflib
                    row["new_similar"] = max((m["tex"] for m in new["maths"]), default="",
                                             key=lambda t: difflib.SequenceMatcher(None, row["old"], t, autojunk=False).ratio())
                got = dict(s, anchor=dict(s["anchor"], gone=True))
            else:
                row["result"] = "moved"
                if kind == "text":
                    row["new"], expect[s["id"]] = sentence(new["text"], how), {"kind": "text", "pos": how}
                elif kind in ("glyph", "math"):
                    row["new"] = new["maths"][how[0]]["tex"]
                    expect[s["id"]] = {"kind": kind, "math": how[0], "gi": how[1]}
                else:
                    row["new"] = how
            # 匹配字段换了：updatedAt 取现在，同步合并时胜过各设备本机存的旧锚点（mergeNotes 按 updatedAt 取新的；drawn 照样带上）
            got["drawn"], got["updatedAt"] = drawn, now_ms
            out.append(got)
            rows.append(row)
        result = dict(notes, strokes=out, tombstones=tombs, saved=datetime.datetime.now(datetime.timezone.utc).isoformat())
        pathlib.Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        # 核：最新版上导入迁移后的笔记，每笔挂的位置 = 映射到的位置；隐藏的不显示；可见灰色 0
        pg = open_served(browser, base, f"/{args.page}", new_html, notes=result)
        w_new = pg.evaluate("ids => window.__nipcNotes.where(ids)", ids)
        gray = pg.evaluate(VISIBLE_GRAY_JS)
        browser.close()
        srv.shutdown()
    bad = []
    for r in rows:
        w = w_new.get(r["id"]) or {}
        if r["result"] == "hidden":
            r["check"] = "ok" if w.get("state") == "orphan" else f"应隐藏，实际 {w.get('state')}"
        elif r["id"] in expect:
            e = expect[r["id"]]
            same = all(w.get(k) == v for k, v in e.items())
            r["check"] = "ok" if same and w.get("state") in ("ok", "hidden") else f"挂的位置与映射不同：{w}"
        else:
            r["check"] = "ok" if w.get("state") in ("ok", "hidden") else f"状态 {w.get('state')}"
        if r["check"] != "ok":
            bad.append(r)
    write_apply_report(args, rows, gray, bad, drawn_page)


def check_drawn(args):
    """--check-drawn NOTES_JSON：读回写出的笔记，与笔记仓库 SNAP_NOTES 那份逐笔逐字节比 drawn（pts / frame / anchor / orig
    各自 json.dumps 后相同），并核笔的集合 = SNAP_NOTES 的笔去掉 TOMBS_AT 墓碑里的、墓碑 = TOMBS_AT 的。只读。"""
    import collections
    stem = pathlib.Path(args.page).stem
    raw = lambda sha: json.loads(gh("-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/notes/{stem}.json?ref={sha}"))
    base, tombs = raw(SNAP_NOTES), raw(TOMBS_AT).get("tombstones", {})
    got = json.loads(pathlib.Path(args.check_drawn).read_text(encoding="utf-8"))
    want = {s["id"]: s for s in base["strokes"] if s["id"] not in tombs}
    have = {s["id"]: s for s in got["strokes"]}
    dump = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    diff = [i for i in want if i in have and any(dump(have[i]["drawn"][k]) != dump(want[i][k]) for k in ("pts", "frame", "anchor", "orig"))]
    pages = collections.Counter(s["drawn"]["page"] for s in got["strokes"])
    print(f"== 写出 {len(have)} 笔，{SNAP_NOTES} 去掉墓碑后 {len(want)} 笔；缺 {sorted(set(want) - set(have))}，多 {sorted(set(have) - set(want))}；"
          f"drawn 与 {SNAP_NOTES} 逐字节不同 {len(diff)} 笔 {diff[:10]}；drawn.page 分布 {dict(pages)}；"
          f"墓碑与 {TOMBS_AT} 相同 {dump(got.get('tombstones')) == dump(tombs)}（{len(tombs)} 条）")


PLAIN_WHY = [   # 隐藏原因 → 给用户看的话（按原因开头认）
    ("圈住的字改了", "你圈的字在新版里改写了"),
    ("锚点字在最新版里没有对应", "这句话在新版里改写或删掉了"),
    ("所在原文被改写（公式改了", "公式改了，你标的那个符号新版里没有了"),
    ("所在原文被改写", "这句话在新版里改写了"),
    ("所在公式在最新版里没有对应", "这个公式在新版里改写或删掉了"),
    ("公式对上了", "公式改了，你标的那个符号新版里没有了"),
    ("图重画过，离笔最近的标注", "图重画了，你标的那个标注新版图里没有了"),
    ("最新版里没有这张图", "这张图新版里没有了"),
]
HIDDEN_CSS = """
:root { --ink: #1d232b; --muted: #5d6673; --line: #dde2e8; --bg: #f6f7f9; --card: #fff; --mark: #ffe58a; }
@media (prefers-color-scheme: dark) { :root { --ink: #e8ebef; --muted: #a3acb8; --line: #333a44; --bg: #16191d;
  --card: #1f2329; --mark: #7a6516; } }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.7 -apple-system, "PingFang SC", "Noto Sans CJK SC", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 6px; } p.lead { color: var(--muted); margin: 0 0 20px; }
ol { padding: 0; list-style: none; counter-reset: n; }
li { counter-increment: n; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; margin: 0 0 12px; }
li::before { content: counter(n) "．" attr(data-kind); font-weight: 600; }
.row { display: grid; grid-template-columns: 5.5em 1fr; gap: 2px 8px; margin-top: 6px; }
.k { color: var(--muted); } mark { background: var(--mark); color: inherit; padding: 0 1px; border-radius: 2px; }
.why { font-weight: 600; } code { font-size: 13px; word-break: break-all; }
"""


def tokens_html(toks):
    """render_tokens 的结果 → HTML：字转义，行内公式交给 MathJax（\\( \\)），被圈住的套 <mark>。"""
    import html
    out = []
    for text, is_math, marked in toks:
        piece = f"\\({html.escape(text[1:-1])}\\)" if is_math else html.escape(text)
        out.append(f"<mark>{piece}</mark>" if marked else piece)
    return "".join(out).replace("</mark><mark>", "")


def hidden_item(r):
    """隐藏清单里的一条：原来（圈住的标出）、新版这里、为什么不显示。"""
    import html
    why = next((v for k, v in PLAIN_WHY if r["why"].startswith(k)), r["why"])
    kind = {"text": "正文", "math": "公式", "media": "图"}.get(r["type"], r["type"])
    if r["type"] == "text":
        old = tokens_html(r.get("old_tokens") or [[r.get("old", ""), False, False]])
        now = tokens_html(r["new_tokens"]) if r.get("new_tokens") else "（附近没有对得上的句子）"
    elif r["type"] == "math":
        old, now = f"\\[{html.escape(r.get('old', ''))}\\]", f"\\[{html.escape(r.get('new_similar', ''))}\\]（最像的公式）"
    else:
        old, now = f"<code>{html.escape(r.get('old', ''))}</code>", "—"
    return (f'<li data-kind="{kind}"><div class="row"><span class="k">原来</span><span>{old}</span>'
            f'<span class="k">新版这里</span><span>{now}</span>'
            f'<span class="k">为什么</span><span class="why">{html.escape(why)}</span></div></li>')


def write_hidden_page(path, rows):
    """preview/hidden.html：每笔隐藏的笔一条，给用户在 iPad 上看（中文、外行能懂）。"""
    hid = [r for r in rows if r["result"] == "hidden"]
    body = "".join(hidden_item(r) for r in hid)
    path.write_text(f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>新版里不显示的笔</title><style>{HIDDEN_CSS}</style>
<script>window.MathJax = {{tex: {{inlineMath: [["\\\\(", "\\\\)"]]}}, svg: {{fontCache: "global"}}}};</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg.js" async></script>
<main><h1>新版里不显示的笔（{len(hid)} 笔）</h1>
<p class="lead">每一条是你第 2 次画的一笔。它圈住的内容在新版里改了或删了，所以在新版页面上不显示（笔记本身还在）。
黄色底的是你当时圈住的字。</p><ol>{body}</ol></main></html>
""", encoding="utf-8")


def preview(args):
    """--preview NOTES_JSON：只读预览页 preview/<页>（工作区里 build_site 生成的最新版页面 + 嵌入 NOTES_JSON，快照模式：
    不读写存储、不同步）和隐藏清单 preview/hidden.html（取 NOTES_JSON 同目录的 apply_snapshot2_report.json）。
    生成后无头浏览器核：可见灰色 0、显示的笔数 = 报告里迁过去的笔数。同名文件覆盖，不删任何东西。"""
    import collections
    notes_path = pathlib.Path(args.preview)
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    rows = json.loads((notes_path.parent / "apply_snapshot2_report.json").read_text(encoding="utf-8"))["rows"]
    d = ROOT / "preview"
    d.mkdir(exist_ok=True)
    write_snapshot_page(d, args.page, (ROOT / args.page).read_text(encoding="utf-8"), notes,
                        lambda ref: (ROOT / ref).read_bytes() if (ROOT / ref).is_file() else None)
    write_hidden_page(d / "hidden.html", rows)
    states, gray = snapshot_states(d / args.page)
    c = collections.Counter(states.values())
    shown, want = sum(v != "orphan" for v in states.values()), sum(r["result"] == "moved" for r in rows)
    print(f"== 预览页 {d / args.page}：{len(states)} 笔，状态 {dict(c)}；显示（含在折叠里的）{shown} 笔，报告里迁过去 {want} 笔，"
          f"{'一致' if shown == want else '不一致'}；可见灰色 {gray}；隐藏清单 {d / 'hidden.html'}（{len(rows) - want} 条）")


# hidden.html 页内核对：条目数、每条的行数、MathJax 报错、手机宽度横向溢出、每条黄底里的内容（公式还原成 $TeX$）
HIDDEN_CHECK_JS = """() => {
  const items = [...document.querySelectorAll('main ol > li')];
  const texOf = el => { const m = [...MathJax.startup.document.math].find(x => x.typesetRoot === el); return m ? '$' + m.math + '$' : '?'; };
  const markText = li => [...li.querySelectorAll('mark')].map(mk => [...mk.childNodes].map(n =>
      n.nodeType === 3 ? n.data : (n.tagName === 'MJX-CONTAINER' ? texOf(n) : n.textContent)).join('')).join('');
  const de = document.documentElement;
  return { items: items.length, rows: items.map(li => li.querySelectorAll('.row > .k').length),
           merror: document.querySelectorAll('mjx-merror').length, scrollW: de.scrollWidth, clientW: de.clientWidth,
           marks: items.map(markText) };
}"""


def check_hidden(args):
    """--check-hidden：无头浏览器在 390px 宽打开 preview/hidden.html，核条目数 = 报告里隐藏的笔数、每条三行、MathJax 无报错
    （mjx-merror 与控制台错误都为 0）、无横向滚动，并抽前 5 条正文逐字比黄底内容与报告里「圈住的字」（pressed_old）。只读。"""
    rows = json.loads((pathlib.Path(args.out).parent / "apply_snapshot2_report.json").read_text(encoding="utf-8"))["rows"]
    hid = [r for r in rows if r["result"] == "hidden"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto((ROOT / "preview" / "hidden.html").as_uri(), wait_until="load", timeout=120000)
        page.wait_for_function("window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
        page.evaluate("MathJax.startup.promise")
        got = page.evaluate(HIDDEN_CHECK_JS)
        browser.close()
    sample = [(i, r) for i, r in enumerate(hid) if r.get("pressed_old")][:5]
    same = [(r["id"], got["marks"][i] == r["pressed_old"], got["marks"][i], r["pressed_old"]) for i, r in sample]
    print(f"== hidden.html：条目 {got['items']}（报告隐藏 {len(hid)}）；每条行数 {sorted(set(got['rows']))}；mjx-merror {got['merror']}；"
          f"控制台错误 {len(errors)} {errors[:3]}；390px 下 scrollWidth {got['scrollW']} / clientWidth {got['clientW']}"
          f"（{'无' if got['scrollW'] <= got['clientW'] else '有'}横向滚动）；抽 {len(same)} 条黄底与「圈住的字」逐字相同 "
          f"{sum(s for _, s, *_ in same)} 条")
    for i, ok, page_txt, rep_txt in same:
        print(f"  {i} {'相同' if ok else '不同'}：页面「{page_txt}」 报告「{rep_txt}」")


def write_apply_report(args, rows, gray, bad, drawn_page):
    """--apply-snapshot 的结果表：json 原样逐笔，md 给人看（统计、与上一次运行相比变了的笔、挪位公式、隐藏清单）。"""
    import collections
    d = pathlib.Path(args.out).parent
    path = d / "apply_snapshot2_report.json"
    prev = {r["id"]: r["result"] for r in json.loads(path.read_text(encoding="utf-8"))["rows"]} if path.exists() else {}
    path.write_text(json.dumps(dict(drawn_page=drawn_page, gray_visible=gray, rows=rows), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    c = collections.Counter((r["type"], r["result"]) for r in rows)
    hid = [r for r in rows if r["result"] == "hidden"]
    flips = [r for r in rows if r["id"] in prev and prev[r["id"]] != r["result"]]
    md = [f"# 第 2 次快照的笔迁到最新版：结果表", "",
          f"基准：线上 snapshot/2（页面 {SNAP_PAGE}，笔记 {SNAP_NOTES}，按 {TOMBS_AT} 的墓碑去掉之后删的笔），共 {len(rows)} 笔。",
          f"对应方法：difflib.SequenceMatcher(None, 旧, 新, autojunk=False)。正文的 diff 序列 = 每个字一个记号、每个行内公式按 TeX "
          f"原文一个记号（行内公式一改 equal 段就断开）。这笔实际压住的记号（画笔设备上锚字的宽 orig.w 与笔画相对锚字的横向范围算出；"
          f"字按 orig.w 宽，行内公式按 8288b02 页面上「该公式宽 / 锚字宽」× orig.w 估——估出的是「圈住的字」那一列，判定看这些记号是否同在一个 equal 段）"
          f"全部落在同一个 equal 段里、且该段 ≥ {TEXT_SEG_MIN} 个记号才接受；"
          f"笔画竖向跨度超过 {CROSS_LINE} 倍锚字高（跨行）的退回「锚字前后各 {TEXT_SEG_MARGIN} 个记号在同一段」。"
          f"公式按 TeX 序列对应（改过的 TeX 相似度 ≥ {MATH_PAIR_MIN} 保序配对；剩下的里 TeX 原样相同且两边各唯一的算挪了位置），"
          f"TeX 改过的公式内锚点符号所在 equal 段 ≥ {GLYPH_SEG_MIN} 个符号才接受。图像素变了按图中文字标注（follow_label）：先要求标注"
          f"原样相同，没有再按讲稿改名表 tex 里只差字母一一换名的公式对（letter_maps）认换了字母的同一个标注。"
          f"不用改名表的 quote / text 和任何逐笔对应表。",
          f"迁过去 {sum(r['result'] == 'moved' for r in rows)} 笔，隐藏 {len(hid)} 笔；最新版上核对不符 {len(bad)} 笔；可见灰色 {gray} 笔。", "",
          "| 类型 | 迁过去 | 隐藏 |", "|---|---|---|"]
    md += [f"| {t} | {c[(t, 'moved')]} | {c[(t, 'hidden')]} |" for t in sorted({r['type'] for r in rows})]
    md += ["", f"## 与上一次运行相比结果变了的笔（{len(flips)} 笔）", ""]
    md += [f"- `{r['id']}`（{r['type']}）{prev[r['id']]} → {r['result']}：{r.get('why') or r.get('new', '')}" for r in flips]
    md += ["", "## 按「挪了位置的同 TeX 公式」对上的笔", ""]
    md += [f"- `{r['id']}`（{r['result']}）{r['old']}" for r in rows if r.get("math_pair")]
    md += ["", "## 隐藏的笔（内容在最新版里找不到对应）", ""]
    for r in hid:
        md += [f"- `{r['id']}`（{r['type']}，所在 equal 段长 {r.get('seg', '—')}，判据 {r.get('rule', '—')}）{r['why']}",
               f"  - 8288b02 里：{r.get('old', '')}"]
        if "pressed_old" in r:
            md += [f"  - 圈住的字：旧「{r['pressed_old']}」→ 新位置「{r.get('pressed_new', '（锚字没有对应）')}」"]
        md += [f"  - 最新版同一位置附近：{r.get('new_here', '')}", f"  - 最新版最像的一句：{r.get('new_similar', '')}"]
    (d / "apply_snapshot2_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"== {len(rows)} 笔：{dict(c)}；核对不符 {len(bad)}；可见灰色 {gray}；与上一次相比变了 {len(flips)} 笔；"
          f"报告 {d / 'apply_snapshot2_report.md'}")
    for r in flips:
        print(f"  变了 {r['id']} {r['type']} {prev[r['id']]} → {r['result']}：{r.get('why') or r.get('new', '')}")
    for r in bad[:20]:
        print(f"  不符 {r['id']} {r['check']}")


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
    ap.add_argument("--font-check", action="store_true",
                    help="每笔起笔那个字在画笔设备上量的宽高（orig）与本机排版下的比，判断两边字体排版是否一致，写到 --out")
    ap.add_argument("--write-original", metavar="BASE_SHA",
                    help="以笔记仓库 BASE_SHA 那份的笔为准，每笔换回第一次存下时的样子（用户画的原始位置），写回笔记仓库")
    ap.add_argument("--check-pages", action="store_true",
                    help="每笔分到它画的那一版，生成 check/<sha7>/ 核对页（只显示那一版上画的笔），逐页核灰色 0 笔；分组写 --out")
    ap.add_argument("--snapshots", action="store_true",
                    help="每一次主动画笔之后的样子（那次最后保存的笔记原样 + 当时线上页面）做成只读快照页 snapshot/<次>/，统计灰色笔数")
    ap.add_argument("--apply-snapshot", action="store_true",
                    help="用户确认过的第 2 次快照里每笔挂的内容按两版 diff 迁到最新版：写 --out（带 drawn 的笔记）与同目录 "
                         "apply_snapshot2_report.json / .md，并在最新版上核对（只读笔记仓库，不写）")
    ap.add_argument("--check-drawn", metavar="NOTES_JSON",
                    help="只读核对：NOTES_JSON 里每笔 drawn 与笔记仓库第 2 次快照那份逐字节比（配 --apply-snapshot 的输出用）")
    ap.add_argument("--preview", metavar="NOTES_JSON",
                    help="只读预览页 preview/<页>（最新版 + 嵌入 NOTES_JSON，零写入不同步）和隐藏清单 preview/hidden.html，并核灰色 0")
    ap.add_argument("--check-hidden", action="store_true",
                    help="只读核对 preview/hidden.html（390px 宽）：条目数、每条三行、MathJax 无报错、无横向滚动、抽 5 条比黄底内容")
    args = ap.parse_args()
    if args.check_hidden:
        check_hidden(args)
        return
    if args.preview:
        preview(args)
        return
    if args.check_drawn:
        check_drawn(args)
        return
    if args.apply_snapshot:
        apply_snapshot(args)
        return
    if args.snapshots:
        snapshots(args)
        return
    if args.check_pages:
        check_pages(args)
        return
    if args.write_original:
        write_original(args)
        return
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
    if args.font_check:
        # 每笔用它第一次存下时的锚点（画笔设备上建的），在当初那版页面上按本机排版放，量锚点参考矩形，与存的 orig（设备上量的）比
        jobs = [dict(common, sha=sha, strokes=g) for sha, g in groups.items()]
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            outs = pool.map(font_check, jobs)
        orig = {s["id"]: s for g in groups.values() for s in g}
        rows = []
        for got, fonts in outs:
            for i, r in got.items():
                o = orig[i].get("orig")
                if r and o and orig[i].get("frame") == "px":
                    rows.append(dict(id=i, type=r["type"], device=[o["w"], o["h"]], here=[round(r["w"], 2), round(r["h"], 2)]))
        pathlib.Path(args.out).write_text(json.dumps(dict(fonts=[f for _, f in outs], rows=rows), ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        text = [r for r in rows if r["type"] == "text"]
        same = [r for r in text if abs(r["device"][0] - r["here"][0]) <= 0.5 and abs(r["device"][1] - r["here"][1]) <= 0.5]
        print(f"== 正文锚点 {len(text)} 笔：起笔那个字的宽高，画笔设备上与本机相同（≤ 0.5 px）{len(same)} 笔、不同 {len(text) - len(same)} 笔；"
              f"本机正文字体 {outs[0][1]}；明细 {args.out}")
        for r in [r for r in text if r not in same][:12]:
            print(f"  {r['id']}  设备 {r['device']}  本机 {r['here']}")
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
