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
    out[id] = {state: 'ok', text, glyph, media: med, near, zone};
  }
  return out;
}"""


def hits(job):
    """一个进程在一个页面上算一批笔压住的内容：job 里 sha 为 None 表示当前页面。"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        path = job["root"] / job["page"] if job["sha"] is None else checkout(job["sha"], job["worktrees"]) / job["page"]
        page = open_with_notes(browser, path, dict(job["current"], strokes=job["strokes"]))
        got = page.evaluate(HIT_JS, [s["id"] for s in job["strokes"]])
        browser.close()
    return got


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


def side_by_side(left, right, path):
    h = max(left.height, right.height)
    out = Image.new("RGB", (left.width + right.width + 12, h), "white")
    out.paste(left, (0, 0))
    out.paste(right, (left.width + 12, 0))
    out.save(path)


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
    ap.add_argument("--shots-for", nargs=2, metavar=("IDS_TXT", "NOTES_JSON"),
                    help="只出对照图（要配 --shots）：这些笔在原版页面与「当前页面 + NOTES_JSON」上的样子")
    ap.add_argument("--math-from-origin", metavar="BASE_JSON",
                    help="只做一件事：当初挂在公式上的笔，在各自原版页面上换成符号锚点，写进 BASE_JSON 的副本（--out）")
    args = ap.parse_args()
    stem = pathlib.Path(args.page).stem
    if args.shots:
        pathlib.Path(args.shots).mkdir(parents=True, exist_ok=True)
    history = note_history(stem)
    current = history[-1][1]
    ids = {s["id"] for s in current["strokes"]} & set(args.only or [s["id"] for s in current["strokes"]])
    origin = {}
    for t, notes in history:
        for s in notes["strokes"]:
            if s["id"] in ids and s["id"] not in origin:
                origin[s["id"]] = (t, s)
    versions = page_versions(args.page)
    groups = {}
    for i, (t, s) in origin.items():
        sha = next((v for v, vt in versions if vt <= t), versions[-1][0])
        groups.setdefault(sha, []).append(s)
    print(f"== {len(ids)} 笔，原始版本分布：" + "，".join(f"{v[:7]} {len(g)}" for v, g in groups.items()), flush=True)

    import multiprocessing as mp
    import time
    t0 = time.time()
    common = dict(page=args.page, worktrees=pathlib.Path(args.worktrees), root=ROOT, current=current)
    for sha in groups:
        checkout(sha, common["worktrees"])          # 先检出，免得几个进程同时 git worktree add
    if args.verify:
        # 精确核对：每笔在原版页面（原始笔画）与当前页面（VERIFY_JSON 的笔画）上压住的字 / 公式符号 / 图位置是否相同
        shown = json.loads(pathlib.Path(args.verify).read_text(encoding="utf-8"))
        jobs = [dict(common, sha=sha, strokes=g) for sha, g in groups.items()]
        jobs.append(dict(common, sha=None, current=shown, strokes=[s for s in shown["strokes"] if s["id"] in ids]))
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
        print(f"== 精确核对 {len(rows)} 笔：位置对上（影响范围 40 px 内的字 / 符号 ≥ 95% 一一对应、相对位置差 ≤ 1 px；"
              f"图上的笔图内位置差 ≤ 0.01）"
              f"{len(rows) - len(bad)} 笔，对不上 {len(bad)} 笔；"
              f"用 {time.time() - t0:.0f} s；逐笔明细 {report}")
        for r in bad:
            a, b = r["old"], r["new"]
            fmt = lambda h: (h["state"] if h["state"] != "ok" else
                             f"范围内「{''.join(z[0] for z in h['zone'])}」 压住「{h['text']}{h['glyph']}」 图{h['media']}")
            pct = "" if r["match"] is None else (f"（范围内对上 {100 * r['match']:.0f}%；原来有、现在对不上的「{a.get('lost', '')}」，"
                                                f"现在多出来的「{b.get('extra', '')}」）")
            print(f"  {r['id']}{pct}\n      原来：{fmt(a)}\n      现在：{fmt(b)}")
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
