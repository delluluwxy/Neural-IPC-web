"""手写标注层的浏览器测试（playwright + chromium）：iPad 上写、导出，Mac 上导入，检查笔迹是否落在同样的内容上。

检查项（每项打印 PASS / FAIL 和实测数字，全部通过退出码 0）：
  a) 1180×820（iPad 横屏）视口：手指滑动只滚屏不出笔画；用模拟 Apple Pencil（CDP pointerType=pen，带压感）
     在 week3_4.html 的一张图和一段文字上各画一笔；橡皮擦 + 撤销；导出 JSON。
  b) 1440×900（Mac）视口导入该 JSON（替换），两笔包围盒相对目标（图 / 起笔那个字）的坐标与 iPad 上相差 ≤ 2px；
     再「合并」导入同一文件不重复；820×1180（iPad 竖屏，正文被 zoom 缩小）同样 ≤ 2px；打印 media 下同样 ≤ 2px。
  c) week2.html：折叠框后面的一笔在展开 / 收起后仍对齐；折叠框里的一笔收起时隐藏、展开后对齐。
  d) 标注关闭时：点击落在视频上（能解码时还检查真的在播放）；点击折叠框标题能展开。
  e) 内容改动：在两个目标之前插入一个新段落和一张图，并改掉起笔段落开头几个字，导入后两笔仍落在原来的图 / 字上（≤ 2px）。
  f) 删掉目标图、整段改写目标段落：两笔隐藏（数据不丢），工具栏提示「有 2 笔圈的内容已改或删，已隐藏」。
  s) 滚动稳定性（回归）：1440 / 820 宽、有笔迹关标注 / 开标注时，滚轮、PageDown、手指拖动、跳到页中静置，
     逐帧记 scrollY：没有输入时不许动（> 2px），滚轮 / 手指滚动不许反向。
     --scroll-probe 只跑这一项的完整矩阵（1440 / 1024 / 820 × 对照 / 有笔迹关标注 / 开标注），逐帧记录写到 --out。
截图与导出的 JSON 写到 --out（默认 /nas/xiaoyingwang/Neural-IPC/outputs/web_annotate_test/，每次原路覆盖）。

用法：
    /bin/python tools/test_annotate.py [--out DIR] [--scroll-probe]
页面用仓库根目录下已生成的 week3_4.html、week2.html（先跑 tools/build_site.py --execute）。不改任何仓库文件。
"""
import argparse
import base64
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUT = pathlib.Path("/nas/xiaoyingwang/Neural-IPC/outputs/web_annotate_test")
TOL = 2.0   # 允许的偏差（未缩放 CSS px）

# 页面内测量：某一笔的包围盒相对目标（图按 src、字按一段唯一的文字 needle 的第一个字）的坐标，单位未缩放 CSS px
MEASURE_JS = """(spec) => {
  const N = window.__nipcNotes, f = N.frame();
  const article = document.querySelector('article');
  let T = null;
  if (spec.kind === 'img') {
    const img = [...article.querySelectorAll('img')].find(i => i.getAttribute('src').split('/').pop() === spec.src);
    T = img && img.getBoundingClientRect();
  } else if (spec.kind === 'math') {
    const m = [...MathJax.startup.document.math].find(m => m.display && m.typesetRoot && m.typesetRoot.isConnected &&
                                                         m.math.replace(/\\s+/g, ' ').trim() === spec.tex);
    // 量相对公式本身（svg 外框）的位置：公式笔锚在符号上，公式居中且宽度变了时整体平移，笔应跟着符号走
    T = m && m.typesetRoot.querySelector('svg').getBoundingClientRect();
  } else {
    const w = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const i = n.data.indexOf(spec.needle);
      if (i >= 0) { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1); T = r.getClientRects()[0]; break; }
    }
  }
  const sel = spec.print ? `svg.annot-print path[data-id="${spec.id}"]` : `#annot-layer path[data-id="${spec.id}"]`;
  const path = document.querySelector(sel);
  if (!T) return {error: 'target not found'};
  if (!path) return {error: 'path not found'};
  const pr = path.getBoundingClientRect(), shown = getComputedStyle(path).display !== 'none' && pr.width > 0;
  return {shown, cls: path.getAttribute('class'), s: f.s,
          x: (pr.left - T.left) / f.s, y: (pr.top - T.top) / f.s, w: pr.width / f.s, h: pr.height / f.s,
          targetTop: (T.top - main().top) / f.s};
  function main() { return document.querySelector('main').getBoundingClientRect(); }
}"""

# 选测试目标：第一张配图；一段没有公式、第一个文本节点够长的正文段落（取第 25 个字起的 16 个字作 needle）
PICK_JS = """() => {
  const article = document.querySelector('article');
  const img = article.querySelector('figure img');
  const ps = [...article.querySelectorAll('section > p')].filter(p =>
      !p.querySelector('mjx-container') && p.firstChild && p.firstChild.nodeType === 3 && p.firstChild.data.trim().length > 80);
  const p = ps[Math.min(4, ps.length - 1)];
  const t = p.firstChild.data, k = 25;
  return {src: img.getAttribute('src').split('/').pop(), needle: t.slice(k, k + 16)};
}"""

# 截获导出：a[download].click() 时读出 Blob 内容（不真的下载到磁盘）
CATCH_DOWNLOAD_JS = """() => {
  window.__dl = null;
  const orig = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (!this.download) return orig.call(this);
    const name = this.download;
    fetch(this.href).then(r => r.text()).then(text => { window.__dl = {name, text}; });
  };
}"""


class Report:
    """收集每项检查的结果并打印。"""

    def __init__(self):
        self.fails = 0

    def check(self, name, ok, detail):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}：{detail}", flush=True)
        self.fails += 0 if ok else 1


def open_page(ctx, rel):
    page = ctx.new_page()
    page.on("pageerror", lambda err: print(f"      [页面 JS 报错] {rel}: {err}", flush=True))
    page.goto((ROOT / rel).as_uri())
    page.wait_for_function("window.__nipcNotes")
    return page


def toolbar_pen(page, color="red"):
    """展开工具栏、选笔（会自动打开标注）。"""
    if page.is_visible("#annot-fab"):
        page.click("#annot-fab")
    page.click(f'[data-tool="pen"][data-color="{color}"]')


def set_annotation(page, on):
    if page.is_visible("#annot-fab"):
        page.click("#annot-fab")
    if (page.get_attribute("#annot-toggle", "aria-pressed") == "true") != on:
        page.click("#annot-toggle")


def pen_stroke(cdp, pts, forces):
    """用 CDP 发一笔 Apple Pencil 输入（pointerType=pen，force 即压感）。"""
    base = {"pointerType": "pen", "button": "left"}
    (x, y), f = pts[0], forces[0]
    cdp.send("Input.dispatchMouseEvent", {**base, "type": "mousePressed", "x": x, "y": y, "buttons": 1, "clickCount": 1, "force": f})
    for (x, y), f in zip(pts[1:], forces[1:]):
        cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseMoved", "x": x, "y": y, "buttons": 1, "force": f})
    cdp.send("Input.dispatchMouseEvent", {**base, "type": "mouseReleased", "x": x, "y": y, "buttons": 0, "force": 0})


