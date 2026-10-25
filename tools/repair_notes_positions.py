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
    out = []
    for sha, t in zip(lines[::2], lines[1::2]):
        raw = gh("-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/notes/{stem}.json?ref={sha}")
        out.append((datetime.datetime.fromisoformat(t.replace("Z", "+00:00")), json.loads(raw)))
    return sorted(out, key=lambda x: x[0])


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


def grab(page, x, y, w, h, clean=True):
    """页面坐标里一块区域的灰度图（clean：不画笔记和页面浮层）。返回 (数组, 实际左上角)。"""
    x, y = max(0, int(x)), max(0, int(y))
    tag = page.add_style_tag(content=HIDE_CSS) if clean else None
    png = page.screenshot(full_page=True, clip=dict(x=x, y=y, width=int(w), height=int(h)))
    if tag:
        tag.evaluate("e => e.remove()")
    return np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=float), (x, y)


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

    with sync_playwright() as p:
        browser = p.chromium.launch()
        tmpl, before = {}, {}
        for sha, strokes in groups.items():
            d = checkout(sha, pathlib.Path(args.worktrees))
            old = open_with_notes(browser, d / args.page, dict(current, strokes=strokes))
            boxes = old.evaluate(BOX_JS, [s["id"] for s in strokes])
            for s in strokes:
                b = boxes.get(s["id"])
                if not b:
                    continue
                tmpl[s["id"]] = grab(old, b[0] - MARGIN, b[1] - MARGIN, b[2] + 2 * MARGIN, b[3] + 2 * MARGIN)[0]
                if args.shots:
                    png = old.screenshot(full_page=True, clip=dict(x=max(0, b[0] - 150), y=max(0, b[1] - 150),
                                                                   width=b[2] + 300, height=b[3] + 300))
                    before[s["id"]] = Image.open(io.BytesIO(png)).convert("RGB")
            old.close()
            print(f"  {sha[:7]}：取到 {sum(1 for s in strokes if s['id'] in tmpl)}/{len(strokes)} 笔的原位置", flush=True)

        new = open_with_notes(browser, ROOT / args.page, current)
        scale = new.evaluate("window.__nipcNotes.frame().s")
        boxes = new.evaluate(BOX_JS, sorted(ids))
        moved, kept, lost = [], [], []
        for i in sorted(ids):
            b, t = boxes.get(i), tmpl.get(i)
            if b is None or t is None:
                lost.append((i, "原位置或现位置看不到"))
                continue
            search, (sx, sy) = grab(new, b[0] - MARGIN - SEARCH_X, b[1] - MARGIN - SEARCH_Y,
                                    b[2] + 2 * (MARGIN + SEARCH_X), b[3] + 2 * (MARGIN + SEARCH_Y))
            m = best_match(search, t)
            if m is None:
                lost.append((i, "原位置下面是空白，认不出"))
                continue
            r, c, score = m
            if score < MIN_SCORE:
                lost.append((i, f"最像处相关系数只有 {score:.2f}"))
                continue
            dx, dy = sx + c + MARGIN - b[0], sy + r + MARGIN - b[1]
            if abs(dx) <= 1 and abs(dy) <= 1:
                kept.append(i)
                continue
            new.evaluate(f"window.scrollTo(0, {max(0, b[1] + dy - 300)})")
            new.wait_for_timeout(100)
            new.evaluate("([i, dx, dy]) => window.__nipcNotes.moveBy(i, dx, dy)", [i, dx / scale, dy / scale])
            moved.append((i, dx, dy, score))
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
        if args.shots:
            new.wait_for_timeout(500)
            after = new.evaluate(BOX_JS, [m[0] for m in moved])
            for i, *_ in moved:
                b = after.get(i)
                if b and i in before:
                    png = new.screenshot(full_page=True, clip=dict(x=max(0, b[0] - 150), y=max(0, b[1] - 150),
                                                                   width=b[2] + 300, height=b[3] + 300))
                    side_by_side(before[i], Image.open(io.BytesIO(png)).convert("RGB"), f"{args.shots}/{i}.png")
        browser.close()

    pathlib.Path(args.out).write_text(json.dumps(repaired, ensure_ascii=False), encoding="utf-8")
    print(f"== 挪动 {len(moved)} 笔，原位不动 {len(kept)} 笔，认不出 {len(lost)} 笔；公式笔改符号锚点 {len(legacy)} 笔；"
          f"写出 {args.out}")
    for i, dx, dy, sc in moved:
        print(f"  挪动 {i}  dx {dx:+.0f}  dy {dy:+.0f}  相关系数 {sc:.3f}")
    for i, why in lost:
        print(f"  认不出 {i}  {why}")


if __name__ == "__main__":
    main()
