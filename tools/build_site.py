"""生成 Neural-IPC 周汇报网页（单页静态 HTML，发布为 claude.ai 私有 Artifact）。

读者是课题组同学：懂 IPC，但没亲手跑过这些东西。页面只放三样：demo 视频、参数扫描小表、发现 / 结论。
数字全部从 NAS 上的结果文件读，缺失或失败的如实写成人话；页面不放 NAS 路径等出处信息。

    # 默认只演练：打印每项结果的状态、页面里用到的关键数字、将写哪些文件，什么都不写
    python tools/build_site.py

    # 真正写文件、压视频
    python tools/build_site.py --execute [--crf 26] [--force-videos]

必须用 Neural-IPC 的 genesis 环境跑（要用它自带的 imageio-ffmpeg 二进制）：
    /data/xiaoyingwang/projects/Neural-IPC-sandbox/.conda/genesis/bin/python tools/build_site.py

数据来源（只读，不修改）：
  demo : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_demos/<目录>/run_info.json、*.mp4、momentum_plot.png
  扫描 : /nas/xiaoyingwang/Neural-IPC/outputs/ipc_sweep/<扫描>/<档位>.json（由 Neural-IPC/tools/ipc_sweep/sweep.py 写出）
  档位清单 : Neural-IPC/tools/ipc_sweep/configs.py（纯数据文件，按路径加载，不写 __pycache__）

发布在 GitHub Pages（https://delluluwxy.github.io/Neural-IPC-web/）：index.html 只列各周的链接，每周一页
week1.html、week2.html …（见 WEEKS）。每页开头是 doctype、charset、viewport，
然后直接 <title> 和 <style>，省略 html / head / body 标签；
颜色全是 CSS 变量（亮 / 暗两套）；不引外部资源；视频和图片用相对路径，发布时作为附属文件上传。
每个压好的视频超过 10 MB、或全部视频加起来超过 60 MB，就报错停止，不写页面。
"""

import argparse
import ast
import datetime
import functools
import html
import importlib.util
import json
import math
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
     DEMO_ROOT / "user_local_ipc_robot_cloth_teleop" / "demo.mp4",
     "机械臂遥控抓起一块布（官方例子，用户本地键盘操作录屏）",
     "用户在自己的 Windows 笔记本上运行官方 ipc_robot_cloth_teleop.py（Genesis 1.4.2，带窗口），用键盘遥控录屏，"
     "抓起了一块布（用户告知；这次运行在服务器上没有数据，以视频为准）。按键以代码为准：方向键水平移动，j / k 下 / 上，"
     "空格按住才合夹子；官方文件开头的按键说明与代码不一致。"),
]

# Genesis's own IPC tests (tests/ipc/), recorded by run_genesis_ipc_example.py --official-test at the test's own
# viewer camera, with the test's own physics assertions: (NAS dir, pytest node, title, what it checks)
OFFICIAL_TESTS = [
    ("genesis_test_test_ground_clearance_0_fit", "tests/ipc/test_rigid.py::test_ground_clearance[0]",
     "离地间隙随接触刚度变（官方测试）",
     "5 个方块落地，contact_resistance 从 1e2 到 1e6；官方断言：不横向漂移、会停住、刚度越大离地间隙越大"
     "（test_rigid.py 257-264 行）。"),
    ("genesis_test_test_ground_sliding_0_fit", "tests/ipc/test_rigid.py::test_ground_sliding[0]",
     "斜向重力下的地面滑动（官方测试）",
     "重力带水平分量，5 个方块摩擦系数 0–0.16；官方断言：不穿地、离地高度与摩擦无关、摩擦越小滑得越远"
     "（test_rigid.py 316-329 行）。"),
    ("genesis_test_test_objects_colliding_0_fit", "tests/ipc/test_rigid.py::test_objects_colliding[0]",
     "布料盖在物体上（官方测试）",
     "物体和布料落地；官方断言：全部落到地面且不穿地、没有飞走、最终静止、布料盖在所有物体上面（test_rigid.py 540-555 行）。"),
    ("genesis_test_test_cloth_corner_drag_0_fit", "tests/ipc/test_deformable.py::test_cloth_corner_drag[0]",
     "夹住布料一角拖动（官方测试）",
     "两个方块夹住布料一角，先静置再拖着画一圈；官方断言：布料没掉、被夹的角始终跟着夹子走（test_deformable.py 253-271 行）。"),
]
# What the server runs found for a test (why an assertion failed, or what the data shows; 2026-10-02/03, from
# official_test.json and the --rigid-trajectory / diagnostic reruns), shown on the card next to the outcome.
OFFICIAL_TEST_NOTES = {
    "genesis_test_test_objects_colliding_0_fit": "蓝色的是布料。实测布料开始是平的，"
                                                 "最后高低差 127 mm：中间被方块和球顶起、四周垂下，确实是软布。",
    "genesis_test_test_ground_clearance_0_fit": "期待：接触刚度越大，方块离地越高。实测 5 个方块离地 6.39 / 6.39 / 6.39 / 7.00 / "
                                            "8.18 mm，前 3 个一样，所以没过。原因：libuipc 会把接触刚度夹进按场景算出的区间，"
                                            "前 3 个方块的刚度低于下限，被夹成同一个值（同下方「接触刚度 κ」实验）。",
    "genesis_test_test_ground_sliding_0_fit": "期待：离地高度与摩擦无关。实测 μ = 0.04 的方块比其他的高 9.3 mm：它从第 66 步起"
                                          "往前翻，最后倾斜 16°。原因：libuipc 默认的半隐式提前终止让 Newton 没算到收敛就停，"
                                          "误差积累成翻倒；只关掉它，最大倾角 0.24°，断言通过（把步长减半也能通过，0.31°）。",
}

# Genesis's own rigid-rigid hydroelastic test (SAPCoupler, not libuipc), recorded by run_genesis_ipc_example.py
# --official-test --fit-camera (two-pass camera, every step checked in frame). Page order (2026-10-09 user):
# setting -> expectation by principle -> video -> conclusions. Sources of every number (not on the page):
# Neural-IPC-sandbox docs/drafts/T2.4_web_chapter_plan.md section 2.
HYDRO = {
    "key": "genesis_test_test_sap_rigid_rigid_hydroelastic_contact_64_readable",
    "nodeid": "tests/coupling/test_hybrid.py::test_sap_rigid_rigid_hydroelastic_contact[64]",
    "title": "两条关节链落到盒子上（官方 hydroelastic 测试）",
}

