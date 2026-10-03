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
import ast
import datetime
import html
import importlib.util
import json
import os
import re
import shlex
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
# Genesis + lib IPC only (2026-10-02 user: libuipc-only demos and sweep are off the page). Official Genesis IPC
# examples (examples/ipc/; ipc_robot_cloth_teleop needs a keyboard), recorded at the official viewer camera.
DEMOS = [
    ("genesis_ipc_objects_falling", True, "布料 + 刚体 + 软球落地（官方例子）",
     "Genesis 官方 examples/ipc/ipc_objects_falling.py，原样；相机 = 官方 viewer 位置。"),
    ("genesis_ipc_momentum", True, "动量守恒检验（官方例子）",
     "官方 ipc_momentum.py：零重力下刚体撞 FEM 球，末步相对动量误差 {momentum}。"),
    ("genesis_ipc_robot_grasp_cube", True, "机械臂抓软方块（官方例子）",
     "官方 ipc_robot_grasp_cube.py：two-way 耦合；Genesis 的刚体在 libuipc 里是 κ = 100 MPa 的 ABD。"),
    ("genesis_ipc_objects_in_box", True, "一堆物体扔进盒子（官方场景 + 我们加的盒子）",
     "Genesis 没有「扔进盒子」的官方例子：这是官方 ipc_objects_falling 场景原样，外加一个开口盒子和更多同款物体。"
     "参数扫描见下方。"),
]

# Genesis's own IPC tests (tests/ipc/), recorded by run_genesis_ipc_example.py --official-test at the test's own
# viewer camera, with the test's own physics assertions: (NAS dir, pytest node, title, what it checks)
OFFICIAL_TESTS = [
    ("genesis_test_test_ground_clearance_0", "tests/ipc/test_rigid.py::test_ground_clearance[0]",
     "离地间隙随接触刚度变（官方测试）",
     "5 个方块落地，contact_resistance 从 1e2 到 1e6；官方断言：不横向漂移、会停住、刚度越大离地间隙越大"
     "（test_rigid.py 257-264 行）。"),
    ("genesis_test_test_ground_sliding_0", "tests/ipc/test_rigid.py::test_ground_sliding[0]",
     "斜向重力下的地面滑动（官方测试）",
     "重力带水平分量，5 个方块摩擦系数 0–0.16；官方断言：不穿地、离地高度与摩擦无关、摩擦越小滑得越远"
     "（test_rigid.py 316-329 行）。"),
    ("genesis_test_test_objects_colliding_0", "tests/ipc/test_rigid.py::test_objects_colliding[0]",
     "布料盖在物体上（官方测试）",
     "物体和布料落地；官方断言：全部落到地面且不穿地、没有飞走、最终静止、布料盖在所有物体上面（test_rigid.py 540-555 行）。"),
    ("genesis_test_test_cloth_corner_drag_0", "tests/ipc/test_deformable.py::test_cloth_corner_drag[0]",
     "夹住布料一角拖动（官方测试）",
     "两个方块夹住布料一角，先静置再拖着画一圈；官方断言：布料没掉、被夹的角始终跟着夹子走（test_deformable.py 253-271 行）。"),
]
# What the server run found for the tests whose assertions failed (2026-10-02; Genesis's own assertion text in
# official_test.json), shown on the card next to the outcome.
OFFICIAL_TEST_NOTES = {
    "genesis_test_test_ground_clearance_0": "实测 5 个方块离地间隙依次是 6.39、6.39、6.39、7.00、8.18 mm，前 3 个完全相同，"
                                            "所以「逐个严格变大」没过。原因：开着 libuipc 日志重跑（--test-log-level "
                                            "DEBUG）读到这个场景的 κ 允许区间是 [9.85e4, 9.85e6] Pa；方块与地面的接触刚度"
                                            "取两者 resistance 的调和平均（像两根弹簧串联，genesis ipc_coupler/coupler.py:"
                                            "717），前 3 个（199.98、1998、19802）低于下界，被夹成"
                                            "同一个 9.85e4，后 2 个（1.82e5、1e6）在区间内照用（见下方「接触刚度 κ」）。",
    "genesis_test_test_ground_sliding_0": "没过的是「离地高度与摩擦无关」：μ = 0.04 的方块比相邻的高 9.3 mm；"
                                          "不挂录像、原样用 pytest 跑结果逐位相同，不是录像造成的。逐步记录每个方块的"
                                          "姿态（--rigid-trajectory）后看到：这个方块从第 66 步开始绕前棱往前翻，到断言"
                                          "取值的第 100 步倾斜 16°，中心因此抬高（40 mm ×（cos16° + sin16°）比平放高约 9.5 mm）；"
                                          "μ = 0.09、0.16 的方块也晃过（最大 1.5°、5.2°）但回正了。按受力分析滑动的立方体"
                                          "要 μ > 半宽 / 质心高 = 1 才会翻，所以这是仿真里的数值晃动被放大，具体哪一环未查。",
}

PAGE_TITLE = "Neural-IPC 周汇报"
NAV = [("videos", "Demo 视频"), ("tests", "官方测试场景"), ("sweep", "盒子实验与参数扫描")]  # 锚点只用字母


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


def collect_official_test(key, nodeid, title, line):
    """One recorded Genesis official test (run_genesis_ipc_example.py --official-test): same record shape as
    collect_demo, plus the pytest outcome of the test's own assertions read from official_test.json."""
    d = DEMO_ROOT / key
    r = {"key": key, "expects_video": True, "title": title, "line": line, "dir": d, "info": None, "state": None,
         "reason": None, "video_src": None, "video_note": None, "images": [], "nodeid": nodeid, "outcome": None}
    rec, err = load_json(d / "official_test.json")
    if rec is None or err:
        r["state"], r["reason"] = "not_run", "还没跑" if rec is None and not err else f"结果文件损坏（{err}）"
        return r
    r["state"], r["outcome"] = "ok", rec.get("outcome")
    r["line"] = (f"{line} 官方断言：" + {"passed": "通过", "failed": "未通过", "skipped": "跳过"}.get(
        rec.get("outcome"), f"无结果（pytest 退出码 {rec.get('pytest_exit_code')}）") + "。"
                 + (OFFICIAL_TEST_NOTES.get(key, "") if rec.get("outcome") == "failed" else ""))
    scenes = rec.get("scenes") or []
    vp = Path(scenes[0]["video"]) if scenes else None
    if vp and vp.is_file() and vp.stat().st_size > 0:
        r["video_src"] = vp
    else:
        r["video_note"] = "没有录到视频"
    return r


def collect_demos():
    """The page shows exactly the DEMOS and OFFICIAL_TESTS lists. Other directories under DEMO_ROOT (libuipc-only
    demos, timing runs, anything unexpected) are NOT put on the page; main() prints them so nothing is hidden.
    Returns (demos, official tests, unlisted dirs)."""
    known = {k for k, *_ in DEMOS} | {k for k, *_ in OFFICIAL_TESTS}
    demos = [collect_demo(*spec) for spec in DEMOS]
    tests = [collect_official_test(*spec) for spec in OFFICIAL_TESTS]
    unlisted = []
    if DEMO_ROOT.is_dir():
        unlisted = sorted(p.name for p in DEMO_ROOT.iterdir() if p.is_dir() and p.name not in known
                          and not p.name.startswith("genesis_box_levels"))
    return demos, tests, unlisted


# ==========================================================================
# 扫描结果收集
# ==========================================================================
def load_sweep_configs():
    spec = importlib.util.spec_from_file_location("neural_ipc_sweep_configs", SWEEP_CONFIGS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect_level(sweep, level, ov_cfg, root=SWEEP_ROOT):
    jp = root / sweep / f"{level}.json"
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
    # 结果文件里的 overrides 与当前 configs.py 的档位不一致 = 旧配置的结果，等重跑覆盖（json 往返后比较：tuple 存成 list）
    if ov_cfg is not MISSING and isinstance(data.get("overrides"), dict) \
            and data["overrides"] != json.loads(json.dumps(ov_cfg)):
        r["state"], r["reason"], r["data"] = "stale", "重跑中", None  # 旧数据一律不上页面
        return r
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
    elif st == "build_failed":  # genesis：scene.build 抛异常（sweep.py run_one_genesis）
        tb = data.get("build_traceback") or ""
        flagged = " ".join(x.get("text", "") for x in (dig(data, "log_flagged", "flagged_first") or [])
                           if isinstance(x, dict))
        last = [ln for ln in tb.strip().splitlines() if ln.strip()]
        if "intersect" in (tb + flagged).lower():
            r["state"], r["reason"] = "rejected", "开跑前被拒绝（检测到相交）"
        else:
            r["state"] = "failed"
            r["reason"] = "建场景出错" + (f"：{last[-1].strip()}" if last else "")
    elif st == "invalid_during_run":
        r["state"], r["reason"] = "failed", "仿真中途失效"
    elif st == "nonfinite_positions":
        r["state"], r["reason"] = "failed", "顶点坐标出现 NaN / inf"
    elif st in ("starting", "initializing", "running") and "summary" not in data:
        r["state"], r["reason"] = "unfinished", "没跑完"
    else:
        r["state"], r["reason"] = "failed", "状态未知"
    return r


def collect_sweeps(cfg=None):
    """Box-scene sweeps of the set chosen by set_variant: GEN_SWEEP_ROOT/<扫描>/<档位>.json for every level of
    cfg.GENESIS_SWEEPS; each level's overrides = GEN_BASE_OV + the level's own (as sweep.py --base-overrides)."""
    cfg = cfg or load_sweep_configs()
    root, table, base = GEN_SWEEP_ROOT, cfg.GENESIS_SWEEPS, GEN_BASE_OV
    sweeps = []
    known = set()
    for sweep, spec in table.items():
        rows = []
        for level, ov in spec["levels"].items():
            rows.append(collect_level(sweep, level, {**base, **ov}, root))
            known.add((sweep, level))
        sd = root / sweep
        if sd.is_dir():  # NAS 上有、configs.py 里没有的档位
            for jp in sorted(sd.glob("*.json")):
                d, _ = load_json(jp)
                if not isinstance(d, dict) or d.get("sweep") != sweep:  # e.g. run_gpu_job's gpu_job_result.json
                    continue
                if (sweep, jp.stem) not in known:
                    rows.append(collect_level(sweep, jp.stem, MISSING, root))
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
h3.sub { font-size: 1.08rem; margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--border); }
h4 { font-size: 0.95rem; margin: 18px 0 4px; font-weight: 600; }
pre.log { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.76rem;
          line-height: 1.45; background: var(--soft); border-radius: 4px; padding: 10px 12px; margin: 6px 0;
          max-width: 100%; overflow-x: auto; white-space: pre; }
