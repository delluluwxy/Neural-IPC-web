"""页面骨架与通用 HTML 片段：整页模板（含 CSS、JS 与 MathJax）、目录与标题编号、表格、视频卡片、设置小字。

每页开头是 doctype、charset、viewport，然后直接 <title> 和 <style>，省略 html / head / body 标签（HTML5 允许）；
版式仿照「研究记录」风格：顶栏（站名 + 各周链接）、阅读进度条、左侧目录（随滚动高亮，窄屏折叠）、
浅灰底上的白色圆角正文卡片、点图放大。CSS / JS 都内嵌在页面里，不加载外部字体；公式由 MathJax（SVG 输出）渲染。
"""

import re

from .annotate import ANNOT_CSS, ANNOT_JS, renames_script
from .common import MISSING, esc, pct
from .config import PAGE_TITLE, WEEKS

PAGE_CSS = """
:root {
  color-scheme: light;
  --ink: #202b3a; --muted: #637184; --line: #e2e7ed; --accent: #2563b8; --paper: #fff; --bg: #f7f8fa;
  --soft: #f0f3f7; --good: #1f6b3a; --warn: #8a5a0b; --key: #354a63;
  --font: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
          "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 100px; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); font-size: 16px;
       line-height: 1.9; overflow-x: clip; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
a:focus-visible, button:focus-visible, summary:focus-visible, [tabindex]:focus-visible { outline: 3px solid #4b91ec; outline-offset: 4px; }
#progress { position: fixed; left: 0; top: 0; height: 3px; background: var(--accent); width: 0; z-index: 30; }
.skip { position: fixed; top: -100px; left: 10px; background: #fff; padding: 10px; z-index: 40; }
.skip:focus { top: 10px; }

/* 顶栏 */
.topbar { height: 74px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between;
          gap: 16px; padding: 0 5%; background: #ffffffed; backdrop-filter: blur(12px); position: sticky; top: 0; z-index: 10; }
.brand { display: flex; gap: 12px; align-items: center; font-weight: 750; color: var(--ink); letter-spacing: .01em; white-space: nowrap; }
.brand:hover { text-decoration: none; }
.brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--ink); color: #fff; display: grid; place-items: center;
              font-size: 17px; line-height: 1; }
.brand-separator { font-weight: 400; color: #bac3ce; }
.brand-note { font-size: 13px; font-weight: 500; color: var(--muted); }
.topbar nav { display: flex; gap: 25px; font-size: 13px; white-space: nowrap; }
.topbar nav a { color: var(--muted); }
.topbar nav a[aria-current="page"] { color: var(--accent); font-weight: 600; }

/* 左目录 + 正文卡片 */
.layout { display: grid; grid-template-columns: 245px minmax(0, 920px); gap: 50px; max-width: 1320px; margin: 0 auto; padding: 45px 32px 0; }
.layout.single { grid-template-columns: minmax(0, 920px); justify-content: center; }
aside { position: sticky; top: 110px; align-self: start; max-height: calc(100vh - 140px); overflow: auto; padding-right: 16px;
        font-size: 13px; scrollbar-width: thin; }
#contents > summary { font-weight: 700; font-size: 12px; letter-spacing: .1em; color: var(--muted); margin-bottom: 18px; cursor: pointer; }
#contents nav { display: flex; flex-direction: column; }
#contents a { color: #5d6b7c; line-height: 1.65; padding: 6px 10px; border-left: 2px solid var(--line); }
#contents a.toc-level-3 { font-size: 12px; padding-left: 22px; }
#contents a.active { border-color: var(--accent); color: var(--accent); background: #eaf1fb; font-weight: 600; }
#contents a:hover { color: var(--accent); background: #eef2f7; text-decoration: none; }
main { position: relative; min-width: 0; background: var(--paper); padding: 44px 54px 24px; border: 1px solid var(--line); border-radius: 12px;
       box-shadow: 0 4px 30px #22354b03; }
.eyebrow { font-size: 10px; letter-spacing: .17em; color: var(--muted); font-weight: 650; }
.eyebrow span { color: #91a7c2; padding: 0 9px; }
article { overflow-wrap: break-word; }
footer.pagefoot { border-top: 1px solid var(--line); margin-top: 52px; padding-top: 22px; font-size: 11px; color: var(--muted);
                  display: flex; justify-content: space-between; gap: 16px; }

/* 标题与正文 */
h1 { font-size: 31px; line-height: 1.45; letter-spacing: -.035em; margin: 17px 0 0; }
header.top { margin-bottom: 42px; padding-bottom: 28px; border-bottom: 1px solid var(--line); }
article .summary { color: var(--muted); margin: 12px 0 0; }
h2 { font-size: 24px; line-height: 1.55; letter-spacing: -.02em; margin: 56px 0 20px; padding-top: 12px; }
h3 { font-size: 19px; line-height: 1.6; margin: 32px 0 17px; }
h4 { font-size: 17px; line-height: 1.6; margin: 26px 0 8px; font-weight: 650; }
h5 { font-size: 16px; line-height: 1.6; margin: 20px 0 6px; font-weight: 650; }
section h4, section h5 { color: var(--accent); }
article { counter-reset: sec; }
article section > h2 { counter-increment: sec; counter-reset: sub; }
article section > h2::before { content: counter(sec) ". "; }
article section > h3 { counter-increment: sub; }
article section > h3::before { content: counter(sec) "." counter(sub) " "; }
article section > h3.numbered::before { content: none; }
p { margin: 16px 0; }
li { margin: 6px 0; }
ul, ol { padding-left: 1.5em; }
strong, b { font-weight: 650; }
code { font-size: .87em; background: var(--soft); padding: 2px 5px; border-radius: 4px; overflow-wrap: anywhere; }
pre { overflow: auto; padding: 16px; background: var(--soft); border-radius: 8px; }
pre code { padding: 0; }
pre.log { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; line-height: 1.5;
          padding: 10px 12px; margin: 6px 0; max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
article .lead { padding: 8px 20px; margin: 24px 0; background: #f3f7fc; border: 1px solid #e1eaf6; border-radius: 8px; }
article .kp { font-weight: 600; color: var(--key); }
p.concl { color: var(--good); }
p.expect { color: var(--accent); }
p.setting { font-size: 13px; }
.note-src { color: #0d8a7a; }    /* 【ref】【无 ref】【我们定的】整行：出处（draft_md，用户 11-05） */
.note-cond { color: #c2570c; }   /* 【条件】整行：公式成立的前提 */
.muted { color: var(--muted); }
.small { font-size: 13px; }
blockquote { font-size: 13px; line-height: 1.85; margin: 12px 0 32px; padding: 4px 18px; border-left: 3px solid #b5cbe5;
             background: #f7f9fc; color: #5a6879; border-radius: 0 5px 5px 0; }
ul.obs { font-weight: 600; } ul.findings li { margin: 8px 0; }

/* 配图与视频：没有行内宽度的占满所在列；有行内宽度的（build_site.size_media 按子图面积定）以行内宽度为准 */
article img, article video { display: block; width: 100%; height: auto; max-width: 100%; margin: 26px auto 12px; }
article img { border: 1px solid #edf0f4; border-radius: 6px; cursor: zoom-in; }
article video { background: #101722; border-radius: 8px; }
article figure { margin: 26px 0 32px !important; min-width: 0; }
article figure img, article figure video { margin: 0 auto 12px; }
article figcaption { font-size: 13px; line-height: 1.85; margin: 12px 0 0; padding: 12px 18px; border-left: 3px solid #b5cbe5;
                     background: #f7f9fc; color: #5a6879; border-radius: 0 5px 5px 0; }
img.initframe { max-width: 640px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); gap: 22px 18px; }
.grid.pair { grid-template-columns: repeat(auto-fill, minmax(min(100%, 373px), 1fr)); align-items: start; }   /* 每例压入前 / 静止后：每张分得到 ≥ 373 px 就左右并排，否则上下排（用户 11-23「你的图太大了」；图内字 15–18 px 只容许 373–448 px，出图 agent width_range） */
.grid.pair figure { margin: 0 0 18px !important; } .pair-label { font-weight: 600; margin: 0 0 6px; }
.demo { min-width: 0; }
.demo h3 { margin: 0 0 6px; font-size: 15px; }
.demo h3 .key { color: var(--muted); font-weight: 400; font-size: 13px; }
.demo p { font-size: 13px; color: var(--muted); margin-top: 6px; }
.demo video { margin: 0; }
.novideo { padding: 22px 12px; background: var(--soft); color: var(--muted); border-radius: 6px; font-size: 13px; }

/* 公式：MathJax 客户端渲染，保持正文字号；超宽的在讲稿里手动断行，这里只留横向滚动兜底 */
mjx-container[display="true"] { max-width: 100%; overflow-x: auto; overflow-y: hidden; }

/* 表格：带边框的滚动框 + 斑马纹 */
.tablewrap { max-width: 100%; overflow: auto; margin: 24px 0; border: 1px solid var(--line); border-radius: 7px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; line-height: 1.75; }
th { background: #f1f5fa; text-align: left; font-weight: 650; color: #354a63; }
td, th { padding: 11px 14px; border-bottom: 1px solid var(--line); min-width: 90px; vertical-align: top; text-align: left; }
tr:last-child td { border-bottom: 0; }
tbody tr:nth-child(even) { background: #fafbfd; }
td { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
td.good { color: var(--good); }
td.warn { color: var(--warn); }

/* 折叠推导 */
article details { margin: 22px 0; border: 1px solid var(--line); padding: 16px 18px; border-radius: 8px; }
article details > summary { font-size: 14px; color: var(--accent); margin: 0; font-weight: 600; cursor: pointer; }
article details[open] > summary { margin-bottom: 16px; }
article details > :last-child { margin-bottom: 0; }

/* 首页各周卡片 */
nav.weeks { display: grid; gap: 14px; margin: 0 0 24px; }
a.weekcard { display: block; padding: 16px 20px; border-radius: 8px; background: #f7f9fc; border: 1px solid var(--line);
             border-left: 4px solid var(--accent); color: var(--ink); }
a.weekcard:hover { text-decoration: none; background: #eef3fa; }
a.weekcard b { font-size: 19px; color: var(--accent); }
a.weekcard span { display: block; font-size: 13px; color: var(--muted); margin: 2px 0 6px; line-height: 1.7; }
a.weekcard p { margin: 6px 0 0; }

/* 点图放大 */
dialog { border: 0; border-radius: 10px; max-width: 95vw; max-height: 94vh; padding: 40px 18px 8px; overflow: auto; background: #fff; }
dialog::backdrop { background: #111c30c9; backdrop-filter: blur(3px); }
dialog img { display: block; max-width: 88vw; max-height: 78vh; object-fit: contain; margin: auto; }
dialog p { font-size: 12px; text-align: center; color: var(--muted); margin: 10px; }
#close-viewer { position: absolute; right: 12px; top: 8px; background: #edf1f7; border: 0; padding: 6px 12px; border-radius: 5px;
                cursor: pointer; color: var(--ink); }

@media (max-width: 1000px) {
  .layout { grid-template-columns: 200px minmax(0, 1fr); gap: 24px; padding: 28px 20px 0; }
  .layout.single { grid-template-columns: minmax(0, 1fr); }
  main { padding: 32px 28px; } h1 { font-size: 27px; }
}
@media (max-width: 720px) {
  body { font-size: 15px; }
  .topbar { height: 62px; padding: 0 18px; }
  .brand { font-size: 15px; gap: 8px; } .brand-note, .brand-separator { display: none; }
  .topbar nav { gap: 15px; font-size: 12px; overflow-x: auto; }
  .layout { display: block; padding: 18px 12px 0; }
  aside { position: static; max-height: none; padding: 0; margin-bottom: 16px; }
  #contents { border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 12px 14px; }
  #contents > summary { margin: 0; } #contents[open] > summary { margin-bottom: 10px; }
  #contents nav { max-height: 45vh; overflow: auto; }
  main { padding: 28px 20px; border-radius: 8px; }
  h1 { font-size: 25px; } header.top { margin-bottom: 30px; } h2 { font-size: 21px; margin-top: 40px; } h3 { font-size: 18px; }
  td, th { padding: 9px 11px; } .eyebrow { font-size: 9px; letter-spacing: .1em; }
  article figcaption { padding: 10px 12px; } article .lead { padding: 6px 14px; } article details { padding: 12px; }
  footer.pagefoot { flex-wrap: wrap; }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print {
  .topbar, aside, #progress, .eyebrow, dialog { display: none; }
  .layout { display: block; margin: 0; padding: 0; } main { border: 0; padding: 0; box-shadow: none; }
  body { background: #fff; font-size: 11pt; } h2, h3 { break-after: avoid; } img, video, table { break-inside: avoid; }
  a { color: inherit; } .tablewrap { overflow: visible; }
}
"""

