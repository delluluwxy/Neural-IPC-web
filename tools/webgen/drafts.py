"""整页讲稿 → 页面各节。讲稿是 Neural-IPC-sandbox 的 docs/drafts/web_<周>.md，结构：

    ## A. …                  （可选）只取 A 部分，到「## B.」为止；没有 A 标题时取全文
    ## INTRO                 页首导语；整行加粗的「**本页结论…**」变成醒目色总结句
    ## SECTION w1: 标题       每节一个 <section id="w1">，标题进 <h2>
    ### 小标题                节内小标题（<h3>，页面自动编号）
    TAKEAWAY: …              醒目色总结句；整行加粗的「**结论…**」同样处理
    [[占位符]]               换成调用方给的 HTML（视频卡片、数据表等由代码生成的块）

其余写法由 draft_md.notion_md_to_html 转换。
"""

import re

from .common import esc
from .config import DRAFTS
from .draft_md import notion_md_to_html


def _kp(m):
    """正则命中的一句话 → 醒目色总结句段落（前后补空行，独占一段）。"""
    return f'\n<p class="kp">{m.group(1).strip()}</p>\n'


def draft_path(name):
    """讲稿 web_<name>.md 的路径。"""
    return DRAFTS / f"web_{name}.md"


def page_from_draft(name, placeholders):
    """讲稿 web_<name>.md → [(锚点, 标题, HTML)]；页首导语的锚点是 "intro"、标题为 None。
    placeholders：{占位符名: HTML 字符串，或返回 HTML 的无参函数（只在讲稿真的用到时才调用）}，
    讲稿里写 [[名]]；用到了没给的占位符就停止。讲稿不存在时返回 None。"""
    path = draft_path(name)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## A\..*?\n(.*?)(?=^## B\.|\Z)", text)
    parts = re.split(r"(?m)^## SECTION (w\d+):\s*(.+)$", m.group(1) if m else text)
    used = []   # 本页出现过的占位符名，按出现顺序；HTML 里用 @@PH<序号>@@ 标位置

    def mark(k):
        """[[名]] → 独占一行的 @@PH<序号>@@（序号不含下划线，Markdown 不会把它当强调）。"""
        if k.group(1) not in placeholders:
            raise SystemExit(f"[build_site] web_{name}.md 用了没有定义的占位符 [[{k.group(1)}]]")
        used.append(k.group(1))
        return f"\n@@PH{len(used) - 1}@@\n"

    def fill(k):
        """@@PH<序号>@@（连同包住它的 <p></p>）→ 该占位符的 HTML。"""
        rep = placeholders[used[int(k.group(1))]]
        return rep() if callable(rep) else rep

    out = []
    intro = re.search(r"(?ms)^## INTRO\s*\n(.*)", parts[0])
    if intro:
        intro_md = re.sub(r"(?m)^\*\*(本页结论.+)\*\*[ \t]*$", _kp, intro.group(1))
        out.append(("intro", None, notion_md_to_html(intro_md)))
    for anchor, title, md in zip(parts[1::3], parts[2::3], parts[3::3]):
        md = re.sub(r"(?m)^### (.+)$", lambda h: f"\n<h3>{esc(h.group(1).strip())}</h3>\n", md)
        md = re.sub(r"(?m)^TAKEAWAY:\s*(.+)$", _kp, md)
        md = re.sub(r"(?m)^\*\*(结论.+)\*\*[ \t]*$", _kp, md)
        md = re.sub(r"\[\[([A-Za-z0-9_]+)\]\]", mark, md)
        html = re.sub(r"(?:<p>)?@@PH(\d+)@@(?:</p>)?", fill, notion_md_to_html(md))
        out.append((anchor, title.strip(), html))
    return out


def sections_html(whole):
    """page_from_draft 的结果 → 每节一段 HTML：导语包在 <div class="lead">，其余各为一个 <section>。"""
    return [f'<div class="lead">{body}</div>' if t is None else f'<section id="{a}"><h2>{esc(t)}</h2>\n{body}</section>'
            for a, t, body in whole]
