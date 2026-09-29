"""生成 Neural-IPC demo 汇总网页（单页静态 HTML，发布为 claude.ai 私有 Artifact）。

读 NAS 上的 demo / 参数扫描结果，生成一个 index.html（总览 / demo 画廊 / 参数扫描都是同一页里的版块，
用 #demos、#sweep 这类纯字母锚点跳转），并把 demo 视频压成 H.264 / 720p 放进 assets/videos/，
momentum_plot.png 之类的图原样复制到 assets/images/。

index.html 按 Artifact 页面规范写：文件开头就是 <title> 和 <style>，不写 doctype / html / head / body
（发布时自动包一层）；CSS 全部内联，颜色全部是 CSS 变量并支持亮 / 暗两种主题；不引任何外部资源；
视频和图片用相对路径，由主会话发布时作为附属文件一起上传（见 README.md）。

    # 默认只演练：打印将生成哪些文件、压缩哪些视频，什么都不写
    python tools/build_site.py

    # 真正写文件、压视频
    python tools/build_site.py --execute [--crf 26] [--force-videos]

必须用 Neural-IPC 的 genesis 环境跑（要用它自带的 imageio-ffmpeg 二进制）：
    /data/xiaoyingwang/projects/Neural-IPC/.conda/genesis/bin/python tools/build_site.py

数据来源（只读，不修改）：
  demo : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_demos/<目录>/run_info.json、*.mp4、momentum_plot.png
         字段由 Neural-IPC/tools/ipc_demos/run_uipc_sample_headless.py:276-286 和
         run_genesis_ipc_example.py:655-664 写出。
  扫描 : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_sweep/<扫描>/<档位>.json
         字段由 Neural-IPC/tools/ipc_sweep/sweep.py 写出（summary 见 sweep.py:537-562）。
  档位清单 : Neural-IPC/tools/ipc_sweep/configs.py（纯数据文件，按路径加载，不写 __pycache__）。

原则：
  * 结果缺失、报错、半截文件如实显示，不编造、不补占位数值。
  * 表里的数都来自 json 原字段；唯一的派生量是"逐帧均值"（对 frames[*] 原值求算术平均），
    页面上标明了是本脚本算的。显示时浮点数保留 4 位有效数字，单元格的 title 里是 json 原值。
  * 每个压好的视频超过 10 MB、或全部视频加起来超过 60 MB，就报错停止，不写页面
    （Artifact 附属文件上限每个 15 MB，留余量）。
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
# demo 清单（说明文字摘自 Neural-IPC/run_commands.txt [IPC demo 命令] 区块，是"计划跑什么"，不是结果）
# 目录名规则：run_uipc_sample_headless.py:102-115 resolve_script（benchmark 为 <目录>_run），
#             run_genesis_ipc_example.py:616（genesis_<example>）
# --------------------------------------------------------------------------
DEMOS = [
    # key(=NAS 目录名), 来源, run_commands 编号, 是否应出视频, 说明
    ("0_check_libuipc", "libuipc-samples", 4, False,
     "打印版本 / constitution / 单位，并初始化一次 cuda Engine（无 GUI，不出视频）"),
    ("1_hello_libuipc", "libuipc-samples", 5, True,
     "两个 ABD 四面体下落接触，EGL 离屏 300 帧（dt=0.02，每帧 advance 1 次）"),
    ("10_ramp_sliding", "libuipc-samples", 6, True,
     "8 个 ABD 方块在斜坡上滑，friction 0 ~ 1 八档（dt=0.01, d_hat=0.01），300 帧"),
    ("13_init_velocity", "libuipc-samples", 7, True,
     "ABD 方块 + FEM 方块带 z 向 1 m/s 初速度（dt=0.02，摩擦开），300 帧"),
    ("20_contact_system_feature", "libuipc-samples", 8, True,
     "ABD 方块 + FEM 方块 + 地面，读出各类 contact primitive 的 energy / gradient / Hessian；"
     "300 帧（官方回调每帧 advance 2 次）"),
    ("27_compute_mesh_d_hat", "libuipc-samples", 9, True,
     "按网格分辨率自动算 d_hat（上方块）vs 手设 0.01（下方块），300 帧（dt=0.01）"),
    ("89_mas_bunny", "libuipc-samples", 10, True,
     "FEM bunny 落地，MAS preconditioner（dt=0.01），300 帧"),
    ("90_abd_fem_cube_stack", "libuipc-samples", 11, True,
     "4x4 网格、8 层 ABD / FEM 交替方块叠落（dt=0.01），300 帧"),
    ("abd_bunny_grid_drop_run", "libuipc-samples benchmark", 12, False,
     "10x10 个 ABD bunny 下落，两种 broadphase 各 100 帧、各起一个子进程；stats / timer 写进 workspace/（无 GUI）"),
    ("wrecking_balls_run", "libuipc-samples benchmark", 13, False,
     "wrecking_ball.json 场景（ABD 方块 / 球 / 链节 + 地面），两种方法各 300 帧（无 GUI）"),
    ("genesis_ipc_objects_falling", "Genesis examples/ipc", 14, True,
     "布料 + 刚体盒子 + FEM 软球落到地面，100 步（dt=0.02），离屏相机 50 fps"),
    ("genesis_ipc_momentum", "Genesis examples/ipc", 15, True,
     "零重力下刚体方块以 4 m/s 撞 FEM 球，检验动量守恒（dt=0.001，300 步）；"
     "realtime_factor 0.05 只改录像节奏"),
    ("genesis_ipc_robot_grasp_cube", "Genesis examples/ipc", 16, True,
     "Franka 用 IK + PD 抓起 FEM 软方块（dt=0.01，two_way_soft_constraint，410 步），50 fps"),
]

# 首页"本周做了什么"。每条都注明出处；要改就改这里。
PROJECT_ONE_LINER = ("只暴露少量 coarse 自由度（rigid / affine body / ROM），碰撞导致的局部形变交给神经网络学出的"
                     "额外碰撞能量项：低自由度 + Neural IPC ≈ 高自由度 + IPC，要快，同时和 IPC 一样准。")
PROJECT_ONE_LINER_SRC = "Neural-IPC/AGENTS.md「项目目标」"
WEEKLY = [
    ("查清仿真框架：notes 里的 “lib IPC” 就是 libuipc（Python 包 pyuipc），Genesis 的 IPC Coupler 底层也是它；"
     "整理了 Genesis 暴露的 IPC 参数（dt、contact_d_hat、contact_friction_mu、ipc_constraint_strength 等）。",
     "AGENTS.md「仿真框架事实」"),
    ("建好 conda 环境 .conda/genesis：python 3.11、genesis-world 1.4.2、pyuipc 0.0.28、polyscope 2.6.1。",
     "tools/ipc_demos/README.md"),
    ("写了在无显示器服务器上离屏跑官方 demo 的外壳脚本（官方文件一字不改）：libuipc-samples 10 个、Genesis examples/ipc 3 个；"
     "启动时核对 EGL 设备和 CUDA_VISIBLE_DEVICES 是同一张卡。",
     "tools/ipc_demos/README.md、run_commands.txt [IPC demo 命令]"),
    ("设计了 “一堆物体扔进盒子” 场景（8 个 FEM 软球落进开口盒子）上的 IPC 参数单变量扫描：d_hat、dt、friction、"
     "resistance、初始穿透、网格粗细，共 7 个扫描 29 个配置；穿透用 libuipc 自带的 SanityChecker 判定。",
     "tools/ipc_sweep/README.md、configs.py"),
    ("做了这个网页：demo 视频 + 参数扫描结果表，所有数字都从 NAS 上的 json 原样读取。",
     "Neural-IPC-web/tools/build_site.py"),
]

PAGE_TITLE = "Neural-IPC Demos"
# 页内锚点：只用纯字母
NAV = [("overview", "总览"), ("weekly", "本周"), ("demos", "Demo 画廊"), ("sweep", "参数扫描")]


# ==========================================================================
# 通用小工具
# ==========================================================================
def esc(x):
    return html.escape(str(x), quote=True)


def rel_out(p):
    """NAS 路径显示成相对 outputs/ 的短形式（来源标注用）。"""
    p = Path(p)
    try:
        return "outputs/" + str(p.relative_to(OUT_ROOT))
    except ValueError:
        return str(p)


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


def raw_repr(v):
    return json.dumps(v, ensure_ascii=False, default=str)


def fmt(v):
    """显示用字符串：浮点保留 4 位有效数字，其余原样。"""
    if v is MISSING:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return str(v)
        return f"{v:.4g}"
    if v is None:
        return "null"
    if isinstance(v, (list, dict)):
        return raw_repr(v)
    return str(v)


def num_span(v, src):
    """一个数值 + 来源（title 里是来源和 json 原值）。"""
    if v is MISSING:
        return f'<span class="na" title="来源：{esc(src)}（字段不存在）">—</span>'
    return f'<span class="num" title="来源：{esc(src)}&#10;原值：{esc(raw_repr(v))}">{esc(fmt(v))}</span>'


def file_size_str(n):
    return f"{n / 1024 / 1024:.2f} MB"


def mtime_str(p):
    return datetime.datetime.fromtimestamp(Path(p).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


# ==========================================================================
# demo 结果收集
# ==========================================================================
def collect_demo(key, source, cmd_no, expects_video, desc):
    d = DEMO_ROOT / key
    info_path = d / "run_info.json"
    r = {"key": key, "source": source, "cmd_no": cmd_no, "expects_video": expects_video, "desc": desc,
         "dir": d, "info_path": info_path, "info": None, "state": None, "reason": None,
         "video_src": None, "video_note": None, "images": []}
    if not d.is_dir():
        r["state"], r["reason"] = "not_run", f"尚未运行：NAS 上没有目录 {d}"
        return r
    info, err = load_json(info_path)
    if err:
        r["state"], r["reason"] = "failed", f"运行失败：run_info.json 无法解析（{err}）"
        return r
    if info is None:
        r["state"] = "failed"
        r["reason"] = ("运行失败或尚未结束：目录已存在但没有 run_info.json"
                       "（两个外壳脚本只在整个 demo 跑完、视频写好之后才写 run_info.json；失败原因要看运行日志）")
        return r
    r["info"], r["state"] = info, "ok"

    # 视频：以 run_info.json 里的 "video" 字段为准
    v = info.get("video", MISSING)
    if v is MISSING:
        r["video_note"] = "run_info.json 没有 video 字段"
    elif v is None:
        n = info.get("screenshots", MISSING)
        r["video_note"] = (f"没有视频：run_info.json 里 video = null（screenshots = {fmt(n)}）"
                           + ("；此 demo 本来就不开 GUI、不出视频" if not expects_video else ""))
    else:
        vp = Path(v)
        if vp.is_file() and vp.stat().st_size > 0:
            r["video_src"] = vp
        else:
            r["video_note"] = f"run_info.json 记录的视频文件不存在或为空：{vp}"

    # 额外产物：ipc_momentum 的 momentum_plot.png（路径以 run_info.run.plot 为准）
    plot = dig(info, "run", "plot")
    if isinstance(plot, str):
        pp = Path(plot)
        if pp.is_file():
            r["images"].append((pp, f"{key}__{pp.name}", "run_info.json → run.plot"))
        else:
            r["images_note"] = f"run_info.json 的 run.plot 指向的文件不存在：{pp}"
    return r


def collect_demos():
    known = {k for k, *_ in DEMOS}
    demos = [collect_demo(*spec) for spec in DEMOS]
    extras = []
    if DEMO_ROOT.is_dir():
        for p in sorted(DEMO_ROOT.iterdir()):
            if p.is_dir() and p.name not in known:
                extras.append(collect_demo(p.name, "未登记（NAS 上多出来的目录）", None, None,
                                           "build_site.py 的 DEMOS 清单里没有这个目录"))
    return demos, extras


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
        r["state"], r["reason"] = "failed", f"运行失败：json 无法解析（{err}）"
        return r
    if data is None:
        r["state"], r["reason"] = "not_run", "尚未运行"
        return r
    r["data"] = data
    st = data.get("status", MISSING)
    has_summary = "summary" in data
    if st == "ok":
        r["state"], r["reason"] = "ok", "完成（status = ok）"
    elif st == "exception":
        tb = data.get("exception_traceback") or ""
        last = [ln for ln in tb.strip().splitlines() if ln.strip()]
        r["state"] = "failed"
        r["reason"] = "运行失败：" + (last[-1].strip() if last else "status = exception，但 exception_traceback 为空")
    elif st == "init_invalid":
        r["state"], r["reason"] = "failed", "运行失败：world.init 之后 world.is_valid() 为 False（status = init_invalid）"
    elif st == "invalid_during_run":
        r["state"], r["reason"] = "failed", "运行失败：仿真中途 world.is_valid() 变为 False（status = invalid_during_run）"
    elif st == "nonfinite_positions":
        r["state"], r["reason"] = "failed", "运行失败：球顶点坐标出现非有限值（status = nonfinite_positions）"
    elif st in ("starting", "initializing", "running") and not has_summary:
        r["state"] = "unfinished"
        r["reason"] = (f"未结束：status = {st} 且没有 summary（进程可能还在跑，也可能已崩溃留下半截文件；"
                       f"看 {rel_out(jp.with_suffix('.log'))}）")
    else:
        r["state"], r["reason"] = "failed", f"未知状态：status = {fmt(st)}"
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
        # NAS 上有、configs.py 里没有的档位
        sd = SWEEP_ROOT / sweep
        if sd.is_dir():
            for jp in sorted(sd.glob("*.json")):
                if (sweep, jp.stem) not in known:
                    row = collect_level(sweep, jp.stem, MISSING)
                    row["extra"] = True
                    rows.append(row)
                    known.add((sweep, jp.stem))
        sweeps.append({"name": sweep, "why": spec.get("why", ""), "rows": rows})
    # 整个扫描目录都不在 configs.py 里
    if SWEEP_ROOT.is_dir():
        for sd in sorted(SWEEP_ROOT.iterdir()):
            if sd.is_dir() and sd.name not in cfg.SWEEPS:
                rows = []
                for jp in sorted(sd.glob("*.json")):
                    row = collect_level(sd.name, jp.stem, MISSING)
                    row["extra"] = True
                    rows.append(row)
                if rows:
                    sweeps.append({"name": sd.name, "why": "configs.py 里没有这个扫描（NAS 上多出来的目录）",
                                   "rows": rows})
    return cfg, sweeps


# ==========================================================================
# 视频 / 图片计划
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
# HTML
# ==========================================================================
STATE_LABEL = {"ok": ("完成", "ok"), "failed": ("失败", "bad"), "not_run": ("尚未运行", "idle"),
               "unfinished": ("未结束", "warn")}


def badge(state):
    label, cls = STATE_LABEL.get(state, (state, "idle"))
    return f'<span class="badge {cls}">{esc(label)}</span>'


# 全部内联；颜色只用变量，亮 / 暗两套；不引外部字体（系统字体栈，含中文后备）
PAGE_CSS = """
:root {
  --bg: #fbfbfa; --surface: #ffffff; --fg: #1d2127; --muted: #5d6570; --border: #d9dde2;
  --accent: #1f5f9e; --code-bg: #eef1f4; --row-alt: #f5f7f9;
  --ok-bg: #e2f2e6; --ok-fg: #1d6b35; --bad-bg: #fbe4e2; --bad-fg: #9b2a22;
  --warn-bg: #fbf0d9; --warn-fg: #7a5410; --idle-bg: #eceef1; --idle-fg: #535a64;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
          "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15181c; --surface: #1c2025; --fg: #e3e6ea; --muted: #9aa3ad; --border: #343a42;
    --accent: #7db3e8; --code-bg: #262b31; --row-alt: #1f2328;
    --ok-bg: #1d3a26; --ok-fg: #8fd6a4; --bad-bg: #432322; --bad-fg: #f0a39b;
    --warn-bg: #3d3119; --warn-fg: #e9c47a; --idle-bg: #2a2f35; --idle-fg: #aab2bb;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #15181c; --surface: #1c2025; --fg: #e3e6ea; --muted: #9aa3ad; --border: #343a42;
  --accent: #7db3e8; --code-bg: #262b31; --row-alt: #1f2328;
  --ok-bg: #1d3a26; --ok-fg: #8fd6a4; --bad-bg: #432322; --bad-fg: #f0a39b;
  --warn-bg: #3d3119; --warn-fg: #e9c47a; --idle-bg: #2a2f35; --idle-fg: #aab2bb;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { overflow-x: hidden; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--font);
       font-size: 15px; line-height: 1.6; }