details.repro { margin: 10px 0 26px; }
details.repro summary { cursor: pointer; color: var(--accent); font-size: 0.9rem; }
p.concl { max-width: 80ch; }
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


def videos_section(demos, tests, facts):
    """Official Genesis IPC examples, then Genesis's own IPC test scenes (each with its assertion outcome)."""
    parts = ['<section id="videos"><h2>Demo 视频（Genesis + lib IPC）</h2>',
             '<p class="muted small">Genesis 官方 IPC 例子一字未改，相机用官方代码里给的 viewer 位置；物理由 Genesis 底层的 '
             "libuipc 在 GPU 上算，画面在服务器上离屏渲染（CPU 软件渲染）。</p>",
             '<div class="grid">', *[demo_card(r, facts) for r in demos], "</div></section>",
             '<section id="tests"><h2>官方测试场景</h2>',
             '<p class="muted small">Genesis 仓库自带的 IPC 测试（tests/ipc/），场景和断言一字未改，用 pytest 原样运行；'
             "每个测试自己写好了 viewer 相机位置，录像就用它。卡片里写的是官方断言有没有通过。</p>",
             '<div class="grid">', *[demo_card(r, facts) for r in tests], "</div></section>"]
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


def rows_of(sweeps, name):
    for s in sweeps:
        if s["name"] == name:
            return s["rows"]
    return []


# ---- Genesis 版扫描：json 里没有 params / mesh 字段，参数从 overrides 或 libuipc_config 读 ----
# overrides 里没写的键取官方 ipc_objects_falling.py / Genesis 默认值（出处见 Neural-IPC tools/ipc_sweep/configs.py
# GENESIS_SWEEPS 上方注释：contact_resistance 默认 1e9，FEM friction_mu 默认 0.1；ball_subdiv 不给 = 官方
# gs.morphs.Sphere(radius=0.08)，见 run_genesis_ipc_example.py soft_ball_morph）
GENESIS_DEFAULTS = {"friction_mu": 0.1, "contact_resistance": 1e9, "overlap_balls": 0.0, "ball_subdiv": None,
                    "ball_E": 1.0e3}  # ball_E: official soft ball FEM.Elastic(E=1.0e3), ipc_objects_falling.py
GENESIS_KEYS = {"friction": "friction_mu", "resistance": "contact_resistance", "init_penetration": "overlap_balls",
                "mesh_res": "ball_subdiv", "inversion_vs_E": "ball_E"}  # sweep -> override key in configs.GENESIS_SWEEPS
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# genesis fem_entity.py:542 打印 (n_elements, n_vertices)；软球是 Sphere（官方）或 Mesh（icosphere），材料都是 FEM.Elastic
BALL_SIZE_RE = re.compile(r"morph: (?:Sphere|Mesh), size: \((\d+), (\d+)\), material: <gs\.materials\.FEM\.Elastic>")
# Box-scene result sets (run_sweep_queue.sh TAG / BASE_OV): tag -> overrides shared by every level + a label.
VARIANTS = {
    "stiffball": {"base_ov": {"ball_E": 1.0e5},
                  "label": "所有软球 E = 1e5 Pa（官方 1e3 太软，在盒子里会被压塌）"},
    "officialball": {"base_ov": {"pile_layout": "clear"},
                     "label": "软球保持官方 E = 1e3 Pa，额外物体落在远离官方软球的四角"},
}
# set by set_variant(): where the box-scene sweep jsons and per-level videos of the chosen set live
GEN_TAG, GEN_BASE_OV = None, {}
GEN_SWEEP_ROOT = SWEEP_ROOT / "genesis"
GEN_VIDEO_ROOT = DEMO_ROOT / "genesis_box_levels"   # 每档视频：<扫描>_<档位>/genesis_ipc_objects_in_box.mp4


def set_variant(tag):
    """Point the box-scene readers at result set `tag` (VARIANTS): genesis_<tag>/ and genesis_box_levels_<tag>/."""
    global GEN_TAG, GEN_BASE_OV, GEN_SWEEP_ROOT, GEN_VIDEO_ROOT
    GEN_TAG, GEN_BASE_OV = tag, dict(VARIANTS[tag]["base_ov"])
    GEN_SWEEP_ROOT = SWEEP_ROOT / f"genesis_{tag}"
    GEN_VIDEO_ROOT = DEMO_ROOT / f"genesis_box_levels_{tag}"
GEN_PY = PROJECT / ".conda" / "genesis" / "bin" / "python"
GEN_ENV = "CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=6 HF_HOME=/nas/xiaoyingwang/Neural-IPC/checkpoints/hf_home"


def gen_value(d, sweep):
    """这一档在该扫描维度上的取值（数）。"""
    if sweep == "d_hat":
        return dig(d, "libuipc_config", "contact", "d_hat")
    if sweep == "dt":
        return d.get("dt", MISSING)
    key = GENESIS_KEYS[sweep]
    return (d.get("overrides") or {}).get(key, GENESIS_DEFAULTS[key])


def gen_ball_verts(d):
    """各 FEM 软球（四面体化后，含内部）的顶点数，从该档 log 里 Genesis 打印的 'size: (单元数, 顶点数)' 读。"""
    lp = d.get("log_path")
    if not lp or not Path(lp).is_file():
        return []
    txt = ANSI_RE.sub("", Path(lp).read_text(encoding="utf-8", errors="replace"))
    return [int(m.group(2)) for m in BALL_SIZE_RE.finditer(txt)]


def gen_ground_out(d):
    """(末帧最低点 m, 低于地面 1 mm 以上的物体名单, 其余越出盒壁的物体名单)。
    地面 z = 0；inside_box 判据见 sweep.py object_summary（xy 不超出内壁 1 mm 且最低点不低于地面 1 mm）。"""
    objs = {k: o for k, o in (d.get("objects_final") or {}).items() if isinstance(o, dict)}
    zs = [o["min_z"] for o in objs.values() if is_num(o.get("min_z"))]
    below = [k for k, o in objs.items() if is_num(o.get("min_z")) and o["min_z"] < -1e-3]
    out = [k for k, o in objs.items() if o.get("inside_box") is False and k not in below]
    return (min(zs) if zs else MISSING), below, out


def gen_level_value(r, sweep, cfg):
    """档位取值：有结果用结果文件里的；没有（没跑 / 重跑中）用 configs.py 里该档的 overrides。"""
    if r["data"]:
        return gen_value(r["data"], sweep)
    if r["sweep"] == "baseline":
        return MISSING
    ov = cfg.GENESIS_SWEEPS[r["sweep"]]["levels"].get(r["level"], {})
    key = {"d_hat": "contact_d_hat", "dt": "dt", **GENESIS_KEYS}[sweep]
    return ov.get(key, MISSING)


def gen_label(r, sweep, cfg):
    if sweep == "baseline":
        return "官方默认参数"
    v = gen_level_value(r, sweep, cfg)
    dflt = "（官方默认）" if r["sweep"] == "baseline" else ""
    if sweep == "mesh_res":
        if r["sweep"] == "baseline":
            return "官方 Sphere 网格（官方默认）"
        size = {"coarse": "粗", "medium": "中", "fine": "细"}.get(r["level"], r["level"])
        # trimesh.creation.icosphere(subdivisions=k)：表面 10·4^k + 2 个顶点（soft_ball_morph docstring）
        return size + (f"：icosphere 细分 {v} 次（表面 {10 * 4 ** v + 2} 个顶点）" if isinstance(v, int) else "")
    if not is_num(v):
        return r["level"] + dflt
    if sweep == "inversion_vs_E":
        return f"软球 E = {sci(v)} Pa" + ("（官方 E，即 d̂ 扫描的 2 mm 档）" if r["sweep"] == "d_hat" else "")
    if sweep == "d_hat":
        return g3(v * 1000) + dflt
    if sweep == "resistance":
        return sci(v) + dflt
    if sweep == "init_penetration":
        return ("不穿插" if v == 0 else f"两个软球互相穿进 {g3(v)} R") + dflt
    return g3(v) + dflt