# 目录滚动高亮（窄屏折叠、点了就收起）、阅读进度条、点图放大
PAGE_JS = """
(() => {
  const contents = document.querySelector('#contents');
  const desktop = matchMedia('(min-width: 721px)');
  if (contents) {
    const syncMenu = () => { contents.open = desktop.matches; };
    syncMenu();
    desktop.addEventListener('change', syncMenu);
    contents.addEventListener('click', e => { if (e.target.closest('a') && !desktop.matches) contents.open = false; });
  }
  const links = [...document.querySelectorAll('#contents a[href^="#"]')];
  const targets = links.map(a => document.getElementById(decodeURIComponent(a.hash.slice(1))));
  const progress = document.querySelector('#progress');
  let scheduled = false;
  function update() {
    scheduled = false;
    const h = document.documentElement.scrollHeight - innerHeight;
    progress.style.width = (h > 0 ? 100 * scrollY / h : 0) + '%';
    let cur = 0;
    targets.forEach((t, i) => { if (t && t.getBoundingClientRect().top <= 150) cur = i; });
    links.forEach((a, i) => {
      a.classList.toggle('active', i === cur);
      if (i === cur) a.setAttribute('aria-current', 'location'); else a.removeAttribute('aria-current');
    });
  }
  addEventListener('scroll', () => { if (!scheduled) { scheduled = true; requestAnimationFrame(update); } }, { passive: true });
  addEventListener('resize', update);
  update();
  const viewer = document.querySelector('#image-viewer');
  let opener;
  function openImage(img) {
    opener = img;
    viewer.querySelector('img').src = img.currentSrc || img.src;
    viewer.querySelector('img').alt = img.alt;
    viewer.showModal();
  }
  document.querySelectorAll('article img').forEach(img => {
    img.tabIndex = 0;
    img.setAttribute('role', 'button');
    img.addEventListener('click', () => openImage(img));
    img.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openImage(img); } });
  });
  document.querySelector('#close-viewer').addEventListener('click', () => viewer.close());
  viewer.addEventListener('click', e => { if (e.target === viewer) viewer.close(); });
  viewer.addEventListener('close', () => opener && opener.focus({ preventScroll: true }));
})();
"""

