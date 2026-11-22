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
from .config import DRAFTS, WEB
from .draft_md import notion_md_to_html


def _kp(m):
    """正则命中的一句话 → 醒目色总结句段落（前后补空行，独占一段）。"""
    return f'\n<p class="kp">{m.group(1).strip()}</p>\n'


def draft_path(name):
    """讲稿 web_<name>.md 的路径。"""
    current = {"week5": "week5 (1).md", "week7": "week7.md"}
    if name in current:
        return WEB / current[name]
    return DRAFTS / f"web_{name}.md"


def _current_sections(text):
    """新版整页稿的标题与章转成既有 INTRO/SECTION；正文仍走同一渲染器。"""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("current weekly manuscript needs a top-level page title")
    out = ["## INTRO", "", f"**{lines[0][2:].strip()}**", ""]
    section, fence = 0, False
    for line in lines[1:]:
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("# "):
            section += 1
            line = f"## SECTION w{section}: {line[2:].strip()}"
        out.append(line)
    if section == 0:
        raise ValueError("current weekly manuscript has no sections")
    return "\n".join(out)


MODE = "publish"   # build_site.py --mode：publish（推送版，默认）/ review（审阅版，嵌入依据留档）


def evidence_path(name):
    """依据留档 web_<name>_evidence.md 的路径（tools/verify/web_sources_split_1121.py 生成）。"""
    return DRAFTS / f"web_{name}_evidence.md"


def _load_evidence(name):
    """留档 → {key: 原文}：单行「- [key] 原文」，多行块 <!-- ev:key --> … <!-- /ev:key -->。"""
    p = evidence_path(name)
    if not p.is_file():
        return None
    t = p.read_text(encoding="utf-8")
    ev = {m.group(1): m.group(2) for m in re.finditer(r"(?ms)^<!-- ev:(\S+) -->\n(.*?)\n<!-- /ev:\1 -->", t)}
    for m in re.finditer(r"(?m)^- \[([^\]]+)\] (.*)$", re.sub(r"(?ms)^<!-- ev:(\S+) -->.*?<!-- /ev:\1 -->", "", t)):
        ev.setdefault(m.group(1), m.group(2))
    return ev


def _apply_mode(name, text):
    """用户 11-21 03:3x：「web我觉得可以有审阅版和推送版」「审阅的可以放一些代码啥的但是推送的不能写这些」。
    讲稿里每处依据只留一个空标记 <span data-ev="key"></span>（推送版里不显示任何东西）；
    review 模式把留档里同 key 的原文（路径、文件:行号、commit、命令）嵌到标记处。两版出自同一份讲稿。"""
    ev = _load_evidence(name)
    if "data-ev=" in text and ev is None:
        raise SystemExit(f"[build_site] web_{name}.md 有依据标记，但没有依据留档 {evidence_path(name)}")
    if MODE != "review":
        return text

    def rep(m):
        raw = ev.get(m.group(1))
        if raw is None:
            raise SystemExit(f"[build_site] 依据留档里没有 key {m.group(1)}（web_{name}.md）")
        if "\n" in raw:   # 多行块（如逐根箭头的证据表）：原样放回
            return f"\n\n{raw}\n\n"
        return f' <span class="ev" style="color:#777;font-size:0.85em">【依据】{raw}</span>'
    return re.sub(r'<span data-ev="([^"]+)"></span>', rep, text)


def page_from_draft(name, placeholders):
    """讲稿 web_<name>.md → [(锚点, 标题, HTML)]；页首导语的锚点是 "intro"、标题为 None。
    placeholders：{占位符名: HTML 字符串，或返回 HTML 的无参函数（只在讲稿真的用到时才调用）}，
    讲稿里写 [[名]]；用到了没给的占位符就停止。讲稿不存在时返回 None。"""
    path = draft_path(name)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if path.parent == WEB:
        text = _current_sections(text)
    text = _apply_mode(name, text)
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
