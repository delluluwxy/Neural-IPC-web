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

# Recordings the user made on their own machine (no run_info.json: nothing was recorded on the server for these runs);
# (card key, video file on NAS, title, what it shows - in the user's words where the server has no data to check it)
USER_RECORDINGS = [
    ("user_local_ipc_robot_cloth_teleop",
     DEMO_ROOT / "user_local_ipc_robot_cloth_teleop" / "2026-10-03 03-46-59.mkv",
     "机械臂遥控抓起一块布（官方例子，用户本地键盘操作录屏）",
     "用户在自己的 Windows 笔记本上运行官方 ipc_robot_cloth_teleop.py（Genesis 1.4.2，带窗口），用键盘遥控录屏，"
     "抓起了一块布（用户告知；这次运行在服务器上没有数据，以视频为准）。按键以代码为准：方向键水平移动，j / k 下 / 上，"
     "空格按住才合夹子；官方文件开头的按键说明与代码不一致。"),
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
# What the server runs found for a test (why an assertion failed, or what the data shows; 2026-10-02/03, from
# official_test.json and the --rigid-trajectory / diagnostic reruns), shown on the card next to the outcome.
OFFICIAL_TEST_NOTES = {
    "genesis_test_test_objects_colliding_0": "蓝色的是布料。官方相机几乎平视，所以看起来像一块板；实测布料开始是平的，"
                                             "最后高低差 127 mm：中间被方块和球顶起、四周垂下，确实是软布。",
    "genesis_test_test_ground_clearance_0": "期待：接触刚度越大，方块离地越高。实测 5 个方块离地 6.39 / 6.39 / 6.39 / 7.00 / "
                                            "8.18 mm，前 3 个一样，所以没过。原因：libuipc 会把接触刚度夹进按场景算出的区间，"
                                            "前 3 个方块的刚度低于下限，被夹成同一个值（同下方「接触刚度 κ」实验）。",
    "genesis_test_test_ground_sliding_0": "期待：离地高度与摩擦无关。实测 μ = 0.04 的方块比其他的高 9.3 mm：它从第 66 步起"
                                          "往前翻，最后倾斜 16°。原因：libuipc 默认的半隐式提前终止让 Newton 没算到收敛就停，"
                                          "误差积累成翻倒；只关掉它，最大倾角 0.24°，断言通过（把步长减半也能通过，0.31°）。",
}

PAGE_TITLE = "Neural-IPC 周汇报"
NAV = [("videos", "Demo 视频"), ("tests", "官方测试场景"), ("sweep", "盒子实验与参数扫描"), ("data", "对生成数据的意义")]  # 锚点只用字母


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
                 + OFFICIAL_TEST_NOTES.get(key, ""))
    scenes = rec.get("scenes") or []
    vp = Path(scenes[0]["video"]) if scenes else None
    if vp and vp.is_file() and vp.stat().st_size > 0:
        r["video_src"] = vp
    else:
        r["video_note"] = "没有录到视频"
    return r


def collect_user_recording(key, video, title, line):
    """A USER_RECORDINGS entry as a demo record (same fields as collect_demo), with the video file as its source."""
    ok = video.is_file() and video.stat().st_size > 0
    return {"key": key, "expects_video": True, "title": title, "line": line, "dir": video.parent,
            "info_path": None, "info": None, "state": "ok" if ok else "not_run",
            "reason": None if ok else f"找不到录屏文件 {video}", "video_src": video if ok else None,
            "video_note": None, "images": []}


def collect_demos():
    """The page shows exactly the DEMOS and OFFICIAL_TESTS lists. Other directories under DEMO_ROOT (libuipc-only
    demos, timing runs, anything unexpected) are NOT put on the page; main() prints them so nothing is hidden.
    Returns (demos, official tests, unlisted dirs)."""
    known = {k for k, *_ in DEMOS} | {k for k, *_ in OFFICIAL_TESTS} | {k for k, *_ in USER_RECORDINGS}
    demos = [collect_demo(*spec) for spec in DEMOS] + [collect_user_recording(*spec) for spec in USER_RECORDINGS]
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
    head = f'<h3>{esc(r["title"])}</h3>'
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
# contact_eps_velocity default: libuipc scene_default_config.cpp contact/eps_velocity
GENESIS_DEFAULTS = {"friction_mu": 0.1, "contact_resistance": 1e9, "overlap_balls": 0.0, "ball_subdiv": None,
                    "ball_E": 1.0e3, "contact_eps_velocity": 0.01}