_H3_RE = re.compile(r'(?<!<div class="demo">)<h3>(.*?)</h3>', re.S)   # 节内小标题（视频卡片里的 <h3> 不算）


def outline(body):
    """正文各节的标题编号与目录。每个 <section id="X"> 的 <h2> 由 CSS 计数器编号「n.」；节内小标题 <h3> 编号「n.m」，
    但只要该节有一个 h3 自带「1.1」这类编号，整节的 h3 都加 class="numbered"（不再由计数器加前缀，免得自带编号与
    计数器编号混在一起重号）。每个 h3 加锚点 id="X-m"。
    返回 (改好的 body, 目录 [(层级 2/3, 锚点, 显示文字)])，显示文字与页面上看到的编号一致。"""
    toc, sec = [], 0

    def one_section(m):
        nonlocal sec
        sec += 1
        sid, inner = m.group(1), m.group(2)
        h2 = re.search(r"<h2>(.*?)</h2>", inner, re.S)
        toc.append((2, sid, f"{sec}. {h2.group(1)}"))
        self_numbered = any(re.match(r"\d+\.\d+", t) for t in _H3_RE.findall(inner))
        k = 0

        def one_h3(hm):
            nonlocal k
            k += 1
            hid, text = f"{sid}-{k}", hm.group(1)
            toc.append((3, hid, text if self_numbered else f"{sec}.{k} {text}"))
            return f'<h3 id="{hid}"' + (' class="numbered"' if self_numbered else "") + f">{text}</h3>"
        return f'<section id="{sid}">' + _H3_RE.sub(one_h3, inner) + "</section>"
    body = re.sub(r'(?s)<section id="([^"]+)">(.*?)</section>', one_section, body)
    return body, toc