def finger_drag(page, cdp, x, y, steps, step):
    """用 CDP 发一次手指向上拖（页面向下滚）：touchStart → steps 次 touchMove（每次上移 step 像素，间隔 16 ms）→ touchEnd。"""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for k in range(1, steps + 1):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - step * k}]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def scroll_to(page, js_target):
    """把目标元素滚到视口中间（立即滚动），并确认它真的在视口里。"""
    r = page.evaluate(f"""() => {{
      const el = ({js_target});
      const r0 = el.getBoundingClientRect();
      window.scrollTo({{top: scrollY + r0.top - innerHeight / 2 + Math.min(r0.height, innerHeight * 0.6) / 2, behavior: 'instant'}});
      const r = el.getBoundingClientRect();
      return {{top: r.top, bottom: r.bottom, vh: innerHeight, sy: scrollY}};
    }}""")
    page.wait_for_timeout(300)
    if r["top"] > r["vh"] or r["bottom"] < 0:
        raise SystemExit(f"scroll_to 没把目标滚进视口：{r}")


def img_js(src):
    return f"[...document.querySelectorAll('article img')].find(i => i.getAttribute('src').endsWith('/{src}'))"


def char_rect(page, needle):
    return page.evaluate("""(needle) => {
      const w = document.createTreeWalker(document.querySelector('article'), NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const i = n.data.indexOf(needle);
        if (i >= 0) { const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1);
                      const q = r.getClientRects()[0]; return {x: q.left, y: q.top, w: q.width, h: q.height}; }
      }
      return null;
    }""", needle)


def needle_parent_js(needle):
    return ("(() => { const w = document.createTreeWalker(document.querySelector('article'), NodeFilter.SHOW_TEXT);"
            f" for (let n = w.nextNode(); n; n = w.nextNode()) if (n.data.includes({json.dumps(needle)})) return n.parentElement; }})()")


def zigzag(x, y, n=12, dx=10, dy=14):
    return [(x + i * dx, y + (dy if i % 2 else 0)) for i in range(n)]


def strokes(page):
    return page.evaluate("window.__nipcNotes.strokes()")


def close_enough(a, b):
    return all(abs(a[k] - b[k]) <= TOL for k in ("x", "y", "w", "h"))


def fmt(m):
    return "{" + ", ".join(f"{k}={m[k]:.2f}" for k in ("x", "y", "w", "h")) + "}"


def compare(rep, name, ref, got):
    if "error" in got:
        rep.check(name, False, got["error"])
        return
    diff = max(abs(ref[k] - got[k]) for k in ("x", "y", "w", "h"))
    rep.check(name, got["shown"] and close_enough(ref, got),
              f"参考 {fmt(ref)} 实测 {fmt(got)} 最大差 {diff:.2f}px 显示={got['shown']}")


def import_notes(page, text, mode="replace"):
    if page.is_visible("#annot-fab"):
        page.click("#annot-fab")
    page.set_input_files("#annot-file", files=[{"name": "notes.json", "mimeType": "application/json",
                                                "buffer": text.encode()}])
    page.click(f'[data-import="{mode}"]')
    page.wait_for_timeout(400)


def shot_around(page, out, name, js_target):
    """把目标滚到视口中间后截图；上方懒加载的图片加载完会把内容往下推，所以等一下再滚一次。"""
    scroll_to(page, js_target)
    page.wait_for_timeout(800)
    scroll_to(page, js_target)
    page.screenshot(path=str(out / name))


def test_ipad_draw(browser, rep, out):
    """a) iPad 横屏：手指滚屏、两笔、橡皮擦 + 撤销、导出。返回 (导出文本, 目标, 两笔 id, 参考测量)。"""
    ctx = browser.new_context(viewport={"width": 1180, "height": 820}, has_touch=True)
    page = open_page(ctx, "week3_4.html")
    cdp = ctx.new_cdp_session(page)
    tgt = page.evaluate(PICK_JS)
    toolbar_pen(page)
    lay = page.evaluate("({w: document.querySelector('main').offsetWidth, cls: document.documentElement.className})")
    rep.check("a0 iPad 横屏版式", lay["w"] == 920 and "annot-noaside" in lay["cls"], f"main 宽 {lay['w']}px，html class={lay['cls']}")

    y0 = page.evaluate("scrollY")
    finger_drag(page, cdp, 600, 600, 8, 30)
    page.wait_for_timeout(600)
    y1 = page.evaluate("scrollY")
    ta = page.evaluate("getComputedStyle(document.getElementById('annot-capture')).touchAction")
    rep.check("a1 标注开时手指由浏览器原生滚动、不出笔画", y1 - y0 > 200 and len(strokes(page)) == 0,
              f"scrollY {y0} → {y1}，笔画数 {len(strokes(page))}，输入层 touch-action={ta!r}")

    scroll_to(page, img_js(tgt["src"]))
    r = page.evaluate(f"(() => {{ const q = ({img_js(tgt['src'])}).getBoundingClientRect(); return {{x: q.left, y: q.top, w: q.width, h: q.height}}; }})()")
    pts = zigzag(r["x"] + 0.3 * r["w"], r["y"] + 0.4 * r["h"])
    pen_stroke(cdp, pts, [0.2 + 0.06 * i for i in range(len(pts))])
    scroll_to(page, needle_parent_js(tgt["needle"]))
    c = char_rect(page, tgt["needle"])
    sx, sy = c["x"] + c["w"] / 2, c["y"] + c["h"] / 2
    pts = [(sx + 8 * i, sy + (6 if i else 0)) for i in range(16)]
    pen_stroke(cdp, pts, [0.9 - 0.04 * i for i in range(len(pts))])
    page.wait_for_timeout(300)
    st = strokes(page)
    kinds = [s["anchor"]["type"] for s in st]
    rep.check("a2 两笔已存、锚点类型", kinds == ["media", "text"],
              f"锚点 {kinds}；文字锚点 exact={st[1]['anchor'].get('exact')!r}" if len(st) == 2
              else f"笔画数 {len(st)}，锚点 {[s['anchor'] for s in st]}，点数 {[len(s['pts']) for s in st]}")
    if len(st) != 2:
        top = page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return e && (e.tagName + '#' + e.id + '.' + e.className); }", [sx, sy])
        print(f"      文字那一笔的起点 ({sx:.1f}, {sy:.1f})，字矩形 {c}，该点最上层元素 {top}")
        raise SystemExit("a2 没画出两笔，后面的检查无法进行")
    pres = sorted({p[2] for p in st[0]["pts"]})
    widths = page.evaluate(f"""() => [...document.querySelectorAll('#annot-layer path')].map(p => p.getBBox().height)""")
    rep.check("a3 压感进了笔画", len(pres) > 3 and min(pres) >= 0, f"第一笔压感取值 {pres[:3]}…{pres[-2:]}（共 {len(pres)} 种）")
    ids = [s["id"] for s in st]
    ref = [page.evaluate(MEASURE_JS, {"id": ids[0], "kind": "img", "src": tgt["src"]}),
           page.evaluate(MEASURE_JS, {"id": ids[1], "kind": "char", "needle": tgt["needle"]})]
    print(f"      iPad 参考：图 {fmt(ref[0])}，字 {fmt(ref[1])}（path 高度 {widths}）")
    stored = page.evaluate("(k) => localStorage.getItem(k) !== null", "nipc-notes:" + page.evaluate("location.pathname"))
    rep.check("a4 自动存进本机 localStorage", stored, f"键存在={stored}")

    page.click('[data-tool="eraser"]')
    pen_stroke(cdp, [(sx + 40, sy - 10 + 3 * k) for k in range(10)], [0.5] * 10)
    page.wait_for_timeout(200)
    n_erased = len(strokes(page))
    page.click("#annot-undo")
    page.wait_for_timeout(200)
    rep.check("a5 橡皮擦按笔擦、撤销恢复", n_erased == 1 and len(strokes(page)) == 2, f"擦后 {n_erased} 笔，撤销后 {len(strokes(page))} 笔")
    # 撤销恢复的笔已有删除墓碑，会换新 id（同步时墓碑优先，否则会被删掉）；几何不变，后面按新 id 测
    ids = [s["id"] for s in strokes(page)]

    shot_around(page, out, "a_ipad_text.png", needle_parent_js(tgt["needle"]))
    shot_around(page, out, "a_ipad_image.png", img_js(tgt["src"]))
    page.evaluate(CATCH_DOWNLOAD_JS)
    page.click("#annot-export")
    page.wait_for_function("window.__dl")
    dl = page.evaluate("window.__dl")
    data = json.loads(dl["text"])
    (out / dl["name"]).write_text(dl["text"], encoding="utf-8")
    rep.check("a6 导出", dl["name"].startswith("week3_4_notes_") and data["version"] == 1 and data["colWidth"] == 920
              and len(data["strokes"]) == 2, f"文件名 {dl['name']}，page={data['page']}，{len(data['strokes'])} 笔，{len(dl['text'])} 字节")
    ctx.close()
    return dl["text"], tgt, ids, ref