GENESIS_KEYS = {"friction": "friction_mu", "resistance": "contact_resistance", "init_penetration": "overlap_balls",
                "mesh_res": "ball_subdiv",
                "eps_velocity": "contact_eps_velocity"}  # sweep -> override key in configs
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
     "所以全程不该有穿透。"),
    ("d_hat", "d̂（barrier 作用距离）", "改 contact_d_hat。官方本例取 1 cm，Genesis 注释说应按网格分辨率取。", "d̂（mm）",
     "d̂ 是 barrier 开始起作用的距离。d̂ 变小，停住时每一对接触面之间的间隙跟着变小，接触更“硬”，"
     "Newton 迭代一般会变多；d̂ 变大，物体隔得更远就被推开。不论 d̂ 取多少，都不该出现穿透。"),
    ("dt", "时间步长 dt", "改 SimOptions.dt。官方本例 0.02 s；物理时长固定 2 s，帧数随 dt 变。", "dt（s）",
     "dt 变小，每一步物体移动得更少；但 libuipc 会按 1/dt² 抬高接触刚度的下限，接触更硬，"
     "所以每步的 Newton 次数不一定减少，总步数则成倍增加。不论 dt 多大都不该穿透。"),
    ("friction", "摩擦系数 μ", "改所有 FEM 物体（布料和软球）的 friction_mu，与接触对象按几何平均组合；Genesis 默认 0.1。",
     "μ",
     "μ 只管物体互相滑动时的切向阻力：μ 越大越不容易滑，堆得越陡。它不改变法向的 barrier，"
     "所以不该影响会不会穿透，对 Newton 次数的影响也应该很小。"),
    ("resistance", "接触刚度 κ", "改 Genesis 的 contact_resistance，默认 1e9 Pa。", "设的 κ（Pa）",
     "κ 是 barrier 的刚度。libuipc 会把它夹进一个按场景算出的区间：在区间内，κ 越大接触越硬，物体陷进 barrier 越浅，"
     "间隙越接近 d̂；区间外的值会被夹到边界，结果应该和边界值一样。"),
    ("init_penetration", "初始穿插", "多放一个软球，让它和官方软球一开始就互相穿进去一部分（R 为球半径）。",
     "初始状态",
     "IPC 的 barrier 只在两个表面距离为正时才有定义，一开始就穿插的话能量没有意义，"
     "所以 libuipc 的初始化检查应该直接拒绝开跑。"),
    ("mesh_res", "网格分辨率", "把软球换成同样大小、表面细分 2 / 3 / 4 次的 icosphere，由 Genesis 自己四面体化。",
     "软球网格",
     "网格越细，球面越接近真球，接触时参与的顶点越多，每步要解的未知数越多、越慢。"
     "只要初始无穿插，网格粗细都不该影响会不会穿透，物体落地后的大致位置应该接近。"),
    ("eps_velocity", "静摩擦判定速度 ε_v", "改 contact_eps_velocity（libuipc 默认 0.01 m/s）：相对滑动速度低于它按静摩擦处理。",
     "ε_v（m/s）",
     "ε_v 越大，越慢的滑动都被当成“粘住”，物体更容易停住、不容易慢慢滑走；ε_v 越小越接近真实的库仑摩擦，"
     "静止的物体可能还在缓慢滑移。不影响法向间隙。"),
]