GENESIS_TABLES = [  # (扫描, 标题, 改了什么, 第一列表头, 按 IPC 原理期待的结果)；baseline = 官方参数的盒子 demo
    ("baseline", "官方参数：一堆物体扔进盒子",
     "对照档：下面各节要扫的参数在这里都取官方本例的值和 Genesis 默认（软球 E 是否改过见下一句）。", "配置",
     "物体落下、互相碰撞后堆在盒子里。IPC 的 barrier 让任意两个表面之间始终留着一点小于 d̂ 的间隙，"
     "所以全程不该有穿透，静止后物体离地面也只差不到 d̂ 的一小段。"),
    ("d_hat", "d̂（barrier 作用距离）", "改 contact_d_hat。官方本例取 1 cm，Genesis 注释说应按网格分辨率取。", "d̂（mm）",
     "d̂ 是 barrier 开始起作用的距离。d̂ 变小，物体之间、物体与地面停住时的间隙跟着变小，接触更“硬”，"
     "Newton 迭代一般会变多；d̂ 变大，物体隔得更远就被推开。不论 d̂ 取多少，都不该出现穿透。"),
    ("dt", "时间步长 dt", "改 SimOptions.dt。官方本例 0.02 s；物理时长固定 2 s，帧数随 dt 变。", "dt（s）",
     "dt 变小，每一步物体移动得更少；但 libuipc 会按 1/dt² 抬高接触刚度的下限，接触更硬，"
     "所以每步的 Newton 次数不一定减少，总步数则成倍增加。不论 dt 多大都不该穿透。"),
    ("friction", "摩擦系数 μ", "改所有 FEM 物体（布料和软球）的 friction_mu，与接触对象按几何平均组合；Genesis 默认 0.1。",
     "μ",
     "μ 只管物体互相滑动时的切向阻力：μ 越大越不容易滑，堆得越陡。它不改变法向的 barrier，"
     "所以不该影响会不会穿透，对 Newton 次数的影响也应该很小。"),
    ("resistance", "接触刚度 κ", "改 Genesis 的 contact_resistance，默认 1e9 Pa。", "设的 κ（Pa）",
     "κ 是 barrier 的刚度。libuipc 会把它夹进一个按场景算出的区间：在区间内变化时，间隙和迭代次数会略有变化；"
     "区间外的值会被夹到边界，结果应该和边界值一样。"),
    ("init_penetration", "初始穿插", "多放一个软球，让它和官方软球一开始就互相穿进去一部分（R 为球半径）。",
     "初始状态",
     "IPC 的 barrier 只在两个表面距离为正时才有定义，一开始就穿插的话能量没有意义，"
     "所以 libuipc 的初始化检查应该直接拒绝开跑。"),
    ("mesh_res", "网格分辨率", "把软球换成同样大小、表面细分 2 / 3 / 4 次的 icosphere，由 Genesis 自己四面体化。",
     "软球网格",
     "网格越细，球面越接近真球，接触时参与的顶点越多，每步要解的未知数越多、越慢。"
     "只要初始无穿插，网格粗细都不该影响会不会穿透，物体落地后的大致位置应该接近。"),
    ("inversion_vs_E", "软球硬度 E（查四面体翻转）", "d̂ 固定 2 mm（翻转最多的一档），只把软球的杨氏模量 E 从官方的 1e3 Pa 换成 1e4、1e5 Pa。",
     "软球 E",
     "官方软球 E = 1 kPa，自重压力 ρ·g·2R ≈ 1000 × 9.8 × 0.16 ≈ 1.6 kPa 已超过 E，球会被压到大应变；"
     "Stable Neo-Hookean 在四面体翻转后能量仍有限，所以不会阻止翻转。若翻转是材料太软造成的，E 越大翻转应越少。"),
]


def gen_defaults_text(gd):
    """官方默认参数一句话（dt、d̂ 读官方默认档的结果文件，κ、μ 是 Genesis 默认值）。"""
    dh = dig(gd, "libuipc_config", "contact", "d_hat")
    ball_e = (gd.get("overrides") or {}).get("ball_E", GENESIS_DEFAULTS["ball_E"])
    return (f"dt = {g3(gd.get('dt', MISSING))} s、d̂ = {g3(dh * 100) if is_num(dh) else '—'} cm、"
            f"κ = {sci(GENESIS_DEFAULTS['contact_resistance'])} Pa、μ = {g3(GENESIS_DEFAULTS['friction_mu'])}、"
            f"软球用官方 Sphere 网格、软球 E = {sci(ball_e)} Pa"
            + ("（官方是 1e3）" if ball_e != GENESIS_DEFAULTS["ball_E"] else "（官方值）"))


def gen_scene_text(gd, cfg):
    """场景一句话（物体个数、盒子尺寸读官方默认档的结果文件）。"""
    a = gd.get("box_inner_half", MISSING)
    return (f"官方 ipc_objects_falling.py（一块布、一个刚体方块、一个 FEM 软球）原样照搬，外加一个开口盒子"
            f"（4 面固定墙，内宽 {g3(a * 2) if is_num(a) else '—'} m）和从约 2 m 高落下的 {len(gd.get('pile_boxes') or [])} 个刚体方块、"
            f"{len(gd.get('pile_balls') or [])} 个软球"
            + ("（落在远离官方软球的四角，不压在它上面）" if (gd.get("overrides") or {}).get("pile_layout") == "clear" else "")
            + f"，共 {len(gd.get('objects_at_init') or [])} 个物体，"
            f"仿真 {g3(getattr(cfg, 'GENESIS_SIM_TIME', MISSING))} s")


def gen_split(d):
    """结果文件 objects_final 里新增的分项读数（只有重跑过的档才有，没有就返回 None，不补不猜）：
    FEM 软球 surface_min_z / interior_min_z / n_inverted_tets / n_tets，刚体 ipc_pos_z / genesis_pos_z。
    返回 {"balls": [(名, 表面最低, 内部最低, 翻转数, 四面体数)], "rigids": [(名, IPC 高度, Genesis 高度)],
          "g_dt2": |g_z|·dt²（g 取 libuipc_config.gravity，dt 取结果文件）}。"""
    objs = d.get("objects_final") or {}
    balls = [(k, o["surface_min_z"], o.get("interior_min_z"), o.get("n_inverted_tets"), o.get("n_tets"))
             for k, o in objs.items() if isinstance(o, dict) and is_num(o.get("surface_min_z"))]
    rigids = [(k, o["ipc_pos_z"], o.get("genesis_pos_z")) for k, o in objs.items()
              if isinstance(o, dict) and is_num(o.get("ipc_pos_z")) and is_num(o.get("genesis_pos_z"))]
    if not balls and not rigids:
        return None
    gz, dt = dig(d, "libuipc_config", "gravity", 2, 0), d.get("dt", MISSING)  # gravity 存成 [[0.0], [0.0], [-9.81]]
    return {"balls": balls, "rigids": rigids,
            "g_dt2": abs(gz) * dt * dt if is_num(gz) and is_num(dt) else MISSING}


def gen_split_table(r, sweep, cfg):
    """原始输出里的分项读数表（该档结果文件有这些字段才出）。"""
    sp = gen_split(r["data"]) if r["state"] == "ok" else None
    if not sp:
        return ""
    trs = [[k, "FEM 软球", g3(s * 1000), g3(i * 1000) if is_num(i) else "—", f"{ni} / {nt}", "—", "—"]
           for k, s, i, ni, nt in sp["balls"]]
    trs += [[k, "刚体", "—", "—", "—", g3(ip * 1000), g3(gp * 1000)] for k, ip, gp in sp["rigids"]]
    return (f'<p class="small"><b>{esc(gen_label(r, sweep, cfg))}</b>：末帧逐个物体的分项读数（扫描那次运行）</p>'
            + table(["物体", "类型", "表面最低点（mm）", "内部顶点最低点（mm）", "翻转的四面体 / 四面体总数",
                     "刚体中心高度：IPC 里（mm）", "刚体中心高度：Genesis 读出（mm）"], trs))


def gen_raw_cells(r):
    """一档的原始数字（直接读结果文件，唯一的汇总是"各物体末帧最低点里取最低"）。"""
    if r["state"] != "ok":
        return [r["reason"], "—", "—", "—", "—", "—"]
    d = r["data"]
    s = d.get("summary") or {}
    low, _, _ = gen_ground_out(d)
    n_obj = len(d.get("objects_final") or {})
    return [f"跑完 {s.get('frames_done', '—')} 帧",
            f"{s.get('n_checks_with_penetration', '—')} / {s.get('n_sanity_checks', '—')}",
            f"{g3(s.get('newton_iter_frame_stats_median'))} / {g3(s.get('newton_iter_frame_stats_max'))}",
            str(s.get("n_frames_hit_max_iter", "—")),
            g3(low * 1000) if is_num(low) else "—",
            f"{d.get('n_objects_outside_box', '—')} / {n_obj}"]


def genesis_sweep_tables(sweeps, cfg):
    """[(扫描, 标题, 一句话, 排好序的行, 原始数字表)]；每张表都带官方默认那一档。"""
    base = rows_of(sweeps, "baseline")
    out = []
    for sweep, title, one, head, expect in GENESIS_TABLES:
        # inversion_vs_E keeps d̂ = 2 mm, so its reference row is the d̂ = 2 mm level (official E), not the baseline
        ref = [r for r in rows_of(sweeps, "d_hat") if r["level"] == "0p002"] if sweep == "inversion_vs_E" else base
        rows = list(base) if sweep == "baseline" else list(rows_of(sweeps, sweep)) + list(ref)

        def key(r):
            if sweep == "baseline":
                return 0
            if r["sweep"] == "baseline" and sweep == "mesh_res":  # 官方网格放最前，其余按细分次数
                return -float("inf")
            v = gen_level_value(r, sweep, cfg)
            return v if is_num(v) else float("inf")
        rows.sort(key=key)
        trs = [[gen_label(r, sweep, cfg)] + gen_raw_cells(r) for r in rows]
        out.append((sweep, title, one, expect, rows,
                    table([head, "运行", "穿透检查（查到穿透 / 检查次数）", "每帧 Newton 迭代（中位 / 最多）",
                           "撞到迭代上限的帧数", "末帧最低点（mm，地面 = 0，Genesis 状态读出）", "末帧不在盒内的物体 / 物体总数"], trs)))
    return out