def test_mac_import(browser, rep, out, text, tgt, ids, ref):
    """b) Mac 宽屏导入、合并不重复、打印 media；iPad 竖屏（zoom）导入。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = open_page(ctx, "week3_4.html")
    import_notes(page, text)
    lay = page.evaluate("({w: document.querySelector('main').offsetWidth, aside: getComputedStyle(document.querySelector('aside')).display})")
    rep.check("b0 Mac 版式", lay["w"] == 920 and lay["aside"] != "none", f"main 宽 {lay['w']}px，目录 display={lay['aside']}")
    specs = [{"id": ids[0], "kind": "img", "src": tgt["src"]}, {"id": ids[1], "kind": "char", "needle": tgt["needle"]}]
    for k, (spec, name) in enumerate(zip(specs, ["图", "字"])):
        compare(rep, f"b1 Mac 1440 导入后{name}上的笔对齐", ref[k], page.evaluate(MEASURE_JS, spec))
    shot_around(page, out, "b_mac_text.png", needle_parent_js(tgt["needle"]))
    shot_around(page, out, "b_mac_image.png", img_js(tgt["src"]))
    import_notes(page, text, "merge")
    rep.check("b2 合并同一文件不重复", len(strokes(page)) == 2, f"合并后 {len(strokes(page))} 笔")
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    link = page.evaluate("(() => { const a = document.querySelectorAll('#contents a')[3]; return {href: a.getAttribute('href'), text: a.textContent}; })()")
    page.click(f'#contents a[href="{link["href"]}"]')
    page.wait_for_timeout(1500)
    top = page.evaluate("(id) => document.getElementById(id).getBoundingClientRect().top", link["href"][1:])
    rep.check("b6 有笔记时点目录链接照常跳转", 0 <= top <= 200, f"点「{link['text']}」后该节标题距视口顶 {top:.0f}px")

    page.emulate_media(media="print")
    page.wait_for_timeout(400)
    for k, (spec, name) in enumerate(zip(specs, ["图", "字"])):
        compare(rep, f"b3 打印 media 下{name}上的笔对齐", ref[k], page.evaluate(MEASURE_JS, {**spec, "print": True}))
    scroll_to(page, needle_parent_js(tgt["needle"]))
    page.screenshot(path=str(out / "b_print_text.png"))
    page.emulate_media(media="screen")
    page.wait_for_timeout(300)
    left = page.evaluate("document.querySelectorAll('svg.annot-print').length")
    rep.check("b4 退出打印后清理", left == 0, f"残留打印 svg {left} 个")
    ctx.close()

    ctx = browser.new_context(viewport={"width": 820, "height": 1180}, has_touch=True)
    page = open_page(ctx, "week3_4.html")
    import_notes(page, text)
    z = page.evaluate("document.querySelector('main').style.zoom")
    for k, (spec, name) in enumerate(zip(specs, ["图", "字"])):
        compare(rep, f"b5 iPad 竖屏 820（zoom={z}）{name}上的笔对齐", ref[k], page.evaluate(MEASURE_JS, spec))
    shot_around(page, out, "b_ipad_portrait_text.png", needle_parent_js(tgt["needle"]))
    ctx.close()


def test_week2_details(browser, rep, out):
    """c) 折叠框展开 / 收起后对齐；d) 关标注时折叠框可点。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = open_page(ctx, "week2.html")
    cdp = ctx.new_cdp_session(page)
    pick = page.evaluate("""() => {
      const ds = [...document.querySelectorAll('article details')].filter(d => !d.open);
      for (const d of ds) {
        let n = d.nextElementSibling;
        while (n && !(n.tagName === 'P' && n.firstChild && n.firstChild.nodeType === 3 && n.firstChild.data.trim().length > 40)) n = n.nextElementSibling;
        const inner = [...d.querySelectorAll('p')].find(p => p.firstChild && p.firstChild.nodeType === 3 && p.firstChild.data.trim().length > 40);
        if (n && inner) {
          d.dataset.testPick = '1';
          const t = n.firstChild.data.trim(), u = inner.firstChild.data.trim();
          return {after: t.slice(10, 26), inside: u.slice(10, 26)};
        }
      }
      return null;
    }""")
    if not pick:
        rep.check("c0 week2 找到折叠框", False, "没有合适的折叠框")
        return
    det = "document.querySelector('details[data-test-pick]')"
    toolbar_pen(page, "blue")
    scroll_to(page, needle_parent_js(pick["after"]))
    c = char_rect(page, pick["after"])
    pen_stroke(cdp, [(c["x"] + c["w"] / 2 + 7 * i, c["y"] + c["h"] / 2) for i in range(12)], [0.5] * 12)
    page.wait_for_timeout(200)
    sid = strokes(page)[-1]["id"]
    spec = {"id": sid, "kind": "char", "needle": pick["after"]}
    ref = page.evaluate(MEASURE_JS, spec)
    rep.check("c0 刚画的笔立刻显示", ref.get("shown") is True,
              f"anchor={strokes(page)[-1]['anchor']}，explain={page.evaluate('id => window.__nipcNotes.explain(id)', sid)}")

    set_annotation(page, False)
    page.evaluate(f"{det}.scrollIntoView({{block: 'center', behavior: 'instant'}})")
    page.click("details[data-test-pick] > summary")
    page.wait_for_timeout(400)
    opened = page.evaluate(f"{det}.open")
    rep.check("d1 关标注时点折叠框标题能展开", opened, f"open={opened}")
    got = page.evaluate(MEASURE_JS, spec)
    compare(rep, "c1 折叠框展开后，框后面的笔仍对齐", ref, got)
    rep.check("c2 框后面的笔跟着内容下移", got.get("targetTop", 0) - ref["targetTop"] > 20,
              f"目标字在卡片内 y {ref['targetTop']:.1f} → {got.get('targetTop', 0):.1f}")

    toolbar_pen(page, "black")
    scroll_to(page, needle_parent_js(pick["inside"]))
    c = char_rect(page, pick["inside"])
    pen_stroke(cdp, [(c["x"] + c["w"] / 2 + 7 * i, c["y"] + c["h"] / 2 + 3) for i in range(12)], [0.6] * 12)
    page.wait_for_timeout(200)
    iid = strokes(page)[-1]["id"]
    ispec = {"id": iid, "kind": "char", "needle": pick["inside"]}
    iref = page.evaluate(MEASURE_JS, ispec)
    page.screenshot(path=str(out / "c_week2_details_open.png"))
    set_annotation(page, False)
    page.click("details[data-test-pick] > summary")
    page.wait_for_timeout(400)
    hidden = page.evaluate(f"""() => getComputedStyle(document.querySelector('#annot-layer path[data-id="{iid}"]')).display""")
    rep.check("c3 收起后框里的笔隐藏", hidden == "none", f"display={hidden}")
    compare(rep, "c4 收起后框后面的笔仍对齐", ref, page.evaluate(MEASURE_JS, spec))
    page.click("details[data-test-pick] > summary")
    page.wait_for_timeout(400)
    compare(rep, "c5 再展开后框里的笔对齐", iref, page.evaluate(MEASURE_JS, ispec))
    ctx.close()