# What each experiment looks at (its SWEEP_COLUMNS) and why
OBSERVE = {
    "baseline": "停住时每一对接触面（球–地、球–布、布–方块……）之间的最短距离，每帧 Newton 迭代几次（好不好解）。",
    "d_hat": "每一对接触面之间的最短距离（是否跟着 d̂ 变）、每帧 Newton 迭代几次。",
    "dt": "每帧 Newton 迭代几次、总耗时，以及停住时各接触面之间的间隙（接触变硬没有）。",
    "friction": "最高物体有多高（堆起来还是摊开）、物体离墙多远（有没有滑散）。",
    "resistance": "每一对接触面之间的最短距离：刚度真的变了，间隙就该跟着变。",
    "init_penetration": "libuipc 开跑前检查的日志原文。",
    "mesh_res": "libuipc 实际用的 κ（日志原文）、停住时各接触面之间的间隙、总耗时。",
    "eps_velocity": "最高物体有多高、物体离墙多远（滑没滑散），以及各接触面之间的间隙。",
}
# Each experiment's conclusion in one or two sentences (the numbers are in the table right above it; the verified
# data behind each sentence is in Neural-IPC docs/claude_todo.md and the meeting outline section 4)
FINDINGS = {
    "baseline": "与期待一致：全程没有穿透；停住时每一对接触面（不只是和地面，也包括球–布、布–方块）之间都留着一条"
                "0.7–1 倍 d̂ 的缝，物体停在 barrier 起作用的那一层里。越重的接触缝越小：方块压地约 0.8 d̂，"
                "软球约 0.85 d̂，很轻的布只陷到约 0.97 d̂——barrier 的推力随间隙变小急剧增大，越重越要陷得深才托得住。",
    "d_hat": "与期待一致：所有接触的间隙都跟着 d̂ 走（始终约 0.65–1 倍 d̂），d̂ 越小物体靠得越近，但每帧 Newton 迭代越多"
             "（越难解）。d̂ 不能大于软体表面网格的边长：d̂ = 30 mm 时自接触把球从里面撑开（表面的边被拉长到约 2 倍）。",
    "dt": "与期待一致：每帧迭代次数基本不随 dt 变，总耗时随步数成倍增加；dt 越小，接触间隙越接近 d̂（libuipc 按 1/dt² 抬高接触刚度下限，接触更硬、物体陷得更浅）。",
    "friction": "与期待一致：μ 小时物体滑散到墙边，μ 大时堆在中间；μ 不影响会不会穿透。",
    "resistance": "与期待一致：libuipc 只认这个场景允许的区间 [1.57e5, 1.57e7] Pa。区间内（2e5 → 1e6 → 3e6 → 1e7）"
                  "κ 越大接触越硬、物体陷得越浅，各接触面的间隙一路变大（软球–地面 4.6 → 6.5 → 7.4 → 8.2 mm，d̂ = 10 mm）；"
                  "区间外被夹到边界：1e4 被夹到下限、结果和 2e5 差不多，默认 1e9 被夹到上限、结果和 1e7 差不多。"
                  "所以在 Genesis 里设 contact_resistance 只有落在这个区间里才有用。",
    "eps_velocity": "ε_v 从 0.001 到 0.1 m/s，接触间隙、最高物体高度和物体离墙距离都没有明显变化：这个场景里物体最后都"
                    "停住了，ε_v 只管很慢的滑动算不算粘住，要看出它的作用需要有持续慢速滑动的场景（比如斜面），这里没有。",
    "init_penetration": "与期待一致：一开始就穿插时，libuipc 直接拒绝开跑。IPC 必须从无穿透的状态开始。",
    "mesh_res": ("libuipc 整个场景只用一个 κ，按全场景所有顶点的平均质量定区间："
                 "软球网格越密，顶点越多、平均质量越小，κ 区间整体往下移（默认 1e9 被夹到的上限从粗网格的 4.9e7 降到细网格的 "
                 "3.4e6）。每对接触的 barrier 不按面积加权，所以加密的软球自己多了接触点、大致抵消（软球–地面间隙 8.4–8.7 → "
                 "6.4 mm）；而网格没变的方块、布没有抵消，接触明显变软（方块–地面 8.1–8.5 → 4.1 mm）。也就是说加密一个物体"
                 "会让场景里其他物体的接触变软。另外最细那档表面边长已小于 d̂，自接触把球撑开；网格越细越慢。"),
}


def gen_defaults_text(gd):
    """官方默认参数一句话（dt、d̂ 读官方默认档的结果文件，κ、μ 是 Genesis 默认值）。"""
    dh = dig(gd, "libuipc_config", "contact", "d_hat")
    ball_e = (gd.get("overrides") or {}).get("ball_E", GENESIS_DEFAULTS["ball_E"])
    return (f"dt = {g3(gd.get('dt', MISSING))} s、d̂ = {g3(dh * 100) if is_num(dh) else '—'} cm、"
            f"κ = {sci(GENESIS_DEFAULTS['contact_resistance'])} Pa、μ = {g3(GENESIS_DEFAULTS['friction_mu'])}、"
            f"软球用官方 Sphere 网格、软球 E = {sci(ball_e)} Pa"
            + ("（官方是 1e3）" if ball_e != GENESIS_DEFAULTS["ball_E"] else "（官方值）"))


