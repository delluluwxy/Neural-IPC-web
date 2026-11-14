"""组装整站：首页（各周卡片）+ 每周一页（页首、目录、各节）。"""

import re
from .common import esc
from .config import PAGE_TITLE, WEB, WEEKS
from .drafts import page_from_draft, sections_html
from .html_parts import page
from .week2_hydro import week2_sections

# 页首直接用讲稿标题的周（用户 11-12 21:1x 的 week5_web 稿：页首就是她的标题和导语，不另放摘要）
DRAFT_HEADED = {"week5"}


def week_summaries(demos, tests, gsweeps):
    """各周的一段摘要（首页卡片和该周页首共用）；week1 的数字由收集结果现数。"""
    n_video = sum(1 for r in demos + tests if r.get("video_web"))
    n_pass = sum(1 for r in tests if r.get("outcome") == "passed")
    n_ran = sum(1 for r in tests if r.get("outcome") in ("passed", "failed"))
    n_gsweeps = sum(1 for s in gsweeps if s["name"] != "baseline")
    n_grows = sum(len(s["rows"]) for s in gsweeps)
    return {
        "week5": "我们在 18 类刻意刁难的 edge case 上测了 skeleton：14 类已有判为基本对的算例，靠连接处、棱、两侧都薄、中等整体应变这 4 类全部待定，判为明显不对的 0 个；在我们的 IPC 标签上，它比 hydroelastic 更接近。目前发现的最大一处偏差（龙鳍，只取 24 个 mode 时偏硬 36%）出在 mode 取得不够，加够 mode 后回到 1.1 附近。",   # 与讲稿 web_week5.md 第 5 行导语同句（审核 review_web_formula_1113.md 四审）
        "week3_4": ("要学的碰撞能量；两个软物体相碰的解析解（降维法、Hertz）；标签对解析解与独立裁判；hydro、hydro++ 与死结，"
                    "Genesis 中 hydro 实测；网络结构与多接触区耦合；接进引擎、体力法造标签、试训与椭圆骨架；待定事项。"),
        "week2": "文献（hydroelastic、神经网络接触模型）、NeuralIPC 代码、Genesis + hydroelastic demo、实验方案、问题与待决定。",
        "week1": ("在服务器上跑通了 Genesis + lib IPC（Genesis 的 IPC 接触底层由 libuipc 计算）："
                  f"{len(demos)} 个 Genesis IPC 例子和 {len(tests)} 个 Genesis 官方 IPC 测试场景，共 {n_video} 段视频"
                  f"（官方测试 {n_ran} 个跑完，其中官方断言通过 {n_pass} 个）。"
                  f"然后做了「一堆物体扔进盒子」（官方 ipc_objects_falling 场景加一个盒子），对 {n_gsweeps} 个 IPC 参数"
                  f"做了单变量扫描，共 {n_grows} 个配置。主要发现：初始穿插会被拒绝开跑；表面全程没有穿透；"
                  "d̂ 越小越难解，且不能大于软体表面网格的边长；Genesis 默认 κ 1e9 会被 libuipc 夹到区间上界；"
                  "官方软球 E = 1 kPa 太软，会被压塌，所以主结果用 E = 1e5。")}


def build_pages(demos, tests, hydro, hydro_more, gsweeps, hy):
    """生成全部页面，返回 {仓库内路径: HTML}：index.html 只列各周的链接和摘要，每周一页 <weekN>.html
    （config.WEEKS 决定有哪几周及其标题）。参数是 build_site.main 收集到的各类记录。
    WEEKS 里目录为空的周（week3、week1）整页从讲稿 web_<周>.md 生成，目录（页面顶部与首页卡片共用）取讲稿各节标题；
    讲稿不存在就停止。"""
    summary = week_summaries(demos, tests, gsweeps)
    toc = {wid: items for wid, _, items in WEEKS}   # 每周的目录：页面顶部与首页卡片共用
    sections = {"week2": week2_sections(hydro, hydro_more, hy)}
    for wid in (w for w, _, items in WEEKS if not items):
        whole = page_from_draft(wid, {})
        if whole is None:
            raise SystemExit(f"[build_site] 找不到 {wid} 讲稿 web_{wid}.md，停止（不退回旧内容）")
        sections[wid] = sections_html(whole)
        toc[wid] = [(a, t) for a, t, _ in whole if t is not None]
    cards = "".join(f'<a class="weekcard" href="{wid}.html"><b>{esc(title)}</b>'
                    f'<span>{esc(" · ".join(n for _, n in toc[wid]))}</span><p>{esc(summary[wid])}</p></a>'
                    for wid, title, _ in WEEKS)
    pages = {WEB / "index.html": page(f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1></header>\n'
                                      f'<nav class="weeks">{cards}</nav>', PAGE_TITLE, "index.html")}
    for wid, title, _ in WEEKS:   # 章节目录和各周链接由 html_parts.page 放在左栏和顶栏
        if wid in DRAFT_HEADED:   # 页首 = 讲稿导语第一行的加粗标题 + 导语其余部分（不另放摘要）
            lead = sections[wid][0]
            m = re.match(r'<div class="lead"><p><strong>(.+?)</strong></p>\n?', lead)
            if m is None:
                raise SystemExit(f"[build_site] {wid} 讲稿导语第一行不是整行加粗的标题")
            body = (f'<header class="top"><h1>{m.group(1)}</h1></header>\n<div class="lead">{lead[m.end():]}\n'
                    + "\n".join(sections[wid][1:]))
        else:
            body = (f'<header class="top"><h1>{esc(title)}</h1><p class="summary">{esc(summary[wid])}</p></header>\n'
                    + "\n".join(sections[wid]))
        pages[WEB / f"{wid}.html"] = page(body, f"{PAGE_TITLE} · {title}", f"{wid}.html")
    return pages