.wrap { max-width: 1100px; margin: 0 auto; padding-left: 16px; padding-right: 16px;
        padding-block: 0; }
header.top { border-bottom: 1px solid var(--border); background: var(--surface); }
header.top .wrap { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 20px;
                   padding-block: 12px; }
.brand { font-weight: 600; }
header.top nav { display: flex; flex-wrap: wrap; gap: 4px 16px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
main.wrap { padding-block: 8px 32px; }
h1 { font-size: 1.5rem; margin: 24px 0 8px; font-weight: 600; }
h2 { font-size: 1.2rem; margin: 32px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border);
     font-weight: 600; }
h3 { font-size: 1rem; margin: 0 0 4px; font-weight: 600; word-break: break-all; }
section { scroll-margin-top: 12px; }
p { margin: 8px 0; }
.lead { color: var(--fg); }
.src, .meta, .reason, .why, .counts { color: var(--muted); font-size: 0.85rem; }
code { font-family: var(--mono); font-size: 0.85em; background: var(--code-bg); padding: 0 3px;
       border-radius: 3px; word-break: break-all; }
ul { padding-left: 20px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 12px 0; }
.stat { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; }
.stat .v { font-size: 1.4rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.stat .l { color: var(--muted); font-size: 0.85rem; }
.tablewrap { overflow-x: auto; max-width: 100%; margin: 8px 0; border: 1px solid var(--border);
             border-radius: 6px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { text-align: left; vertical-align: top; padding: 6px 10px; border-bottom: 1px solid var(--border); }
thead th { background: var(--row-alt); font-weight: 600; white-space: nowrap; }
tbody tr:nth-child(even) td { background: var(--row-alt); }
td { font-variant-numeric: tabular-nums; }
.num { font-variant-numeric: tabular-nums; font-family: var(--mono); font-size: 0.95em; }
.na { color: var(--muted); }
table.sweep td.lvl { min-width: 160px; }
.ov { font-family: var(--mono); font-size: 0.8rem; color: var(--muted); word-break: break-all; }
table.kv th { width: 40%; font-weight: normal; color: var(--muted); }
table.kv td.src { width: 25%; }
.badge { display: inline-block; font-size: 0.75rem; line-height: 1.5; padding: 0 6px; border-radius: 3px;
         white-space: nowrap; vertical-align: middle; }
.badge.ok { background: var(--ok-bg); color: var(--ok-fg); }
.badge.bad { background: var(--bad-bg); color: var(--bad-fg); }
.badge.warn { background: var(--warn-bg); color: var(--warn-fg); }
.badge.idle { background: var(--idle-bg); color: var(--idle-fg); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 460px), 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 12px;
        min-width: 0; }
.desc { margin: 4px 0 8px; }
video, img { display: block; max-width: 100%; height: auto; background: var(--code-bg); border-radius: 4px; }
video { width: 100%; }
figure { margin: 8px 0; }
figcaption { color: var(--muted); font-size: 0.8rem; }
.novideo { padding: 16px 12px; background: var(--idle-bg); color: var(--idle-fg); border-radius: 4px;
           font-size: 0.88rem; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); font-size: 0.9rem; }