def col_newton(d):
    s = d.get("summary") or {}
    return f"{g3(s.get('newton_iter_frame_stats_median'))} / {g3(s.get('newton_iter_frame_stats_max'))}"


def col_wall_seconds(d):
    return g3(dig(d, "summary", "wall_seconds_total"))


def col_top(d):
    """Highest centroid among the boxes and soft balls (cloth excluded) at the last frame, mm (piled up or spread)."""
    zs = [o["centroid"][2] for n, o in (d.get("objects_final") or {}).items() if "Cloth" not in n and o.get("centroid")]
    return g3(max(zs) * 1000) if zs else "—"


def col_wall_gap(d):
    """Range of the boxes' and soft balls' centroid distances to the nearest wall at the last frame, mm."""
    a = d.get("box_inner_half")
    gaps = [a - max(abs(o["centroid"][0]), abs(o["centroid"][1])) for n, o in (d.get("objects_final") or {}).items()
            if "Cloth" not in n and o.get("centroid")] if is_num(a) else []
    return f"{g3(min(gaps) * 1000)}–{g3(max(gaps) * 1000)}" if gaps else "—"


def contact_kind(name):
    """Readable kind of a contact_gaps_final name (sweep.py: "<i>_Sphere_Elastic" or "<i>_Mesh_Elastic" (icosphere balls
    of mesh_res), "<i>_Box_Rigid", "<i>_Mesh_Cloth", "<i>_wall", "ground")."""
    for key, kind in (("Elastic", "软球"), ("Box", "方块"), ("Cloth", "布"), ("wall", "墙"), ("ground", "地面")):
        if key in name:
            return kind
    return name


def col_contact(d):
    """Gaps of the surface pairs in contact at the last frame (shortest surface-to-surface distance below d_hat, where
    libuipc's barrier acts), grouped by the kinds of the two surfaces (sweep.py contact_gaps_final). Levels run before
    sweep.py recorded them are read from their rerun under genesis_<set>_contact/."""
    c = d if "contact_gaps_final" in d else load_json(
        SWEEP_ROOT / f"genesis_{GEN_TAG}_contact" / d["sweep"] / f"{d['level']}.json")[0]
    dh = dig(c, "libuipc_config", "contact", "d_hat") if isinstance(c, dict) else MISSING
    if not (isinstance(c, dict) and c.get("status") == "ok" and "contact_gaps_final" in c and is_num(dh)):
        return "—"
    groups = {}
    for g in c["contact_gaps_final"]:
        if g["distance_m"] < dh:
            key = "–".join(sorted((contact_kind(g["a"]), contact_kind(g["b"])), key="软球方块布墙地面".find))
            groups.setdefault(key, []).append(g["distance_m"] * 1000)
    return "；".join(f"{k} {g3(min(v))}" + (f"–{g3(max(v))}" if len(v) > 1 else "") + f"（{len(v)} 处）"
                    for k, v in sorted(groups.items())) or "没有"


def col_kappa(d):
    """The contact stiffness libuipc actually used, from its own log lines (sweep.py kappa_log, needs --log-level
    Debug; a level run without it is read from its Debug rerun under genesis_<set>_kdebug/): the clamped value when
    the set kappa fell outside the scene's corridor, else the set value. Pairs logged with kappa 0 are Genesis's
    disabled pairs (its no-collision element, coupler.py:717-720) and are skipped."""
    kl = d.get("kappa_log") or {}
    if not kl.get("kappa_corridor"):
        kl = (load_json(SWEEP_ROOT / f"genesis_{GEN_TAG}_kdebug" / d["sweep"] / f"{d['level']}.json")[0] or {}) \
            .get("kappa_log") or {}
    corr = [h["groups"] for h in kl.get("kappa_corridor", [])]
    clamped = sorted({float(h["groups"][3]) for h in kl.get("model_kappa_clamped", []) if float(h["groups"][0]) > 0})
    if not corr:
        return "—（这次没开 libuipc 日志）"
    lo, hi = sci(float(corr[0][0])), sci(float(corr[0][1]))
    if clamped:
        return f"被夹到 {'、'.join(sci(k) for k in clamped)}（允许区间 [{lo}, {hi}]）"
    return f"没被夹，用设的值（允许区间 [{lo}, {hi}]）"


