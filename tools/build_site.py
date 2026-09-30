"""生成 Neural-IPC 周汇报网页（单页静态 HTML，发布为 claude.ai 私有 Artifact）。

读者是课题组同学：懂 IPC，但没亲手跑过这些东西。页面只放三样：demo 视频、参数扫描小表、发现 / 结论。
数字全部从 NAS 上的结果文件读，缺失或失败的如实写成人话；数据出处只在页脚用一句话说明。

    # 默认只演练：打印每项结果的状态、页面里用到的关键数字、将写哪些文件，什么都不写
    python tools/build_site.py

    # 真正写文件、压视频
    python tools/build_site.py --execute [--crf 26] [--force-videos]

必须用 Neural-IPC 的 genesis 环境跑（要用它自带的 imageio-ffmpeg 二进制）：
    /data/xiaoyingwang/projects/Neural-IPC/.conda/genesis/bin/python tools/build_site.py

数据来源（只读，不修改）：
  demo : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_demos/<目录>/run_info.json、*.mp4、momentum_plot.png
  扫描 : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_sweep/<扫描>/<档位>.json（由 Neural-IPC/tools/ipc_sweep/sweep.py 写出）
  档位清单 : Neural-IPC/tools/ipc_sweep/configs.py（纯数据文件，按路径加载，不写 __pycache__）

index.html 按 Artifact 页面规范写：开头直接是 <title> 和 <style>，不写 doctype / html / head / body；
颜色全是 CSS 变量（亮 / 暗两套）；不引外部资源；视频和图片用相对路径，发布时作为附属文件上传。
每个压好的视频超过 10 MB、或全部视频加起来超过 60 MB，就报错停止，不写页面。
"""

import argparse
import datetime
import html
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # 加载 Neural-IPC 的 configs.py 时不往那个仓库写 __pycache__

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
WEB = Path(__file__).resolve().parents[1]
PROJECT = Path("/data/xiaoyingwang/projects/Neural-IPC")
OUT_ROOT = Path("/nas/xiaoyingwang/Neural-IPC/outputs")
DEMO_ROOT = OUT_ROOT / "ipc_demos"
SWEEP_ROOT = OUT_ROOT / "ipc_sweep"
SWEEP_CONFIGS = PROJECT / "tools" / "ipc_sweep" / "configs.py"

VIDEO_DIR = WEB / "assets" / "videos"
IMAGE_DIR = WEB / "assets" / "images"
BUILD_TMP = WEB / "build_tmp"          # 压缩中间文件；已在 .gitignore
VIDEO_MANIFEST = VIDEO_DIR / "encode_manifest.json"

MAX_VIDEO_BYTES = 10 * 1024 * 1024        # 单个视频上限（Artifact 附属文件每个 ≤ 15 MB，留余量）
MAX_TOTAL_VIDEO_BYTES = 60 * 1024 * 1024  # 全部视频加起来的上限
MAX_IMAGE_BYTES = 5 * 1024 * 1024         # 单张图上限

# --------------------------------------------------------------------------
# demo 清单：(NAS 目录名, 是否应出视频, 页面标题, 一句话说明)
# 说明里的 {momentum} 由 run_info.json 的 run.final_rel_momentum_error 填入。
# --------------------------------------------------------------------------
DEMOS = [
    ("1_hello_libuipc", True, "两个 ABD 四面体下落接触",
     "最小例子，Scene / World / advance 的基本骨架。"),
    ("10_ramp_sliding", True, "斜坡滑块",
     "8 个 ABD 方块在斜坡上滑，摩擦系数从 0 到 1 共八档。"),
    ("13_init_velocity", True, "带初速度的 ABD + FEM 方块",
     "刚体（ABD）和软体（FEM）方块带初速度运动、接触。"),
    ("20_contact_system_feature", True, "导出接触能量 / 梯度 / Hessian",
     "对我们最重要：ContactSystemFeature 能按接触基元类型（PT / EE / PE / PP / PH，法向和摩擦分开）"
     "导出能量、梯度、Hessian，是以后生成训练数据的入口。"),
    ("27_compute_mesh_d_hat", True, "按网格自动算 d̂",
     "上方块用按网格分辨率自动算出的 d̂，下方块用手设的 d̂，两者对比。"),
    ("89_mas_bunny", True, "FEM bunny + MAS 预条件",
     "软体 bunny 落地，线性求解用 MAS 预条件。"),
    ("90_abd_fem_cube_stack", True, "ABD / FEM 交替叠放",
     "同一场景里低自由度（ABD）和高自由度（FEM）混合，最接近我们的设定。"),
    ("genesis_ipc_objects_falling", True, "Genesis：布料 + 刚体 + 软球",
     "Genesis 调 libuipc，布料、刚体、FEM 软球一起落地。"),
    ("genesis_ipc_momentum", True, "Genesis：动量守恒检验",
     "零重力下刚体撞 FEM 球，末步相对动量误差 {momentum}。"),
    ("genesis_ipc_robot_grasp_cube", True, "Genesis：机械臂抓软方块",
     "two-way 耦合；Genesis 的刚体在 libuipc 里是 κ = 100 MPa 的 ABD。"),
    # 两个 benchmark 的官方 GUI 版（benchmarks/<name>/main.py）；计时版 run.py 不出画面，不上页面。
    # 0_check_libuipc 是环境自检，不算 demo，不上页面（2026-09-30 用户）。
    ("abd_bunny_grid_drop", True, "100 个 ABD bunny 下落",
     "10×10 个 ABD bunny 同时下落堆积，大规模 ABD 接触。"),
    ("wrecking_balls", True, "Wrecking balls",
     "ABD 方块墙、摆锤球和链节组成的大场景，大规模 ABD 接触。"),
]

PAGE_TITLE = "Neural-IPC 周汇报"
NAV = [("videos", "Demo 视频"), ("sweep", "参数扫描"), ("findings", "发现与结论")]  # 锚点只用字母