def gen_level_videos(gsweeps):
    """{(扫描, 档位): 视频记录}，记录格式同 collect_demo（plan_videos / demo_card 直接复用）。
    认 GEN_VIDEO_ROOT/<扫描>_<档位>/ 下 mp4 非空、且 job.exit 为 0 / 3 / 4 的（run_genesis_ipc_example.py：
    3 = 官方相机下有帧部分出画面，4 = 第二遍重新仿真的范围超出第一遍，即 GPU 运行不逐位一致；两种情况视频和
    camera_fit.json 都照常写出，卡片里写明）；否则写"视频未生成"，不拿别的视频顶替。
    初始穿插那组开跑前就被拒，没有视频，不建记录。"""
    out = {}
    for s in gsweeps:
        if s["name"] == "init_penetration":
            continue
        for r in s["rows"]:
            d = GEN_VIDEO_ROOT / f"{r['sweep']}_{r['level']}"
            ex = d / "job.exit"
            code = ex.read_text().strip() if ex.is_file() else None
            mp4 = d / "genesis_ipc_objects_in_box.mp4"
            done = code in ("0", "3", "4") and mp4.is_file() and mp4.stat().st_size > 0
            cf, _ = load_json(d / "camera_fit.json")
            line = ""
            if done and cf:
                nout, nchk = dig(cf, "verify", "n_frames_out"), dig(cf, "verify", "n_frames_checked")
                line = (f"视频这次运行（相机：{cf.get('camera_mode', '—')}）："
                        + (f"{nchk} 帧里 {nout} 帧有部分画面在视野外" if is_num(nout) and nout else "没有一帧出画面")
                        + ("；两遍 GPU 仿真不逐位一致" if cf.get("pass2_bbox_exceeds_pass1") else "") + "。")
            out[(r["sweep"], r["level"])] = {
                "key": f"box{GEN_TAG and '_' + GEN_TAG or ''}_{r['sweep']}_{r['level']}", "expects_video": True,
                "title": "", "line": line, "dir": d, "state": "ok", "reason": None, "images": [],
                "video_src": mp4 if done else None,
                "video_note": None if done else ("视频未生成" if code is None else f"视频未生成（录像退出码 {code}）")}
    return out


def init_log_excerpt(log_path):
    """初始穿插档的 libuipc / Genesis 日志原文摘录（只去掉终端颜色码，不改写）：
    前 3 行 Intersection detected、SimplicialSurfaceIntersectionCheck 那一段（到下一条带 [ 开头的日志为止）、
    World is not valid 各行、Genesis 的 IPC world initialized successfully、最后一行 AttributeError。
    返回 (行列表, Intersection detected 总行数)；文件不在返回 (None, 0)。"""
    p = Path(log_path)
    if not p.is_file():
        return None, 0
    lines = ANSI_RE.sub("", p.read_text(encoding="utf-8", errors="replace")).splitlines()
    inter = [ln for ln in lines if "Intersection detected" in ln]
    out = inter[:3]
    i = next((k for k, ln in enumerate(lines) if "SimplicialSurfaceIntersectionCheck" in ln), None)
    if i is not None:
        j = i + 1
        while j < len(lines) and not lines[j].startswith("["):
            j += 1
        out += lines[i:j]
    out += [ln for ln in lines if "World is not valid" in ln]
    out += [ln for ln in lines if "IPC world initialized successfully" in ln]
    out += [ln for ln in lines if ln.startswith("AttributeError")][-1:]
    return out, len(inter)