NEWTON, SECONDS = ("每帧 Newton 迭代（中位 / 最多）", col_newton), ("仿真总耗时（s）", col_wall_seconds)
KAPPA = ("libuipc 实际用的 κ（Pa，日志原文）", col_kappa)
CONTACT = ("停住时各接触面之间的间隙（mm，只列小于 d̂ 的）", col_contact)
TOP, WALL = ("最高物体的高度（mm）", col_top), ("物体离墙（mm）", col_wall_gap)
# The columns each experiment's table shows: only the quantities its conclusion is about, so the trend reads at a
# glance. Penetration is the same for every level (none found) and is stated once in the section text instead.
# CONTACT measures each gap to the surface an object actually touches (cloth, another object, a wall or the ground).
SWEEP_COLUMNS = {"baseline": [CONTACT, NEWTON], "d_hat": [CONTACT, NEWTON], "dt": [CONTACT, NEWTON, SECONDS],
                 "friction": [TOP, WALL], "resistance": [KAPPA, CONTACT], "init_penetration": [],
                 "mesh_res": [KAPPA, CONTACT, SECONDS],
                 "eps_velocity": [TOP, WALL, CONTACT]}


def gen_raw_cells(r, sweep):
    """One level's row: SWEEP_COLUMNS[sweep] read from its result file."""
    cols = SWEEP_COLUMNS[sweep]
    if r["state"] != "ok":
        return [r["reason"]] + ["—"] * (len(cols) - 1)
    return [fn(r["data"]) for _, fn in cols]


def genesis_sweep_tables(sweeps, cfg):
    """[(扫描, 标题, 一句话, 排好序的行, 原始数字表)]；每张表都带官方默认那一档。"""
    base = rows_of(sweeps, "baseline")
    out = []
    for sweep, title, one, head, expect in GENESIS_TABLES:
        ref = base
        rows = list(base) if sweep == "baseline" else list(rows_of(sweeps, sweep)) + list(ref)

        def key(r):
            if sweep == "baseline":
                return 0
            if r["sweep"] == "baseline" and sweep == "mesh_res":  # 官方网格放最前，其余按细分次数
                return -float("inf")
            v = gen_level_value(r, sweep, cfg)
            return v if is_num(v) else float("inf")
        rows.sort(key=key)
        cols = SWEEP_COLUMNS[sweep]
        trs = [[gen_label(r, sweep, cfg)] + gen_raw_cells(r, sweep) for r in rows]
        out.append((sweep, title, one, expect, rows,
                    table([head] + [h for h, _ in cols], trs) if cols else ""))
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
            out[(r["sweep"], r["level"])] = {
                "key": f"box{GEN_TAG and '_' + GEN_TAG or ''}_{r['sweep']}_{r['level']}", "expects_video": True,
                "title": "", "line": "", "dir": d, "state": "ok", "reason": None, "images": [],
                "video_src": mp4 if done else None,
                "video_note": None if done else ("视频未生成" if code is None else f"视频未生成（录像退出码 {code}）")}
    return out


def init_frame_image(level):
    """(source png, published name) of the frame-0 picture of an initial-penetration level (run_genesis_ipc_example.py
    --initial-frame, written next to that level's video), or None when it was not rendered."""
    src = GEN_VIDEO_ROOT / f"init_penetration_{level}" / "initial_frame.png"
    return (src, f"box{GEN_TAG and '_' + GEN_TAG or ''}_init_penetration_{level}.png") if src.is_file() else None


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