# ==========================================================================
# 通用小工具
# ==========================================================================
def esc(x):
    return html.escape(str(x), quote=True)


def load_json(path):
    """返回 (data, error)。文件不存在返回 (None, None)。"""
    path = Path(path)
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:  # 解析失败原样报告，不猜内容
        return None, f"{type(e).__name__}: {e}"


MISSING = object()


def dig(d, *keys):
    """按 key 链取值，任何一层不存在返回 MISSING。"""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and -len(cur) <= k < len(cur):
            cur = cur[k]
        else:
            return MISSING
    return cur


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def to_float(v):
    """json 里有些数以字符串存（kappa_log 的正则分组），统一转成 float；转不了返回 MISSING。"""
    if is_num(v):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return MISSING
    return MISSING


def g3(v):
    """3 位有效数字。"""
    return "—" if not is_num(v) else f"{v:.3g}"


def sci(v):
    """科学计数，如 1.71e5、1e9。"""
    if not is_num(v):
        return "—"
    m, e = f"{v:.2e}".split("e")
    m = m.rstrip("0").rstrip(".")
    return f"{m}e{int(e)}"


def pct(v):
    return "—" if not is_num(v) else f"{v * 100:.3g}%"


def file_size_str(n):
    return f"{n / 1024 / 1024:.2f} MB"


# ==========================================================================
# demo 结果收集
# ==========================================================================
def collect_demo(key, expects_video, title, line):
    d = DEMO_ROOT / key
    info_path = d / "run_info.json"
    r = {"key": key, "expects_video": expects_video, "title": title, "line": line,
         "dir": d, "info_path": info_path, "info": None, "state": None, "reason": None,
         "video_src": None, "video_note": None, "images": []}
    if not d.is_dir():
        r["state"], r["reason"] = "not_run", "还没跑"
        return r
    info, err = load_json(info_path)
    if err:
        r["state"], r["reason"] = "failed", f"结果文件损坏（{err}）"
        return r
    if info is None:
        r["state"], r["reason"] = "failed", "没跑完（没有结果记录）"
        return r
    r["info"], r["state"] = info, "ok"

    v = info.get("video", MISSING)
    if v is None or v is MISSING:
        r["video_note"] = "这个 demo 本来就不出视频" if not expects_video else "这次运行没有产出视频"
    else:
        vp = Path(v)
        if vp.is_file() and vp.stat().st_size > 0:
            r["video_src"] = vp
        else:
            r["video_note"] = "视频文件缺失"

    # ipc_momentum 的 momentum_plot.png 不上页面（2026-09-30 用户：图表小、放不大、看不清，页面只放视频）；
    # 动量误差这个数字已写在卡片说明里（run.final_rel_momentum_error），原图仍在 NAS 该 demo 目录。
    return r


def collect_demos():
    """The page shows exactly the DEMOS list. Other directories under DEMO_ROOT (0_check_libuipc, the benchmark
    timing runs *_run, anything unexpected) are NOT put on the page; main() prints them so nothing is hidden."""
    known = {k for k, *_ in DEMOS}
    demos = [collect_demo(*spec) for spec in DEMOS]
    unlisted = []
    if DEMO_ROOT.is_dir():
        unlisted = sorted(p.name for p in DEMO_ROOT.iterdir() if p.is_dir() and p.name not in known)
    return demos, [], unlisted