PAGE_TITLE = "Neural-IPC 周汇报"
# 每周一块，新的一周在前；周的分界 = weekly todo/weekN.txt 文件头的日期。锚点只用字母和数字
WEEKS = [("week2", "Week 2（TODO 2026-10-04）",
          [("w0", "在做什么"), ("w1", "文献"), ("w2", "公式"), ("wc", "代码"), ("w3", "Demo"), ("w4", "实验设计"), ("w5", "结论"),
           ("w6", "问题")]),
         ("week1", "Week 1（TODO 2026-09-26）",
          [("videos", "IPC demo"), ("tests", "官方测试"), ("sweep", "盒子实验与参数扫描"), ("data", "对生成数据的意义")])]


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
    known = ({k for k, *_ in DEMOS} | {k for k, *_ in OFFICIAL_TESTS} | {k for k, *_ in USER_RECORDINGS}
             | {HYDRO["key"]})
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
.wrap { max-width: 1200px; margin: 0 auto; padding: 0 16px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header.top { padding: 28px 0 8px; }
h1 { font-size: 1.85rem; margin: 0 0 8px; font-weight: 650; letter-spacing: 0.01em; }
.summary { margin: 8px 0 0; }
nav.toc { font-size: 0.9rem; margin: 14px 0 0; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
nav.toc { display: flex; flex-wrap: wrap; gap: 4px 18px; }
nav.weeks { display: grid; gap: 14px; margin: 18px 0 40px; }
a.weekcard { display: block; padding: 14px 16px; border-radius: 6px; background: var(--soft);
             border-left: 5px solid var(--accent); color: var(--fg); }
a.weekcard:hover { text-decoration: none; filter: brightness(0.97); }
a.weekcard b { font-size: 1.25rem; color: var(--accent); }
a.weekcard span { display: block; font-size: 0.86rem; color: var(--muted); margin: 2px 0 6px; }
h2 { font-size: 1.6rem; margin: 64px 0 14px; font-weight: 650; }
h3 { font-size: 1rem; margin: 22px 0 4px; font-weight: 600; }
h3.sub { font-size: 1.08rem; margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--border); }
h4 { font-size: 0.95rem; margin: 18px 0 4px; font-weight: 600; }
pre.log { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.76rem;
          line-height: 1.45; background: var(--soft); border-radius: 4px; padding: 10px 12px; margin: 6px 0;
          max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
details.repro { margin: 10px 0 26px; }
details.repro summary { cursor: pointer; color: var(--accent); font-size: 0.9rem; }
p.concl { font-size: 1.05rem; color: var(--good); }
p.expect { font-size: 1.08rem; color: var(--accent); }
p.setting { font-size: 0.8rem; color: var(--fg); }
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
.tablewrap { max-width: 100%; margin: 6px 0 4px; }
table { border-collapse: collapse; font-size: 0.88rem; width: 100%; table-layout: auto; }
th, td { text-align: left; padding: 5px 14px 5px 0; border-bottom: 1px solid var(--border);
         white-space: normal; overflow-wrap: anywhere; vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: 0.82rem; border-bottom-color: var(--fg); }
td { font-variant-numeric: tabular-nums; }
td.good { color: var(--good); }
td.warn { color: var(--warn); }
ul.obs { font-size: 1.02rem; font-weight: 600; margin: 8px 0; padding-left: 22px; } ul.obs li { margin: 3px 0; }
main.wrap { counter-reset: sec; } main.wrap section > h2 { counter-increment: sec; counter-reset: sub; } main.wrap section > h2::before { content: counter(sec) ". "; } main.wrap section > h3 { counter-increment: sub; } main.wrap section > h3::before { content: counter(sec) "." counter(sub) " "; }
main.wrap section > h3 { font-size: 1.3rem; margin: 56px 0 10px; padding-top: 20px; border-top: 1px solid var(--border); }
img.initframe { display: block; max-width: 640px; width: 100%; margin: 6px 0; }
mjx-container { max-width: 100%; } mjx-container svg { max-width: 100%; height: auto; }
ul.findings { padding-left: 20px; }
ul.findings li { margin: 8px 0; }
.next { }
footer.foot { color: var(--muted); font-size: 0.8rem; border-top: 1px solid var(--border);
              margin-top: 40px; padding: 12px 16px 28px; }
@media (max-width: 480px) {
  body { font-size: 14px; }
  th, td { padding-right: 10px; }
}
"""


def page(body, title):
    """整页 HTML：doctype + charset/viewport + <title>/<style>，省略 html/head/body 标签（HTML5 允许）。"""
    # math: MathJax (SVG output, no font files) renders \( \) inline and \[ \] display LaTeX; the d̂ symbol in prose
    # becomes \hat d as well
    body = body.replace("d̂", r"\(\hat d\)")
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{PAGE_CSS}</style>
<script>window.MathJax = {{tex: {{inlineMath: [["\\\\(", "\\\\)"]], displayMath: [["\\\\[", "\\\\]"]]}}, svg: {{fontCache: "global"}}}};</script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg.js" async></script>
<main class="wrap">
{body}
</main>
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


OUTCOME_LABEL = {"passed": "通过", "failed": "未通过", "skipped": "跳过"}


# ---------------- 造 label 的收敛检查（实验方案 E0） ----------------
# Data: Neural-IPC-sandbox tools/ipc_sweep/uipc_constitution_check.py json files (run_commands 19), figure from
# tools/figs/e0_label_convergence.py. Every number on the page is read from them.
E0_ROOT = OUT_ROOT / "e0_label_convergence"
E0_FIG = "e0_label_convergence.png"
# supervisor's setting (al-ipc, contact mesh 0.025, d̂ 1e-3, sphere subdivision 4); depth 0.004 is its first row
E0_SUPERVISOR = "ref_supervisor/al-ipc_hc0.025_dhat0.001_sph4.json"
# everything fixed (direct material attributes, SNK1 label, sphere 7), d̂ → 0 through the two smallest d̂;
# then block → ∞ by 1/L (half-width 2.4 vs 4.8) and mesh → 0 by h (0.0125 vs 0.00625), both two-point estimates
E0_FIXED = {"L2.4": ["direct_L2.4/ipc_hc0.0125_dhat0.00025_sph7_L2.4_snk1_direct.json",
                     "direct_L2.4/ipc_hc0.0125_dhat0.000125_sph7_L2.4_snk1_direct.json"],
            "L4.8": ["direct_L4.8/ipc_hc0.0125_dhat0.00025_sph7_L4.8_snk1_direct.json",
                     "direct_L4.8/ipc_hc0.0125_dhat0.000125_sph7_L4.8_snk1_direct.json"],
            "L4.8_fine": ["fine_direct_L4.8_s7/ipc_hc0.00625_dhat0.00025_sph7_L4.8_snk1_direct.json",
                          "fine_direct_L4.8_s7/ipc_hc0.00625_dhat0.000125_sph7_L4.8_snk1_direct.json"]}
UNIAXIAL_AS_INPUT = "uniaxial/uniaxial_as_input.json"   # slender bar pulled with E = 1e5, ν = 0.3 passed as-is


def label_facts():
    """Week 2 页用到的 label 数字：supervisor 设置的比值、全部改对后的比值（压深 0.004 / 0.024）、单轴实测 E。"""
    fin = {k: _extrap_to_zero(p, "U_snk1_over_UH") for k, p in E0_FIXED.items()}
    # mesh → 0 at half-width 4.8, plus the block-size correction (block → ∞ minus half-width 4.8 = L4.8 − L2.4)
    fixed = [2 * fin["L4.8_fine"][k] - fin["L4.8"][k] + (fin["L4.8"][k] - fin["L2.4"][k]) for k in (0, 1)]
    return {"supervisor": _read_e0(E0_SUPERVISOR)["rows"][0]["U_over_UH"], "fixed": fixed,
            "E_meas": _read_e0(UNIAXIAL_AS_INPUT)["E_meas"]}


def _read_e0(path):
    d, err = load_json(E0_ROOT / path)
    if d is None:
        raise SystemExit(f"[build_site] 结果文件读不了：{E0_ROOT / path} {err}")
    return d


def _extrap_to_zero(paths, key):
    """(depth 0.004, depth 0.024) values of `key` on the straight line through the two smallest d̂, at d̂ = 0
    (successive d̂ are halved, so the line gives 2·y(smallest) − y(next))."""
    a, b = _read_e0(paths[-2]), _read_e0(paths[-1])
    return tuple(2 * b["rows"][k][key] - a["rows"][k][key] for k in (0, -1))




# ---------------- Week 2 组会稿（45 分钟，给不了解项目的人听；用户 10-10：只讲重点、全部 bullet、不标分钟；公式讲细；设置小字不读） ----------------
# 独立有限元（supervisor 轴对称 torch FEM，不经过 IPC）：Neural-IPC-sandbox tools/ipc_sweep/torch_fem_hertz_reference.py，
# run_commands 19i。线弹性 + 间隙按变形前算 = Hertz 的全部假设（检验裁判）；snk1 + 真球面 = label 实际的材料和压头。
TORCH_ROOT = E0_ROOT / "torch_fem"
TORCH_RUNS = {"hertz_assumptions": "linear_parabref_nr192.json", "label_setup": "snk1_sphere_nr192.json"}


def _torch_block_limit(name):
    """每个压深按 1/b 把最大两档圆柱外推到无限大块：{δ: 能量 ÷ Hertz}。"""
    d, err = load_json(TORCH_ROOT / TORCH_RUNS[name])
    if d is None:
        raise SystemExit(f"[build_site] 独立有限元结果读不了：{TORCH_ROOT / TORCH_RUNS[name]} {err}")
    out = {}
    for delta in sorted({r["delta"] for r in d["rows"]}):
        by_b = sorted((r["b"], r["ratio"]) for r in d["rows"] if r["delta"] == delta)
        out[delta] = 2 * by_b[-1][1] - by_b[-2][1]
    return out


def _ul(items):
    return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"


def _setting(text):
    """实验设置：小号灰字，留给想看细节的人，组会上不读。"""
    return f'<p class="setting muted">设置：{esc(text)}</p>'


def week2_sections(hydro, f):
    """Week 2 页：七段，按讲的顺序排；设置用小字（组会不读）；文字里的公式是原样 LaTeX（MathJax 渲染），其余经 esc。"""
    outcome = OUTCOME_LABEL.get(hydro.get("outcome"), "无结果") if hydro["state"] == "ok" else hydro["reason"]
    fixed = f["fixed"]
    # 独立有限元：Hertz 的全部假设、label 实际的材料和压头，两种都算，取离 1 最远的那个
    torch_err = max(abs(v - 1) for name in TORCH_RUNS for v in _torch_block_limit(name).values())
    e = esc
    S = []

    S.append(('w0', "0. 我们在做什么", _ul([
        e("两个物体相碰时，接触的地方会凹进去一小块，这块变形里存着弹性能量"),
        e("精细仿真能把它算准，但每个物体要成千上万个自由度，太慢"),
        e("我们的做法：物体只用很少的自由度来仿真，接触处那份能量交给神经网络学"),
        e("网络的训练数据（label）= 精细仿真算出来的这份能量")])))

    def paper(title, para, formula, ours):
        """一篇文献：标题、一段话（核心贡献 + 怎么做）、公式（论文没有就不写）、和我们的关联。"""
        return (f"<h3>{e(title)}</h3><p>{e(para)}</p>" + (f"<p>{formula}</p>" if formula else "")
                + f"<p><b>和我们：</b>{e(ours)}</p>")

    S.append(('w1', "1. 这周读的文献", "".join([
        paper("Elandt 2019：压力场接触（hydroelastic 的原始论文）",
              "核心贡献：一种又快、力又连续的刚体接触模型，也就是 Drake 和 Genesis 里的 hydroelastic。做法：每个物体内部"
              "预先放一个「压力场」，表面为 0、越往里越大；两个物体按原形状直接重叠，不做变形，重叠区里两边压力相等的那张面"
              "就是接触面，把这张面上的压力加起来就是接触力。物体切成很粗的四面体也能用，每对四面体只需求一张平面。",
              r"\[p_0=E\,\varepsilon,\qquad \text{接触面：}p_{0,A}=p_{0,B},\qquad \mathbf f=\int_S p_0\,\hat{\mathbf n}\,dS\]",
              "它的碰撞能量是人为设计的压力场积分，软硬只由 E 这一个数和几何决定；我们的碰撞能量从精细仿真的不穿透解里来，"
              "由网络学。"),
        _ul([e("p₀：物体内部某点的「压力」，预先算好、跟着物体一起动，不是真实的应力"),
             e("ε：这个点离表面有多深，换算到 0–1（表面 0，最深处 1）"),
             e("E：一个刚度数，决定物体多「硬」；Genesis 里叫 hydroelastic 模量，由用户填"),
             e("S：重叠区里两边压力相等的那张面；n̂：它的法向"),
             e("嵌得越深 → 压力越大、接触面越大 → 力越大"),
             e("软的一方要嵌得更深才顶得住同样的压力，所以接触面陷进软的那一边"),
             e("物体本身不变形，软硬全靠 E 和几何；所以几何一变，力的变化规律可能和真实的弹性体不一样"
               "（第 5 部分的实验就是查这个）")]),
        paper("Masterjohn 2022：把每块接触面换成一根弹簧",
              "核心贡献：原始压力场模型写在加速度层面，要用自适应步长，塞不进 MuJoCo、Drake 这类「固定步长、每步解速度」"
              "的主流求解器。做法：接触面上每块小多边形的压力是线性的，合力正好等于「面积 × 质心压力」，于是每块多边形换成"
              "一根等效弹簧，刚度由面积和压力梯度决定，就能直接交给这类求解器，跑到实时。",
              r"\[f_n=(-k\phi)_+,\qquad k=g\,A_0,\qquad \phi_0=-\frac{p_{c,0}}{g},\qquad g=\frac{g_Ag_B}{g_A+g_B}\]",
              "Genesis 的 hydroelastic 就用这一步把接触面变成弹簧（见第 3 部分）；力仍来自人为的压力场。"),
        paper("TAMSI（Castro 2020）：柔性点接触 + 隐式法向力",
              "核心贡献：允许物体互相嵌入一点，力由嵌入量算，不用解「不接触就没力、有力就必须贴紧」的互补问题。做法：几何"
              "每步只查一次，但嵌入量用「步首嵌入量 + 这一步速度带来的变化」来预测，于是法向力随下一步速度变化、隐式求解，"
              "硬接触也能大步长稳定。",
              r"\[\pi=k\,(1+d\,\dot\delta)_+\,\delta_+\]",
              "力和嵌入量 δ 成正比（线性），不像 Hertz 那样按 1.5 次方增长；k 是人为的刚度。我们的能量从弹性力学的真解里学，"
              "力随压深的规律由数据决定。"),
        paper("SAP（Castro 2022）：每一步写成一个凸优化",
              "核心贡献：把柔性接触写成只含速度、没有约束、碗形（只有一个最低点）的最小化问题，用牛顿法又稳又快地解；"
              "Genesis 的 hydroelastic 用的就是这个求解器。做法：给定速度后最优冲量能直接算出来，代回去就只剩速度一个变量。"
              "刚度太大时自动按步长封顶，代价是一点穿透：物体静止压在地上时穿透约 β²gδt²/4π²，和质量无关。",
              r"\[\frac{\gamma_n}{\delta t}=(-k\phi-\tau_d\,k\,v_n)_+,\qquad "
              r"\min_{\mathbf v}\ \tfrac12\|\mathbf v-\mathbf v^*\|_A^2+\tfrac12\|P_{\mathcal F}(\mathbf y(\mathbf v))\|_R^2\]",
              "IPC 也是把每一步写成一个能量最小化；我们的碰撞能量 U_c 就是要作为一项加进这种能量里。"),
        paper("Han 2023：把 SAP 推广到可变形体",
              "核心贡献：刚体、软体和它们之间的接触在同一个凸问题里解。难点是大变形材料会让问题不再是碗形；做法：材料在一步之内"
              "线性化（转动用上一步的，扣掉转动后按线性材料算），保证凸；不碰接触的自由度精确消掉，只在接触相关的少数"
              "自由度上解。",
              r"\[\mathbf M(\mathbf v-\mathbf v_0)=\delta t\,\mathbf k+\mathbf J^T\boldsymbol\gamma\]",
              "「把不碰接触的自由度消掉、只留接触相关的」和我们的静态凝聚是同一个思路；但它的接触力律仍是人为的弹簧。"),
        paper("ICF（Castro、Han、Masterjohn）：什么样的接触力律能放进凸优化",
              "核心贡献：SAP 的接触软硬是方法自带的，用户换不了力律。这篇给出条件：只要「穿得越深力越大、合拢越快力越大」，"
              "任意力律都能放进凸优化，仍然保证一定收敛、解唯一。文中还把 IPC 归类为「刚核外面包一层薄软层」。",
              r"\[\frac{\partial f_n}{\partial \phi}\le 0,\qquad \frac{\partial f_n}{\partial v_n}\le 0,\qquad "
              r"f_n=k\,(-\phi)_+\,(1-d\,v_n)_+\]",
              "我们网络给出的力是 U_c 对压深的导数，压得越深力越大，满足第一条，所以理论上可以接进这类凸求解器。"),
        paper("GPU SDF Hydroelastic（Newton 项目）：压力场改存在距离场上",
              "核心贡献：原来的压力场存在四面体网格上，任意形状难生成、网格一细就慢。做法：改用有符号距离场（空间每点到表面的"
              "距离），两物体的距离场按刚度加权相减，零等值面就是接触面，用 marching cubes 抠出来，每个小三角形放一根弹簧，"
              "全部在 GPU 上并行。",
              r"\[g(\mathbf x)=k_A\,\phi_A(\mathbf x)-k_B\,\phi_B(\mathbf x),\qquad "
              r"f_n=\frac{k_Ak_B}{k_A+k_B}\,a\,|\phi_A+\phi_B|\]",
              "只是把压力场模型做快，物理还是同一个人为模型。"),
        paper("Romero 2021：子空间 + 网络补接触凹坑",
              "核心贡献：物体整体运动只用少数几个「把手」（控制点）来算，所以快；接触压出的局部凹坑，粗表示做不出来，"
              "由网络补上。训练数据是全有限元仿真在每个姿态下的静力平衡形状减去粗表示的形状。2D 例子里全有限元 20 帧/秒，"
              "本文 140 帧/秒，凹坑细节找回来了。",
              r"\[\mathbf x(\mathbf q)=\mathbf U\mathbf q+\mathbf F(\mathbf q)\,\mathbf r(\mathbf q)\]",
              "思路和我们一样：粗自由度 + 网络补接触处的局部变形。区别是它学位移 r，没有接触能量，也不保证不穿透；"
              "我们学的是一个能量，力由能量求导得到。"),
        paper("Romero 2022：在碰撞体坐标系里学凹坑",
              "核心贡献：凹坑的形状换到「站在碰撞体上看」的坐标系里描述，变化平滑得多，所以要的训练数据少很多、网络也小；"
              "网络是一个连续函数，物体表面任何一点都能查询，也能直接求导算力。远处的把手权重为 0，直接屏蔽。",
              r"\[u(\bar x)=\mathbf T(\mathbf z)\,r(\bar z),\qquad \bar z=\mathbf T(\mathbf z)^{-1}\tilde x(\bar x)\]",
              "仍然学位移；接触项是人为设计的罚函数，不保证不穿透。「换到接触的局部坐标系里学」这一点我们可以借鉴。"),
        paper("Romero 2023：换一个没见过的碰撞体也能用",
              "核心贡献：前两篇只对训练时那一个碰撞体有效；这篇在每个点周围撒 65 个探针，读出碰撞体在附近长什么样"
              "（有符号距离），网络只看这一小块局部形状，所以运行时换成任意形状的碰撞体也能用，9–26 帧/秒，全空间仿真 1 帧/秒。",
              r"\[r=T\,R(x)\,r_{\text{local}},\qquad r_{\text{local}}=\mathbb N\big(\hat\phi(x),\,W(\bar x)\,R^{-1}T^{-1}(q-x)\big)\]",
              "泛化到没见过的形状，正是我们「一个模型覆盖多种形状」要做的；它还是学位移、不保证不穿透，我们学能量、"
              "label 来自保证不穿透的 IPC。"),
        paper("RigidFormer：用 Transformer 直接预测刚体怎么动",
              "核心贡献：只输入点云、不需要网格连接关系；每个物体压成一个向量，物体之间用 attention 交互；"
              "每个物体只预测 4 个锚点的加速度，再用一次最贴合的刚体变换把整个物体摆正，保证物体本身不变形。"
              "比 FIGNet 快 8 倍、比 HopNet 快 101 倍。",
              r"\[\mathbf x_{t+1}=f_\theta(\mathbf x_{t-1},\mathbf x_t,\Delta t)\]",
              "它把整个求解器换成网络，没有能量的概念，物体之间也不保证不穿透；我们保留仿真器，只补一个碰撞能量项。")])))

    S.append(('w2', "2. 三个公式", "".join([
        "<h3>Hertz：刚性球压进弹性体（标准答案）</h3>",
        r"<p>\[a=\sqrt{R\,\delta},\qquad F=\tfrac{4}{3}E^*\sqrt{R}\,\delta^{3/2},\qquad "
        r"U=\tfrac{8}{15}E^*\sqrt{R}\,\delta^{5/2},\qquad E^*=\frac{E}{1-\nu^2}\]</p>",
        _ul([e("δ：压深，球压进去多深；R：球半径；a：接触圆的半径；F：压力；U：被压物体里存的弹性能"),
             e("E：杨氏模量，材料多硬；ν：泊松比，压扁时往旁边鼓多少；E*：两者合成的「有效硬度」"),
             e("a = √(Rδ)：压深 ×4，接触圆只 ×2"),
             e("F ∝ δ^1.5，不像普通弹簧那样 F ∝ δ：越压越硬，因为接触圆在跟着变大，顶着的材料越来越多"),
             e("U 是 F 对压深的积分：压深翻倍，能量约 ×5.7；材料越硬、球越大，能量越大"),
             e("成立条件：变形小、材料线弹性、无摩擦、物体比接触圆大得多。我们检查 label 的算例正好满足，"
               "所以拿它当标准答案")]),
        "<h3>IPC：保证不穿透的接触能量</h3>",
        r"<p>\[B(d)=\kappa\,\big(d^2-\hat d^{\,2}\big)^2\Big[\ln\frac{d^2}{\hat d^{\,2}}\Big]^2"
        r"\quad (d<\hat d),\qquad B=0\quad (d\ge\hat d)\]</p>",
        _ul([e("d：两个表面之间的距离；d̂：「安全距离」；κ：推力强度"),
             e("距离大于安全距离：不管；小于安全距离：开始互相推"),
             e("距离 → 0：对数项 → 无穷，能量 → 无穷，所以两个表面永远碰不上，保证不穿透"),
             e("IPC 把这个能量和物体的弹性能一起求最小，得到「不穿透、能量最低」的变形，这就是我们的 label"),
             e("代价：两表面还隔着 d̂ 就开始推，相当于接触提前了；d̂ 越大，算出的能量越偏大")]),
        "<h3>我们：碰撞能量</h3>",
        r"<p>\[U_c(z)=\min_{w}\ \Pi(\Phi z+\Psi w)-\Pi(\Phi z)\]</p>",
        _ul([e("z：粗自由度，比如刚体的位置和朝向、少数几个整体变形模式；仿真器只算这几个数"),
             e("Φz：只用粗自由度时物体的形状，碰撞时会和对方穿插"),
             e("Ψw：接触附近一小块局部变形，粗自由度表达不了；w 是它的系数"),
             e("Π：精细模型里物体的总弹性能"),
             e("min_w：固定 z，让局部变形自己调整到不穿透、能量最低"),
             e("U_c：为了不穿透多付出的能量，就是「碰撞能量」；网络输入 z，输出 U_c"),
             e("力 = U_c 对 z 求导，由能量得到，所以不会凭空多出能量"),
             e("球压平面时 U_c 就等于 Hertz 的 U，所以可以用 Hertz 检验")])])))

    S.append(('wc', "3. 看代码：supervisor 的 NeuralIPC 仓库、Genesis 的 hydroelastic", "".join([
        "<h3>这是什么仓库、怎么跑</h3>",
        _ul([e("github.com/YumengHe/NeuralIPC，Python 包 nipc；目标是学第 2 部分的碰撞能量 U_c"),
             e("环境：pip install -e .（numpy、scipy、matplotlib、torch）；造 3D label 的部分另要 pyuipc 0.0.25（CUDA）。"
               "作者在 Windows 11、Python 3.13、torch 2.6、RTX 4080 上测过"),
             e("每个脚本都在仓库根目录下用 python -m nipc.<组>.<模块> 跑；文件路径统一写在 nipc/paths.py"),
             e("生成的数据和图放 out/（不进 git），各实验的参考结果表放 results/，说明文档放 docs/"),
             e("最快的两条：python -m nipc.analytic.hertz_contact（10 秒，验证 Hertz）；"
               "python -m nipc.teacher.gen3d_press --deep（GPU 约 4 分钟，造 3D label）")]),
        "<h3>六组代码各干什么</h3>",
        table(["组", "做什么", "关键文件"],
              [["analytic（7 个）", "线性的解析核心：边界元接触求解器（和 Hertz 差 < 0.1%，大多数学习实验的 label 都由它造）、"
                "轴对称有限元、仿射粗化、多接触区耦合、100 个真实网格的基准", "hertz_contact.py"],
               ["finite_strain（6 个）", "大变形：Neo-Hookean 有限元 + 增广拉格朗日接触（可信的非线性参考解）；"
                "torch 自动求导的组装器，和 NumPy 版对到 1e-15", "rung2_step_a…e、rung2_torch_fem.py"],
               ["learning（32 个）", "网络实验：*_gen.py 造数据，同名脚本训练和评估；全部是合成数据——刚性压头压半空间",
                "neuralipc_*.py"],
               ["teacher（7 个）", "用 libuipc 造 3D label：刚球压软块；数据生成器的雏形；网页查看器",
                "gen3d_press.py、teacher_viz.py"],
               ["reduced_dynamics（3 个）", "刚体 + 罚函数碰撞；一维弹性杆真解；Craig–Bampton 模态补回静态势漏掉的振动", "dem_*.py"],
               ["validation（3 个）", "label 的含义：重力不进 U_c；动力学下静态势会漏能量，冲击越快漏得越多",
                "gravity_projection_check.py"]]),
        "<h3>现在的模型</h3>",
        r"<p>\[U=\sum_k U_k(\delta_k)\;-\;\sum_{k<l}\mathrm{softplus}(\mathrm{NN})\,\sqrt{U_kU_l}\,\frac{L}{r_{kl}},"
        r"\qquad \delta_k=\max\big(0,\,-\min\text{gap}_k\big)\]</p>",
        _ul([e("流程：物体状态（形状编码、位姿、仿射量）→ 每个接触区用几何算有向间隙 gap → 穿插量 δ → "
               "每个接触区一个学出来的能量 U_k → 两两之间一条耦合边 → 总能量 U → 力 = U 的梯度"),
             e("耦合边是负的：两个接触区靠得越近（r 越小），互相预压、一起变软，总能量比各自相加小"),
             e("δ 只由几何算，从不作为网络的输入或输出，所以不接触时能量严格为 0、刚接触时平滑"),
             e("loss 只拟合能量；学到的指数中位数 2.47，接近 Hertz 的 2.5")]),
        "<h3>代码里定死的设计</h3>",
        _ul([e("label 是准静态的：位移控制地压、关掉重力；动态效果交给降阶模型的振动模态"),
             e("粗状态用质量加权拟合，所以重力不进 label"),
             e("3D label 用 libuipc 自己的材料能量算，不用小变形公式")]),
        "<h3>还没做的（README 原话整理）</h3>",
        _ul([e("任意压头网格、任意物体和位姿的数据生成器；两个都会变形的物体；3D 的学习模型；用学出来的能量跑的降阶仿真器"),
             e("所有学习实验的 label 都来自边界元和有限元，IPC（libuipc）造的 3D 数据还没接进训练；"
               "要接进来，先要确认 IPC 造的 label 本身是对的，这就是第 6 部分的检查")]),
        "<h3>Genesis 的 hydroelastic 怎么实现（读源码）</h3>",
        r"<p>\[p(v)=\frac{|d(v)|}{\max_v|d|}\,H,\qquad g=\frac{1}{1/g_0+1/g_1},\qquad k=A\,g,\qquad "
        r"\phi_0=-\frac{p}{g}\]</p>",
        _ul([e("就是三篇论文拼起来：Elandt 的压力场 + Masterjohn 的「每块接触面一根弹簧」+ SAP 求解器"),
             e("压力场：顶点到表面的距离 ÷ 最大距离 × 刚度 H；原文用 Laplace 方程生成，Genesis 简化了"),
             e("刚体共用一个全局刚度 H（默认 1e8 Pa）；软体用各自的 hydroelastic 模量（默认 1e7），和材料的杨氏模量不联动"),
             e("从文件加载的刚体默认换成凸包再算，所以凹的形状按凸包接触"),
             e("静止时每块接触面的推力 = 面积 × 质心压力")])])))

    S.append(('w3', "4. Demo：Genesis 的 hydroelastic", "".join([
        '<div class="grid">' + demo_card(dict(hydro, title=HYDRO["title"], line=f"官方检查：{outcome}"), {}) + "</div>",
        _ul([e("Genesis 自带的 hydroelastic 测试，原样跑通"),
             e("两条链掉到盒子上，被托住、叠起来"),
             e("盒子只压进地面约 8 微米：是靠「嵌进去一点」托住的，肉眼看不出")]),
        _setting("Genesis 官方测试 test_sap_rigid_rigid_hydroelastic_contact，场景和检查条件原样；地上一个 "
                 "0.5 × 0.5 × 0.2 m 的方盒，两条由球和胶囊（半径 24 mm）连成的链从上方落下；全部接触用 hydroelastic"
                 "（SAP 求解器，不经过 IPC）；压力场刚度 1e8 Pa，阻尼时间尺度 0.1 s；80 步 = 1.33 s，视频慢放约 3.75 倍；为看清接触，只改了灯光、盒子颜色和相机仰角，物理和检查条件不变")])))

    S.append(('w4', "5. 实验设计：怎么证明我们原理上比 hydro 对", "".join([
        _ul([e("标准答案用弹性力学的精确公式，不用任何一方自己的仿真"),
             e("每个实验只改一个几何量，看谁的变化跟标准答案一致")]),
        table(["实验", "改什么", "标准答案", "hydro"],
              [["E1", "球半径 ×2（同样压深）", "力 ×1.41", "力 ×2"],
               ["E2", "平底圆柱压头半径 ×2、×4", "力 ×2、×4", "力 ×4、×16"],
               ["E3", "一个物体上两个凸起，改间距", "两个凸起互相影响", "各算各的"],
               ["E4", "很薄、几乎压不缩的软层", "变得很硬", "表达不出来"],
               ["E5", "物体本身也会变形", "变形只算一次", "变形算了两遍"]]),
        _ul([e("先做 E0：检查我们的训练数据本身对不对（下一部分）")])])))

    S.append(("w5", "6. 结论", "".join([
        _ul([e("Hydro：物体靠微米级的嵌入托住；软硬只由一个人为的数决定")]),
        "<h3>E0：训练数据对不对</h3>",
        _setting("supervisor 造 label 的球压算例：刚性球（半径 1）竖直压进底面固定的软块（E = 1e5，ν = 0.3），"
                 "无摩擦、无重力，压深 0.004–0.024，每个压深静置到平衡后算块里的弹性能，除以 Hertz 的 U。"
                 "原来：块宽 2.4、高 1.2，接触区网格 0.025，安全距离 d̂ = 1e-3，球面细分 4 次。"
                 "改对后：d̂ 取三档外推到 0，球面细分 7 次，块半宽 2.4 / 4.8 外推到无限大，网格 0.0125 / 0.00625 外推到 0，"
                 "材料参数改写成软件实际需要的值"),
        f'<figure><img src="assets/images/{E0_FIG}" alt="label 收敛图">'
        f'<figcaption class="small">{e("纵轴 = label ÷ 标准答案（1 = 完全一致）；横轴 = IPC 的安全距离，越往左越小")}'
        f"</figcaption></figure>",
        _ul([e(f"原来的设置：label ÷ 标准答案 = {f['supervisor']:.2f}，偏差里混着两个方向相反的误差"),
             e("· IPC 有个「安全距离」，两表面还没碰到就开始推 → 能量偏大"),
             e("· 球面网格太粗，球底是个尖角 → 能量偏小"),
             e(f"两项都改对、块够大、网格够细后：小压深 {fixed[0]:.2f}、大压深 {fixed[1]:.2f}，和标准答案一致"),
             e(f"另用一个完全独立的有限元程序验证：Hertz 公式在这些压深下误差 < {torch_err:.1%}，"
               "可以当标准答案")]),
        _setting("独立有限元：supervisor 的轴对称有限元程序（不经过 IPC，没有安全距离），圆柱块半径 = 高 = 2.4 → 19.2，"
                 "两种网格；按 Hertz 的全部假设算一遍、按 label 实际的材料和真球面再算一遍，都按块大小外推到无限大")])))

    S.append(("w6", "7. 遇到的问题", _ul([
        e("IPC 的「安全距离」让能量偏大 → 取几个不同的安全距离，外推到 0"),
        e("球面网格太粗 → 加细"),
        e(f"新版 IPC 库里材料实际的硬度比设定的大 {f['E_meas'] / 1e5 - 1:.0%}（软件更新时漏改了一处换算）→ 已找到改法"),
        e("全部改对后每个样本很慢（约半小时）→ 要定一个又快又够准的造数据设置")])))
    return [f'<section id="{a}"><h2>{e(t)}</h2>\n{body}</section>' for a, t, body in S]


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
                    "ball_E": 1.0e3}
GENESIS_KEYS = {"resistance": "contact_resistance", "init_penetration": "overlap_balls",
                "mesh_res": "ball_subdiv"}  # sweep -> override key in configs
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
            # gs.morphs.Sphere -> mu.create_sphere(radius) 默认 subdivisions=3（genesis/engine/mesh.py:527, utils/mesh.py:1110）
            return "官方 Sphere（官方默认）：icosphere 细分 3 次（表面 642 个顶点）"
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
     "d̂ 是 barrier 开始起作用的距离。d̂ 变小，停住时每一对接触面之间的间隙跟着变小；\\(\\kappa_{\\min}\\propto 1/\\hat d^{4}\\)（见本节开头的式子），接触更硬，"
     "Newton 迭代一般会变多；d̂ 变大，物体隔得更远就被推开。不论 d̂ 取多少，都不该出现穿透。"),
    ("dt", "时间步长 dt", "改 SimOptions.dt。官方本例 0.02 s；物理时长固定 2 s，帧数随 dt 变。", "dt（s）",
     "dt 变小，每一步物体移动得更少；但 \\(\\kappa_{\\min}\\propto 1/\\Delta t^{2}\\)（见本节开头的式子，dt 缩小 10 倍、κ 区间抬高 100 倍），"
     "所以每步的 Newton 次数不一定减少，总步数则成倍增加。不论 dt 多大都不该穿透。"),
    ("resistance", "接触刚度 κ", "改 Genesis 的 contact_resistance，默认 1e9 Pa。", "设的 κ（Pa）",
     "κ 是 barrier 的刚度（式子见本节开头）。libuipc 会把它夹进区间 \\([\\kappa_{\\min},\\kappa_{\\max}]\\)："
     "在区间内，κ 越大 barrier 这堵墙越陡，物体陷进 barrier 越浅，间隙越接近 d̂；区间外的值会被夹到边界，"
     "结果应该和边界值一样。"
     "§本场景代入得 \\([1.57\\times10^{5},\\,1.57\\times10^{7}]\\) Pa，和日志打印的一致。"),
    ("init_penetration", "初始穿插", "多放一个软球，让它和官方软球一开始就互相穿进去一部分（R 为球半径）。",
     "初始状态",
     "IPC 的 barrier 只在两个表面距离为正时才有定义，一开始就穿插的话能量没有意义，"
     "所以 libuipc 的初始化检查应该直接拒绝开跑。"),
    ("mesh_res", "网格分辨率", "官方 Sphere 本身就是表面细分 3 次的 icosphere；把软球换成同样大小、表面细分 2 / 3 / 4 次的 icosphere，"
     "由 Genesis 自己四面体化。",
     "软球网格",
     "网格越细，球面越接近真球，接触时参与的顶点越多，每步要解的未知数越多、越慢。"
     "只要初始无穿插，网格粗细都不该影响会不会穿透，物体落地后的大致位置应该接近。"),
]


# What each experiment looks at (its SWEEP_COLUMNS) and why
OBSERVE = {
    "baseline": "停住时每一对接触面（球–地、球–布、布–方块……）之间的最短距离，每帧 Newton 迭代几次（好不好解）。",
    "d_hat": "每一对接触面之间的最短距离（是否跟着 d̂ 变）、每帧 Newton 迭代几次、软球表面边长（球有没有被自接触撑开）。",
    "dt": "每帧 Newton 迭代几次、总耗时，以及停住时各接触面之间的间隙（接触变硬没有）。",
    "resistance": "每一对接触面之间的最短距离：刚度真的变了，间隙就该跟着变。",
    "init_penetration": "libuipc 开跑前检查的日志原文。",
    "mesh_res": "libuipc 实际用的 κ（日志原文）、停住时各接触面之间的间隙、总耗时。",
}
# Short cause -> effect observations of each experiment, every one readable off its table (numbers in brackets)
OBSERVATIONS = {
    "baseline": ["每一对接触面停住时 → 间隙 0.73–0.99 倍 d̂，不贴死",
                 "接触越重 → 间隙越小（方块–地面 0.8 倍 d̂ < 软球–地面 0.85 < 布–地面 0.97）"],
    "d_hat": ["d̂ ↓ → 间隙 ↓（始终约 0.6–1 倍 d̂）",
              "d̂ ↓ → Newton 次数 ↑（每帧最多：10 mm 7 次 → 5 mm 9 次 → 2 mm 11 次）",
              "d̂ > 软球表面边长（约 12 mm）→ 球被自接触撑开（边长被撑到约 d̂：15 mm 时 15 mm，30 mm 时 26 mm）"],
    "dt": ["dt ↓ → κ 下限 ↑ → 接触更硬 → 间隙更接近 d̂（0.04 s 时 6.7–9.2 mm → 0.002 s 时 9.4–9.6 mm）",
           "dt ↓ → 总步数 ↑ → 耗时 ↑（0.04 s 16 s → 0.002 s 152 s），每帧 Newton 次数基本不变"],
    "resistance": ["区间内 κ ↑ → 接触更硬 → 间隙 ↑（软球–地面：2e5 时 4.6 mm → 1e7 时 8.2 mm）",
                   "κ 在区间外 → 被夹到边界 → 结果不再变（1e4 ≈ 2e5，默认 1e9 ≈ 1e7）"],
    "init_penetration": ["一开始就穿插 → libuipc 开跑前的检查报相交 → 拒绝开跑",
                         "关掉检查硬跑 → 能跑完、不报错，但两个球一直嵌在一起（球心距离始终约 120–136 mm，不穿插至少要 160 mm）"],
    "mesh_res": ["网格越密 → 顶点平均质量 ↓ → κ ↓（粗 4.9e7 → 细 3.4e6）",
                 "κ ↓ → 网格没变的物体接触变软 → 方块–地面间隙 ↓（粗 8.1–8.5 mm → 细 4.1 mm）",
                 "网格越密 → 耗时 ↑（粗 12.1 s → 细 31.7 s）"],
}
# Each experiment's conclusion in one or two sentences (the numbers are in the table right above it; the verified
# data behind each sentence is in Neural-IPC docs/claude_todo.md and the meeting outline section 4)
FINDINGS = {
    "baseline": "与期待一致：全程没有穿透；停住时每一对接触面（不只是和地面，也包括球–布、布–方块）之间都留着一条"
                "0.7–1 倍 d̂ 的缝，物体停在 barrier 起作用的那一层里。越重的接触缝越小：方块压地约 0.8 d̂，"
                "软球约 0.85 d̂，很轻的布只陷到约 0.97 d̂——barrier 的推力随间隙变小急剧增大，越重越要陷得深才托得住。",
    "d_hat": "与期待一致：所有接触的间隙都跟着 d̂ 走（始终约 0.6–1 倍 d̂），d̂ 越小物体靠得越近，但每帧 Newton 迭代越多"
             "（越难解）。d̂ 不能大于软体表面网格的边长（这个软球静止时约 12 mm）：IPC 的 barrier 也作用在同一个球自己的"
             "不相邻面片之间，d̂ 一旦超过边长，球就被自己从里面撑开，边被撑到和 d̂ 差不多长才停——15 mm 时边长变成约 15 mm、"
             "球明显变形，30 mm 时边长约 26 mm、球完全变形；2、5、10 mm 时边长不变。",
    "dt": "与期待一致：每帧迭代次数基本不随 dt 变，总耗时随步数成倍增加；dt 越小，接触间隙越接近 d̂（libuipc 的 \\(\\kappa_{\\min}\\propto 1/\\Delta t^{2}\\)，接触更硬、物体陷得更浅）。",
    "resistance": "与期待一致：libuipc 只认这个场景允许的区间 [1.57e5, 1.57e7] Pa。区间内（2e5 → 1e6 → 3e6 → 1e7）"
                  "κ 越大接触越硬、物体陷得越浅，各接触面的间隙一路变大（软球–地面 4.6 → 6.5 → 7.4 → 8.2 mm，d̂ = 10 mm）；"
                  "区间外被夹到边界：1e4 被夹到下限、结果和 2e5 差不多，默认 1e9 被夹到上限、结果和 1e7 差不多。"
                  "所以在 Genesis 里设 contact_resistance 只有落在这个区间里才有用。",
    "init_penetration": ("与期待一致：一开始就穿插时 libuipc 直接拒绝开跑。即使关掉检查硬跑，IPC 也分不开已经穿进去的两个物体："
                         "barrier 和 CCD 只能阻止「没穿 → 穿进去」，管不了一开始就穿着的部分，结果两个球一直粘在一起、"
                         "互相挤变形——程序不报错但物理上是错的。所以 IPC 必须从无穿透的状态开始。"),
    "mesh_res": ("libuipc 整个场景只用一个 κ，按全场景所有顶点的平均质量定区间（\\(\\kappa_{\\min}\\propto\\bar m\\)，见本节开头的式子）："
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


def col_edge(d):
    """Range over the soft balls of the median surface edge length at the last frame, mm (12.1 mm at rest)."""
    es = [o["surface_edge_median_m"] for o in (d.get("objects_final") or {}).values()
          if isinstance(o, dict) and is_num(o.get("surface_edge_median_m"))]
    return f"{g3(min(es) * 1000)}–{g3(max(es) * 1000)}" if es else "—"


def contact_kind(name):
    """Readable kind of a contact_gaps_final name (sweep.py: "<i>_Sphere_Elastic" or "<i>_Mesh_Elastic" (icosphere balls
    of mesh_res), "<i>_Box_Rigid", "<i>_Mesh_Cloth", "<i>_wall", "ground")."""
    for key, kind in (("Elastic", "软球"), ("Box", "方块"), ("Cloth", "布"), ("wall", "墙"), ("ground", "地面")):
        if key in name:
            return kind
    return name


CONTACT_KINDS = ("软球", "方块", "布", "墙", "地面")  # column order of the contact-gap columns


def contact_groups(d):
    """{"<kind>–<kind>": [gap mm, ...]} of the surface pairs in contact at the last frame (shortest surface-to-surface
    distance below d_hat, where libuipc's barrier acts; sweep.py contact_gaps_final), or None without that record.
    Levels run before sweep.py recorded it are read from their rerun under genesis_<set>_contact/."""
    c = d if "contact_gaps_final" in d else load_json(
        SWEEP_ROOT / f"genesis_{GEN_TAG}_contact" / d["sweep"] / f"{d['level']}.json")[0]
    dh = dig(c, "libuipc_config", "contact", "d_hat") if isinstance(c, dict) else MISSING
    if not (isinstance(c, dict) and c.get("status") == "ok" and "contact_gaps_final" in c and is_num(dh)):
        return None
    groups = {}
    for g in c["contact_gaps_final"]:
        if g["distance_m"] < dh:
            key = "–".join(sorted((contact_kind(g["a"]), contact_kind(g["b"])), key=CONTACT_KINDS.index))
            groups.setdefault(key, []).append(g["distance_m"] * 1000)
    return groups


def contact_columns(rows):
    """One (header, fn) column per contact kind seen in any of the rows; each cell is that kind's gap range in mm."""
    kinds = sorted({k for r in rows if r["state"] == "ok" for k in (contact_groups(r["data"]) or {})},
                   key=lambda k: [CONTACT_KINDS.index(p) for p in k.split("–")])

    def cell(d, k):
        v = (contact_groups(d) or {}).get(k)
        return (g3(min(v)) + (f"–{g3(max(v))}" if len(v) > 1 else "")) if v else "—"
    return [(f"{k} 间隙（mm）", lambda d, k=k: cell(d, k)) for k in kinds]


def kappa_used(d):
    """(kappa libuipc actually used in Pa, "否" / "夹到下限" / "夹到上限") from its own log lines (sweep.py kappa_log,
    needs --log-level Debug; a level run without it is read from its Debug rerun under genesis_<set>_kdebug/), or
    None without a logged corridor. Pairs logged with kappa 0 are Genesis's disabled pairs (its no-collision element,
    coupler.py:717-720) and are skipped."""
    kl = d.get("kappa_log") or {}
    if not kl.get("kappa_corridor"):
        kl = (load_json(SWEEP_ROOT / f"genesis_{GEN_TAG}_kdebug" / d["sweep"] / f"{d['level']}.json")[0] or {}) \
            .get("kappa_log") or {}
    corr = [h["groups"] for h in kl.get("kappa_corridor", [])]
    if not corr:
        return None
    lo, hi = float(corr[0][0]), float(corr[0][1])
    clamped = sorted({float(h["groups"][3]) for h in kl.get("model_kappa_clamped", []) if float(h["groups"][0]) > 0})
    if clamped:
        return clamped[0], ("夹到下限" if abs(clamped[0] - lo) < 1e-3 * lo else "夹到上限")
    return (d.get("overrides") or {}).get("contact_resistance", GENESIS_DEFAULTS["contact_resistance"]), "否"


def col_kappa(d):
    k = kappa_used(d)
    return sci(k[0]) if k else "—"


def col_clamped(d):
    k = kappa_used(d)
    return k[1] if k else "—"


NEWTON, SECONDS = ("Newton 次数（中位 / 最多）", col_newton), ("仿真总耗时（s）", col_wall_seconds)
KAPPA = ("libuipc 实际用的 κ（Pa）", col_kappa)
CLAMPED = ("被夹", col_clamped)
CONTACT = ("CONTACT", None)  # expanded by contact_columns into one column per contact kind
EDGE = ("软球表面边长（mm，静止 12.1）", col_edge)
# The columns each experiment's table shows: only the quantities its conclusion is about, so the trend reads at a
# glance. Penetration is the same for every level (none found) and is stated once in the section text instead.
# CONTACT measures each gap to the surface an object actually touches (cloth, another object, a wall or the ground).
SWEEP_COLUMNS = {"baseline": [CONTACT, NEWTON], "d_hat": [CONTACT, NEWTON, EDGE], "dt": [CONTACT, NEWTON, SECONDS],
                 "resistance": [KAPPA, CLAMPED, CONTACT], "init_penetration": [],
                 "mesh_res": [KAPPA, CLAMPED, CONTACT, SECONDS]}


def gen_raw_cells(r, cols):
    """One level's row: the (header, fn) columns read from its result file."""
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
            if r["sweep"] == "baseline" and sweep == "mesh_res":  # 官方 Sphere 也是细分 3 次，排在「中」前面
                return 2.5
            v = gen_level_value(r, sweep, cfg)
            return v if is_num(v) else float("inf")
        rows.sort(key=key)
        cols = [c for col in SWEEP_COLUMNS[sweep] for c in (contact_columns(rows) if col is CONTACT else [col])]
        trs = [[gen_label(r, sweep, cfg)] + gen_raw_cells(r, cols) for r in rows]
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


# The contact energy every experiment below refers to: the IPC paper's barrier (Li et al. 2020, eq. 5 and the
# clamped log barrier), libuipc's log^2 barrier (contact_models/sym/codim_ipc_contact.inl KappaBarrierLog2, called with
# kappa * dt^2 in ipc_simplex_normal_contact.cu), Genesis's per-pair kappa (coupler.py:680-717) and libuipc's kappa
# corridor (global_contact_manager.cu:265-303; plugged in, it reproduces every logged corridor)
FORMULA_BLOCK = (
    '<h3>IPC 的接触能量与 κ 区间（下面各实验都用）</h3>'
    '<p class="expect"><b>原版 IPC 论文：</b>每一对距离小于 d̂ 的接触（点–三角形、边–边）加一项 barrier 能量，'
    '距离越近能量越大，距离为 0 时无穷大——这堵「无穷高的墙」保证永远穿不过去：'
    '\\[ b(d)=\\begin{cases}-(d-\\hat d)^2\\ln\\dfrac{d}{\\hat d}, & 0&lt;d&lt;\\hat d\\\\[2pt] 0, & d\\ge\\hat d\\end{cases}'
    '\\qquad E_{\\text{contact}}=\\kappa\\sum_{k\\in C} b(d_k) \\]</p>'
    '<p class="expect"><b>libuipc 实际用的：</b>换成按距离平方 \\(D=d^2\\) 写的 log² 形式（同样 \\(D\\to0\\) 时无穷大、'
    '\\(D\\ge\\hat d^{2}\\) 时为 0），并且整个能量乘了 \\(\\Delta t^2\\)：'
    '\\[ B(D)=\\kappa\\,\\Delta t^{2}\\,(D-\\hat d^{2})^{2}\\Big[\\ln\\frac{D}{\\hat d^{2}}\\Big]^{2},\\qquad '
    '0&lt;D&lt;\\hat d^{2} \\]</p>'
    '<p class="expect"><b>Genesis：</b>自己不算 barrier。它把每个物体的 contact_resistance 当作 κ，两个物体之间取调和平均'
    '\\(\\kappa_{ij}=\\dfrac{2\\,r_i r_j}{r_i+r_j}\\)，交给 libuipc；libuipc 再把它夹进按场景算出的区间：'
    '\\[ \\kappa_{\\min}\\approx\\frac{10^{11}\\,s\\,L^{2}\\,\\bar m}{4\\,\\hat d^{4}\\,\\Delta t^{2}},\\qquad '
    '\\kappa_{\\max}=100\\,\\kappa_{\\min} \\]'
    '直观上：每个顶点的惯性刚度是 \\(\\bar m/\\Delta t^{2}\\)，barrier 必须比它硬才能在一步内挡住物体，所以 '
    '\\(\\bar m\\) 越大 → κ 越大，\\(\\Delta t\\)、\\(\\hat d\\) 越小 → κ 急剧变大。</p>'
    + "<div class=\"tablewrap\"><table><tr><th>符号</th><th>含义</th><th>本场景（官方默认）</th></tr>"
    + "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in [
        ("\\(d\\)", "一对接触图元（点–三角形 / 边–边）之间的距离", "—"),
        ("\\(D=d^2\\)", "距离的平方（libuipc 用它算 barrier）", "—"),
        ("\\(\\hat d\\)", "barrier 开始起作用的距离", "10 mm"),
        ("\\(\\kappa\\)", "barrier 的刚度", "设 1e9 Pa，实际被夹到 1.57e7 Pa"),
        ("\\(C\\)，\\(k\\)", "所有距离小于 \\(\\hat d\\) 的接触对，\\(k\\) 是其中一对", "—"),
        ("\\(b(d)\\)，\\(B(D)\\)", "一对接触的 barrier 能量（论文 / libuipc）", "—"),
        ("\\(\\Delta t\\)", "时间步长", "0.02 s"),
        ("\\(r_i\\)", "物体 \\(i\\) 的 contact_resistance", "1e9 Pa"),
        ("\\(s\\)", "libuipc 的缩放系数 kappa_eval_scale", "1e-16"),
        ("\\(L\\)", "整个场景包围盒的对角线长度", "3.69 m"),
        ("\\(\\bar m\\)", "全场景所有顶点的平均质量", "0.0184 kg"),
        ("\\(\\kappa_{\\min}\\)，\\(\\kappa_{\\max}\\)", "libuipc 允许的 κ 下限 / 上限", "1.57e5 / 1.57e7 Pa"),
    ]) + "</table></div>")


# What each kind of line in the initial-penetration log means (libuipc sanity_check/
# simplicial_surface_intersection_check.cpp:289-336, core/internal/world.cpp:46; Genesis coupler)
INIT_LOG_GLOSSARY = [
    ("Intersection detected between Edge(a,b) … and Triangle(c,d,e) …",
     "开跑前检查发现一个球表面的边穿过了另一个球表面的三角形，即两球一开始就互相穿进去了。"),
    ("World is not valid, skipping init.", "libuipc 因此拒绝初始化，仿真不开跑。"),
]


@functools.lru_cache(maxsize=None)  # one record object: plan_videos sets its video_web, the page reads it
def bypass_video_record(level):
    """Video record (collect_demo fields) of an initial-penetration level run with libuipc's start check turned off
    (sanity_check_enable = false; GEN_VIDEO_ROOT/init_penetration_<level>_bypass/), or None when not recorded."""
    d = GEN_VIDEO_ROOT / f"init_penetration_{level}_bypass"
    mp4, ex = d / "genesis_ipc_objects_in_box.mp4", d / "job.exit"
    if not (ex.is_file() and ex.read_text().strip() in ("0", "3", "4") and mp4.is_file() and mp4.stat().st_size > 0):
        return None
    return {"key": f"box{GEN_TAG and '_' + GEN_TAG or ''}_init_penetration_{level}_bypass", "expects_video": True,
            "title": "关掉开跑前检查、硬跑", "line": "", "dir": d, "state": "ok", "reason": None, "images": [],
            "video_src": mp4, "video_note": None}


def bypass_distance_table(level):
    """Centre distance of the two interpenetrating soft balls (the official one, entity 3, and the extra one, the last
    soft ball) at a few frames of the start-check-off run (genesis_<set>_bypass/init_penetration/<level>.json,
    frames[*].objects centroids), as a table; "" when that run is missing."""
    d, _ = load_json(SWEEP_ROOT / f"genesis_{GEN_TAG}_bypass" / "init_penetration" / f"{level}.json")
    if not (isinstance(d, dict) and d.get("status") == "ok" and d.get("frames")):
        return ""
    balls = [n for n in d["frames"][0]["objects"] if "Sphere" in n]
    a, b = next(n for n in balls if n.startswith("3_")), balls[-1]
    pick = [f for f in d["frames"] if f["frame"] in (1, 5, 10, 25, 50, len(d["frames"]))]
    dist = [math.dist(f["objects"][a][:3], f["objects"][b][:3]) * 1000 for f in pick]
    return table(["帧"] + [str(f["frame"]) for f in pick], [["两球球心距离（mm）"] + [g3(x) for x in dist]])


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
    # an expectation may carry a small-print note after "§" (numbers plugged in, cross-checks)
    principle, _, note = expect.partition("§")
    parts = [f"<h3>{esc(title)}</h3>", f'<p class="expect"><b>按原理期待：</b>{esc(principle)}</p>',
             f'<p class="setting">{esc(note)}</p>' if note else "",
             f'<p class="setting">{esc(setting)}看：{esc(OBSERVE[sweep])}</p>']
    if sweep == "init_penetration":
        for r in rows:
            if r["sweep"] == "baseline":
                continue
            lines, n_inter = init_log_excerpt((r["data"] or {}).get("log_path")
                                              or GEN_SWEEP_ROOT / sweep / f"{r['level']}.log")
            parts.append(f'<p class="small"><b>{esc(gen_label(r, sweep, cfg))}</b>：开跑前就被拒，没有视频。'
                         "上面是这个初始状态（同一场景只关掉开跑前的相交检查、只渲染第 0 帧，侧面看两个软球；球心相距 2R 减去穿插量，不穿插时至少 2R），"
                         "下面是被拒那次的日志原文摘录。</p>")
            img = init_frame_image(r["level"])
            log = (f'<pre class="log">{esc(chr(10).join(lines))}</pre>' if lines is not None
                   else '<div class="novideo">日志文件不存在</div>')
            parts.append((f'<img class="plot initframe" src="assets/images/{img[1]}" alt="初始状态：两个软球互相穿插" '
                          'loading="lazy">' if img else "") + log)
            parts.append(table(["日志里的句子", "是什么意思"], [list(g) for g in INIT_LOG_GLOSSARY]))
            rec = bypass_video_record(r["level"])
            if rec:
                parts.append('<p class="small"><b>关掉开跑前的相交检查（sanity_check_enable = false）硬跑：</b>'
                             "同一场景、同样的穿插，跑满 2 s。两个半径 80 mm 的球不穿插时球心至少相距 160 mm。</p>")
                parts.append('<div class="grid">' + demo_card(dict(rec, key=rec["key"]), {}) + "</div>")
                parts.append(bypass_distance_table(r["level"]))
    else:
        cards = [demo_card(dict(videos[(r["sweep"], r["level"])], title=gen_label(r, sweep, cfg)), {})
                 for r in rows if (r["sweep"], r["level"]) in videos]
        parts.append('<div class="grid">' + "".join(cards) + "</div>")
    parts.append(tbl)
    parts.append('<ul class="obs">' + "".join(f"<li>{esc(o)}</li>" for o in OBSERVATIONS[sweep]) + "</ul>")
    parts.append(f'<p class="concl"><b>结论：</b>{esc(FINDINGS[sweep])}</p>')
    return "\n".join(parts)


def sweep_section(gsweeps, cfg, videos):
    """盒子实验：set_variant 选的那套（主结果）的每个参数扫描。"""
    gb = rows_of(gsweeps, "baseline")
    gd = gb[0]["data"] if gb and gb[0]["data"] else {}
    gscene = ("Genesis 没有「一堆物体扔进盒子」的官方例子，这里用官方 ipc_objects_falling 场景加一个盒子和更多同款物体。"
              f"这一套：{VARIANTS[GEN_TAG]['label']}。所有实验每次只改一个参数。盒子墙画成半透明，盒内物体不透明。"
              "除了一开始就穿插的两档（开跑前被拒），所有档位 libuipc 的穿透检查都没有报穿透。")
    parts = ['<section id="sweep"><h2>盒子实验与 IPC 参数扫描</h2>', f"<p>{esc(gscene)}</p>", FORMULA_BLOCK]
    for sweep, title, one, expect, rows, tbl in genesis_sweep_tables(gsweeps, cfg):
        parts.append(genesis_experiment(sweep, title, one, expect, rows, tbl, cfg, videos, gd))
    parts.append("</section>")
    return "\n".join(parts)


# What the experiments above mean for generating training data: (what to do, which result above it rests on)
DATA_IMPLICATIONS = [
    ("接触刚度 κ 要显式设定，并从 libuipc 日志确认没被夹。",
     "κ 实验：Genesis 的 contact_resistance 只有落在 libuipc 按场景算出的区间里才生效，区间外一律被夹到边界。"),
    ("同一批数据里每个样本用同一个显式 κ，不能交给 libuipc 自动选。",
     "网格实验：κ 区间按全场景所有顶点的平均质量定，加密一个物体会让其他物体的接触变软（方块–地面间隙 8.1–8.5 → "
     "4.1 mm）；dt 实验：\\(\\kappa_{\\min}\\propto 1/\\Delta t^{2}\\)，dt 越小接触越硬。"),
    ("d̂ 是数据里接触的尺度：要记录下来，且不能大于物体最短的表面边长。",
     "d̂ 实验：每一对接触都停在约 0.6–1 倍 d̂ 的地方；d̂ 越小越贴近、但每帧 Newton 迭代越多；d̂ = 30 mm 大于软球表面边长时，"
     "自接触把球从里面撑开。"),
    ("初始状态必须无穿透，生成初始条件时先检查。", "初始穿插实验：一开始就互相穿插时 libuipc 拒绝开跑。"),
    ("关掉 libuipc 的半隐式提前终止，或者逐帧检查是否真的收敛。",
     "官方测试「地面滑动」：它默认开着，Newton 没算到收敛就停，误差积累成方块翻倒；关掉后断言通过。"),
]


def data_section():
    """The training-data implications of the experiments, one item per DATA_IMPLICATIONS entry (what to do + basis)."""
    items = "".join(f"<li><b>{esc(what)}</b><br><span class=\"small\">依据：{esc(why)}</span></li>"
                    for what, why in DATA_IMPLICATIONS)
    return f'<section id="data"><h2>对生成训练数据的意义</h2><ol>{items}</ol></section>'


# ---------------- 关键数字（发现 / 结论里用，dry-run 时也打印出来核对） ----------------
def compute_facts(demos):
    """Numbers the demo cards quote: the momentum example's final relative momentum error (run_info.json)."""
    mom = [r for r in demos if r["key"] == "genesis_ipc_momentum" and r["state"] == "ok"]
    return {"momentum_err": dig(mom[0]["info"], "run", "final_rel_momentum_error") if mom else MISSING}




def build_pages(demos, tests, hydro, gsweeps, cfg, facts, videos, commit):
    """{路径: HTML}：index.html 只列各周的链接和摘要，每周一页 <weekN>.html（WEEKS 决定有哪几周、各放哪几节）。"""
    n_video = sum(1 for r in demos + tests if r.get("video_web"))
    n_pass = sum(1 for r in tests if r.get("outcome") == "passed")
    n_ran = sum(1 for r in tests if r.get("outcome") in ("passed", "failed"))
    n_gsweeps = sum(1 for s in gsweeps if s["name"] != "baseline")
    n_grows = sum(len(s["rows"]) for s in gsweeps)
    summary = {
        "week2": "文献、公式、hydroelastic demo、实验设计、训练数据检查。",
        "week1": ("在服务器上跑通了 Genesis + lib IPC（Genesis 的 IPC 接触底层由 libuipc 计算）："
                  f"{len(demos)} 个 Genesis IPC 例子和 {len(tests)} 个 Genesis 官方 IPC 测试场景，共 {n_video} 段视频"
                  f"（官方测试 {n_ran} 个跑完，其中官方断言通过 {n_pass} 个）。"
                  f"然后做了「一堆物体扔进盒子」（官方 ipc_objects_falling 场景加一个盒子），对 {n_gsweeps} 个 IPC 参数"
                  f"做了单变量扫描，共 {n_grows} 个配置。主要发现：初始穿插会被拒绝开跑；表面全程没有穿透；"
                  "d̂ 越小越难解，且不能大于软体表面网格的边长；Genesis 默认 κ 1e9 会被 libuipc 夹到区间上界；"
                  "官方软球 E = 1 kPa 太软，会被压塌，所以主结果用 E = 1e5。")}
    sections = {"week2": week2_sections(hydro, label_facts()),
                "week1": [videos_section(demos, tests, facts), sweep_section(gsweeps, cfg, videos), data_section()]}
    cards = "".join(f'<a class="weekcard" href="{wid}.html"><b>{esc(title)}</b>'
                    f'<span>{esc(" · ".join(n for _, n in items))}</span><p>{esc(summary[wid])}</p></a>'
                    for wid, title, items in WEEKS)
    pages = {WEB / "index.html": page(f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1></header>\n'
                                      f'<nav class="weeks">{cards}</nav>', PAGE_TITLE)}
    for wid, title, items in WEEKS:
        nav = "".join(f'<a href="#{h}">{esc(n)}</a>' for h, n in items)
        body = (f'<header class="top"><a href="index.html">← 所有周</a><h1>{esc(title)}</h1>'
                f'<p class="summary">{esc(summary[wid])}</p><nav class="toc">{nav}</nav></header>\n'
                + "\n".join(sections[wid]))
        pages[WEB / f"{wid}.html"] = page(body, f"{PAGE_TITLE} · {title}")
    return pages


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
    hydro = collect_official_test(HYDRO["key"], HYDRO["nodeid"], HYDRO["title"], "")
    all_demos = demos + tests + [hydro]
    videos = gen_level_videos(gsweeps)   # 盒子扫描每档自己的视频（和 demo 视频一起压缩、一起上传）
    bypass = [x for x in (bypass_video_record(r["level"]) for r in rows_of(gsweeps, "init_penetration")) if x]
    vjobs, manifest = plan_videos(all_demos + list(videos.values()) + bypass,
                                  args.crf, args.force_videos)
    ijobs = plan_images(all_demos) + [{"src": src, "dst": IMAGE_DIR / name, "size": src.stat().st_size}
                                      for src, name in filter(None, (init_frame_image(r["level"])
                                                                     for r in rows_of(gsweeps, "init_penetration")))]
    ijobs += [{"src": OUT_ROOT / "figs" / name, "dst": IMAGE_DIR / name, "size": (OUT_ROOT / "figs" / name).stat().st_size}
              for name in (E0_FIG,)]
    facts = compute_facts(demos)
    commit = genesis_commit()

    pages = build_pages(demos, tests, hydro, gsweeps, cfg, facts, videos, commit)

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