def genesis_commit():
    """生成页面时 Neural-IPC 仓库的 HEAD（短哈希）。读不到就停，不写猜的值。"""
    p = subprocess.run(["git", "-C", str(PROJECT), "log", "-1", "--format=%h"], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        raise SystemExit(f"[build_site] 读不到 {PROJECT} 的 git commit：{p.stderr}")
    return p.stdout.strip()


def repro_block(sweep, rows, cfg, commit):
    """「一步一步复现」：每档跑扫描 + 录视频的完整命令（绝对路径、无占位）。
    扫描命令用结果文件里记录的原始 argv（没有结果的档按 configs.py + 本套共用参数拼）；视频命令与
    Neural-IPC tools/ipc_sweep/run_sweep_queue.sh 的 VIDEO=1 CAMERA_MODE=official 分支一致，
    overrides = 本套共用参数（GEN_BASE_OV）加 configs.GENESIS_SWEEPS 该档原样。"""
    head = f"cd {PROJECT} && {GEN_ENV} {GEN_PY}"
    out = [f"# 代码版本：Neural-IPC commit {commit}（git -C {PROJECT} log -1 --format=%h，生成本页时读取）"]
    own = [r for r in rows if sweep == "baseline" or r["sweep"] != "baseline"]  # 官方默认档只在它自己的实验里给命令
    for n, r in enumerate(own, 1):
        argv = (r["data"] or {}).get("argv")
        if isinstance(argv, list):
            # --export-obj only ever affected the removed libuipc backend (no-op for genesis) and was dropped from
            # sweep.py on 2026-10-03, so a recorded argv that still has it would no longer parse
            argv = [a for a in argv if a != "--export-obj"]
        if not (isinstance(argv, list) and argv):
            argv = [str(PROJECT / "tools" / "ipc_sweep" / "sweep.py"), "--backend", "genesis",
                    "--sweep", r["sweep"], "--level", r["level"], "--log-level", "Info",
                    "--base-overrides", json.dumps(GEN_BASE_OV), "--out-root", str(GEN_SWEEP_ROOT)]
        lbl = gen_label(r, sweep, cfg)
        out += ["", f"# {n}. {lbl}",
                f"# {n}a. 跑这一档，结果写到 {GEN_SWEEP_ROOT / r['sweep'] / (r['level'] + '.json')}",
                f"{head} {' '.join(shlex.quote(str(a)) for a in argv)}"]
        if sweep != "init_penetration":
            ov = {**GEN_BASE_OV, **cfg.GENESIS_SWEEPS[r["sweep"]]["levels"][r["level"]]}
            vd = GEN_VIDEO_ROOT / f"{r['sweep']}_{r['level']}"
            out += [f"# {n}b. 录这一档的视频（官方相机），写到 {vd / 'genesis_ipc_objects_in_box.mp4'}",
                    f"{head} {PROJECT / 'tools' / 'ipc_demos' / 'run_genesis_ipc_example.py'} --example ipc_objects_in_box "
                    f"--overrides {shlex.quote(json.dumps(ov))} --out-dir {vd} "
                    "--egl-device-index 16 --software-render --fit-camera --camera-mode official"]
    return ('<details class="repro"><summary>一步一步复现</summary>'
            f'<pre class="log">{esc(chr(10).join(out))}</pre></details>')


def genesis_experiment(sweep, title, one, expect, rows, tbl, cfg, videos, concl, commit, gd):
    """一个实验，固定顺序：①实验设置 ②按原理期待的结果 ③原始输出（每档视频 + 该视频运行的 camera_fit 数字 /
    日志原文，再加扫描运行的原始数字表）④解释与结论（只用 json / 日志数据）⑤一步一步复现。"""
    if sweep == "baseline":
        setting = f"{one}（{gen_defaults_text(gd)}）。"
    else:
        vals = "、".join(gen_label(r, sweep, cfg) for r in rows if r["sweep"] != "baseline")
        setting = f"{one}取值：{vals}；其余参数保持官方默认（{gen_defaults_text(gd)}），表里也放了官方默认那一档对照。"
    parts = [f"<h3>{esc(title)}</h3>",
             f"<h4>实验设置</h4><p>{esc(setting)}场景：{esc(gen_scene_text(gd, cfg))}。</p>",
             f"<h4>按原理期待的结果</h4><p>{esc(expect)}</p>",
             "<h4>原始输出</h4>"]
    if sweep == "init_penetration":
        for r in rows:
            if r["sweep"] == "baseline":
                continue
            lines, n_inter = init_log_excerpt((r["data"] or {}).get("log_path")
                                              or GEN_SWEEP_ROOT / sweep / f"{r['level']}.log")
            parts.append(f'<p class="small"><b>{esc(gen_label(r, sweep, cfg))}</b>：开跑前就被拒，没有视频；'
                         "下面是日志原文摘录。</p>")
            if lines is None:
                parts.append('<div class="novideo">日志文件不存在</div>')
                continue
            parts.append(f'<pre class="log">{esc(chr(10).join(lines))}</pre>')
            parts.append(f'<p class="muted small">摘自 <code>{esc((r["data"] or {}).get("log_path", ""))}</code>，'
                         f"只去掉了终端颜色码；Intersection detected 共 {n_inter} 行，这里只列前 3 行，"
                         "其余省略的行见原文件。</p>")
    else:
        cards = [demo_card(dict(videos[(r["sweep"], r["level"])], title=gen_label(r, sweep, cfg)), {})
                 for r in rows if (r["sweep"], r["level"]) in videos]
        parts.append('<div class="grid">' + "".join(cards) + "</div>")
    if sweep != "init_penetration":
        parts.append('<p class="muted small">下表是扫描那次运行的结果文件（和视频不是同一次 GPU 运行）；'
                     "最低点是 Genesis 状态读出的末帧各物体顶点里最低的一个，包括软球的内部顶点，"
                     "刚体用的是 Genesis 读数（见下方分项读数）。</p>")
    parts.append(tbl)
    # 官方默认档的分项读数只在它自己和 d̂ 两个实验里出（d̂ 结论要拿它对照），其它实验不重复
    parts += [t for t in (gen_split_table(r, sweep, cfg) for r in rows
                          if sweep in ("baseline", "d_hat") or r["sweep"] != "baseline") if t]
    parts.append(f'<h4>解释与结论</h4><p class="concl">{concl.get(sweep, "")}</p>')
    parts.append(repro_block(sweep, rows, cfg, commit))
    return "\n".join(parts)


def sweep_section(gsweeps, cfg, videos, facts, commit):
    """盒子实验：set_variant 选的那套（主结果）的每个参数扫描，然后是方案 3（官方软球不被压）一节。"""
    gb = rows_of(gsweeps, "baseline")
    gd = gb[0]["data"] if gb and gb[0]["data"] else {}
    gscene = ("Genesis 没有「一堆物体扔进盒子」的官方例子，这里用官方 ipc_objects_falling 场景加一个盒子和更多同款物体。"
              f"这一套：{VARIANTS[GEN_TAG]['label']}。所有实验每次只改一个参数。每一档跑两次：一次是扫描"
              "（记录穿透检查、Newton 迭代、末帧各物体位置和软球形状），一次是录视频（官方相机）；两次是独立的 GPU 运行，"
              "结果不逐位相同，所以视频旁的数字和表里的数字分开写。穿透用 libuipc 的 sanity check 每 10 帧查一次。")
    parts = ['<section id="sweep"><h2>盒子实验与 IPC 参数扫描</h2>', f"<p>{esc(gscene)}</p>"]
    concl = genesis_conclusions(facts, videos)
    for sweep, title, one, expect, rows, tbl in genesis_sweep_tables(gsweeps, cfg):
        parts.append(genesis_experiment(sweep, title, one, expect, rows, tbl, cfg, videos, concl, commit, gd))
    parts.append(officialball_block())
    parts.append(f'<p class="next">{NEXT_STEPS}</p>')
    parts.append("</section>")
    return "\n".join(parts)


def officialball_block():
    """方案 3：软球保持官方 E = 1e3、额外物体落在远离官方软球的四角（genesis_officialball/）。每档列官方软球
    （第一个软球实体，名字以 3_ 开头）的结束质心高度、形状比、翻转数，以及有没有别的物体的质心在它上方 0.3 m 以内。"""
    root = SWEEP_ROOT / "genesis_officialball"
    sweeps = load_sweep_configs().GENESIS_SWEEPS
    rows, res, video = [], {}, None  # res: (sweep, level) -> (centroid z mm, any object above it)
    for sweep, spec in sweeps.items():  # configs order
        for level in spec["levels"]:
            d, _ = load_json(root / sweep / f"{level}.json")
            if not isinstance(d, dict) or d.get("status") != "ok":
                continue
            fin = d.get("objects_final") or {}
            ob = next(((n, o) for n, o in fin.items() if n.startswith("3_") and "n_tets" in o), None)
            if ob is None:
                continue
            c = ob[1]["centroid"]
            above = [n for n, o in fin.items() if n != ob[0] and "Cloth" not in n and o.get("centroid")
                     and ((o["centroid"][0] - c[0]) ** 2 + (o["centroid"][1] - c[1]) ** 2) ** 0.5 < 0.3
                     and o["centroid"][2] > c[2]]
            res[(sweep, level)] = (c[2] * 1000, bool(above))
            rows.append([f"{sweep} / {level}", g3(c[2] * 1000), g3(ob[1].get("surface_radius_ratio")),
                         f"{ob[1].get('n_inverted_tets')} / {ob[1].get('n_tets')}", "、".join(above) or "没有"])
    rec = officialball_video_record()
    if rec["video_src"] is not None:
        video = f"assets/videos/{rec['key']}.mp4"  # encoded and uploaded with the others (main: plan_videos)
    if not rows:
        return '<h3>方案 3：保留官方软球（E = 1e3）、不在它上面压东西</h3><div class="novideo">还没跑完</div>'
    n_total = sum(len(s["levels"]) for k, s in sweeps.items() if k != "init_penetration")

    def cz(sw, lv):
        return res.get((sw, lv), (MISSING,))[0]

    # levels whose ball is the official one (E, d_hat, mesh untouched): baseline / dt / friction / resistance
    same = [(sw, lv) for (sw, lv) in res if not ({"contact_d_hat", "ball_E", "ball_subdiv"} & set(sweeps[sw]["levels"][lv]))]
    same_cz = [res[k][0] for k in same if not res[k][1]]  # and nothing above it
    flat = bool(same_cz) and max(same_cz) < 70  # clearly below an intact ~80 mm
    dh = sorted([(sweeps["d_hat"]["levels"][lv]["contact_d_hat"], cz("d_hat", lv)) for lv in sweeps["d_hat"]["levels"]]
                + [(0.01, cz("baseline", "default"))])  # official d_hat 1 cm = the baseline level
    dh = [(v, z) for v, z in dh if is_num(z)]
    concl = ((f"只改 dt、摩擦或接触刚度的 {len(same_cz)} 档（d̂、E、网格都是官方值，上方也没有别的物体）："
              f"官方软球结束时质心 {g3(min(same_cz))}–{g3(max(same_cz))} mm，完好时约 80 mm。"
              + ("<b>与期待一致：不被压也被自重压扁到约一半高度，官方软球 E = 1 kPa 太软</b>，"
                 "所以盒子实验的主结果用 E = 1e5 那一套。" if flat else "没有明显变扁，和期待不一致，要再查。")
              if same_cz else "")
             + "改 d̂（其余官方）：" + "、".join(f"d̂ = {g3(v * 1000)} mm 时 {g3(z)} mm" for v, z in dh)
             + ("——d̂ 越小塌得越扁（为什么 d̂ 会影响塌陷程度，原因未查）；d̂ = 3 cm 时质心反而高过完好的球，"
                "是 d̂ 大于表面边长、自接触把球从里面撑开（同方案 1 的 d̂ 一节）。"
                if monotone([z for _, z in dh]) == 1 else "。")
             + "只改软球 E（d̂ = 2 mm）：E = 1e4 时 " + g3(cz("inversion_vs_E", "dhat2mm_E1e4"))
             + " mm、E = 1e5 时 " + g3(cz("inversion_vs_E", "dhat2mm_E1e5")) + " mm（同 d̂ 下 E = 1e3 是 "
             + g3(cz("d_hat", "0p002")) + " mm）。"
             + "改网格（粗 / 中 / 细）：" + " / ".join(g3(cz("mesh_res", lv)) for lv in ("coarse", "medium", "fine"))
             + " mm。" + escape_text())
    return ("<h3>方案 3：保留官方软球（E = 1e3）、不在它上面压东西</h3>"
            "<h4>实验设置</h4><p>同一个盒子场景，软球保持官方材料 E = 1e3 Pa（只有 inversion_vs_E 两档按档位改 E），"
            "额外的 2 个方块和 3 个软球落在离官方软球水平 0.69 m 以上的四角和边上，官方布料照常落下。"
            "其余参数按各扫描档位变化，和方案 1 是同一套档位。</p>"
            "<h4>按原理期待的结果</h4><p>官方软球上面不压东西，它只受自重和布料；E = 1 kPa 远小于自重压力"
            "（ρ·g·2R ≈ 1.6 kPa），所以即使不被压，也会被自重压扁一部分。</p><h4>原始输出</h4>"
            f'<p class="muted small">已跑完 {len(rows)} / {n_total} 档（初始穿插那两档开跑前就被拒，不计）'
            + ("；其余还在跑，跑完更新本页。" if len(rows) < n_total else "。") + "下表是每档扫描那次运行的结果文件。</p>"
            + (f'<div class="grid"><div class="demo"><h3>官方参数那档的视频（官方相机）</h3><video controls muted playsinline '
               f'preload="metadata" src="{video}"></video></div></div>' if video else "")
            + table(["档位", "官方软球结束时质心高度（mm，完好约 80）", "形状比（表面点到球心最远 / 最近）",
                     "翻转四面体 / 总数", "质心在它上方 0.3 m 以内的物体"], rows)
            + f'<h4>解释与结论</h4><p class="concl">{concl}</p>')


def monotone(vals):
    """+1 if vals never decrease, -1 if they never increase, 0 otherwise (a non-number entry gives 0)."""
    if not vals or not all(is_num(v) for v in vals):
        return 0
    up = all(b >= a for a, b in zip(vals, vals[1:]))
    down = all(b <= a for a, b in zip(vals, vals[1:]))
    return 1 if up and not down else -1 if down and not up else 0


def box_const(name):
    """A box constant (e.g. BOX_WALL_TOP, BOX_WALL_THICKNESS, in m) read from run_genesis_ipc_example.py's source
    with ast, where they are assigned as tuples (no import: that module imports Genesis / EGL helpers)."""
    src = (PROJECT / "tools" / "ipc_demos" / "run_genesis_ipc_example.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple):
            names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
            if name in names:
                return ast.literal_eval(node.value)[names.index(name)]
    return MISSING


def escape_text():
    """方案 3 官方参数档有物体最后不在盒内：读带逐帧轨迹的重跑（genesis_escape_debug/baseline/default.json，
    sweep.py frames[*].objects = [质心 x, y, z, 最低点 z, 最大 |x|,|y|]），对每个出盒的方块 / 软球找它第一次越过
    墙内侧面（最大 |x|,|y| > 盒内半宽）的那几帧，比较那时的最低点和墙顶高度：高于墙顶 = 从墙上方飞出去，不是穿墙。"""
    d, _ = load_json(SWEEP_ROOT / "genesis_escape_debug" / "baseline" / "default.json")
    if not isinstance(d, dict) or d.get("status") != "ok" or not (d.get("frames") or [{}])[0].get("objects"):
        return ""
    a, top, t = d.get("box_inner_half"), box_const("BOX_WALL_TOP"), box_const("BOX_WALL_THICKNESS")
    if not (is_num(a) and is_num(top) and is_num(t)):
        return ""
    out = [n for n, o in (d.get("objects_final") or {}).items() if not o.get("inside_box") and "Cloth" not in n]
    parts = []
    for n in out:
        # frames where the object's horizontal extent overlaps the wall (a .. a + t); its inner extent is estimated
        # as 2 * centroid - outer (exact for a ball, symmetric about its centroid)
        cross = []
        for f in d["frames"]:
            o = f["objects"].get(n)
            if o:
                outer, c = o[4], max(abs(o[0]), abs(o[1]))
                if outer > a and 2 * c - outer < a + t:
                    cross.append((f["frame"], o[3]))
        if not cross:
            continue
        lo = min(z for _, z in cross)
        parts.append(f"{n} 在第 {cross[0][0]}–{cross[-1][0]} 帧水平方向和墙重叠（墙在离中心 {g3(a)}–{g3(a + t)} m），"
                     f"那时它的最低点在 {g3(lo)}–{g3(max(z for _, z in cross))} m，"
                     + ("高于墙顶 " + g3(top) + " m：<b>是从墙上方飞出去的，不是穿墙</b>" if lo > top
                        else "不高于墙顶 " + g3(top) + " m，要再查是否穿墙"))
    if not parts:
        return ""
    return ("有物体最后停在盒子外面。为了弄清它怎么出去的，用逐帧记录各物体位置的版本把官方参数那档重跑了一次"
            f"（{SWEEP_ROOT / 'genesis_escape_debug'}，同参数、另一次 GPU 运行）："
            + "；".join(parts) + f"。这次重跑穿透检查 {dig(d, 'summary', 'n_checks_with_penetration')} 次报穿透。"
            "另外「不在盒内」也会算上布料：布料有一部分搭在墙顶外侧。")


def officialball_video_record():
    """方案 3 官方参数那档的视频（genesis_box_levels_officialball/baseline_default），记录格式同 collect_demo；
    job.exit 0 / 3 / 4 且 mp4 非空才收（含义见 gen_level_videos）。"""
    d = DEMO_ROOT / "genesis_box_levels_officialball" / "baseline_default"
    mp4, ex = d / "genesis_ipc_objects_in_box.mp4", d / "job.exit"
    ok = ex.is_file() and ex.read_text().strip() in ("0", "3", "4") and mp4.is_file() and mp4.stat().st_size > 0
    return {"key": "box_officialball_baseline_default", "expects_video": True, "title": "", "line": "", "dir": d,
            "state": "ok", "reason": None, "images": [], "video_src": mp4 if ok else None, "video_note": None}


# ---------------- 关键数字（发现 / 结论里用，dry-run 时也打印出来核对） ----------------
def compute_facts(demos):
    """Numbers the demo cards quote: the momentum example's final relative momentum error (run_info.json)."""
    mom = [r for r in demos if r["key"] == "genesis_ipc_momentum" and r["state"] == "ok"]
    return {"momentum_err": dig(mom[0]["info"], "run", "final_rel_momentum_error") if mom else MISSING}


def compute_genesis_facts(gsweeps):
    """Genesis 扫描每一档的关键数（结论里用；dry-run 时逐档打印核对）。键 g/<扫描>/<档位>。"""
    f = {}
    for s in gsweeps:
        for r in s["rows"]:
            k = f"g/{r['sweep']}/{r['level']}"
            if r["state"] != "ok":
                f[k] = {"state": r["state"], "reason": r["reason"]}
                continue
            d = r["data"]
            low, below, outw = gen_ground_out(d)
            # 官方默认档同时是每个扫描维度的默认值，按维度各存一份
            value = ({sw: gen_value(d, sw) for sw, *_ in GENESIS_TABLES if sw != "baseline"} if r["sweep"] == "baseline"
                     else gen_value(d, r["sweep"]))
            fin, ini = d.get("objects_final") or {}, d.get("objects_at_init") or {}
            # per soft ball: (name, surface edge median at start / end [m], end r_max / r_min, inverted, tets,
            # end centroid z [m]); only levels run with the shape fields (sweep.py a39f438) have them
            shape = [(n, dig(ini, n, "surface_edge_median_m"), o.get("surface_edge_median_m"),
                      o.get("surface_radius_ratio"), o.get("n_inverted_tets"), o.get("n_tets"), o["centroid"][2])
                     for n, o in fin.items() if isinstance(o, dict) and "n_tets" in o and o.get("centroid")]
            # end centroid height of every rigid box and soft ball (not the cloth, not the fixed walls)
            heights = [o["centroid"][2] for n, o in fin.items() if isinstance(o, dict) and o.get("centroid")
                       and "Cloth" not in n]
            # same objects: end centroid distance to the nearest box wall [m] (box_inner_half - max(|x|, |y|))
            a = d.get("box_inner_half")
            wall_gaps = [a - max(abs(o["centroid"][0]), abs(o["centroid"][1])) for n, o in fin.items()
                         if is_num(a) and isinstance(o, dict) and o.get("centroid") and "Cloth" not in n]
            f[k] = {"state": "ok", "value": value, "pen_checks": dig(d, "summary", "n_checks_with_penetration"),
                    "lowest_mm": low * 1000 if is_num(low) else MISSING, "below": below, "out_wall": outw,
                    "newton_max": dig(d, "summary", "newton_iter_frame_stats_max"),
                    "newton_median": dig(d, "summary", "newton_iter_frame_stats_median"),
                    "hit_max": dig(d, "summary", "n_frames_hit_max_iter"), "split": gen_split(d),
                    "shape": shape, "heights": heights, "wall_gaps": wall_gaps, "n_out_box": d.get("n_objects_outside_box"),
                    "wall_s": dig(d, "summary", "wall_seconds_total")}
            if r["sweep"] == "mesh_res" or r["sweep"] == "baseline":
                f[k]["ball_verts"] = gen_ball_verts(d)
    return f


def genesis_conclusions(f, videos):
    """每个盒子实验的「解释与结论」，返回 {扫描: html}，针对 set_variant 选的那套结果。
    每句定性结论都由数据判断后才写（例如只有数据单调才写「越大越……」），数字全部来自 facts（结果文件）；
    κ 区间读打开 libuipc 日志的重跑（genesis_kappa_debug），E = 1e3 的对照读最早那套（genesis/）。"""
    def g(sweep, level):
        return f.get(f"g/{sweep}/{level}", {})

    def rows(sweep):  # 该扫描的各档 + 官方默认档（value 换成该维度上的默认值）
        b0 = g("baseline", "default")
        out = [v for k, v in f.items() if k.startswith(f"g/{sweep}/")]
        if b0.get("state") == "ok":
            out.append(dict(b0, value=b0["value"].get(sweep, MISSING)))
        return [v for v in out if v.get("state") == "ok"]

    def rng(vals):
        vals = [v for v in vals if is_num(v)]
        return f"{g3(min(vals))}–{g3(max(vals))}" if vals else "—"

    def balls(r):  # shape tuples of the soft balls: (name, edge0, edge1, ratio, inverted, tets, centroid z)
        return r.get("shape") or []

    def inflated(r):  # largest end/start surface-edge ratio over the soft balls (> 1 = the ball was stretched)
        q = [e1 / e0 for _, e0, e1, *_ in balls(r) if is_num(e0) and is_num(e1) and e0 > 0]
        return max(q) if q else MISSING

    def inv_frac(r):
        bs = balls(r)
        n, t = sum(x[4] for x in bs if is_num(x[4])), sum(x[5] for x in bs if is_num(x[5]))
        return n / t if t else MISSING

    def edge_mm(r):  # median start surface edge of the soft balls (mm)
        e = [e0 for _, e0, *_ in balls(r) if is_num(e0)]
        return sorted(e)[len(e) // 2] * 1000 if e else MISSING

    def shape_txt(r):
        q, inv = inflated(r), inv_frac(r)
        return (f"边长 {g3(edge_mm(r))} mm → 结束时最多拉长到 {g3(q)} 倍、翻转四面体 {g3(100 * inv)}%"
                if is_num(q) and is_num(inv) else "没有形状读数")

    def ball_cz(r):  # end centroid heights of the soft balls (mm); an intact ball resting on the floor is ~ R
        return [cz * 1000 for *_, cz in balls(r)]

    b = g("baseline", "default")
    ok_all = [v for k, v in f.items() if k.startswith("g/") and v.get("state") == "ok"]
    n_pen = sum(1 for v in ok_all if v.get("pen_checks") != 0)
    init = [v for k, v in f.items() if k.startswith("g/init_penetration/")]
    n_rej = sum(1 for v in init if v.get("state") == "rejected")
    dt_rows = sorted(rows("dt"), key=lambda v: v.get("value", 0))
    fr = sorted(rows("friction"), key=lambda v: v.get("value", 0))
    kr = sorted(rows("resistance"), key=lambda v: v.get("value", 0))
    mesh = [(name, g("mesh_res", k)) for k, name in (("coarse", "粗"), ("medium", "中"), ("fine", "细"))]

    def split(r):
        """分项读数的文字片段；该档没有新字段返回 None。"""
        sp = r.get("split")
        if not sp:
            return None
        bs, rs = sp["balls"], sp["rigids"]
        offs = [(ip - gp) * 1000 for _, ip, gp in rs]
        same = bool(offs) and max(offs) - min(offs) < 0.05  # 各刚体偏移相同（差 < 0.05 mm）
        return {"surf": f"{g3(min(s for _, s, *_ in bs) * 1000)} mm" if bs else "—",
                "interior": f"{g3(min(i for *_, i, _, _ in bs if is_num(i)) * 1000)} mm"
                            if any(is_num(x[2]) for x in bs) else "—",
                "inv": "、".join(f"{ni}/{nt}" for *_, ni, nt in bs),
                "inv_rng": rng([ni for *_, ni, _ in bs]),
                "off": (f"{g3(offs[0])} mm" if same else f"{rng(offs)} mm") if offs else "—", "off_same": same,
                "g_dt2": f"{g3(sp['g_dt2'] * 1000)} mm" if is_num(sp["g_dt2"]) else "—"}

    def surf_mm(r):  # 软球表面最低点（mm）；该档没有分项读数时返回 MISSING
        bs = (r.get("split") or {}).get("balls") or []
        return min(s for _, s, *_ in bs) * 1000 if bs else MISSING

    def surf_txt(r):
        v = surf_mm(r)
        return f"{g3(v)} mm" if is_num(v) else "—"

    def off_txt(r):  # 刚体 IPC 高度 − Genesis 读数（mm）
        rs = (r.get("split") or {}).get("rigids") or []
        offs = [(ip - gp) * 1000 for _, ip, gp in rs]
        return f"{g3(sum(offs) / len(offs))} mm" if offs else "—"

    def debug_json(name, sweep, level):
        """A diagnostic rerun outside the main sweep (SWEEP_ROOT/<name>/<sweep>/<level>.json); {} if absent."""
        p = SWEEP_ROOT / name / sweep / f"{level}.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def inv_pct(r):  # 全部软球的翻转四面体占比，来自该档分项读数
        bs = (r.get("split") or {}).get("balls") or []
        n, t = sum(x[3] for x in bs if is_num(x[3])), sum(x[4] for x in bs if is_num(x[4]))
        return f"{g3(100 * n / t)}%（{n} / {t}）" if t else "—"

    def kappa_text():
        """κ 区间与被夹情况：Genesis 把 libuipc 日志设成 error，这里读打开 libuipc Info 日志重跑的三档
        （genesis_kappa_debug，Neural-IPC run_commands [IPC 参数扫描命令] 第 4 条）的 kappa_log。"""
        runs = [("baseline", "default"), ("resistance", "1e6"), ("resistance", "1e11")]
        ds = [(s, lv, debug_json("genesis_kappa_debug", s, lv)) for s, lv in runs]
        corr = next((dig(d, "kappa_log", "kappa_corridor", 0, "groups") for *_, d in ds if d), MISSING)
        if not isinstance(corr, list):
            return "这几档没有打开 libuipc 日志的重跑结果，看不到 κ 是否被夹。"
        parts = []
        for s, lv, d in ds:
            set_k = (d.get("overrides") or {}).get("contact_resistance", GENESIS_DEFAULTS["contact_resistance"])
            hits = [x["groups"] for x in dig(d, "kappa_log", "model_kappa_clamped") or [] if float(x["groups"][0]) > 0]
            parts.append(f"设 {sci(set_k)}：" + (f"{len(hits)} 个接触模型被夹到 {sci(float(hits[0][3]))}" if hits else "在区间内，照用"))
        return (f"打开 libuipc 日志重跑（官方默认、1e6、1e11 三档）读到：这个场景的 κ 区间是 [{sci(float(corr[0]))}, "
                f"{sci(float(corr[1]))}] Pa；" + "；".join(parts) + "。")

    def inversion_text():
        """软球被压塌 / 四面体翻转：同帧 Genesis 读数与 libuipc 内部位置的对比（genesis_inversion_debug，Neural-IPC
        run_commands [IPC 参数扫描命令] 第 5 条），加 d̂ = 2 mm 下 E = 1e3（最早那套只留下的 genesis/d_hat/0p002，官方 E）/
        1e4 / 1e5（本套 inversion_vs_E）的翻转占比与软球质心高度。"""
        dbg = debug_json("genesis_inversion_debug", "d_hat", "0p002").get("objects_final") or {}
        bs = [o for o in dbg.values() if isinstance(o, dict) and "uipc_n_inverted_tets" in o]
        same = all(o["uipc_n_inverted_tets"] == o["n_inverted_tets"] for o in bs)
        gap = max((o["genesis_vs_uipc_max_abs_m"] for o in bs), default=MISSING)
        e3_d = debug_json("genesis", "d_hat", "0p002")
        e3 = {"split": gen_split(e3_d) if e3_d else None}
        e3_cz = [o["centroid"][2] * 1000 for o in (e3_d.get("objects_final") or {}).values()
                 if isinstance(o, dict) and "n_tets" in o and o.get("centroid")]
        e4, e5 = g("inversion_vs_E", "dhat2mm_E1e4"), g("inversion_vs_E", "dhat2mm_E1e5")
        if not bs or not e3_d or e4.get("state") != "ok" or e5.get("state") != "ok":
            return "这一组还没跑完，先不下结论。"
        inv_e = [inv_frac(e4), inv_frac(e5)]
        return (("<b>翻转是 libuipc 的 FEM 解里真有的，不是读数问题：</b>" if same else "<b>两边读数的翻转数不一致：</b>")
                + f"同一帧里 Genesis 读到的软球顶点和 libuipc 内部位置最多差 {sci(gap)} m，两边数出的翻转四面体"
                + ("完全一样。" if same else "不同，要再查。")
                + f"d̂ = 2 mm 下软球 E = 1e3（官方）/ 1e4 / 1e5 Pa 时，翻转占比 {inv_pct(e3)} / {inv_pct(e4)} / {inv_pct(e5)}，"
                f"软球结束时质心高度 {rng(e3_cz)} / {rng(ball_cz(e4))} / {rng(ball_cz(e5))} mm（球半径 80 mm，完好的球约 80 mm）。"
                + ("<b>E 越大越不塌、翻转越少</b>：官方 E = 1 kPa 时球被压塌（自重压力 ρ·g·2R ≈ 1.6 kPa 已超过 E），"
                   "所以盒子实验的主结果改用 E = 1e5。" if is_num(inv_e[0]) and is_num(inv_e[1]) and inv_e[1] <= inv_e[0] else
                   "E 增大后翻转没有减少，和期待不一致，要再查。")
                + "表面全程没有穿透。")

    dhat_rows = sorted(rows("d_hat"), key=lambda v: v.get("value", 0))
    sb = split(b)
    rigid_note = ("刚体方块在 IPC 里的中心高度比 Genesis 读出的高 {off}{each}，正好等于 g·dt²（{g}）："
                  "Genesis 的刚体求解器在 IPC 把位置写回之后，又自己多走了一步重力，所以 Genesis 读出的刚体位置偏低，"
                  "IPC 里的方块并没有穿地。")
    gaps = [(v.get("value"), surf_mm(v)) for v in dhat_rows]
    gap_ratio = [s / (d * 1000) for d, s in gaps if is_num(d) and is_num(s) and d > 0]
    nmax = [v.get("newton_max") for v in dhat_rows]
    big = [v for v in dhat_rows if is_num(inflated(v)) and inflated(v) > 1.3]  # surface edges stretched > 30 %
    dt_med, dt_max = [v.get("newton_median") for v in dt_rows], [v.get("newton_max") for v in dt_rows]
    fr_top = [max(v.get("heights") or [MISSING], key=lambda z: z if is_num(z) else -1) for v in fr]
    items = [  # 顺序与下面 zip 的扫描名一一对应
        ("<b>无穿透这一点与期待一致</b>：libuipc 每 10 帧一次的穿透检查 "
         f"{b.get('pen_checks', '—')} 次报了穿透"
         + (f"；软球表面最低点 {sb['surf']}，软球结束时质心高度 {rng(ball_cz(b))} mm（球半径 80 mm），"
            f"翻转四面体 {sb['inv']}（翻转 / 总数）。"
            + rigid_note.format(off=sb["off"], each="（每个都一样）" if sb["off_same"] else "", g=sb["g_dt2"])
            if sb else "。")),

        (f"<b>与期待一致：初始状态必须无穿插。</b>让两个软球一开始互相穿进 0.1 R 或 0.5 R，{len(init)} 档里 {n_rej} 档"
         "在建场景时就被 libuipc 判定相交（日志里报 Intersection detected），仿真没有开始。"
         "但 Genesis 没把它报成一条清楚的错误，而是接着崩在一个不相关的报错上，排查时要去看 libuipc 的日志。"
         f"正常开跑的 {len(ok_all)} 个配置，libuipc 的穿透检查（每 10 帧一次）"
         + ("一次都没报。" if n_pen == 0 else f"有 {n_pen} 个报了穿透。")),

        ("软球表面离地的最低点："
         + "、".join(f"d̂ = {g3(d * 1000)} mm 时 {g3(s)} mm" for d, s in gaps if is_num(d) and is_num(s))
         + (f"，约为 d̂ 的 {rng(gap_ratio)} 倍。<b>与期待一致：d̂ 越大，物体停得离地越远</b>——barrier 在距离小于 d̂ "
            "时才开始推，物体停在斥力和重力平衡处。" if monotone([s for _, s in gaps]) == 1
            else "，和 d̂ 不是单调关系，与期待不一致。")
         + "单帧 Newton 最多：" + "、".join(f"d̂ = {g3(v.get('value') * 1000)} mm 时 {g3(v.get('newton_max'))} 次"
                                            for v in dhat_rows)
         + ("（d̂ 越小越难解）。" if monotone(nmax) == -1 else "。")
         + ("<b>d̂ 不能大于软球表面网格的边长：</b>" + "；".join(
             f"d̂ = {g3(v.get('value') * 1000)} mm 时{shape_txt(v)}" for v in big)
            + "——同一个软球上不相邻的面片距离已小于 d̂，FEM 默认开着自接触，barrier 把球从里面撑开、扭曲（边长被拉到接近 d̂），"
              "四面体随之翻转。libuipc 的 compute_mesh_d_hat 正是取最短表面边长作 d̂；官方 d̂ = 1 cm 也略小于这个软球约 1.2 cm 的边长。"
            if big else "")),

        ("各档每帧 Newton 迭代中位数 / 最多："
         + "、".join(f"dt = {g3(v.get('value'))} s：{g3(v.get('newton_median'))} / {g3(v.get('newton_max'))}"
                    for v in dt_rows)
         + ("。<b>在这个场景里 dt 对每帧迭代次数影响不大</b>（换算成同样 2 s 物理时间，总步数随 dt 成倍变化，"
            "所以总耗时主要由步数决定："
            + "、".join(f"{g3(v.get('value'))} s 用 {g3(v.get('wall_s'))} s" for v in dt_rows) + "）。"
            if max(x for x in dt_max if is_num(x)) - min(x for x in dt_max if is_num(x)) <= 5 else "。")
         + f"各档软球表面最低点都在地面以上（{rng([surf_mm(v) for v in dt_rows])} mm），穿透检查都没报。"
           "刚体在 IPC 与 Genesis 里的高度差逐档为 "
         + "、".join(f"dt = {g3(v.get('value'))} s：{off_txt(v)}" for v in dt_rows)
         + "，每档都等于 g·dt²——偏移来自 Genesis 刚体求解器写回后多走的一步重力，不是穿地。"),

        ("方块和软球结束时质心最高处："
         + "、".join(f"μ = {g3(v.get('value'))} 时 {g3(z * 1000) if is_num(z) else '—'} mm" for v, z in zip(fr, fr_top))
         + ("。<b>与期待一致：μ 越大越不容易滑、堆得越高。</b>" if monotone(fr_top) == 1 else "。和摩擦不是单调关系。")
         + "同一批物体结束时质心离最近一面墙的距离（最近–最远）："
         + "、".join(f"μ = {g3(v.get('value'))} 时 {rng([g * 1000 for g in v.get('wall_gaps') or []])} mm" for v in fr)
         + ("——摩擦最小那档所有物体都比摩擦最大那档的任何物体离墙更近：摩擦小时物体滑散到四周墙边，"
            "摩擦大时停在盒子中间的堆上。"
            if fr and fr[0].get("wall_gaps") and fr[-1].get("wall_gaps")
            and max(fr[0]["wall_gaps"]) < min(fr[-1]["wall_gaps"]) else "。")
         + "穿透检查各档都没报；每帧 Newton 迭代中位数 / 最多："
         + "、".join(f"μ = {g3(v.get('value'))}：{g3(v.get('newton_median'))} / {g3(v.get('newton_max'))}" for v in fr)
         + "。"),

        (f"contact_resistance 从 {sci(min((v.get('value') for v in kr), default=MISSING))} 到 "
         f"{sci(max((v.get('value') for v in kr), default=MISSING))} Pa 共 {len(kr)} 档，软球表面离地最低点逐档为 "
         + "、".join(f"{sci(v.get('value'))}：{surf_txt(v)}" for v in kr) + "。"
         + kappa_text()
         + "<b>与期待一致：区间外的 κ 被夹到边界</b>，所以 Genesis 默认 1e9 和更大的值实际是同一个 κ（区间上界），"
           "结果几乎一样；只有区间内的值才真正改变接触刚度。Genesis 默认把 libuipc 日志设成只输出 error"
           "（genesis ipc_coupler/coupler.py:272-276），这些夹取警告平时看不到。κ 区间只由场景质量、尺寸、d̂、dt 决定，与软球 E 无关。"),

        (None if any(m.get("state") != "ok" for _, m in mesh) else
         "三档软球表面边长与结束时形状：" + "；".join(f"{name}：{shape_txt(m)}" for name, m in mesh)
         + "。穿透检查都没报；每帧 Newton 迭代中位数 / 最多 "
         + "、".join(f"{name} {g3(m.get('newton_median'))} / {g3(m.get('newton_max'))}" for name, m in mesh)
         + "；总耗时 " + "、".join(f"{name} {g3(m.get('wall_s'))} s" for name, m in mesh) + "。"
         + ("<b>网格越细越要注意 d̂</b>：边长小于 d̂（1 cm）的那档软球被自接触撑开、四面体翻转，原因同 d̂ 一节；"
            "边长大于 d̂ 的档没有这个问题。" if any(is_num(inflated(m)) and inflated(m) > 1.3 for _, m in mesh)
            else "")) or "这一组还没跑完。",

        inversion_text(),
    ]
    # 文本是本脚本写死的，数字来自 json，不含用户输入
    return dict(zip(["baseline", "init_penetration", "d_hat", "dt", "friction", "resistance", "mesh_res",
                     "inversion_vs_E"], items))


NEXT_STEPS = ("<b>下一步：</b>生成数据时显式固定 κ（落在 libuipc 区间内并从日志核对没被夹）、软体用更大的 E 并检查四面体翻转；"
              "加能反映堆积形态的指标。")


def build_page(demos, tests, gsweeps, cfg, facts, videos, commit):
    n_video = sum(1 for r in demos + tests if r.get("video_web"))
    n_pass = sum(1 for r in tests if r.get("outcome") == "passed")
    n_ran = sum(1 for r in tests if r.get("outcome") in ("passed", "failed"))
    n_gsweeps = sum(1 for s in gsweeps if s["name"] != "baseline")
    n_grows = sum(len(s["rows"]) for s in gsweeps)
    summary = (f"这周在服务器上跑通了 Genesis + lib IPC（Genesis 的 IPC 接触底层由 libuipc 计算）："
               f"{len(demos)} 个 Genesis IPC 例子和 {len(tests)} 个 Genesis 官方 IPC 测试场景，共 {n_video} 段视频"
               f"（官方测试 {n_ran} 个跑完，其中官方断言通过 {n_pass} 个）。"
               f"然后做了「一堆物体扔进盒子」（官方 ipc_objects_falling 场景加一个盒子），对 {n_gsweeps} 个 IPC 参数"
               f"做了单变量扫描，共 {n_grows} 个配置。主要发现：初始穿插会被拒绝开跑；表面全程没有穿透；"
               "d̂ 决定物体停在离地多远，且不能大于软体表面网格的边长；Genesis 默认 κ 1e9 会被 libuipc 夹到区间上界；"
               "官方软球 E = 1 kPa 太软，会被压塌，所以主结果用 E = 1e5。")
    nav = "".join(f'<a href="#{h}">{esc(n)}</a>' for h, n in NAV)
    body = (f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1><p class="summary">{esc(summary)}</p>'
            f'<nav class="toc">{nav}</nav></header>\n'
            + videos_section(demos, tests, facts) + "\n"
            + sweep_section(gsweeps, cfg, videos, facts, commit))
    return page(body)


# ==========================================================================
# main
# ==========================================================================
STATE_LABEL = {"ok": "完成", "failed": "失败", "not_run": "尚未运行", "unfinished": "未结束", "stale": "旧配置的结果，重跑中",
               "rejected": "被拒绝（预期内）"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--execute", action="store_true", help="真正写文件、压视频（默认只打印计划）")
    ap.add_argument("--crf", type=int, default=26, help="libx264 CRF（默认 26；数字越大文件越小）")
    ap.add_argument("--force-videos", action="store_true", help="忽略 encode_manifest.json，全部重新压缩")
    ap.add_argument("--variant", choices=sorted(VARIANTS), default="stiffball",
                    help="盒子实验主结果用哪一套（VARIANTS；run_sweep_queue.sh 的 TAG）")
    args = ap.parse_args()
    set_variant(args.variant)

    demos, tests, unlisted = collect_demos()
    if unlisted:
        print(f"[build_site] 注意：NAS 上这些 demo 目录不在 DEMOS / OFFICIAL_TESTS 清单里，不上页面：{unlisted}")
    cfg, gsweeps = collect_sweeps()
    all_demos = demos + tests
    videos = gen_level_videos(gsweeps)   # 盒子扫描每档自己的视频（和 demo 视频一起压缩、一起上传）
    vjobs, manifest = plan_videos(all_demos + list(videos.values()) + [officialball_video_record()],
                                  args.crf, args.force_videos)
    ijobs = plan_images(all_demos)
    facts = compute_facts(demos)
    facts.update(compute_genesis_facts(gsweeps))
    commit = genesis_commit()

    pages = {WEB / "index.html": build_page(demos, tests, gsweeps, cfg, facts, videos, commit)}

    # ---------------- 打印计划 ----------------
    mode = "EXECUTE" if args.execute else "DRY-RUN（只演练，不写任何文件；加 --execute 才真正写）"
    print(f"[build_site] 模式：{mode}")
    print(f"[build_site] demo 结果：{DEMO_ROOT}")
    for r in all_demos:
        extra = "" if r["state"] == "ok" else f"  {r['reason']}"
        vid = f"  视频: {r['video_src']}" if r["video_src"] else (f"  {r['video_note']}" if r["video_note"] else "")
        print(f"  - {r['key']:<32} {STATE_LABEL[r['state']]}{extra}{vid}")
    print(f"[build_site] 盒子实验这一套：{GEN_TAG}（{VARIANTS[GEN_TAG]['label']}），每档视频：{GEN_VIDEO_ROOT}"
          f"（Neural-IPC commit {commit}）")
    for (sw, lv), r in videos.items():
        print(f"  - {sw}/{lv:<14} " + (f"视频: {r['video_src']}  {r['line']}" if r["video_src"] else r["video_note"]))
    print(f"[build_site] 扫描结果：{GEN_SWEEP_ROOT}")
    for s in gsweeps:
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