def genesis_experiment(sweep, title, one, expect, rows, tbl, cfg, videos, gd):
    """One experiment, in the order of Neural-IPC AGENTS.md (expectation, setup and what is observed, videos (or the
    log excerpt for the rejected initial-penetration levels) with a table of only SWEEP_COLUMNS[sweep], conclusion)."""
    if sweep == "baseline":
        setting = f"{one}（{gen_defaults_text(gd)}）。"
    else:
        vals = "、".join(gen_label(r, sweep, cfg) for r in rows if r["sweep"] != "baseline")
        setting = f"{one}取值：{vals}；其余参数保持官方默认（{gen_defaults_text(gd)}），表里也放了官方默认那一档对照。"
    parts = [f"<h3>{esc(title)}</h3>", f"<p>{esc(expect)}</p>",
             f"<p class=\"small\">{esc(setting)}看：{esc(OBSERVE[sweep])}</p>"]
    if sweep == "init_penetration":
        for r in rows:
            if r["sweep"] == "baseline":
                continue
            lines, n_inter = init_log_excerpt((r["data"] or {}).get("log_path")
                                              or GEN_SWEEP_ROOT / sweep / f"{r['level']}.log")
            parts.append(f'<p class="small"><b>{esc(gen_label(r, sweep, cfg))}</b>：开跑前就被拒，没有视频。'
                         "左边是这个初始状态（同一场景只关掉开跑前的相交检查、只渲染第 0 帧，侧面看两个软球；球心相距 2R 减去穿插量，不穿插时至少 2R），"
                         "右边是被拒那次的日志原文摘录。</p>")
            img = init_frame_image(r["level"])
            log = (f'<pre class="log">{esc(chr(10).join(lines))}</pre>' if lines is not None
                   else '<div class="novideo">日志文件不存在</div>')
            parts.append('<div class="grid">' + (f'<div class="demo"><img class="plot" src="assets/images/{img[1]}" '
                                                 'alt="初始状态：两个软球互相穿插" loading="lazy"></div>' if img else "")
                         + f'<div class="demo">{log}</div></div>')
    else:
        cards = [demo_card(dict(videos[(r["sweep"], r["level"])], title=gen_label(r, sweep, cfg)), {})
                 for r in rows if (r["sweep"], r["level"]) in videos]
        parts.append('<div class="grid">' + "".join(cards) + "</div>")
    parts.append(tbl)
    parts.append(f'<p class="concl"><b>结论：</b>{esc(FINDINGS[sweep])}</p>')
    return "\n".join(parts)


def sweep_section(gsweeps, cfg, videos):
    """盒子实验：set_variant 选的那套（主结果）的每个参数扫描，然后是方案 3（官方软球不被压）一节。"""
    gb = rows_of(gsweeps, "baseline")
    gd = gb[0]["data"] if gb and gb[0]["data"] else {}
    gscene = ("Genesis 没有「一堆物体扔进盒子」的官方例子，这里用官方 ipc_objects_falling 场景加一个盒子和更多同款物体。"
              f"这一套：{VARIANTS[GEN_TAG]['label']}。所有实验每次只改一个参数。盒子墙画成半透明，盒内物体不透明。"
              "除了一开始就穿插的两档（开跑前被拒），所有档位 libuipc 的穿透检查都没有报穿透。")
    parts = ['<section id="sweep"><h2>盒子实验与 IPC 参数扫描</h2>', f"<p>{esc(gscene)}</p>"]
    for sweep, title, one, expect, rows, tbl in genesis_sweep_tables(gsweeps, cfg):
        parts.append(genesis_experiment(sweep, title, one, expect, rows, tbl, cfg, videos, gd))
    parts.append(officialball_block())
    parts.append("</section>")
    return "\n".join(parts)


