"""列出页面里放不下的陈列公式：在给定窗口宽度下打开生成好的页面，等 MathJax 排完，逐个比较陈列公式的
实际宽度与正文列宽，打印超宽的公式（超出的像素和 TeX 源码），供讲稿里手动断行。

用法：
    /bin/python tools/check_math_width.py week3_4.html week2.html [--width 1440]
    /bin/python tools/check_math_width.py week3_4.html --shot "第一步：平底的解" <输出 png>
页面按仓库根目录的相对路径给。不给 --shot 时只读、不写文件；--shot 展开全部折叠，把含这段文字的最小折叠块
（不在折叠里就从所在段落截到后面第一个折叠的标题，该折叠保持收起）整块截成一张长图，写到给的路径，供自查实际版式。依赖 playwright（用户目录安装的 chromium）。
"""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 每个陈列公式：容器可用宽度 vs 公式 svg 的自然宽度；TeX 源码从 MathJax 的 math 列表取
JS = """() => {
  const out = [];
  for (const m of MathJax.startup.document.math) {
    if (!m.display) continue;
    const c = m.typesetRoot;
    if (!c) continue;
    const svg = c.querySelector('svg');
    const need = svg ? svg.getBoundingClientRect().width : c.scrollWidth;
    const have = c.parentElement.getBoundingClientRect().width;
    if (need > have + 0.5) out.push({over: Math.round(need - have), tex: m.math});
  }
  return out;
}"""


def check(page, rel):
    """打开一页、等公式排完，返回超宽公式列表。"""
    page.goto((ROOT / rel).as_uri(), wait_until="domcontentloaded", timeout=120000)   # 只等公式，不等图片视频（共享机负载高时 load 会超时）
    page.wait_for_function("window.MathJax && MathJax.startup && MathJax.startup.promise", timeout=120000)
    page.evaluate("MathJax.startup.promise")
    bad = page.evaluate(JS)
    n_err = page.evaluate("document.querySelectorAll('mjx-merror, [data-mjx-error]').length")   # MathJax 渲染报错的公式
    if n_err:
        bad.append({"over": 0, "tex": f"MathJax 报错的公式 {n_err} 个"})
    return bad


def section_shot(page, sid, out):
    """展开全部 <details>、等图片加载完，把 <section id=sid>（sid 写 lead 时取页首 .lead）整块截图，按 1800 px 分段。"""
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : "
                  "new Promise(r => { i.onload = i.onerror = r; }))))")
    el = page.locator(".lead" if sid == "lead" else f"section#{sid}").first
    box = el.evaluate("e => { const r = e.getBoundingClientRect(); return {x: r.x + scrollX, y: r.y + scrollY, "
                      "w: r.width, h: r.height}; }")
    stem, seg = out[:-4] if out.endswith(".png") else out, 1800
    for k, y0 in enumerate(range(0, int(box["h"]) + 1, seg), 1):
        path = f"{stem}_{k:02d}.png"
        page.screenshot(path=path, full_page=True,
                        clip=dict(x=box["x"], y=box["y"] + y0, width=box["w"], height=min(seg, box["h"] - y0)))
        print(f"wrote {path}")


def shot(page, text, out):
    """展开全部 <details>，等图片加载完，把含 text 的最小折叠块（没有就取所在段落）截成一张长图。"""
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.evaluate("Promise.all([...document.images].map(i => (i.loading = 'eager', i.complete ? 0 : "
                  "new Promise(r => { i.onload = i.onerror = r; }))))")
    target = page.locator("details", has_text=text).last
    tail = None
    if target.count() == 0:            # 折叠外的文字：从该段截到后面第一个折叠的标题（那个折叠收起，即读者不展开时看到的样子）
        target = page.locator("p", has_text=text).first
        tail = target.element_handle().evaluate_handle(
            "p => { let e = p; while (e.nextElementSibling && e.tagName !== 'DETAILS') e = e.nextElementSibling;"
            " if (e.tagName === 'DETAILS') e.open = false; return e; }")
    target.scroll_into_view_if_needed()
    box = page.evaluate("([e, t]) => { const r = e.getBoundingClientRect(), b = (t || e).getBoundingClientRect();"
                        " return {x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: b.bottom - r.top}; }",
                        [target.element_handle(), tail])
    stem, seg = out[:-4] if out.endswith(".png") else out, 1800      # 长块按 1800 px 一段切开，便于逐段看清
    for k, y0 in enumerate(range(0, int(box["h"]) + 1, seg), 1):
        path = f"{stem}_{k:02d}.png"
        page.screenshot(path=path, full_page=True,
                        clip=dict(x=box["x"], y=box["y"] + y0, width=box["w"], height=min(seg, box["h"] - y0)))
        print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--shot", nargs=2, metavar=("TEXT", "OUT_PNG"), help="把含 TEXT 的折叠块截图到 OUT_PNG（只用第一个页面）")
    ap.add_argument("--section", nargs=2, metavar=("ID", "OUT_PNG"),
                    help="把 <section id=ID>（lead = 页首导语）展开折叠后整块截图到 OUT_PNG（只用第一个页面）")
    args = ap.parse_args()
    total = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 900})
        if args.section:
            check(page, args.pages[0])
            section_shot(page, *args.section)
            browser.close()
            return
        if args.shot:
            check(page, args.pages[0])
            shot(page, *args.shot)
            browser.close()
            return
        for rel in args.pages:
            bad = check(page, rel)
            total += len(bad)
            print(f"== {rel}（窗口 {args.width}px）：超宽陈列公式 {len(bad)} 个")
            for b in bad:
                print(f"   超出 {b['over']} px：{b['tex'].strip()}")
        browser.close()
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    main()