nav.toc { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 0.9rem; margin: 8px 0; }
.note { border-left: 3px solid var(--border); padding: 4px 12px; color: var(--muted); font-size: 0.88rem; }
footer.foot { color: var(--muted); font-size: 0.8rem; border-top: 1px solid var(--border);
              padding-block: 12px 24px; }
@media (max-width: 480px) {
  body { font-size: 14px; }
  th, td { padding: 5px 7px; }
}
"""


def page(body, gen_time):
    """Artifact 页面：开头直接是 <title> 和 <style>，不写 doctype / html / head / body。"""
    nav = "".join(f'<a href="#{href}">{esc(name)}</a>' for href, name in NAV)
    return f"""<title>{esc(PAGE_TITLE)}</title>
<style>{PAGE_CSS}</style>
<header class="top">
  <div class="wrap">
    <div class="brand">Neural-IPC</div>
    <nav>{nav}</nav>
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="wrap foot">
  <p>本页由 <code>tools/build_site.py</code> 于 {esc(gen_time)} 生成。数据只读自
  <code>{esc(DEMO_ROOT)}</code> 与 <code>{esc(SWEEP_ROOT)}</code>；
  缺失或失败的结果如实标出，没有任何占位数值。鼠标悬停在数字上可以看到它来自哪个 json 的哪个字段以及 json 原值。</p>