def test_video_click(browser, rep):
    """d) 关标注时视频照常可点。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = open_page(ctx, "week3_4.html")
    toolbar_pen(page)
    set_annotation(page, False)
    page.evaluate("""() => { const v = document.querySelector('article video'); window.__vclick = false;
                    v.addEventListener('click', () => { window.__vclick = true; }); v.scrollIntoView({block: 'center', behavior: 'instant'}); }""")
    page.wait_for_timeout(300)
    box = page.evaluate("(() => { const r = document.querySelector('article video').getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()")
    top = page.evaluate("([x, y]) => document.elementFromPoint(x, y).tagName", [box["x"], box["y"]])
    can = page.evaluate("document.querySelector('article video').canPlayType('video/mp4; codecs=\"avc1.42E01E\"')")
    page.mouse.click(box["x"], box["y"])
    page.wait_for_timeout(800)
    clicked = page.evaluate("window.__vclick")
    paused = page.evaluate("document.querySelector('article video').paused")
    rep.check("d2 关标注时点击落在视频上", top == "VIDEO" and clicked, f"该点最上层元素 {top}，video 收到 click={clicked}")
    if can:
        rep.check("d3 视频开始播放", not paused, f"canPlayType={can!r}，paused={paused}")
    else:
        print(f"[SKIP] d3 视频开始播放：本测试浏览器不能解码 H.264（canPlayType 为空），只核点击是否到达视频；paused={paused}")
    ctx.close()


def test_drift(browser, rep, out, text, tgt, ids, ref):
    """e) 目标之前插段落和图、改起笔段落开头几个字后导入；f) 删掉目标后导入，笔标灰不丢。"""
    specs = [{"id": ids[0], "kind": "img", "src": tgt["src"]}, {"id": ids[1], "kind": "char", "needle": tgt["needle"]}]
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = open_page(ctx, "week3_4.html")
    tops0 = page.evaluate(   # 改动前两个目标在卡片内的 y，用来证明改动确实把它们推下去了
"""([src, needle]) => {
      const m = document.querySelector('main').getBoundingClientRect();
      const img = [...document.querySelectorAll('article img')].find(i => i.getAttribute('src').endsWith('/' + src));
      const w = document.createTreeWalker(document.querySelector('article'), NodeFilter.SHOW_TEXT);
      let t = null; for (let n = w.nextNode(); n; n = w.nextNode()) if (n.data.includes(needle)) { t = n.parentElement; break; }
      return [img.getBoundingClientRect().top - m.top, t.getBoundingClientRect().top - m.top];
    }""", [tgt["src"], tgt["needle"]])
    edit = page.evaluate("""([src, needle]) => {
      const article = document.querySelector('article');
      const img = [...article.querySelectorAll('img')].find(i => i.getAttribute('src').endsWith('/' + src));
      const w = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
      let tn = null; for (let n = w.nextNode(); n; n = w.nextNode()) if (n.data.includes(needle)) { tn = n; break; }
      const para = tn.parentElement.closest('p');
      const other = [...article.querySelectorAll('figure img')].find(i => !i.getAttribute('src').endsWith('/' + src));
      const first = (img.compareDocumentPosition(para) & Node.DOCUMENT_POSITION_FOLLOWING) ? img.closest('figure') : para;
      const p = document.createElement('p');
      p.textContent = '（测试插入的新段落）这一段是测试时临时插进来的，用来把后面的内容往下推，检查笔迹是否跟着原来的内容走。';
      const fig = document.createElement('figure');
      const im = document.createElement('img'); im.src = other.getAttribute('src'); im.style.width = '400px'; im.dataset.testInserted = '1';
      fig.appendChild(im);
      first.before(p, fig);
      const lead = para.firstChild;
      const old = lead.data.slice(0, 4);
      lead.data = '改过的字' + lead.data.slice(4);
      return {inserted_before: first.tagName, old_head: old, new_img: other.getAttribute('src')};
    }""", [tgt["src"], tgt["needle"]])
    page.wait_for_function("document.querySelector('img[data-test-inserted]').complete")
    page.wait_for_timeout(300)
    import_notes(page, text)
    for k, (spec, name) in enumerate(zip(specs, ["图", "字"])):
        got = page.evaluate(MEASURE_JS, spec)
        compare(rep, f"e{k + 1} 插入段落+图、改段首 {edit['old_head']!r}→'改过的字' 后{name}上的笔对齐", ref[k], got)
        if "targetTop" in got:
            rep.check(f"e{k + 3} 目标{name}确实被推下去了", got["targetTop"] - tops0[k] > 50,
                      f"目标在卡片内 y {tops0[k]:.1f} → {got['targetTop']:.1f}")
    st = page.evaluate("window.__nipcNotes.status()")
    rep.check("e5 没有误判为内容已变", st["orphans"] == 0, f"orphans={st['orphans']}")
    shot_around(page, out, "e_drift_text.png", needle_parent_js(tgt["needle"]))
    shot_around(page, out, "e_drift_image.png", img_js(tgt["src"]))
    ctx.close()

    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = open_page(ctx, "week3_4.html")
    page.evaluate("""([src, needle]) => {
      const article = document.querySelector('article');
      [...article.querySelectorAll('img')].filter(i => i.getAttribute('src').endsWith('/' + src)).forEach(i => i.closest('figure').remove());
      const w = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) if (n.data.includes(needle)) {
        n.parentElement.closest('p').textContent = '这一段整段改写成了完全不同的内容，原来的字一个都不剩，用来测试找不到对应内容时笔迹不丢、标灰显示。';
        break;
      }
    }""", [tgt["src"], tgt["needle"]])
    page.wait_for_timeout(300)
    import_notes(page, text)
    st = page.evaluate("window.__nipcNotes.status()")
    cls = page.evaluate("[...document.querySelectorAll('#annot-layer path')].map(p => [p.getAttribute('class'), getComputedStyle(p).display])")
    status_text = page.inner_text("#annot-status")
    rep.check("f1 目标删掉后两笔隐藏、数据不丢", st["orphans"] == 2 and all("orphan" in c and d == "none" for c, d in cls),
              f"orphans={st['orphans']}，path={cls}，提示「{status_text.strip()}」")
    page.evaluate(f"document.querySelector('#annot-layer path[data-id=\"{ids[1]}\"]').scrollIntoView({{block: 'center', behavior: 'instant'}})")
    page.wait_for_timeout(300)
    page.screenshot(path=str(out / "f_orphan.png"))
    ctx.close()


# ---------- GitHub 同步：内存里的假 Contents API ----------

SYNC_INIT_JS = ("localStorage.setItem('nipc-notes-sync', JSON.stringify("
                "{token: 'test-token', repo: 'delluluwxy/neural-ipc-notes'}))")
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "GET, PUT, OPTIONS"}


class FakeGitHub:
    """假的 GitHub REST Contents API（按路径存 base64 内容和 sha）。PUT 带的 sha 与当前不符返回 409；
    inject 可设一个函数，在下一次 GET 应答后立刻改动文件，模拟「读完被别的设备抢先写了」。"""

    def __init__(self):
        self.files, self.calls, self.inject, self.n = {}, [], None, 0

    def _reply(self, route, status, obj):
        route.fulfill(status=status, headers={**CORS, "Content-Type": "application/json"}, body=json.dumps(obj))

    def handle(self, route, request):
        if request.method == "OPTIONS":
            route.fulfill(status=204, headers=CORS)
            return
        path = request.url.split("/contents/", 1)[1]
        if request.method == "GET":
            cur = self.files.get(path)
            self.calls.append(("GET", path, 200 if cur else 404))
            self._reply(route, 200, {"content": cur[0], "sha": cur[1], "encoding": "base64"}) if cur else \
                self._reply(route, 404, {"message": "Not Found"})
            if self.inject:
                f, self.inject = self.inject, None
                f(self, path)
            return
        body = json.loads(request.post_data)
        cur = self.files.get(path)
        if (cur and body.get("sha") != cur[1]) or (not cur and body.get("sha")):
            self.calls.append(("PUT", path, 409))
            self._reply(route, 409, {"message": "sha does not match"})
            return
        self.calls.append(("PUT", path, 201))
        self.write(path, body["content"])
        self._reply(route, 201, {"content": {"sha": self.files[path][1]}})

    def write(self, path, content_b64):
        self.n += 1
        self.files[path] = (content_b64, f"sha{self.n}")

    def data(self, path):
        return json.loads(base64.b64decode(self.files[path][0]).decode("utf-8"))


def sync_context(browser, fake, token=True):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.route("https://api.github.com/**", fake.handle)
    if token:
        ctx.add_init_script(SYNC_INIT_JS)
    return ctx


def wait_synced(page):
    page.wait_for_function("window.__nipcNotes && window.__nipcNotes.status().sync === '已同步'", timeout=15000)
    page.wait_for_timeout(300)   # 合并结果在下一帧才画出来


def sync_now(page):
    page.evaluate("window.__nipcNotes.syncNow()")
    wait_synced(page)


def ids_of(page):
    return sorted(s["id"] for s in strokes(page))


def draw_on_image(page, cdp, src, fx=0.3, fy=0.4):
    scroll_to(page, img_js(src))
    r = page.evaluate(f"(() => {{ const q = ({img_js(src)}).getBoundingClientRect(); return {{x: q.left, y: q.top, w: q.width, h: q.height}}; }})()")
    pen_stroke(cdp, zigzag(r["x"] + fx * r["w"], r["y"] + fy * r["h"]), [0.5] * 12)
    page.wait_for_timeout(200)


def draw_on_text(page, cdp, needle):
    scroll_to(page, needle_parent_js(needle))
    c = char_rect(page, needle)
    pen_stroke(cdp, [(c["x"] + c["w"] / 2 + 8 * i, c["y"] + c["h"] / 2 + 4) for i in range(12)], [0.6] * 12)
    page.wait_for_timeout(200)


def test_sync(browser, rep):
    """g) GitHub 同步：A 写 B 看到并对齐；两边各加一笔后都有；A 删除传到 B；sha 冲突后重读合并成功；没 token 不联网。"""
    fake = FakeGitHub()
    path = "notes/week3_4.json"
    ctx_a, ctx_b = sync_context(browser, fake), sync_context(browser, fake)
    a = open_page(ctx_a, "week3_4.html")
    cdp_a = ctx_a.new_cdp_session(a)
    wait_synced(a)
    tgt = a.evaluate(PICK_JS)
    toolbar_pen(a)
    draw_on_image(a, cdp_a, tgt["src"])
    sid = strokes(a)[0]["id"]
    a.wait_for_timeout(3600)   # 约 3 s 防抖后自动推送
    wait_synced(a)
    pushed = path in fake.files and [s["id"] for s in fake.data(path)["strokes"]] == [sid]
    ref = a.evaluate(MEASURE_JS, {"id": sid, "kind": "img", "src": tgt["src"]})
    b = open_page(ctx_b, "week3_4.html")
    cdp_b = ctx_b.new_cdp_session(b)
    wait_synced(b)
    got = b.evaluate(MEASURE_JS, {"id": sid, "kind": "img", "src": tgt["src"]}) if ids_of(b) == [sid] else {"error": "B 没有这一笔"}
    rep.check("g1 A 写一笔后约 3 s 自动推送", pushed, f"仓库文件 {path} 笔画 {fake.data(path)['strokes'][0]['id'] if pushed else '无'}")
    compare(rep, "g1 B 打开即拉到这一笔且对齐", ref, got)

    draw_on_text(a, cdp_a, tgt["needle"])
    toolbar_pen(b, "blue")
    draw_on_image(b, cdp_b, tgt["src"], 0.6, 0.6)
    sync_now(a)
    sync_now(b)
    sync_now(a)
    ia, ib = ids_of(a), ids_of(b)
    rep.check("g2 两边各加一笔，同步后两边都有全部 3 笔", len(ia) == 3 and ia == ib,
              f"A {ia}，B {ib}；文字锚点 exact={next(s['anchor'].get('exact') for s in strokes(b) if s['anchor']['type'] == 'text')!r}")

    a.click('[data-tool="eraser"]')
    scroll_to(a, img_js(tgt["src"]))
    r = a.evaluate(f"(() => {{ const q = ({img_js(tgt['src'])}).getBoundingClientRect(); return {{x: q.left, y: q.top, w: q.width, h: q.height}}; }})()")
    pen_stroke(cdp_a, [(r["x"] + 0.3 * r["w"] + 30, r["y"] + 0.4 * r["h"] - 20 + 4 * k) for k in range(12)], [0.5] * 12)
    a.wait_for_timeout(200)
    gone = [i for i in ia if i not in ids_of(a)]
    sync_now(a)
    sync_now(b)
    tomb = fake.data(path).get("tombstones", {})
    rep.check("g3 A 删除一笔，同步后 B 也没了（墓碑）", gone == [sid] and sid not in ids_of(b) and sid in tomb,
              f"A 删除 {gone}，B 现有 {ids_of(b)}，仓库墓碑 {sorted(tomb)}")

    def foreign_write(fk, p):
        d = fk.data(p)
        extra = dict(d["strokes"][0], id="foreign1", updatedAt=1)
        d["strokes"].append(extra)
        fk.write(p, base64.b64encode(json.dumps(d).encode("utf-8")).decode())
    a.click('[data-tool="pen"][data-color="black"]')
    draw_on_text(a, cdp_a, tgt["needle"])
    fake.inject = foreign_write   # 下一次 GET（就是下面这次同步的）应答后，仓库被「别的设备」抢先改了
    n409 = sum(1 for c in fake.calls if c[2] == 409)
    sync_now(a)
    conflicts = sum(1 for c in fake.calls if c[2] == 409) - n409
    final = [s["id"] for s in fake.data(path)["strokes"]]
    rep.check("g4 sha 过期（409）后重读、合并、写入成功", conflicts >= 1 and "foreign1" in ids_of(a) and set(ids_of(a)) == set(final),
              f"409 次数 {conflicts}，A {len(ids_of(a))} 笔含 foreign1={'foreign1' in ids_of(a)}，仓库 {len(final)} 笔")
    ctx_a.close()
    ctx_b.close()

    lone = FakeGitHub()
    ctx = sync_context(browser, lone, token=False)
    p = open_page(ctx, "week3_4.html")
    cdp = ctx.new_cdp_session(p)
    toolbar_pen(p)
    draw_on_image(p, cdp, tgt["src"])
    p.wait_for_timeout(3600)
    st = p.evaluate("window.__nipcNotes.status()")
    rep.check("g5 没填 token 不联网", not lone.calls and len(strokes(p)) == 1 and not st["sync"],
              f"请求 {len(lone.calls)} 次，本机 {len(strokes(p))} 笔，同步状态 {st['sync']!r}")
    ctx.close()


# ---------- 记号改名：内容只换写法时笔记跟着走 ----------

# 选三个目标：一个陈列公式（TeX ≥ 10 字）、两段第一个文本节点够长的正文（各取第 40 个字起 16 字作 needle）
PICK_RENAME_JS = """() => {
  const norm = s => s.replace(/\\s+/g, ' ').trim();
  const m = [...MathJax.startup.document.math].find(m => m.display && m.typesetRoot && norm(m.math).length >= 10);
  const ps = [...document.querySelectorAll('article section > p')].filter(p =>
      !p.querySelector('mjx-container') && p.firstChild && p.firstChild.nodeType === 3 && p.firstChild.data.length > 90);
  const n = ps.length, pick = p => p.firstChild.data.slice(40, 56);
  const pm = ps[1], pc = ps[Math.floor(n / 2)], pr = ps[n - 2];   // 三段互不相同、分散在全页（需要 n ≥ 4）
  return {tex: norm(m.math), moved: pick(pm), changed: pick(pc), renamed: pick(pr), old: pr.firstChild.data.slice(37, 40), n};
}"""

# 在 B 页上做「改讲稿」：公式按两步改名表换写法并重排；一段搬到最后一节末尾；一段整段改写（不在表里）；
# 一段把起笔字前的 3 个字全段换成新写法（在表里）。改名表写进 #annot-renames。
EDIT_FOR_RENAME_JS = """async ([tex, mid, t2, moved, changed, renamed, oldTxt, newTxt]) => {
  const article = document.querySelector('article');
  const nodeWith = s => { const w = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (n.data.includes(s)) return n; return null; };
  document.getElementById('annot-renames').textContent =
    JSON.stringify({tex: {[tex]: mid, [mid]: t2}, text: {[oldTxt]: newTxt}});
  const item = [...MathJax.startup.document.math].find(m => m.display && m.typesetRoot &&
                                                             m.math.replace(/\\s+/g, ' ').trim() === tex);
  const span = document.createElement('span');
  span.textContent = '\\\\[' + t2 + '\\\\]';
  item.typesetRoot.replaceWith(span);
  await MathJax.typesetPromise([span]);
  const p = nodeWith(moved).parentElement.closest('p');
  const sections = article.querySelectorAll('section');
  sections[sections.length - 1].appendChild(p);
  nodeWith(changed).parentElement.closest('p').textContent = '这一段内容本身改写了，原来的字都不在了，也不在改名表里，所以笔记应该标灰。';
  const rn = nodeWith(renamed);
  rn.data = rn.data.split(oldTxt).join(newTxt);
}"""


def test_renames(browser, rep, out):
    """h) 记号改名：公式换写法（改名表两步链）→ 笔仍在公式上；段落搬家 → 跟着走；整段改写（不在表里）→ 标灰；
    文字换写法（在表里）→ 跟着新写法走，存的锚点也改成新写法。"""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    a = open_page(ctx, "week3_4.html")
    cdp = ctx.new_cdp_session(a)
    tg = a.evaluate(PICK_RENAME_JS)
    toolbar_pen(a)
    mjs = f"[...MathJax.startup.document.math].find(m => m.display && m.typesetRoot && m.math.replace(/\\s+/g, ' ').trim() === {json.dumps(tg['tex'])}).typesetRoot"
    scroll_to(a, mjs)
    svg = a.evaluate(f"(() => {{ const q = ({mjs}).querySelector('svg').getBoundingClientRect(); return {{x: q.left, y: q.top, w: q.width, h: q.height}}; }})()")
    pen_stroke(cdp, zigzag(svg["x"] + 0.4 * svg["w"], svg["y"] + 0.3 * svg["h"], 8, 6, 8), [0.5] * 8)
    for key in ("moved", "changed", "renamed"):
        draw_on_text(a, cdp, tg[key])
    st = strokes(a)
    ids = {k: s["id"] for k, s in zip(("math", "moved", "changed", "renamed"), st)}
    ref = {"math": a.evaluate(MEASURE_JS, {"id": ids["math"], "kind": "math", "tex": tg["tex"]}),
           "moved": a.evaluate(MEASURE_JS, {"id": ids["moved"], "kind": "char", "needle": tg["moved"]}),
           "renamed": a.evaluate(MEASURE_JS, {"id": ids["renamed"], "kind": "char", "needle": tg["renamed"]})}
    notes = a.evaluate("localStorage.getItem('nipc-notes:' + location.pathname)")
    ctx.close()

    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    b = open_page(ctx, "week3_4.html")
    t2, mid, new_txt = tg["tex"] + " \\quad", tg["tex"] + " \\;", "改名后"
    b.evaluate(EDIT_FOR_RENAME_JS, [tg["tex"], mid, t2, tg["moved"], tg["changed"], tg["renamed"], tg["old"], new_txt])
    b.wait_for_timeout(500)
    import_notes(b, notes)
    b.wait_for_timeout(300)
    after = {s["id"]: s for s in strokes(b)}
    got_math = b.evaluate(MEASURE_JS, {"id": ids["math"], "kind": "math", "tex": t2})
    compare(rep, "h1 公式按改名表（两步链）换写法后，笔仍在公式上", ref["math"], got_math)
    rep.check("h1 存的公式锚点已改成新写法", after[ids["math"]]["anchor"].get("tex") == t2,
              f"anchor.tex={after[ids['math']]['anchor'].get('tex')!r}")
    got_moved = b.evaluate(MEASURE_JS, {"id": ids["moved"], "kind": "char", "needle": tg["moved"]})
    compare(rep, "h2 段落搬到最后一节后，笔跟着走", ref["moved"], got_moved)
    rep.check("h2 目标段落确实搬远了", got_moved.get("targetTop", 0) - ref["moved"]["targetTop"] > 1000,
              f"目标字在卡片内 y {ref['moved']['targetTop']:.0f} → {got_moved.get('targetTop', 0):.0f}")
    cls = b.evaluate(f"document.querySelector('#annot-layer path[data-id=\"{ids['changed']}\"]').getAttribute('class')")
    rep.check("h3 内容本身改写（不在改名表）→ 隐藏", "orphan" in (cls or ""), f"class={cls!r}")
    new_needle = tg["renamed"].replace(tg["old"], new_txt)
    got_ren = b.evaluate(MEASURE_JS, {"id": ids["renamed"], "kind": "char", "needle": new_needle})
    compare(rep, f"h4 文字换写法（{tg['old']!r}→{new_txt!r}，在表里）后笔跟着走", ref["renamed"], got_ren)
    pre = after[ids["renamed"]]["anchor"].get("prefix", "")
    rep.check("h4 存的文字锚点已改成新写法", pre.endswith(new_txt), f"prefix 末尾 {pre[-8:]!r}")
    b.evaluate(f"document.querySelector('#annot-layer path[data-id=\"{ids['math']}\"]').scrollIntoView({{block: 'center', behavior: 'instant'}})")
    b.wait_for_timeout(300)
    b.screenshot(path=str(out / "h_rename_math.png"))
    ctx.close()


# ---------- 滚动稳定性探针：逐帧记 scrollY，找「没有输入却在动」和「与输入方向相反」的帧 ----------

# 页面内装记录器：每帧 [时间 ms, scrollY]；输入事件、html 类名 / style、main.style、resize、图片加载、笔迹图层变化都记时间
PROBE_INSTALL_JS = """() => {
  const t0 = performance.now(), now = () => Math.round(performance.now() - t0);
  const log = window.__probe = {frames: [], events: []};
  const ev = (k, extra) => log.events.push(Object.assign({t: now(), k}, extra || {}));
  window.__probeMark = (name) => { ev('action', {name}); return now(); };
  window.__probeNow = now;
  addEventListener('wheel', e => ev('wheel', {d: e.deltaY}), {capture: true, passive: true});
  addEventListener('keydown', e => ev('key', {key: e.key}), true);
  addEventListener('pointermove', e => { if (e.pointerType === 'touch') ev('touchmove', {y: Math.round(e.clientY)}); }, true);
  addEventListener('touchmove', () => ev('touchmove-native', {}), {capture: true, passive: true});
  addEventListener('resize', () => ev('resize', {w: innerWidth, h: innerHeight}));
  document.addEventListener('load', e => {
    if (e.target.tagName === 'IMG') ev('imgload', {src: e.target.getAttribute('src').split('/').pop(),
                                                    top: Math.round(e.target.getBoundingClientRect().top)});
  }, true);
  const root = document.documentElement, main = document.querySelector('main');
  new MutationObserver(() => ev('html-attr', {cls: root.className, style: root.getAttribute('style') || ''}))
    .observe(root, {attributes: true, attributeFilter: ['class', 'style']});
  new MutationObserver(() => ev('main-style', {zoom: main.style.zoom}))
    .observe(main, {attributes: true, attributeFilter: ['style']});
  const layer = document.getElementById('annot-layer');
  if (layer) new MutationObserver(ms => ev('layer', {n: ms.length})).observe(layer, {subtree: true, childList: true, attributes: true});
  // 参照元素：视口正中的正文元素，记它每帧在屏幕上的位置（scrollY 会被滚动锚定修正，屏上位置才是人看到的）；
  // 它离开视口或测试自己跳转（__probeMark）时重选，编号 refId 加一
  let ref = null, refId = 0;
  const pickRef = () => {
    const m = main.getBoundingClientRect(), x = m.left + m.width / 2, y = innerHeight / 2;
    for (const el of document.elementsFromPoint(x, y)) {
      if (el.closest('#annot-capture, .annot-ui, #annot-layer')) continue;
      if (main.contains(el) && el !== main) return el;
    }
    return null;
  };
  const mark = window.__probeMark;
  window.__probeMark = (name) => { ref = null; return mark(name); };
  (function tick() {
    if (ref) { const r = ref.getBoundingClientRect(); if (!ref.isConnected || r.bottom < 0 || r.top > innerHeight) ref = null; }
    if (!ref) { ref = pickRef(); if (ref) refId++; }
    const top = ref ? Math.round(ref.getBoundingClientRect().top * 10) / 10 : null;
    log.frames.push([now(), Math.round(scrollY * 10) / 10, ref ? refId : 0, top]);
    requestAnimationFrame(tick);
  })();
}"""

INPUT_KINDS = {"wheel", "key", "touchmove", "touchmove-native"}
QUIET_MS = 600       # 最近一次输入之后超过这么久还在动，算「没有输入却在动」（平滑滚动动画一般 < 500 ms）
KEY_QUIET_MS = 1000  # PageDown 的平滑滚动动画更长


def probe_actions(page, cdp):
    """依次做：静置、滚轮向下 20 次、PageDown 5 次、手指向下拖 3 次、跳到页中静置、滚轮向上 10 次；每段后静置 2 s。
    返回每段的 (名字, 开始 ms, 结束 ms, 期望方向 +1 向下 / -1 向上 / 0 静置)，时间与记录器同一时钟。"""
    segs = []

    def seg(name, direction, fn):
        t0 = page.evaluate(f"window.__probeMark({json.dumps(name)})")
        fn()
        segs.append((name, t0, page.evaluate("window.__probeNow()"), direction))
        page.wait_for_timeout(2000)

    def wheel(n, dy):
        page.mouse.move(640, 400)
        for _ in range(n):
            page.mouse.wheel(0, dy)
            page.wait_for_timeout(60)

    def keys():
        page.evaluate("document.activeElement && document.activeElement.blur()")
        for _ in range(5):
            page.keyboard.press("PageDown")
            page.wait_for_timeout(400)

    def touch():
        for _ in range(3):
            finger_drag(page, cdp, 500, 600, 20, 20)   # 各状态下手指都由浏览器原生滚动
            page.wait_for_timeout(300)

    def jump_middle():
        page.evaluate("window.scrollTo({top: document.documentElement.scrollHeight / 2, behavior: 'instant'})")
        page.wait_for_timeout(2000)

    seg("静置", 0, lambda: page.wait_for_timeout(2000))
    seg("滚轮向下", 1, lambda: wheel(20, 120))
    seg("PageDown", 1, keys)
    seg("手指向下拖", 1, touch)
    seg("跳到页中后静置", 0, jump_middle)
    seg("滚轮向上", -1, lambda: wheel(10, -120))
    return segs


def probe_analyze(log, segs):
    """逐帧找异常，按人眼看到的移动算：参照元素（同一个 refId）在屏上上移 dy = -Δtop 像素即「内容向下滚了 dy」。
    |dy| > 2 但距最近一次输入超过 QUIET_MS（按键后 KEY_QUIET_MS）→「无输入却在动」；在有方向的段里 dy 与期望方向相反 →
    「与输入方向相反」。滚动锚定修正 scrollY 时参照元素在屏上不动，不算异常。"""
    frames, events = log["frames"], log["events"]
    inputs = [e for e in events if e["k"] in INPUT_KINDS]
    bad = []
    for i in range(1, len(frames)):
        t, _, rid, top = frames[i]
        _, _, rid0, top0 = frames[i - 1]
        if not rid or rid != rid0 or top is None or top0 is None:
            continue
        dy = -(top - top0)
        if abs(dy) <= 2:
            continue
        last_in = max((e for e in inputs if e["t"] <= t), key=lambda e: e["t"], default=None)
        quiet = KEY_QUIET_MS if last_in and last_in["k"] == "key" else QUIET_MS
        seg = max((s for s in segs if s[1] <= t), key=lambda s: s[1], default=None)
        name, direction = (seg[0], seg[3]) if seg else ("开始前", 0)
        if last_in is None or t - last_in["t"] > quiet:
            bad.append({"i": i, "t": t, "dy": round(dy, 1), "kind": "无输入却在动", "seg": name})
        elif direction and dy * direction < 0:
            bad.append({"i": i, "t": t, "dy": round(dy, 1), "kind": "与输入方向相反", "seg": name})
    return bad


def probe_excerpt(log, item, k=4):
    """异常帧前后 k 帧的原始 [t, scrollY]，以及这段时间（含前 300 ms）内的事件。"""
    frames, i = log["frames"], item["i"]
    lo, hi = max(0, i - k), min(len(frames), i + k + 1)
    t_lo, t_hi = frames[lo][0], frames[hi - 1][0]
    return frames[lo:hi], [e for e in log["events"] if t_lo - 300 <= e["t"] <= t_hi][:12]


def probe_case(browser, rep, out, width, height, mode):
    """一个视口 × 一种状态跑完整动作并检查。mode：control（无笔迹、标注关）、strokes_off（有 1 笔、标注关，
    即 Mac 投屏讲课的状态）、notes_on（有 1 笔、标注开）。原始逐帧记录写到 out/scroll_probe_<mode>_<宽>.json。"""
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=True)
    page = open_page(ctx, "week3_4.html")
    cdp = ctx.new_cdp_session(page)
    if mode != "control":
        tgt = page.evaluate(PICK_JS)
        toolbar_pen(page)
        scroll_to(page, img_js(tgt["src"]))
        r = page.evaluate(f"(() => {{ const q = ({img_js(tgt['src'])}).getBoundingClientRect(); return {{x: q.left, y: q.top, w: q.width, h: q.height}}; }})()")
        pen_stroke(cdp, zigzag(r["x"] + 0.3 * r["w"], r["y"] + 0.4 * r["h"]), [0.5] * 12)
        page.wait_for_timeout(300)
        if mode == "strokes_off":
            set_annotation(page, False)
    page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(500)
    page.evaluate(PROBE_INSTALL_JS)
    segs = probe_actions(page, cdp)
    log = page.evaluate("window.__probe")
    ctx.close()
    (out / f"scroll_probe_{mode}_{width}.json").write_text(json.dumps({"segments": segs, **log}), encoding="utf-8")
    bad = probe_analyze(log, segs)
    ends = []
    for n, t0, t1, _ in segs:
        ys = [y for t, y, *_ in log["frames"] if t0 <= t <= t1 + 2000]
        ends.append(f"{n} {ys[0] if ys else '?'}→{ys[-1] if ys else '?'}")
    jumps = [b for b in bad if b["kind"] == "无输入却在动"]
    back = [b for b in bad if b["kind"] == "与输入方向相反"]
    rep.check(f"s {width}px {mode}：没有输入时 scrollY 不动", not jumps,
              f"{len(log['frames'])} 帧，异常 {len(jumps)} 帧" + (f"，最大 |Δy| {max(abs(b['dy']) for b in jumps)}" if jumps else ""))
    rep.check(f"s {width}px {mode}：滚轮 / 手指滚动方向单调", not back, f"反向 {len(back)} 帧；各段 scrollY：" + "；".join(ends))
    for b in (jumps + back)[:5]:
        fr, evs = probe_excerpt(log, b)
        print(f"      异常 {b['kind']} 段「{b['seg']}」t={b['t']}ms Δy={b['dy']}：帧 {fr}")
        print(f"        同时段事件 {evs}")


def run_scroll_probe(browser, rep, out, widths, modes):
    """滚动探针矩阵：每个 (宽, 高) × 每种 mode 跑一次 probe_case。"""
    for w, h in widths:
        for mode in modes:
            probe_case(browser, rep, out, w, h, mode)


PROBE_WIDTHS = [(1440, 900), (1024, 768), (820, 1180)]
PROBE_MODES = ["control", "strokes_off", "notes_on"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    ap.add_argument("--scroll-probe", action="store_true",
                    help="只跑滚动稳定性探针：1440/1024/820 宽 × 对照/有笔迹关标注/开标注，逐帧记录写到 --out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rep = Report()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        print("chromium", browser.version)
        if args.scroll_probe:
            run_scroll_probe(browser, rep, args.out, PROBE_WIDTHS, PROBE_MODES)
            browser.close()
            print(f"== 失败 {rep.fails} 项；逐帧记录在 {args.out}/scroll_probe_*.json")
            sys.exit(1 if rep.fails else 0)
        text, tgt, ids, ref = test_ipad_draw(browser, rep, args.out)
        print(f"      目标：图 {tgt['src']}，字 needle={tgt['needle']!r}")
        test_mac_import(browser, rep, args.out, text, tgt, ids, ref)
        test_week2_details(browser, rep, args.out)
        test_video_click(browser, rep)
        test_drift(browser, rep, args.out, text, tgt, ids, ref)
        test_sync(browser, rep)
        test_renames(browser, rep, args.out)
        run_scroll_probe(browser, rep, args.out, [(1440, 900), (820, 1180)], ["strokes_off", "notes_on"])
        browser.close()
    print(f"== 失败 {rep.fails} 项；截图与 JSON 在 {args.out}")
    sys.exit(1 if rep.fails else 0)


if __name__ == "__main__":
    main()