# What the experiments above mean for generating training data: (what to do, which result above it rests on)
DATA_IMPLICATIONS = [
    ("接触刚度 κ 要显式设定，并从 libuipc 日志确认没被夹。",
     "κ 实验：Genesis 的 contact_resistance 只有落在 libuipc 按场景算出的区间里才生效，区间外一律被夹到边界。"),
    ("同一批数据里每个样本用同一个显式 κ，不能交给 libuipc 自动选。",
     "网格实验：κ 区间按全场景所有顶点的平均质量定，加密一个物体会让其他物体的接触变软（方块–地面间隙 8.1–8.5 → "
     "4.1 mm）；dt 实验：区间下限随 1/dt² 变，dt 越小接触越硬。"),
    ("d̂ 是数据里接触的尺度：要记录下来，且不能大于物体最短的表面边长。",
     "d̂ 实验：每一对接触都停在 0.65–1 倍 d̂ 的地方；d̂ 越小越贴近、但每帧 Newton 迭代越多；d̂ = 30 mm 大于软球表面边长时，"
     "自接触把球从里面撑开。"),
    ("初始状态必须无穿透，生成初始条件时先检查。", "初始穿插实验：一开始就互相穿插时 libuipc 拒绝开跑。"),
    ("关掉 libuipc 的半隐式提前终止，或者逐帧检查是否真的收敛。",
     "官方测试「地面滑动」：它默认开着，Newton 没算到收敛就停，误差积累成方块翻倒；关掉后断言通过。"),
    ("软体材料要足够硬，并检查四面体有没有被压翻。", "方案 3：官方软球 E = 1 kPa 只靠自重就被压到一半高。"),
    ("位置从 libuipc 那一侧读，不用 Genesis 的读数。",
     "Genesis 在 libuipc 写回位置后又多走一步重力，刚体读数比 IPC 里低 g·dt²（dt = 0.02 s 时约 3.9 mm，和 d̂ 同一量级）。"),
    ("同一个初始条件在 GPU 上跑两次结果不逐位相同，不能假设可重复。",
     "同参数的两次运行，物体最后的水平落点能差 80 mm。"),
]


def data_section():
    """The training-data implications of the experiments, one item per DATA_IMPLICATIONS entry (what to do + basis)."""
    items = "".join(f"<li><b>{esc(what)}</b><br><span class=\"small\">依据：{esc(why)}</span></li>"
                    for what, why in DATA_IMPLICATIONS)
    return f'<section id="data"><h2>对生成训练数据的意义</h2><ol>{items}</ol></section>'


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
            rows.append([f"{sweep} / {level}", g3(c[2] * 1000)])
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
             + " mm。")
    return ("<h3>方案 3：保留官方软球（E = 1e3）、不在它上面压东西</h3>"
            "<h4>实验设置</h4><p>同一个盒子场景，软球保持官方材料 E = 1e3 Pa（只有 inversion_vs_E 两档按档位改 E），"
            "额外的 2 个方块和 3 个软球落在离官方软球水平 0.69 m 以上的四角和边上，官方布料照常落下。"
            "其余参数按各扫描档位变化，和方案 1 是同一套档位。</p>"
            "<h4>按原理期待的结果</h4><p>官方软球上面不压东西，它只受自重和布料；E = 1 kPa 远小于自重压力"
            "（ρ·g·2R ≈ 1.6 kPa），所以即使不被压，也会被自重压扁一部分。</p><h4>原始输出</h4>"
            + (f'<div class="grid"><div class="demo"><h3>官方参数那档的视频（官方相机）</h3><video controls muted playsinline '
               f'preload="metadata" src="{video}"></video></div></div>' if video else "")
            + table(["档位", "官方软球结束时质心高度（mm，完好约 80）"], rows)
            + f'<p class="concl"><b>结论：</b>{concl}</p>')


def monotone(vals):
    """+1 if vals never decrease, -1 if they never increase, 0 otherwise (a non-number entry gives 0)."""
    if not vals or not all(is_num(v) for v in vals):
        return 0
    up = all(b >= a for a, b in zip(vals, vals[1:]))
    down = all(b <= a for a, b in zip(vals, vals[1:]))
    return 1 if up and not down else -1 if down and not up else 0


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
               "d̂ 越小越难解，且不能大于软体表面网格的边长；Genesis 默认 κ 1e9 会被 libuipc 夹到区间上界；"
               "官方软球 E = 1 kPa 太软，会被压塌，所以主结果用 E = 1e5。")
    nav = "".join(f'<a href="#{h}">{esc(n)}</a>' for h, n in NAV)
    body = (f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1><p class="summary">{esc(summary)}</p>'
            f'<nav class="toc">{nav}</nav></header>\n'
            + videos_section(demos, tests, facts) + "\n"
            + sweep_section(gsweeps, cfg, videos) + "\n" + data_section())
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
    ijobs = plan_images(all_demos) + [{"src": src, "dst": IMAGE_DIR / name, "size": src.stat().st_size}
                                      for src, name in filter(None, (init_frame_image(r["level"])
                                                                     for r in rows_of(gsweeps, "init_penetration")))]
    facts = compute_facts(demos)
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