# ==========================================================================
# 扫描结果收集
# ==========================================================================
def load_sweep_configs():
    spec = importlib.util.spec_from_file_location("neural_ipc_sweep_configs", SWEEP_CONFIGS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect_level(sweep, level, ov_cfg):
    jp = SWEEP_ROOT / sweep / f"{level}.json"
    r = {"sweep": sweep, "level": level, "ov_cfg": ov_cfg, "json_path": jp, "data": None,
         "state": None, "reason": None}
    data, err = load_json(jp)
    if err:
        r["state"], r["reason"] = "failed", "结果文件损坏"
        return r
    if data is None:
        r["state"], r["reason"] = "not_run", "还没跑"
        return r
    r["data"] = data
    st = data.get("status", MISSING)
    if st == "ok":
        r["state"], r["reason"] = "ok", "跑完"
    elif st == "exception":
        tb = data.get("exception_traceback") or ""
        last = [ln for ln in tb.strip().splitlines() if ln.strip()]
        r["state"] = "failed"
        r["reason"] = "运行出错" + (f"：{last[-1].strip()}" if last else "")
    elif st == "init_invalid" and dig(data, "sanity_at_init", "penetration") is True:
        r["state"], r["reason"] = "rejected", "被 sanity check 拒绝（预期内）"
    elif st == "init_invalid":
        r["state"], r["reason"] = "failed", "初始化失败"
    elif st == "invalid_during_run":
        r["state"], r["reason"] = "failed", "仿真中途失效"
    elif st == "nonfinite_positions":
        r["state"], r["reason"] = "failed", "顶点坐标出现 NaN / inf"
    elif st in ("starting", "initializing", "running") and "summary" not in data:
        r["state"], r["reason"] = "unfinished", "没跑完"
    else:
        r["state"], r["reason"] = "failed", "状态未知"
    return r


def collect_sweeps():
    cfg = load_sweep_configs()
    sweeps = []
    known = set()
    for sweep, spec in cfg.SWEEPS.items():
        rows = []
        for level, ov in spec["levels"].items():
            rows.append(collect_level(sweep, level, ov))
            known.add((sweep, level))
        sd = SWEEP_ROOT / sweep
        if sd.is_dir():  # NAS 上有、configs.py 里没有的档位
            for jp in sorted(sd.glob("*.json")):
                if (sweep, jp.stem) not in known:
                    rows.append(collect_level(sweep, jp.stem, MISSING))
                    known.add((sweep, jp.stem))
        sweeps.append({"name": sweep, "why": spec.get("why", ""), "rows": rows})
    return cfg, sweeps


# ==========================================================================
# 视频 / 图片（压缩逻辑不动）
# ==========================================================================
def load_manifest():
    m, err = load_json(VIDEO_MANIFEST)
    if err:
        raise SystemExit(f"[build_site] {VIDEO_MANIFEST} 无法解析：{err}")
    return m or {}


def ffmpeg_args(exe, src, dst, crf):
    return [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(src),
            "-vf", "scale=-2:'min(720,ih)'",   # 高度 720（原片更小则不放大），宽度按比例取偶数
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            str(dst)]


def plan_videos(demos, crf, force):
    manifest = load_manifest()
    jobs = []
    for r in demos:
        if r["video_src"] is None:
            continue
        src = r["video_src"]
        dst = VIDEO_DIR / f"{r['key']}.mp4"
        st = src.stat()
        prev = manifest.get(dst.name)
        uptodate = (not force and dst.is_file() and isinstance(prev, dict)
                    and prev.get("src") == str(src) and prev.get("src_size") == st.st_size
                    and prev.get("src_mtime") == st.st_mtime and prev.get("crf") == crf)
        jobs.append({"key": r["key"], "src": src, "dst": dst, "src_size": st.st_size,
                     "src_mtime": st.st_mtime, "action": "skip" if uptodate else "encode"})
        r["video_web"] = f"assets/videos/{dst.name}"
        r["video_job"] = jobs[-1]
    return jobs, manifest


def plan_images(demos):
    jobs = []
    for r in demos:
        r["images_web"] = []
        for src, name, src_label in r["images"]:
            dst = IMAGE_DIR / name
            jobs.append({"src": src, "dst": dst, "size": src.stat().st_size})
            r["images_web"].append((f"assets/images/{name}", src, src_label))
    return jobs


def get_ffmpeg_exe():
    try:
        import imageio_ffmpeg  # genesis 环境自带（imageio-ffmpeg 0.6.0，binaries/ffmpeg-linux-x86_64-v7.0.2）
    except ImportError as e:
        raise SystemExit(f"[build_site] 找不到 imageio_ffmpeg（{e}）。请用 "
                         f"{PROJECT}/.conda/genesis/bin/python 运行本脚本。")
    return imageio_ffmpeg.get_ffmpeg_exe()


def encode_video(exe, job, crf, manifest):
    BUILD_TMP.mkdir(parents=True, exist_ok=True)
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BUILD_TMP / job["dst"].name
    cmd = ffmpeg_args(exe, job["src"], tmp, crf)
    print(f"[build_site] 压缩 {job['src']} -> {job['dst']}", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"[build_site] ffmpeg 失败（退出码 {p.returncode}）：{job['src']}\n"
                         f"命令：{cmd}\nstderr：\n{p.stderr}")
    if not tmp.is_file() or tmp.stat().st_size == 0:
        raise SystemExit(f"[build_site] ffmpeg 返回 0 但没有产出文件：{tmp}")
    size = tmp.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise SystemExit(f"[build_site] 压缩后 {file_size_str(size)} 超过上限 {file_size_str(MAX_VIDEO_BYTES)}："
                         f"{tmp}（留在 build_tmp/ 供检查，未放进 assets/）。"
                         f"停止：请提高 --crf 或和用户商量，不要推大文件。")
    os.replace(tmp, job["dst"])
    manifest[job["dst"].name] = {
        "src": str(job["src"]), "src_size": job["src_size"], "src_mtime": job["src_mtime"],
        "crf": crf, "ffmpeg": exe, "args": cmd[1:-1] + ["<out>"],
        "out_size": size, "encoded_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 每压完一个就落盘 manifest，中途失败也能知道哪些已经好了
    VIDEO_MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[build_site]   -> {file_size_str(size)}", flush=True)


# ==========================================================================
# 扫描结果的派生量（都只从 json 读，唯一的计算是逐帧均值和"总耗时 / 帧数"）
# ==========================================================================
def newton_mean(d):
    """每帧 Newton 迭代的平均：frames[*].newton_iter_timer_count 的算术平均。"""
    vals = [f.get("newton_iter_timer_count") for f in (d.get("frames") or []) if isinstance(f, dict)]
    vals = [v for v in vals if is_num(v)]
    return sum(vals) / len(vals) if vals else MISSING


def sec_per_frame(d):
    tot, done = dig(d, "summary", "wall_seconds_total"), dig(d, "summary", "frames_done")
    return tot / done if is_num(tot) and is_num(done) and done > 0 else MISSING


def sanity_on(d):
    return dig(d, "params", "sanity_check", "enable") != 0


def kappa_info(d):
    """(区间下界, 区间上界, 实际 κ, 设的 κ, 是否被夹)；取不到的是 MISSING。"""
    lo = to_float(dig(d, "kappa_log", "kappa_corridor", 0, "groups", 0))
    hi = to_float(dig(d, "kappa_log", "kappa_corridor", 0, "groups", 1))
    set_k = dig(d, "params", "contact_model_used", "resistance")
    clamped = d.get("kappa_clamped", MISSING)
    if clamped is True:
        actual = to_float(dig(d, "kappa_log", "default_kappa_clamped", 0, "groups", 1))
    elif clamped is False:
        actual = set_k if is_num(set_k) else MISSING
    else:
        actual = MISSING
    return lo, hi, actual, set_k, clamped


def pen_text(row):
    """穿透那一格的人话。"""
    if row["state"] != "ok":
        return row["reason"]
    d = row["data"]
    if not sanity_on(d):
        return "跑完，但检查已关，无法判断"
    n_pen, n_chk = dig(d, "summary", "n_checks_with_penetration"), dig(d, "summary", "n_sanity_checks")
    if n_pen == 0:
        return "无"
    if is_num(n_pen):
        return f"有（{n_pen} / {n_chk} 次检查）"
    return "结果缺失"


def newton_text(row):
    if row["state"] != "ok":
        return "—"
    d = row["data"]
    s = f"{g3(newton_mean(d))} / {g3(dig(d, 'summary', 'newton_iter_timer_max'))}"
    hit = dig(d, "summary", "n_frames_hit_max_iter")
    if is_num(hit) and hit > 0:
        s += f"（{hit} 帧撞到上限）"
    return s


# ==========================================================================
# HTML
# ==========================================================================
PAGE_CSS = """
:root {
  --bg: #fafaf8; --fg: #1f2328; --muted: #646b74; --border: #dcdfe3; --soft: #f1f3f5;
  --accent: #245f96; --good: #1f6b3a; --warn: #8a5a0b;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
          "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16191d; --fg: #e4e7eb; --muted: #9ba3ad; --border: #353b43; --soft: #20252b;
    --accent: #80b4e6; --good: #8fd3a5; --warn: #e6c27a;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #16191d; --fg: #e4e7eb; --muted: #9ba3ad; --border: #353b43; --soft: #20252b;
  --accent: #80b4e6; --good: #8fd3a5; --warn: #e6c27a;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { overflow-x: hidden; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--font);
       font-size: 15px; line-height: 1.65; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 0 16px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { padding: 28px 0 8px; }
h1 { font-size: 1.55rem; margin: 0 0 8px; font-weight: 650; letter-spacing: 0.01em; }
.summary { margin: 8px 0 0; max-width: 72ch; }
nav.toc { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 0.9rem; margin: 14px 0 0;
          padding-bottom: 12px; border-bottom: 1px solid var(--border); }
h2 { font-size: 1.2rem; margin: 36px 0 10px; font-weight: 650; }
h3 { font-size: 1rem; margin: 22px 0 4px; font-weight: 600; }
section { scroll-margin-top: 12px; }
p { margin: 6px 0; }
.muted { color: var(--muted); }
code { font-size: 0.95em; word-break: break-all; }
.small { font-size: 0.86rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
        gap: 22px 18px; }
.demo { min-width: 0; }
.demo h3 { margin: 0 0 6px; font-size: 0.95rem; }
.demo h3 .key { color: var(--muted); font-weight: 400; font-size: 0.8rem; }
.demo p { font-size: 0.86rem; color: var(--muted); margin-top: 6px; }
video, img { display: block; width: 100%; height: auto; border-radius: 4px; background: var(--soft); }
img.plot { margin-top: 8px; }
.novideo { padding: 22px 12px; background: var(--soft); color: var(--muted); border-radius: 4px;
           font-size: 0.86rem; }
.tablewrap { overflow-x: auto; max-width: 100%; margin: 6px 0 4px; }
table { border-collapse: collapse; font-size: 0.88rem; min-width: 60%; }
th, td { text-align: left; padding: 5px 14px 5px 0; border-bottom: 1px solid var(--border);
         white-space: nowrap; }
th { font-weight: 600; color: var(--muted); font-size: 0.82rem; border-bottom-color: var(--fg); }
td { font-variant-numeric: tabular-nums; }
td.good { color: var(--good); }
td.warn { color: var(--warn); }
ul.findings { padding-left: 20px; max-width: 80ch; }
ul.findings li { margin: 8px 0; }
.next { max-width: 80ch; }
footer.foot { color: var(--muted); font-size: 0.8rem; border-top: 1px solid var(--border);
              margin-top: 40px; padding: 12px 16px 28px; }
@media (max-width: 480px) {
  body { font-size: 14px; }
  th, td { padding-right: 10px; }
}
"""


def page(body):
    """Artifact 页面：开头直接是 <title> 和 <style>，不写 doctype / html / head / body。"""
    return f"""<title>{esc(PAGE_TITLE)}</title>
<style>{PAGE_CSS}</style>
<main class="wrap">
{body}
</main>
<footer class="wrap foot">数据：视频与运行记录在 NAS <code>{esc(DEMO_ROOT)}</code>，参数扫描结果在
<code>{esc(SWEEP_ROOT)}</code>（由 Neural-IPC 仓库 <code>tools/ipc_sweep/sweep.py</code> 生成）；本页由
Neural-IPC-web 仓库 <code>tools/build_site.py</code> 读取这些文件生成。</footer>
"""


# ---------------- demo 视频 ----------------
def demo_card(r, facts):
    line = r["line"].replace("{momentum}", pct(facts.get("momentum_err", MISSING)))
    head = f'<h3>{esc(r["title"])} <span class="key">{esc(r["key"])}</span></h3>'
    if r["state"] != "ok":
        media = f'<div class="novideo">{esc(r["reason"])}</div>'
    elif r.get("video_web"):
        media = f'<video controls muted playsinline preload="metadata" src="{esc(r["video_web"])}"></video>'
    else:
        media = f'<div class="novideo">{esc(r["video_note"] or "没有视频")}</div>'
    for web, _src, _lbl in r.get("images_web", []):
        media += f'<img class="plot" src="{esc(web)}" alt="动量随时间的变化曲线" loading="lazy">'
    if r.get("images_note"):
        media += f'<div class="novideo">{esc(r["images_note"])}</div>'
    return f'<div class="demo">{head}{media}<p>{esc(line)}</p></div>'


def videos_section(demos, extras, facts):
    cards = [r for r in demos if r["expects_video"]] + extras
    novid = [r for r in demos if not r["expects_video"]]
    parts = ['<section id="videos"><h2>Demo 视频</h2>',
             '<p class="muted small">官方示例一字未改，在服务器上离屏录像（画面用 CPU 软件渲染，物理在 GPU 上算）。</p>',
             '<div class="grid">', *[demo_card(r, facts) for r in cards], "</div>"]
    if novid:
        ok = [r for r in novid if r["state"] == "ok"]
        bad = [r for r in novid if r["state"] != "ok"]
        s = (f"另有 {len(novid)} 个 demo 本来就不出视频："
             + "；".join(f"{r['key']}（{r['title']}，{r['line']}）" for r in novid))
        s += "。均已跑通。" if not bad else (
            "。其中 " + "、".join(f"{r['key']} {r['reason']}" for r in bad) + "。")
        parts.append(f'<p class="muted small" style="margin-top:18px">{esc(s)}</p>')
    parts.append("</section>")
    return "\n".join(parts)


# ---------------- 参数扫描 ----------------
def table(heads, rows):
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


def pen_cell(row):
    t = pen_text(row)
    return (t, "good") if t == "无" else ((t, "warn") if row["state"] != "ok" or t.startswith("有") else t)


def rows_of(sweeps, name):
    for s in sweeps:
        if s["name"] == name:
            return s["rows"]
    return []


def sweep_tables(sweeps):
    base = rows_of(sweeps, "baseline")
    out = []

    # d̂：把 baseline（默认 d̂）也放进来，按 d̂ 从小到大
    rows = list(rows_of(sweeps, "d_hat")) + [dict(r, is_default=True) for r in base]
    rows.sort(key=lambda r: dig(r["data"], "params", "d_hat") if r["data"] and is_num(
        dig(r["data"], "params", "d_hat")) else float("inf"))
    trs = []
    for r in rows:
        d = r["data"] or {}
        dh = dig(d, "params", "d_hat")
        lbl = (g3(dh * 1000) if is_num(dh) else r["level"]) + ("（默认）" if r.get("is_default") else "")
        trs.append([lbl, g3(dig(d, "params", "d_hat_over_L")), pen_cell(r), newton_text(r)])
    out.append(("d̂（barrier 作用距离）",
                "d̂ 以球表面平均边长为单位，从 0.01 倍取到 1 倍；libuipc 默认的 0.01 m 约为 0.66 倍边长。",
                table(["d̂（mm）", "d̂ / 平均边长", "穿透", "每帧 Newton 迭代（平均 / 最多）"], trs)))

    trs = []
    for r in rows_of(sweeps, "dt"):
        d = r["data"] or {}
        dt = dig(d, "params", "dt")
        trs.append([g3(dt) + ("（默认）" if dt == 0.01 else "") if is_num(dt) else r["level"],
                    pen_cell(r), newton_text(r)])
    out.append(("时间步长 dt",
                "从 0.001（Genesis 默认）取到 0.04，物理时间固定 2 s，帧数随 dt 变。",
                table(["dt（s）", "穿透", "每帧 Newton 迭代（平均 / 最多）"], trs)))

    trs = []
    for r in rows_of(sweeps, "friction"):
        d = r["data"] or {}
        mu = dig(d, "params", "contact_model_used", "friction_rate")
        trs.append([(g3(mu) + ("（默认）" if mu == 0.5 else "")) if is_num(mu) else r["level"],
                    pen_cell(r), newton_text(r)])
    out.append(("摩擦系数 μ", "0 为无摩擦，0.2 / 0.5 是 libuipc 示例里最常用的值，1.0 为高摩擦。",
                table(["μ", "穿透", "每帧 Newton 迭代（平均 / 最多）"], trs)))

    trs = []
    for r in rows_of(sweeps, "resistance"):
        d = r["data"] or {}
        lo, hi, act, set_k, cl = kappa_info(d)
        if cl is True and is_num(act) and is_num(lo) and is_num(hi):
            note = "（被抬到下界）" if abs(act - lo) <= 1e-6 * hi else ("（被压到上界）" if abs(act - hi) <= 1e-6 * hi
                                                                    else "（被夹）")
        elif cl is False:
            note = "（照用）"
        else:
            note = ""
        trs.append([sci(set_k) if is_num(set_k) else r["level"], sci(act) + note, pen_cell(r), newton_text(r)])
    lo, hi, *_ = kappa_info(base[0]["data"]) if base and base[0]["data"] else (MISSING, MISSING)
    out.append(("接触刚度 κ",
                f"libuipc 会把 κ 夹进一个按场景算出的区间，本场景是 [{sci(lo)}, {sci(hi)}] Pa；"
                "区间内取 4 档，两端外各取 1 档看它怎么被夹（1e9 是 libuipc 默认表项）。",
                table(["设的 κ（Pa）", "实际用的 κ（Pa）", "穿透", "每帧 Newton 迭代（平均 / 最多）"], trs)))

    trs = []
    for r in rows_of(sweeps, "init_penetration"):
        d = r["data"] or {}
        gap = dig(d, "params", "pair_center_distance", "nominal_sphere_gap")
        dh = dig(d, "params", "d_hat")
        if is_num(gap) and gap > 0:
            lbl = f"间隙 {g3(gap * 1000)} mm" + (f"（{g3(gap / dh)} d̂）" if is_num(dh) and dh > 0 else "")
        elif is_num(gap):
            lbl = f"穿插 {g3(-gap * 100)} cm"
        else:
            lbl = r["level"]
        if r["data"] and not sanity_on(d):
            lbl += "，关掉检查"
        if r["state"] == "ok" and not sanity_on(d):
            res = ("跑完，但无法判断穿透", "warn")
        elif r["state"] == "ok":
            res = ("正常跑完，无穿透", "good") if pen_text(r) == "无" else (pen_text(r), "warn")
        else:
            res = (r["reason"], "warn")
        trs.append([lbl, res, newton_text(r)])
    out.append(("初始穿插",
                "把相邻两个球的距离改成一开始就贴得很近或互相穿进去。",
                table(["两球初始状态", "结果", "每帧 Newton 迭代（平均 / 最多）"], trs)))

    trs = []
    names = {"coarse": "粗", "medium": "中（默认）", "fine": "细"}
    for r in rows_of(sweeps, "mesh_res"):
        d = r["data"] or {}
        L = dig(d, "mesh", "edge_len_mean")
        trs.append([names.get(r["level"], r["level"]), str(dig(d, "mesh", "n_vertices")) if d else "—",
                    g3(L * 1000) if is_num(L) else "—", g3(dig(d, "params", "d_hat_over_L")),
                    pen_cell(r), newton_text(r)])
    out.append(("网格分辨率", "同一个球用 tetgen 四面体化成粗 / 中 / 细三档，d̂ 保持默认 0.01 m。",
                table(["网格", "每球顶点数", "平均边长（mm）", "d̂ / 边长", "穿透", "每帧 Newton 迭代（平均 / 最多）"], trs)))
    return out


def sweep_section(sweeps, cfg):
    sc = getattr(cfg, "SCENE", {})
    R = sc.get("ball_radius", MISSING)
    base = rows_of(sweeps, "baseline")
    bd = base[0]["data"] if base and base[0]["data"] else {}
    nv = dig(bd, "mesh", "n_vertices")
    T = sc.get("sim_time", MISSING)
    scene = (f"场景：8 个 FEM 软球（半径 {g3(R)} m，每球约 {nv} 个顶点，StableNeoHookean，E = 120 kPa）"
             f"从静止落进开口盒子，仿真 {g3(T)} s；每次只改一个参数，其余用 libuipc 默认"
             f"（dt = {g3(dig(bd, 'params', 'dt'))} s，d̂ = {g3(dig(bd, 'params', 'd_hat'))} m），"
             "穿透用 libuipc 的 sanity check 每 10 帧查一次。")
    parts = ['<section id="sweep"><h2>IPC 参数扫描</h2>', f"<p>{esc(scene)}</p>"]
    for title, one, tbl in sweep_tables(sweeps):
        parts.append(f"<h3>{esc(title)}</h3><p class=\"muted small\">{esc(one)}</p>{tbl}")
    parts.append("</section>")
    return "\n".join(parts)


# ---------------- 关键数字（发现 / 结论里用，dry-run 时也打印出来核对） ----------------
def compute_facts(demos, sweeps):
    f = {}
    by = {(r["sweep"], r["level"]): r for s in sweeps for r in s["rows"]}
    allrows = list(by.values())

    mom = [r for r in demos if r["key"] == "genesis_ipc_momentum" and r["state"] == "ok"]
    f["momentum_err"] = dig(mom[0]["info"], "run", "final_rel_momentum_error") if mom else MISSING

    ok_checked = [r for r in allrows if r["state"] == "ok" and sanity_on(r["data"])]
    f["n_ok_checked"] = len(ok_checked)
    f["n_rows"] = len(allrows)
    f["n_with_pen"] = sum(1 for r in ok_checked if dig(r["data"], "summary", "n_checks_with_penetration") != 0)
    f["n_hit_max"] = sum(1 for r in ok_checked if dig(r["data"], "summary", "n_frames_hit_max_iter") != 0)
    f["max_iter"] = dig(ok_checked[0]["data"], "params", "newton_max_iter") if ok_checked else MISSING
    f["n_not_ok"] = sum(1 for r in allrows if r["state"] in ("failed", "unfinished", "not_run"))
    f["rejected"] = [r["level"] for r in allrows if r["state"] == "rejected"]

    ns = by.get(("init_penetration", "overlap_0p5R_nosanity"))
    if ns and ns["state"] == "ok":
        f["nosanity_mean"] = newton_mean(ns["data"])
        f["nosanity_hit"] = dig(ns["data"], "summary", "n_frames_hit_max_iter")
        f["nosanity_max"] = dig(ns["data"], "summary", "newton_iter_timer_max")
    means = [newton_mean(r["data"]) for r in ok_checked]
    means = [m for m in means if is_num(m)]
    f["other_mean_lo"], f["other_mean_hi"] = (min(means), max(means)) if means else (MISSING, MISSING)
    meds = [dig(r["data"], "summary", "newton_iter_timer_median") for r in ok_checked]
    maxs = [dig(r["data"], "summary", "newton_iter_timer_max") for r in ok_checked]
    f["n_median2"] = sum(1 for m in meds if m == 2)
    f["n_max7"] = sum(1 for m in maxs if m == 7)

    b = by.get(("baseline", "default"))
    if b and b["data"]:
        f["corr_lo"], f["corr_hi"], *_ = kappa_info(b["data"])
        f["base_spf"] = sec_per_frame(b["data"])
    for lvl, key in (("1e4", "k1e4"), ("1e9", "k1e9")):
        r = by.get(("resistance", lvl))
        if r and r["data"]:
            f[key] = kappa_info(r["data"])[2]
    f["n_res_unclamped"] = sum(1 for r in rows_of(sweeps, "resistance")
                               if r["data"] and r["data"].get("kappa_clamped") is False)
    for lvl, key in (("0p001", "dt001_mean"), ("0p01", "dt01_mean")):
        r = by.get(("dt", lvl))
        if r and r["state"] == "ok":
            f[key] = newton_mean(r["data"])
    r = by.get(("d_hat", "rel0p01"))
    if r and r["state"] == "ok":
        f["dhat001_max"] = dig(r["data"], "summary", "newton_iter_timer_max")
        f["dhat001_corr_lo"] = kappa_info(r["data"])[0]
    r = by.get(("d_hat", "rel1p0"))
    if r and r["state"] == "ok":
        f["dhat1_corr_lo"] = kappa_info(r["data"])[0]
    r = by.get(("mesh_res", "medium"))
    if r and r["state"] == "ok":
        f["med_spf"] = sec_per_frame(r["data"])
    return f


def findings_section(f):
    rej = "、".join(f["rejected"]) if f["rejected"] else "无"
    items = [
        f"<b>正常初始状态下都不穿透。</b>{f['n_ok_checked']} 个正常开跑的配置，每 10 帧检查一次，"
        + ("一次穿透都没查到" if f["n_with_pen"] == 0 else f"有 {f['n_with_pen']} 个查到穿透")
        + "；Newton 迭代也"
        + (f"从没撞到 {f['max_iter']} 次上限。" if f["n_hit_max"] == 0 else f"有 {f['n_hit_max']} 个撞到上限。"),

        "<b>初始穿插会被直接拒绝。</b>两球一开始穿进 1 cm 或 5 cm，world.init 的 sanity check 判定相交、不开始仿真；"
        "只隔 0.5 d̂（5 mm）能正常跑。关掉检查硬跑 5 cm 穿插也能跑完，但"
        f"有 {f.get('nosanity_hit', '—')} 帧 Newton 撞到 {g3(f.get('nosanity_max', MISSING))} 次上限，"
        f"平均每帧 {g3(f.get('nosanity_mean', MISSING))} 次（其他配置 {g3(f['other_mean_lo'])}–{g3(f['other_mean_hi'])} 次），"
        "而且关了检查就无法判断穿透。所以生成数据时初始状态必须无穿插。",

        f"<b>κ 会被自动夹进一个区间。</b>本场景是 [{sci(f.get('corr_lo', MISSING))}, {sci(f.get('corr_hi', MISSING))}] Pa："
        f"区间内 {f['n_res_unclamped']} 档照用，1e4 被抬到 {sci(f.get('k1e4', MISSING))}，"
        f"1e9（libuipc 默认表项）被压到 {sci(f.get('k1e9', MISSING))}。不调用 default_model 时用的是下界，不是表里的 1 GPa。"
        f"这个区间还随 d̂、dt、网格变（例如 d̂ = 0.01 倍边长时下界是 {sci(f.get('dhat001_corr_lo', MISSING))}，"
        f"1 倍边长时是 {sci(f.get('dhat1_corr_lo', MISSING))}），所以扫这些参数时实际 κ 也跟着变了。",

        f"<b>现有指标还分不出参数的影响。</b>{f['n_ok_checked']} 个配置里 {f['n_median2']} 个每帧 Newton 中位数是 2、"
        f"{f['n_max7']} 个最多是 7，这主要是 libuipc 默认开着的 semi-implicit 提前终止造成的。能看出的只有："
        f"dt 越小迭代略多（dt = 0.001 平均 {g3(f.get('dt001_mean', MISSING))} 次，dt = 0.01 平均 {g3(f.get('dt01_mean', MISSING))} 次）；"
        f"d̂ = 0.01 倍边长时单帧最多 {g3(f.get('dhat001_max', MISSING))} 次。",

        f"<b>每帧耗时暂时不能比。</b>GPU 是共享的，同一个配置跑两次，每帧分别 {g3(f.get('base_spf', MISSING))} s 和 "
        f"{g3(f.get('med_spf', MISSING))} s，所以表里没放耗时。",

        "<b>libuipc 的 barrier 不是 IPC 原论文的形式</b>，而是 Stiff-GIPC 的 log² 形式 "
        "κ(D − d̂²)²[ln(D / d̂²)]²，D = d²。学生网络对标时要以它为准。",

        "<b>第一轮扫描有两个设计错误，已修正并重跑：</b>libuipc 自带的 ball.msh 其实是 0.168 × 0.2 × 0.168 的长条，"
        "导致穿插量全错，已换成真球；κ 档位原先都在区间外、被夹成同一个值，已改到区间内。",
    ]
    lis = "".join(f"<li>{t}</li>" for t in items)  # 文本是本脚本写死的，数字来自 json，不含用户输入
    nxt = ("<b>下一步：</b>加能反映行为的指标（最终静止位置、最小间距、接触力），在 GPU 空闲时计时或多次取平均；"
           "确认 ContactSystemFeature 导出的能量里是否已乘 κ·dt²。")
    return (f'<section id="findings"><h2>发现与结论</h2><ul class="findings">{lis}</ul>'
            f'<p class="next">{nxt}</p></section>')


def build_page(demos, extras, sweeps, cfg, facts):
    n_video = sum(1 for r in demos + extras if r.get("video_web"))
    n_demo_ok = sum(1 for r in demos + extras if r["state"] == "ok")
    n_demo = len(demos + extras)
    n_sweeps = sum(1 for s in sweeps if s["name"] != "baseline")
    ran = f"{n_demo} 个官方 demo 全部跑完" if n_demo_ok == n_demo else f"{n_demo} 个官方 demo 跑完了 {n_demo_ok} 个"
    summary = (f"这周把 libuipc（Genesis 的 IPC 耦合底层也是它，在 H100 上需从源码编译）在服务器上跑通了，"
               f"{ran}，其中 {n_video} 个录了视频。"
               f"然后在“8 个软球落进盒子”的场景上对 {n_sweeps} 个 IPC 参数做了单变量扫描，共 {facts['n_rows']} 个配置。"
               "主要发现：正常初始状态下全部不穿透，初始穿插会被 libuipc 直接拒绝，接触刚度 κ 会被自动夹进一个区间，"
               "而现有的迭代次数、耗时指标还看不出参数对行为的影响。")
    nav = "".join(f'<a href="#{h}">{esc(n)}</a>' for h, n in NAV)
    body = (f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1><p class="summary">{esc(summary)}</p>'
            f'<nav class="toc">{nav}</nav></header>\n'
            + videos_section(demos, extras, facts) + "\n"
            + sweep_section(sweeps, cfg) + "\n"
            + findings_section(facts))
    return page(body)


# ==========================================================================
# main
# ==========================================================================
STATE_LABEL = {"ok": "完成", "failed": "失败", "not_run": "尚未运行", "unfinished": "未结束",
               "rejected": "被拒绝（预期内）"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="真正写文件、压视频（默认只打印计划）")
    ap.add_argument("--crf", type=int, default=26, help="libx264 CRF（默认 26；数字越大文件越小）")
    ap.add_argument("--force-videos", action="store_true", help="忽略 encode_manifest.json，全部重新压缩")
    args = ap.parse_args()

    demos, extras, unlisted = collect_demos()
    if unlisted:
        print(f"[build_site] 注意：NAS 上这些 demo 目录不在 DEMOS 清单里，不上页面：{unlisted}")
    cfg, sweeps = collect_sweeps()
    all_demos = demos + extras
    vjobs, manifest = plan_videos(all_demos, args.crf, args.force_videos)
    ijobs = plan_images(all_demos)
    facts = compute_facts(demos, sweeps)

    pages = {WEB / "index.html": build_page(demos, extras, sweeps, cfg, facts)}

    # ---------------- 打印计划 ----------------
    mode = "EXECUTE" if args.execute else "DRY-RUN（只演练，不写任何文件；加 --execute 才真正写）"
    print(f"[build_site] 模式：{mode}")
    print(f"[build_site] demo 结果：{DEMO_ROOT}")
    for r in all_demos:
        extra = "" if r["state"] == "ok" else f"  {r['reason']}"
        vid = f"  视频: {r['video_src']}" if r["video_src"] else (f"  {r['video_note']}" if r["video_note"] else "")
        print(f"  - {r['key']:<32} {STATE_LABEL[r['state']]}{extra}{vid}")
    print(f"[build_site] 扫描结果：{SWEEP_ROOT}")
    for s in sweeps:
        states = {}
        for r in s["rows"]:
            states[r["state"]] = states.get(r["state"], 0) + 1
        print(f"  - {s['name']:<18} 共 {len(s['rows'])} 档：" + "，".join(f"{STATE_LABEL[k]} {v}" for k, v in states.items()))
        for r in s["rows"]:
            if r["state"] in ("failed", "unfinished"):
                print(f"      {r['level']}: {r['reason']}")
    print("[build_site] 页面里用到的关键数字（全部读自结果文件）：")
    for k, v in facts.items():
        print(f"  - {k} = {v if v is not MISSING else '缺失'}")
    print("[build_site] 将写页面：")
    for p, text in pages.items():
        print(f"  - {p}（{len(text.encode('utf-8')) / 1024:.1f} KB）")
    print(f"[build_site] 视频（H.264 / 720p / crf {args.crf} / +faststart；单个上限 {file_size_str(MAX_VIDEO_BYTES)}，"
          f"合计上限 {file_size_str(MAX_TOTAL_VIDEO_BYTES)}）：")
    if not vjobs:
        print("  - 无")
    for j in vjobs:
        act = "压缩" if j["action"] == "encode" else "跳过（encode_manifest.json 记录的源文件和 crf 都没变）"
        print(f"  - {act}：{j['src']}（{file_size_str(j['src_size'])}）-> {j['dst']}")
    print("[build_site] 图片（原样复制）：")
    if not ijobs:
        print("  - 无")
    for j in ijobs:
        print(f"  - {j['src']}（{file_size_str(j['size'])}）-> {j['dst']}")
    referenced = {j["dst"].name for j in vjobs}
    if VIDEO_DIR.is_dir():
        stale = sorted(p.name for p in VIDEO_DIR.glob("*.mp4") if p.name not in referenced)
        if stale:
            print(f"[build_site] 注意：assets/videos/ 里有页面不再引用的视频（本脚本不删除）：{stale}")

    if not args.execute:
        print("[build_site] DRY-RUN 结束，什么都没写。")
        return 0

    # ---------------- 真正执行：先视频 / 图片，全部成功后再写页面 ----------------
    for j in ijobs:
        if j["size"] > MAX_IMAGE_BYTES:
            raise SystemExit(f"[build_site] 图片 {j['src']} 有 {file_size_str(j['size'])}，超过 "
                             f"{file_size_str(MAX_IMAGE_BYTES)}，停止。")

    def check_total():
        total = sum(j["dst"].stat().st_size for j in vjobs if j["dst"].is_file())
        if total > MAX_TOTAL_VIDEO_BYTES:
            raise SystemExit(f"[build_site] 页面引用的视频合计 {file_size_str(total)}，超过上限 "
                             f"{file_size_str(MAX_TOTAL_VIDEO_BYTES)}，停止（页面没有写）。请提高 --crf 或和用户商量删减。")
        return total

    for j in vjobs:  # 跳过的也核对一次体积
        if j["action"] == "skip" and j["dst"].stat().st_size > MAX_VIDEO_BYTES:
            raise SystemExit(f"[build_site] {j['dst']} 有 {file_size_str(j['dst'].stat().st_size)}，"
                             f"超过单个上限，停止。加 --force-videos 并提高 --crf 重压。")
    todo = [j for j in vjobs if j["action"] == "encode"]
    if todo:
        exe = get_ffmpeg_exe()
        print(f"[build_site] ffmpeg：{exe}")
        for j in todo:
            encode_video(exe, j, args.crf, manifest)
            check_total()
    total = check_total()
    if ijobs:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        for j in ijobs:
            shutil.copy2(j["src"], j["dst"])
            print(f"[build_site] 复制 {j['src']} -> {j['dst']}")
    for p, text in pages.items():
        p.write_text(text, encoding="utf-8")
        print(f"[build_site] 写入 {p}")
    print(f"[build_site] 完成。页面引用的视频共 {len(vjobs)} 个，合计 {file_size_str(total)}。")
    # 发布时要作为附属文件一起上传的文件（published path -> 本地路径），见 README.md「发布」
    files = {f"assets/videos/{j['dst'].name}": str(j["dst"]) for j in vjobs}
    files.update({f"assets/images/{j['dst'].name}": str(j["dst"]) for j in ijobs})
    print("[build_site] 发布 index.html 时需要一起上传的附属文件（published path -> 本地文件）：")
    print(json.dumps(files, indent=1, ensure_ascii=False) if files else "  - 无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