</footer>
"""


def letters(s):
    """页内锚点只用纯字母。"""
    return "".join(ch for ch in s.lower() if "a" <= ch <= "z")


def demo_video_html(r):
    if r["state"] != "ok":
        return f'<div class="novideo">{esc(r["reason"])}</div>'
    if r.get("video_web"):
        job = r["video_job"]
        return (f'<video controls muted playsinline preload="metadata" src="{esc(r["video_web"])}"></video>'
                f'<p class="src">视频原片：<code>{esc(job["src"])}</code>（{file_size_str(job["src_size"])}，'
                f'来源 run_info.json → video）</p>')
    return f'<div class="novideo">{esc(r["video_note"] or "没有视频")}</div>'


def kv_rows(info, src_file, items):
    """items: [(标签, key 链)]，逐行显示值和来源。"""
    out = []
    for label, keys in items:
        v = dig(info, *keys)
        field = ".".join(str(k) for k in keys)
        src = f"{src_file} → {field}"
        out.append(f"<tr><th>{esc(label)}</th><td>{num_span(v, src)}</td>"
                   f'<td class="src">{esc(field)}</td></tr>')
    return "\n".join(out)


UIPC_FIELDS = [
    ("示例脚本", ("script",)),
    ("转给示例的参数", ("script_args",)),
    ("GUI 帧数（请求）", ("frames_requested",)),
    ("用户回调执行次数", ("callback_calls",)),
    ("开始按钮已触发", ("button_fired",)),
    ("截图张数", ("screenshots",)),
    ("视频 fps", ("fps",)),
    ("渲染 buffer 尺寸", ("buffer_size",)),
    ("耗时 wall_seconds（s，runpy 整段，含截图）", ("wall_seconds",)),
    ("CUDA_VISIBLE_DEVICES", ("CUDA_VISIBLE_DEVICES",)),
    ("nvidia-smi 卡号", ("gpu_guard", "nvsmi_index")),
    ("EGL 设备编号", ("gpu_guard", "egl_index")),
    ("PID 核对", ("pid_checks",)),
]
GENESIS_FIELDS = [
    ("官方文件", ("official_file",)),
    ("gs.init 出处", ("gs_init",)),
    ("dt", ("dt",)),
    ("仿真步数", ("run", "steps")),
    ("录像 fps（请求）", ("fps_requested",)),
    ("realtime_factor 覆盖", ("realtime_factor_override",)),
    ("分辨率", ("res",)),
    ("耗时 sim_wall_seconds（s，run() 整段，含录像）", ("sim_wall_seconds",)),
    ("CUDA_VISIBLE_DEVICES", ("CUDA_VISIBLE_DEVICES",)),
    ("EGL_DEVICE_ID", ("EGL_DEVICE_ID",)),
    ("nvidia-smi 卡号", ("gpu_guard", "nvsmi_index")),
    ("PID 核对", ("pid_checks",)),
]


def vram_html(info, src_file):
    """两个外壳脚本都不记录显存；若以后 run_info 里出现含 mem / vram 的顶层字段就原样显示。"""
    keys = [k for k in info if "mem" in k.lower() or "vram" in k.lower()]
    if not keys:
        return (f'<tr><th>显存</th><td><span class="na">未记录</span></td>'
                f'<td class="src">{esc(src_file)} 里没有显存字段（外壳脚本不记录显存）</td></tr>')
    return "\n".join(f"<tr><th>显存（{esc(k)}）</th><td>{num_span(info[k], f'{src_file} → {k}')}</td>"
                     f'<td class="src">{esc(k)}</td></tr>' for k in keys)


def demo_card(r):
    head = (f'<h3>{esc(r["key"])} {badge(r["state"])}</h3>'
            f'<p class="meta">{esc(r["source"])}'
            + (f' · run_commands.txt 第 {r["cmd_no"]} 条' if r["cmd_no"] else "") + "</p>"
            f'<p class="desc">{esc(r["desc"])}</p>')
    body = demo_video_html(r)
    if r["state"] == "ok":
        info = r["info"]
        src_file = rel_out(r["info_path"])
        fields = GENESIS_FIELDS if "example" in info else UIPC_FIELDS
        rows = kv_rows(info, src_file, fields)
        if r["key"] == "genesis_ipc_momentum":
            rows += "\n" + kv_rows(info, src_file, [("末步相对动量误差", ("run", "final_rel_momentum_error"))])
        if "rerouted_subprocesses" in info:
            n = len(info["rerouted_subprocesses"] or [])
            rows += (f'\n<tr><th>改走外壳的子进程数</th><td>{num_span(n, src_file + " → len(rerouted_subprocesses)")}</td>'
                     f'<td class="src">len(rerouted_subprocesses)</td></tr>')
        rows += "\n" + vram_html(info, src_file)
        imgs = "".join(
            f'<figure><img src="{esc(web)}" alt="{esc(src.name)}" loading="lazy">'
            f'<figcaption>{esc(src.name)}（原图 <code>{esc(src)}</code>，来源 {esc(label)}）</figcaption></figure>'
            for web, src, label in r.get("images_web", []))
        if r.get("images_note"):
            imgs += f'<div class="novideo">{esc(r["images_note"])}</div>'
        body += (imgs + f'<details><summary>参数 / 耗时 / 显存（来源 <code>{esc(src_file)}</code>，'
                 f'文件时间 {esc(mtime_str(r["info_path"]))}）</summary>'
                 f'<div class="tablewrap"><table class="kv"><thead><tr><th>项</th><th>值</th><th>json 字段</th></tr></thead>'
                 f"<tbody>{rows}</tbody></table></div></details>")
    return f'<article class="card">{head}{body}</article>'


def demos_section(demos, extras):
    parts = ['<section id="demos"><h2>Demo 画廊</h2>',
             '<p class="lead">官方示例原样跑（libuipc-samples + Genesis examples/ipc），离屏录像。'
             '视频已压成 H.264 / 720p，原片在 NAS。每个 demo 下面折叠的表是 run_info.json 的原始字段。</p>',
             '<div class="grid">']
    parts += [demo_card(r) for r in demos]
    parts.append("</div>")
    if extras:
        parts.append('<h3 style="margin-top:16px">NAS 上多出来的 demo 目录</h3><div class="grid">')
        parts += [demo_card(r) for r in extras]
        parts.append("</div>")
    parts.append("</section>")
    return "\n".join(parts)


# ---------------- 扫描表 ----------------
# 每个扫描额外显示的"实际生效值"（json 字段）
ACTUAL_FIELDS = {
    "baseline": [("dt", ("params", "dt")), ("d_hat", ("params", "d_hat")), ("L", ("params", "mean_surface_edge_L")),
                 ("d_hat/L", ("params", "d_hat_over_L"))],
    "d_hat": [("d_hat", ("params", "d_hat")), ("d_hat/L", ("params", "d_hat_over_L"))],
    "dt": [("dt", ("params", "dt")), ("n_frames", ("params", "n_frames"))],
    "friction": [("friction", ("params", "contact_model_used", "friction_rate")),
                 ("resistance", ("params", "contact_model_used", "resistance"))],
    "resistance": [("resistance", ("params", "contact_model_used", "resistance")),
                   ("friction", ("params", "contact_model_used", "friction_rate"))],
    "init_penetration": [("球心距", ("params", "pair_center_distance", "center_distance")),
                         ("名义球面间隙", ("params", "pair_center_distance", "nominal_sphere_gap")),
                         ("sanity_check.enable", ("params", "sanity_check", "enable"))],
    "mesh_res": [("顶点数/球", ("mesh", "n_vertices")), ("四面体数/球", ("mesh", "n_tets")),
                 ("L", ("mesh", "edge_len_mean")), ("d_hat/L", ("params", "d_hat_over_L"))],
}

COLUMN_SOURCES = [
    ("档位", "json 文件名；overrides 来自 json → overrides（没跑时来自 configs.py）"),
    ("实际生效值", "json → params.* / mesh.*（见单元格 title）"),
    ("初始 sanity", "json → sanity_at_init.result / .penetration / .too_close"),
    ("运行中穿透", "json → summary.n_checks_with_penetration / summary.n_sanity_checks；"
                 "首次穿透帧 summary.first_penetration_frame"),
    ("Newton（Timer）", "均值 = frames[*].newton_iter_timer_count 的算术平均（build_site.py 计算）；"
                       "中位数 summary.newton_iter_timer_median；最大 summary.newton_iter_timer_max"),
    ("Newton（frame_stats）", "中位数 summary.newton_iter_frame_stats_median；最大 summary.newton_iter_frame_stats_max；"
                             "撞上限帧数 summary.n_frames_hit_max_iter"),
    ("每帧耗时 (s)", "均值 = summary.wall_seconds_total / summary.frames_done（build_site.py 计算）；"
                  "中位数 summary.wall_seconds_median；最大 summary.wall_seconds_max"),
    ("帧数", "summary.frames_done / params.n_frames"),
    ("状态", "json → status；失败时取 exception_traceback 最后一行原文"),
]


def mean_of(frames, key):
    vals = [f.get(key) for f in frames if isinstance(f, dict)]
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return MISSING
    return sum(vals) / len(vals)


def sweep_row(row):
    d = row["data"]
    jsrc = rel_out(row["json_path"])
    ov = d.get("overrides", MISSING) if d else row["ov_cfg"]
    ov_src = f"{jsrc} → overrides" if d else f"configs.py → SWEEPS[{row['sweep']!r}].levels[{row['level']!r}]"
    ov_txt = "（无，全默认）" if ov == {} else ("—" if ov is MISSING else raw_repr(ov))
    level_cell = (f'<td class="lvl"><b>{esc(row["level"])}</b>'
                  + (' <span class="badge warn">configs.py 里没有</span>' if row.get("extra") else "")
                  + f'<div class="ov" title="来源：{esc(ov_src)}">{esc(ov_txt)}</div>'
                  f'<div class="src">{esc(jsrc)}</div></td>')
    status_cell = f'<td>{badge(row["state"])}<div class="reason">{esc(row["reason"])}</div></td>'
    if d is None:
        return (f"<tr>{level_cell}" + '<td class="na">—</td>' * 7 + status_cell + "</tr>")

    def n(*keys):
        return num_span(dig(d, *keys), f"{jsrc} → {'.'.join(str(k) for k in keys)}")

    actual = "<br>".join(f"{esc(lbl)} = {n(*keys)}" for lbl, keys in ACTUAL_FIELDS.get(row["sweep"], []))
    if not actual:
        actual = f"dt = {n('params', 'dt')}<br>d_hat = {n('params', 'd_hat')}"

    if "sanity_at_init" in d:
        sanity = (f"{n('sanity_at_init', 'result')}<br>穿透 {n('sanity_at_init', 'penetration')}"
                  f"<br>too_close {n('sanity_at_init', 'too_close')}")
    else:
        sanity = '<span class="na">json 无 sanity_at_init</span>'

    s = d.get("summary")
    if not s:  # 没有 summary，或 summary = {}（一帧都没跑）
        why = "没有 summary" if s is None else "summary 为空（一帧都没跑）"
        empty = f'<td class="na" colspan="5">{esc(why)}</td>'
        return f"<tr>{level_cell}<td>{actual}</td><td>{sanity}</td>{empty}{status_cell}</tr>"

    pen = (f"{n('summary', 'n_checks_with_penetration')} / {n('summary', 'n_sanity_checks')} 次检查"
           f"<br>首次 {n('summary', 'first_penetration_frame')}")
    frames = d.get("frames") or []
    nt_mean = mean_of(frames, "newton_iter_timer_count")
    newton_t = (f"均 {num_span(nt_mean, jsrc + ' → mean(frames[*].newton_iter_timer_count)，build_site.py 计算')}"
                f"<br>中 {n('summary', 'newton_iter_timer_median')}<br>大 {n('summary', 'newton_iter_timer_max')}")
    if dig(d, "summary", "frame_stats_available") is True:
        newton_f = (f"中 {n('summary', 'newton_iter_frame_stats_median')}"
                    f"<br>大 {n('summary', 'newton_iter_frame_stats_max')}")
    else:
        newton_f = ('<span class="na" title="来源：' + esc(jsrc) + ' → summary.frame_stats_available">'
                    "frame_stats 无 newton_iterations</span>")
    newton_f += f"<br>撞上限 {n('summary', 'n_frames_hit_max_iter')} 帧"
    tot, done = dig(d, "summary", "wall_seconds_total"), dig(d, "summary", "frames_done")
    w_mean = (tot / done) if isinstance(tot, (int, float)) and isinstance(done, int) and done > 0 else MISSING
    wall = (f"均 {num_span(w_mean, jsrc + ' → summary.wall_seconds_total / summary.frames_done，build_site.py 计算')}"
            f"<br>中 {n('summary', 'wall_seconds_median')}<br>大 {n('summary', 'wall_seconds_max')}")
    nfr = f"{n('summary', 'frames_done')} / {n('params', 'n_frames')}"
    return (f"<tr>{level_cell}<td>{actual}</td><td>{sanity}</td><td>{pen}</td><td>{newton_t}</td>"
            f"<td>{newton_f}</td><td>{wall}</td><td>{nfr}</td>{status_cell}</tr>")


def sweep_counts(rows):
    c = {"ok": 0, "failed": 0, "not_run": 0, "unfinished": 0}
    for r in rows:
        c[r["state"]] = c.get(r["state"], 0) + 1
    return c


def sweep_anchor(name):
    return "sweep" + letters(name)


def sweep_section(sweeps):
    heads = "".join(f"<th>{esc(h)}</th>" for h, _ in COLUMN_SOURCES)
    legend = "".join(f"<li><b>{esc(h)}</b>：{esc(s)}</li>" for h, s in COLUMN_SOURCES)
    parts = ['<section id="sweep"><h2>IPC 参数扫描</h2>',
             '<p class="lead">场景：8 个 FEM 软球（StableNeoHookean，libuipc 默认 E = 120 kPa、ν = 0.49，'
             "2 层 × 2×2，R = 0.1 m）从静止落进开口盒子（ground + 4 面固定 ABD 墙），物理时间 2 s。"
             "单变量扫描：每个档位只改一个量，其余全部是 libuipc 默认值（dt = 0.01 s，d̂ = contact.d_hat = 0.01 m，"
             "newton.max_iter = 1024）。d̂ 是 barrier 的作用距离：只有距离 d &lt; d̂ 的基元对才产生接触能量；"
             "d̂ 扫描以球表面平均边长 L 为单位。</p>",
             '<p class="note">穿透由 libuipc 自带的 SanityChecker 判定（world.init 后查一次，之后每 10 帧查一次，'
             "末帧必查）。消息含 “intersects with” 或 “distance &lt;= 0” 记为穿透，“too close” 单列，不算穿透。"
             "这是离散时刻的检查，两次检查之间短暂穿透又分开的情况查不到。"
             "Newton 迭代次数有两个来源：Timer 里 “Newton Iteration” 节点的 count，和 engine.frame_stats() 的 "
             "newton_iterations；两者都原样列出。每帧耗时是 Python 侧 perf_counter 包住 advance() + retrieve() 测的"
             "（不含 sanity check），单位 s。</p>",
             f'<p class="src">场景常量与档位来自 <code>{esc(SWEEP_CONFIGS)}</code>；'
             f"结果来自 <code>{esc(SWEEP_ROOT)}/&lt;扫描&gt;/&lt;档位&gt;.json</code>。"
             "表中浮点数保留 4 位有效数字，悬停看 json 原值和字段名。</p>",
             '<details class="legend"><summary>每一列的数据来源</summary><ul>' + legend + "</ul></details>",
             '<nav class="toc">' + "".join(f'<a href="#{sweep_anchor(s["name"])}">{esc(s["name"])}</a>'
                                           for s in sweeps) + "</nav>"]
    for s in sweeps:
        c = sweep_counts(s["rows"])
        parts.append(f'<div id="{sweep_anchor(s["name"])}" style="scroll-margin-top:12px">'
                     f'<h3 style="margin-top:24px">{esc(s["name"])}</h3>'
                     f'<p class="counts">完成 {c["ok"]} · 失败 {c["failed"]} · 未结束 {c["unfinished"]} · '
                     f'尚未运行 {c["not_run"]}（共 {len(s["rows"])} 档，按 json 的 status 统计）</p>'
                     f'<p class="why">选档理由（configs.py → why）：{esc(s["why"])}</p>'
                     f'<div class="tablewrap"><table class="sweep"><thead><tr>{heads}</tr></thead><tbody>'
                     + "\n".join(sweep_row(r) for r in s["rows"])
                     + "</tbody></table></div></div>")
    parts.append("</section>")
    return "\n".join(parts)


# ---------------- 整页 ----------------
def build_page(demos, extras, sweeps, gen_time):
    all_demos = demos + extras
    dc = sweep_counts(all_demos)
    n_video = sum(1 for r in all_demos if r.get("video_web"))
    rows_all = [r for s in sweeps for r in s["rows"]]
    sc = sweep_counts(rows_all)
    weekly = "".join(f'<li>{esc(t)} <span class="src">（出处：{esc(src)}）</span></li>' for t, src in WEEKLY)

    demo_rows = []
    for r in all_demos:
        if r["state"] == "ok":
            info = r["info"]
            k = "sim_wall_seconds" if "example" in info else "wall_seconds"
            t = num_span(dig(info, k), f"{rel_out(r['info_path'])} → {k}")
        else:
            t = '<span class="na">—</span>'
        vid = "有" if r.get("video_web") else ("—" if r["state"] != "ok" else "无")
        demo_rows.append(f'<tr><td>{esc(r["key"])}</td>'
                         f'<td>{esc(r["source"])}</td><td>{badge(r["state"])}'
                         f'{"" if r["state"] == "ok" else "<div class=reason>" + esc(r["reason"]) + "</div>"}</td>'
                         f"<td>{vid}</td><td>{t}</td></tr>")
    sweep_rows = []
    for s in sweeps:
        c = sweep_counts(s["rows"])
        sweep_rows.append(f'<tr><td><a href="#{sweep_anchor(s["name"])}">{esc(s["name"])}</a></td>'
                          f'<td>{len(s["rows"])}</td><td>{c["ok"]}</td><td>{c["failed"]}</td>'
                          f'<td>{c["unfinished"]}</td><td>{c["not_run"]}</td></tr>')

    body = f"""