def topbar(current):
    """顶栏：站名 + 首页与各周页面的链接（config.WEEKS，新的一周在前）；current 是当前页的文件名。"""
    pages = [("index.html", "所有周")] + [(f"{wid}.html", title.split("（")[0]) for wid, title, _ in WEEKS]
    links = "".join(f'<a href="{href}"' + (' aria-current="page"' if href == current else "") + f">{esc(name)}</a>"
                    for href, name in pages)
    return (f'<header class="topbar"><a class="brand" href="index.html"><span class="brand-mark">N</span> Neural-IPC '
            f'<span class="brand-separator">/</span> <span class="brand-note">研究记录</span></a>'
            f'<nav aria-label="各周页面">{links}</nav></header>')


def page(body, title, current):
    """整页 HTML：doctype + charset / viewport + <title> / <style> + MathJax 配置；顶栏、左侧目录（取正文各节的 h2 / h3，
    没有节的页面不放目录、正文居中）、正文卡片 <main><article>…</article></main>、点图放大的对话框和页面脚本。
    current：本页文件名（顶栏高亮）。正文里的 d̂ 统一换成 MathJax 行内公式 \\(\\hat d\\)。
    除首页外，每页都带手写标注层（annotate.py：右下角笔记工具栏 + 笔迹图层）。"""
    body, toc = outline(body.replace("d̂", r"\(\hat d\)"))
    notes = current != "index.html"
    notes_css = f"<style>{ANNOT_CSS}</style>\n" if notes else ""
    notes_js = f"{renames_script(current)}<script>{ANNOT_JS}</script>\n" if notes else ""
    aside = ""
    if toc:
        items = "\n".join(f'<a class="toc-level-{lv}" href="#{a}">{text}</a>' for lv, a, text in toc)
        aside = (f'<aside><details id="contents" open><summary>本页目录</summary>'
                 f'<nav aria-label="章节目录">{items}</nav></details></aside>')
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{PAGE_CSS}</style>
{notes_css}<script>window.MathJax = {{tex: {{inlineMath: [["\\\\(", "\\\\)"]], displayMath: [["\\\\[", "\\\\]"]]}}, svg: {{fontCache: "global"}}}};</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg.js" async></script>
<a class="skip" href="#content">跳转到正文</a><div id="progress" aria-hidden="true"></div>
{topbar(current)}
<div class="layout{'' if toc else ' single'}">{aside}<main id="content"><div class="eyebrow">NEURAL-IPC <span>·</span> WEEKLY REPORT</div><article>
{body}
</article><footer class="pagefoot"><span>{esc(PAGE_TITLE)}</span><a href="#content">返回顶部 ↑</a></footer></main></div>
<dialog id="image-viewer" aria-label="放大查看图片"><button type="button" id="close-viewer" aria-label="关闭大图">关闭 ×</button><img alt=""><p>点击图片外区域或按 Esc 关闭</p></dialog>
<script>{PAGE_JS}</script>
{notes_js}"""


def table(heads, rows):
    """表格。heads：表头文字列表；rows：每行一个列表，格子是文本，或 (文本, css class) 给格子上色。全部转义。"""
    th = "".join(f"<th>{esc(h)}</th>" for h in heads)
    body = []
    for cells in rows:
        tds = []
        for c in cells:
            if isinstance(c, tuple):  # (文本, css class)
                tds.append(f'<td class="{c[1]}">{esc(c[0])}</td>')
            else:
                tds.append(f"<td>{esc(c)}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<div class="tablewrap"><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def demo_card(r, facts):
    """一张视频卡片：标题 + 视频（或说明为什么没有）+ 一句说明。r 是 collect.py 的记录（需已过 media.plan_videos）；
    facts 提供说明里的占位数字（目前只有 {momentum}）。"""
    line = r["line"].replace("{momentum}", pct(facts.get("momentum_err", MISSING)))
    head = f'<h3>{esc(r["title"])}</h3>'
    if r["state"] != "ok":
        media = f'<div class="novideo">{esc(r["reason"])}</div>'
    elif r.get("video_web"):
        media = f'<video controls muted playsinline preload="metadata" src="{esc(r["video_web"])}"></video>'
    else:
        media = f'<div class="novideo">{esc(r["video_note"] or "没有视频")}</div>'
    return f'<div class="demo">{head}{media}<p>{esc(line)}</p></div>'


def setting(text):
    """实验设置：小号灰字，留给想看细节的人，组会上不读。"""
    return f'<p class="setting muted">设置：{esc(text)}</p>'