<section id="overview">
<h1>Neural-IPC 本周 demo 汇总</h1>
<p class="lead">{esc(PROJECT_ONE_LINER)} <span class="src">（出处：{esc(PROJECT_ONE_LINER_SRC)}）</span></p>
</section>

<section id="weekly">
<h2>本周做了什么</h2>
<ul class="weekly">{weekly}</ul>
</section>

<section id="progress">
<h2>结果进度</h2>
<div class="stats">
  <div class="stat"><div class="v">{dc['ok']} / {len(all_demos)}</div><div class="l">demo 已跑完（有 run_info.json）</div></div>
  <div class="stat"><div class="v">{n_video}</div><div class="l">demo 有视频</div></div>
  <div class="stat"><div class="v">{sc['ok']} / {len(rows_all)}</div><div class="l">扫描配置 status = ok</div></div>
  <div class="stat"><div class="v">{sc['failed']}</div><div class="l">扫描配置失败</div></div>
</div>
<p class="src">以上计数由 build_site.py 统计：demo 看 {esc(DEMO_ROOT)}/&lt;目录&gt;/run_info.json 是否存在，
扫描看每个 json 的 status 字段。</p>
</section>

<section id="summary">
<h2>Demo 一览</h2>
<div class="tablewrap"><table>
<thead><tr><th>demo</th><th>来源</th><th>状态</th><th>视频</th><th>耗时 (s)</th></tr></thead>
<tbody>{''.join(demo_rows)}</tbody></table></div>
<p class="src">耗时：libuipc 示例取 run_info.json → wall_seconds（runpy 整段，含截图）；
Genesis 示例取 run_info.json → sim_wall_seconds（run() 整段，含录像）。两者口径不同，不能直接比较。
<a href="#demos">看视频和完整参数</a></p>
<h3 style="margin-top:20px">参数扫描一览</h3>
<div class="tablewrap"><table>
<thead><tr><th>扫描</th><th>档位数</th><th>完成</th><th>失败</th><th>未结束</th><th>尚未运行</th></tr></thead>
<tbody>{''.join(sweep_rows)}</tbody></table></div>
<p class="src"><a href="#sweep">看每个档位的穿透 / Newton 迭代次数 / 每帧耗时</a></p>
</section>

{demos_section(demos, extras)}

{sweep_section(sweeps)}
"""
    return page(body, gen_time)


# ==========================================================================
# main
# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="真正写文件、压视频（默认只打印计划）")
    ap.add_argument("--crf", type=int, default=26, help="libx264 CRF（默认 26；数字越大文件越小）")
    ap.add_argument("--force-videos", action="store_true", help="忽略 encode_manifest.json，全部重新压缩")
    args = ap.parse_args()

    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    demos, extras = collect_demos()
    cfg, sweeps = collect_sweeps()
    all_demos = demos + extras
    vjobs, manifest = plan_videos(all_demos, args.crf, args.force_videos)
    ijobs = plan_images(all_demos)

    pages = {WEB / "index.html": build_page(demos, extras, sweeps, gen_time)}

    # ---------------- 打印计划 ----------------
    mode = "EXECUTE" if args.execute else "DRY-RUN（只演练，不写任何文件；加 --execute 才真正写）"
    print(f"[build_site] 模式：{mode}")
    print(f"[build_site] demo 结果：{DEMO_ROOT}")
    for r in all_demos:
        extra = "" if r["state"] == "ok" else f"  {r['reason']}"
        vid = f"  视频: {r['video_src']}" if r["video_src"] else (f"  {r['video_note']}" if r["video_note"] else "")
        print(f"  - {r['key']:<32} {STATE_LABEL[r['state']][0]}{extra}{vid}")
    print(f"[build_site] 扫描结果：{SWEEP_ROOT}")
    for s in sweeps:
        c = sweep_counts(s["rows"])
        print(f"  - {s['name']:<18} 共 {len(s['rows'])} 档：完成 {c['ok']}，失败 {c['failed']}，"
              f"未结束 {c['unfinished']}，尚未运行 {c['not_run']}")
        for r in s["rows"]:
            if r["state"] in ("failed", "unfinished"):
                print(f"      {r['level']}: {r['reason']}")
    print(f"[build_site] 将写页面：")
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
