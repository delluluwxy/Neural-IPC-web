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

index.html 发布在 GitHub Pages（https://delluluwxy.github.io/Neural-IPC-web/）：开头是 doctype、charset、viewport，
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
    "key": "genesis_test_test_sap_rigid_rigid_hydroelastic_contact_64_fit",
    "nodeid": "tests/coupling/test_hybrid.py::test_sap_rigid_rigid_hydroelastic_contact[64]",
    "title": "两条关节链落到盒子上（官方 hydroelastic 测试）",
    "setting": "Genesis 仓库自带的 hydroelastic 测试，场景和断言一字未改：地上放一个 0.5 × 0.5 × 0.2 m 的方盒子，"
               "两条由球和胶囊（半径 24 mm）连成的关节链从盒子上方落下。接触由 Genesis 自带的 SAP 求解器按 hydroelastic "
               "模型计算（不是 IPC / libuipc）：刚体对刚体、刚体对地面都用 hydroelastic，刚体求解器自己的碰撞不参与。"
               "视频慢放约 3.75 倍（1.33 s 的仿真放成 5 s）。",
    # (quantity, value, what it shows) - source defaults / values set by the test only
    "params": [("压力场刚度", "1e8 Pa", "物体最深处的压力；越大越「硬」，嵌入越浅"),
               ("接触类型", "全部 hydroelastic", "刚体–刚体、刚体–地面都按压力场算"),
               ("阻尼时间尺度 τ_d", "0.1 s", "接触的耗散：越大越不弹"),
               ("仿真长度", "80 步 = 1.33 s", "视频覆盖的物理时间")],
    "expect": "hydroelastic 不像 IPC 那样留一条缝，而是允许两个物体互相嵌进去一点：每个物体内部预先指定一个压力场，"
              "表面为 0、最深处为 1e8 Pa，按「到表面的距离 ÷ 最大距离」线性增长；两物体压力相等的那张面就是接触面，"
              "接触力 ≈ 面积 × 那里的压力。所以 (1) 物体是靠「嵌进去」托住的，嵌入越深推力越大；(2) 以这个刚度，"
              "托住盒子和链只需要微米级的嵌入，画面上看不出穿插；(3) SAP 带阻尼，链落下后应很快停住、不明显反弹；"
              "(4) 压力场只由几何和这一个刚度数决定，和材料的杨氏模量、真实弹性形变无关。",
    # short observations; None = waiting for the re-recorded data (--rigid-trajectory), not shown until filled in
    "obs": ["官方断言全部通过：80 步（1.33 s）后各连杆速度 < 0.03 m/s、盒子偏离初始位置 < 2 mm、两条链叠在盒子上、第二条在第一条上面",
            "链落到盒子上 → 被托住、叠放，没有穿过盒子或地面",
            "刚度 1e8 Pa → 嵌入看不出来，画面上像硬接触",
            # measured (rigid_trajectory_scene0.npz of the _fit run): box z final 0.0999920 m, deepest 16.9 µm, no tilt
            "盒子最终压进地面约 8 µm（链砸下时最深约 17 µm），盒子没有倾斜 → 是靠嵌入产生的推力托住的，"
            "和期待 (1)(2) 的微米级一致",
            ],
    "concl": "与期待一致：物体靠微米级嵌入被托住，画面上看不出穿插；按官方阈值 80 步后已静止，用更严的判据"
             "（顶点速度 < 2 mm/s 持续 0.3 s）看，1.33 s 时链还有轻微晃动。",
    "explain": ["所有支撑力都来自压力场：用 SAP 时刚体求解器自己的碰撞被跳过，地面也只按物体一侧的压力场算（地面当作无限硬）。",
                "「软硬」只由一个人为指定的刚度数控制，与材料杨氏模量无关。"],
}

PAGE_TITLE = "Neural-IPC 周汇报"
NAV = [("meeting", "组会提纲"), ("videos", "Demo 视频"), ("tests", "官方测试场景"), ("hydro", "Hydroelastic 接触"), ("label", "造 label 的收敛检查"),
       ("sweep", "盒子实验与参数扫描"),
       ("data", "对生成数据的意义")]  # 锚点只用字母


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
nav.toc { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 0.9rem; margin: 14px 0 0;
          padding-bottom: 12px; border-bottom: 1px solid var(--border); }
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


def page(body):
    """整页 HTML：doctype + charset/viewport + <title>/<style>，省略 html/head/body 标签（HTML5 允许）。"""
    # math: MathJax (SVG output, no font files) renders \( \) inline and \[ \] display LaTeX; the d̂ symbol in prose
    # becomes \hat d as well
    body = body.replace("d̂", r"\(\hat d\)")
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(PAGE_TITLE)}</title>
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


def hydro_section(r):
    """The hydroelastic test (HYDRO), in the order setting -> parameter table -> expectation -> video ->
    observations -> conclusion -> explanation. r = collect_official_test(HYDRO ...); its outcome is read live."""
    outcome = OUTCOME_LABEL.get(r.get("outcome"), "无结果") if r["state"] == "ok" else r["reason"]
    obs = [o for o in HYDRO["obs"] if o]
    parts = ['<section id="hydro"><h2>Hydroelastic 接触（Genesis 官方测试）</h2>',
             f'<p class="setting">{esc(HYDRO["setting"])}</p>',
             table(["量", "取值", "直观上是什么"], [list(row) for row in HYDRO["params"]]),
             f'<p class="expect"><b>按原理期待：</b>{esc(HYDRO["expect"])}</p>',
             '<div class="grid">' + demo_card(dict(r, title=HYDRO["title"], line=f"官方断言：{outcome}。"), {})
             + "</div>",
             '<ul class="obs">' + "".join(f"<li>{esc(o)}</li>" for o in obs) + "</ul>",
             f'<p class="concl"><b>结论：</b>{esc(HYDRO["concl"])}</p>',
             *[f'<p class="small">{esc(e)}</p>' for e in HYDRO["explain"]],
             "</section>"]
    return "\n".join(parts)


# ---------------- 组会提纲（用户 10-10：看了啥 ref → 公式理论 → 做了什么 demo → 实验设定 → 视频和结论 → 遇到什么问题） ----------------
# 每部分：(锚点, 标题, 要点列表, 详细内容所在小节的锚点或 None)。第 1、2 部分素材来自 Neural-IPC-sandbox
# docs/drafts/meeting_refs_theory.md（由已核对的讲义 docs/handouts/ 提炼）；其余部分的数字都出自本页各小节。
MEETING = [
    ("m_refs", "1. 这周看了哪些文献", [], None),
    ("m_theory", "2. 公式和理论", [], None),
    ("m_demo", "3. 做了哪些 demo", [
        "Genesis 官方 IPC 例子 4 个原样跑通并录像：物体落地、动量守恒、机械臂抓软方块、一堆物体扔进盒子；"
        "另有本地带窗口遥控机械臂抓布。",
        "Genesis 官方 IPC 测试 4 个按测试自带断言录像（离地间隙、斜向重力滑动、布盖物体、夹布拖动）；"
        "两个断言没过，原因已查清（见各卡片说明）。",
        "Genesis 官方 hydroelastic 测试：两条关节链落到盒子上，接触由 SAP 求解器按压力场计算。"], "videos"),
    ("m_setup", "4. 实验设定", [
        "盒子实验：官方「物体落地」场景加一个开口盒子，只扫 IPC 自己的参数（d̂、接触刚度 κ、ε_v、时间步、初始穿插、网格），"
        "每次只改一个。",
        "造 label 的收敛检查：supervisor 造训练数据的球压算例（刚性球压软块，和 Hertz 解析解比），逐样改 d̂、网格、"
        "球面细分、接触模型、块大小、材料参数，看 label 收敛到哪里。"], "label"),
    ("m_result", "5. 视频和结论", [
        "IPC 全程没有穿透；停住时每一对接触面之间都留一条约 0.7–1 倍 d̂ 的缝，越重的物体陷得越深。",
        "接触刚度 κ 只在 libuipc 按场景算出的区间里生效，区间外会被夹到边界；Genesis 默认值会被夹。",
        "Hydroelastic：物体靠微米级的互相嵌入被托住，画面上看不出穿插；它的软硬只由一个人为指定的刚度数决定。",
        "label：supervisor 之前「label 接近 Hertz」是两个误差抵消的结果（d̂ 太大把能量算高、球面太粗把能量算低），"
        "不能再引用；改对以后小压深在网格外推误差内和 Hertz 一致，大压深剩约 2%，推测是 Hertz 自身的近似，正在验证。"],
     "label"),
    ("m_problems", "6. 遇到的问题", [
        "IPC 的 d̂ 让能量偏大：两表面还隔着 d̂ 就开始推，小压深时 supervisor 的设置多算约 80%；要用多档 d̂ 外推到 0。",
        "球面网格太粗：三角形比接触区还大，球底其实是个多面体尖顶；球面细分至少 7 次。",
        "新版 libuipc 的软体材料参数和设定值对不上：设 E = 1e5、ν = 0.3，拉棒实测是 E ≈ 1.245e5、ν ≈ 0.214。"
        "原因是 2026-08 换了材料能量公式，但参数换算没跟着改；supervisor 用的旧版不受影响，我们的新版和 Genesis 的软体都受影响。"
        "改法：换算后直接改写参数，实测回到设定值。",
        "label 的能量公式要跟着换：supervisor 的脚本按旧公式算能量，在旧版软件下没问题；换到新版后软件内部换了新公式，"
        "两边对不上，label 要改按新公式算。",
        "块不够大、接触区网格不够细也会让结果偏高，都要加大、加密或外推。",
        "这套全改对的设置很贵（每个进程约 20 GB 显存，每档 d̂ 约 8–11 分钟），批量造数据要另定便宜的生产配置，"
        "再用这套贵的量出它的偏差、列成表。"], "label"),
]


MEETING_MD = PROJECT / "docs" / "drafts" / "meeting_refs_theory.md"   # 第 1、2 部分的唯一来源
MEETING_MD_PARTS = {"m_refs": "## 第一部分", "m_theory": "## 第二部分"}   # md 里每部分的标题行开头；「## 出处」是内部路径，不上页面


def _md_to_html(md_text):
    """Markdown（表格、列表、粗体）转 HTML；$$…$$ / $…$ 先换成占位符，转完再还原成 MathJax 的 \\[…\\] / \\(…\\)。"""
    import markdown
    maths = []

    def stash(m, display):
        maths.append((display, m.group(1)))
        return f"@@MATH{len(maths) - 1}@@"
    t = re.sub(r"\$\$(.+?)\$\$", lambda m: stash(m, True), md_text, flags=re.DOTALL)
    t = re.sub(r"\$(.+?)\$", lambda m: stash(m, False), t)
    t = re.sub(r"(?m)^(?![-|\s])(.+)\n(?=[-|] )", r"\1\n\n", t)   # 列表/表格紧跟在文字行后面时补空行，否则不认
    t = re.sub(r"(?m)^### ", "#### ", t)                          # 稿内小标题降一级，挂在提纲的 h3 下面
    out = markdown.markdown(t, extensions=["tables"])
    for i, (display, body) in enumerate(maths):
        out = out.replace(f"@@MATH{i}@@", (f"\\[{esc(body)}\\]" if display else f"\\({esc(body)}\\)"))
    return out


def _meeting_md_part(anchor):
    """meeting_refs_theory.md 里某一部分（从它的 ## 标题到下一个 ## 标题），去掉标题行，转成 HTML。"""
    text = MEETING_MD.read_text(encoding="utf-8")
    body = text[text.index(MEETING_MD_PARTS[anchor]):].split("\n", 1)[1]
    nxt = body.find("\n## ")
    body = body[:nxt] if nxt >= 0 else body
    return _md_to_html(body.replace("\n---", "\n"))


def meeting_section():
    """The meeting outline: six parts in the order the user presents them, each linking to the detailed section."""
    parts = ['<section id="meeting"><h2>组会提纲</h2>']
    for anchor, title, items, detail in MEETING:
        link = f' <a class="small" href="#{detail}">（详细）</a>' if detail else ""
        parts.append(f'<h3 id="{anchor}">{esc(title)}{link}</h3>')
        if anchor in MEETING_MD_PARTS:
            parts.append(f'<div class="meeting-md">{_meeting_md_part(anchor)}</div>')
        else:
            parts.append('<ul>' + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>")
    parts.append("</section>")
    return "\n".join(parts)


# ---------------- 造 label 的收敛检查（实验方案 E0） ----------------
# Data: Neural-IPC-sandbox tools/ipc_sweep/uipc_constitution_check.py json files (one per configuration), figures from
# tools/figs/sphere_facets_vs_contact.py and tools/figs/e0_label_convergence.py. Every number below is read from them.
E0_ROOT = OUT_ROOT / "e0_label_convergence"
SPHERE_ROOT = E0_ROOT   # sphere check: ipc, h_c 0.0125, d̂ 2.5e-4, depths 0.004 / 0.012, sphere 4 / 6 / 7
SPHERE_RUNS = {n: f"sphere_{n}/ipc_hc0.0125_dhat0.00025_sph{n}.json" for n in (4, 6, 7)}
E0_FIGS = [("sphere_facets_vs_contact.png", "从正下方看刚性球的底部：supervisor 的球面细分 4 次时，"
                                            "压深 0.004 的接触圆里只有一圈三角形"),
           ("e0_label_convergence.png", "块的弹性能 ÷ Hertz 能量，随 d̂（横轴）和网格（颜色）的变化；"
                                        "实线 = 压深 0.004，虚线 = 压深 0.024；纵轴 1 = 与解析解一致")]
SUPERVISOR_RECORDED = 1.418   # supervisor results/hertz_results.txt:18（libuipc 0.0.25 al-ipc，压深 0.004 的 U/U_H）
LABEL = {
    "setting": "supervisor 造 label 用的 Hertz 算例原样照搬：一个刚性球（半径 1）竖直压进一块底面固定的软块"
               "（宽 2.4、高 1.2，E = 1e5，ν = 0.3），无摩擦、无重力，每个压深静置到平衡后算块里存的弹性能 U，"
               "这就是训练网络用的 label。和 Hertz 解析解 U_Hertz = (8/15)·E*·√R·δ^2.5 比（变形小时它是精确的；压得深时它自己的小变形、抛物面近似也有误差）。"
               "压深照 supervisor 的 6 个，另外逐样改下表里的设置，看 label 会不会变。",
    "params": [("压深 δ", "0.004 – 0.024（6 个）", "球最低点压到块顶以下多深；supervisor 的主扫描"),
               ("d̂", "1e-3（supervisor）、5e-4、2.5e-4", "barrier 开始推的距离：两表面离得比 d̂ 近就开始互相推"),
               ("接触区网格尺寸 h_c", "0.025（supervisor）、0.0125、0.00625", "块在接触区的四面体大小；最细一档 265 万个四面体"),
               ("球面细分次数", "4（supervisor）、6、7", "球面三角形的大小；次数每加 1，边长减半"),
               ("接触模型", "al-ipc（supervisor）、原版 IPC", "al-ipc 不用 barrier、用增广拉格朗日防穿透"),
               ("块的半宽 = 深度 L", "1.2（supervisor）、2.4、4.8", "块越大越接近 Hertz 假设的无限大半空间"),
               ("细网格区外的单元放大倍数", "1.4（supervisor）、1.2", "越接近 1，远处的网格越细"),
               ("材料参数", "照原样传入（supervisor）、直写属性", "见下面「材料参数」一段"),
               ("label 怎么算", "小应变线弹性公式（supervisor 的球压脚本）、SNK1 公式",
                "SNK1 = 我们这版求解器真正在最小化的能量")],
    "expect": "如果 label 是对的，这些设置都加密、放大以后，U ÷ U_Hertz 应该不再变化，而且接近 1。",
    "concl": "label 目前主要被 d̂ 拉高：supervisor 用的 d̂ = 1e-3 在小压深下多算了约 80% 的能量；"
             "球面太粗又把它压低了一部分，两者恰好抵消成看起来还行的 1.4。按 d̂ 外推到 0 以后剩下的偏高，"
             "来自块不够大、libuipc 新版的材料参数错配和网格分辨率：参数改对、label 按 SNK1 算以后，小压深再把网格加密"
             "就回到 Hertz（外推约 1.00–1.02），大压深再把块按 1/L 外推到无限大还剩约 2%，推测来自 Hertz 公式自身的近似（压深 0.024 时接触半径约 0.155，(a/R)² ≈ 2.4%），正在用不经过 IPC 的独立有限元验证。supervisor 之前「label 接近 Hertz」的结论是两个误差抵消的结果，不能再引用。造数据要用细分 ≥ 7 次的球、"
             "接触区网格足够细（压深 0.004 时 0.0125 还不够）、尽量小的 d̂（至少三档、"
             "确认和 d̂ 成正比后外推到 0）、足够大的块，并在新版 libuipc 上直写材料参数、按 SNK1 算 label。",
    "explain": ["为什么 d̂ 会把能量拉高：barrier 在两表面还隔着不到 d̂ 时就开始推，所以球还没真正碰到，"
                "块就已经在比真实接触圈更大的一片区域上被压下去，存的能量更多。这部分多出来的能量大致和 d̂ ÷ δ 成正比，"
                "所以压得越浅越严重。把实测间隙从压深里扣掉补不回来：扣了以后比值反而更偏离 1。",
                "为什么球面粗会把能量压低：细分 4 次时三角形边长约 0.076，比压深 0.004 时的 Hertz 接触半径 0.063 还大，"
                "球底在这个尺度上是个多面体的尖顶，不是光滑球面，接触区的形状和受力都不对。",
                "材料参数：libuipc 的 StableNeoHookean 先把输入的 E、ν 按原版 Stable Neo-Hookean 能量需要的公式换算"
                "（让它在小变形下正好等于输入值），可是 2026-08-23 起它的 GPU 端换成了另一种能量（Stiff-GIPC 的 SNK1），"
                "换算没跟着改。拉一根细长棒实测（已知拉力 ÷ 量到的伸长，不靠任何能量公式）证实了这一点。"
                "supervisor 用的 pyuipc 0.0.25 早于这次改动，不受影响；我们源码编译的版本和 Genesis 的 IPC 软体都受影响。"
                "「直写属性」= 换算之后再把块上的参数直接改成 SNK1 需要的值，实测 E、ν 回到设定值。",
                "label 为什么要按 SNK1 算：求解器求的是让 SNK1 能量最小的形状；如果 label 用别的公式在这个形状上算能量，"
                "它就不是任何一个材料模型的最小能量，大变形时连力（能量的梯度）都对不上。",
                "d̂ 外推要至少三档：块半宽 2.4、4.8 时 d̂ 每减半多出的部分缩小到约 0.46–0.51 倍，和 d̂ 成正比，外推可靠；"
                "半宽 1.2 时从 5e-4 减到 2.5e-4 只缩小到约 0.26–0.30 倍，再往下（1.25e-4、6.25e-5）又恢复到约 0.5 倍："
                "只有最大那档 d̂ 偏离正比，用最小两档外推不受影响。之前只用 5e-4、2.5e-4 两档外推，半宽 1.2 的小压深值"
                "（1.05）偏低，造成「块越大反而越高」的假象；改用最小两档后单调了。",
                "剩下的偏差怎么拆：小压深时块从半宽 2.4 放到 4.8 只降 0.006，块尺寸不是主因；把网格从 0.0125 加密到 0.00625"
                "就降了 2.7 个百分点（压深 0.004 时接触半径约 0.063，0.0125 的网格只横跨 5 个单元）。大压深时网格只差 0.7 个"
                "百分点，块尺寸占大头。两个外推都只用两个点，假设误差分别和 1/L、网格尺寸成正比；若网格误差按尺寸的平方"
                "减小，小压深外推值约 1.018。"],
}
# Block-size / far-mesh / material checks: ipc, h_c 0.0125, sphere 7. Each row = (label, json files in increasing-d̂
# order (5e-4, 2.5e-4[, 1.25e-4]), which energy); the table shows the straight line through the two smallest d̂ at d̂ = 0.
BLOCK_RUNS = [("1.2（supervisor 的块）", ["ipc_m/ipc_hc0.0125_dhat0.0005_sph7.json",
                                         "ipc_m/ipc_hc0.0125_dhat0.00025_sph7.json",
                                         "dhat125_L1.2/ipc_hc0.0125_dhat0.000125_sph7.json",
                                         "dhat0625_L1.2/ipc_hc0.0125_dhat6.25e-05_sph7.json"], "U_over_UH"),
              ("2.4", ["size_L2.4/ipc_hc0.0125_dhat0.0005_sph7_L2.4.json",
                       "size_L2.4/ipc_hc0.0125_dhat0.00025_sph7_L2.4.json",
                       "dhat125_L2.4/ipc_hc0.0125_dhat0.000125_sph7_L2.4.json"], "U_over_UH"),
              ("2.4，远处网格放大倍数 1.2", ["grow_L2.4_g1.2/ipc_hc0.0125_dhat0.0005_sph7_L2.4_g1.2.json",
                                         "grow_L2.4_g1.2/ipc_hc0.0125_dhat0.00025_sph7_L2.4_g1.2.json"], "U_over_UH"),
              ("4.8", ["size_L4.8/ipc_hc0.0125_dhat0.0005_sph7_L4.8.json",
                       "size_L4.8/ipc_hc0.0125_dhat0.00025_sph7_L4.8.json",
                       "dhat125_L4.8/ipc_hc0.0125_dhat0.000125_sph7_L4.8.json"], "U_over_UH"),
              ("4.8，材料参数直写", ["direct_L4.8/ipc_hc0.0125_dhat0.0005_sph7_L4.8_snk1_direct.json",
                                  "direct_L4.8/ipc_hc0.0125_dhat0.00025_sph7_L4.8_snk1_direct.json",
                                  "direct_L4.8/ipc_hc0.0125_dhat0.000125_sph7_L4.8_snk1_direct.json"], "U_over_UH"),
              ("4.8，材料参数直写，label 按 SNK1 算", ["direct_L4.8/ipc_hc0.0125_dhat0.0005_sph7_L4.8_snk1_direct.json",
                                                  "direct_L4.8/ipc_hc0.0125_dhat0.00025_sph7_L4.8_snk1_direct.json",
                                                  "direct_L4.8/ipc_hc0.0125_dhat0.000125_sph7_L4.8_snk1_direct.json"],
               "U_snk1_over_UH"),
              ("2.4，材料参数直写，label 按 SNK1 算", ["direct_L2.4/ipc_hc0.0125_dhat0.0005_sph7_L2.4_snk1_direct.json",
                                                  "direct_L2.4/ipc_hc0.0125_dhat0.00025_sph7_L2.4_snk1_direct.json",
                                                  "direct_L2.4/ipc_hc0.0125_dhat0.000125_sph7_L2.4_snk1_direct.json"],
               "U_snk1_over_UH"),
              ("4.8，网格 0.00625，材料参数直写，label 按 SNK1 算",
               ["fine_direct_L4.8_s7/ipc_hc0.00625_dhat0.00025_sph7_L4.8_snk1_direct.json",
                "fine_direct_L4.8_s7/ipc_hc0.00625_dhat0.000125_sph7_L4.8_snk1_direct.json"], "U_snk1_over_UH")]
# rows of BLOCK_RUNS used for the two final extrapolations (block size → ∞ by 1/L; mesh → 0 assuming error ∝ h)
FINAL_ROWS = {"L2.4": 6, "L4.8": 5, "L4.8_fine": 7}
UNIAXIAL = {"as_input": "uniaxial/uniaxial_as_input.json", "snk1_direct": "uniaxial/uniaxial_snk1_direct.json"}


def _load_e0():
    """{(constitution, h_c, d_hat, sph_sub): json} under E0_ROOT; per configuration the copy with the most depths."""
    best = {}
    for p in E0_ROOT.glob("*/*.json"):
        if p.name == "gpu_job_result.json":
            continue
        d, err = load_json(p)
        if err:
            raise SystemExit(f"[build_site] {p} 读不了：{err}")
        if "rows" not in d or "constitution" not in d:   # torch_fem/ 等别的工具的 json 不在这张表里
            continue
        if d.get("L", 1.2) != 1.2 or d.get("growth", 1.4) != 1.4:   # block-size checks are read by _load_block_size
            continue
        key = (d["constitution"], d["h_c"], d["d_hat"], d["sph_sub"])
        if key not in best or len(d["rows"]) > len(best[key]["rows"]):
            best[key] = d
    return best


def label_facts():
    """The numbers the label section quotes, all read from the result json files."""
    e0 = _load_e0()
    sph = {k: load_json(SPHERE_ROOT / v)[0] for k, v in SPHERE_RUNS.items()}
    missing = [str(SPHERE_ROOT / v) for k, v in SPHERE_RUNS.items() if sph[k] is None]
    if missing:
        raise SystemExit(f"[build_site] 球面细分检查的结果文件不在：{missing}")
    first = lambda d: d["rows"][0]["U_over_UH"]      # depth 0.004
    last = lambda d: d["rows"][-1]["U_over_UH"]      # depth 0.024
    fine = {dh: e0[("ipc", 0.00625, dh, 7)] for dh in (0.001, 0.0005, 0.00025)}
    al_fine = {dh: e0[("al-ipc", 0.00625, dh, 7)] for dh in (0.001, 0.0005, 0.00025)}
    mesh = {h: e0[("ipc", h, 0.00025, 7)] for h in (0.025, 0.0125, 0.00625)}

    def extrap(k):  # straight line through d̂ = 5e-4 and 2.5e-4 (finest mesh, ipc), evaluated at d̂ = 0
        y1, y2 = fine[0.0005]["rows"][k]["U_over_UH"], fine[0.00025]["rows"][k]["U_over_UH"]
        return y2 - (y1 - y2)

    return {
        "sph": [(n, sph[n]["rows"][0]["U_over_UH"], sph[n]["rows"][1]["U_over_UH"]) for n in (4, 6, 7)],
        "dhat_small": [first(fine[dh]) for dh in (0.001, 0.0005, 0.00025)],
        "dhat_large": [last(fine[dh]) for dh in (0.001, 0.0005, 0.00025)],
        "mesh_small": [first(mesh[h]) for h in (0.025, 0.0125, 0.00625)],
        "ipc_vs_al": [(first(fine[dh]), first(al_fine[dh])) for dh in (0.001, 0.00025)],
        "time_ratio": (sum(d["wall_seconds"] for d in al_fine.values()) / sum(d["wall_seconds"] for d in fine.values())),
        "ref_sph4": first(e0[("al-ipc", 0.025, 0.001, 4)]),
        "ref_sph7": first(e0[("al-ipc", 0.025, 0.001, 7)]),
        "extrap": (extrap(0), extrap(-1)),
        "block": [(lbl, len(paths), *_extrap_to_zero(paths, key)) for lbl, paths, key in BLOCK_RUNS],
        "uniaxial": {m: _read_e0(p) for m, p in UNIAXIAL.items()},
        # SNK1 label, d̂ → 0: block → ∞ assuming the block-size error ∝ 1/L (2·L4.8 − L2.4); mesh → 0 assuming the
        # mesh error ∝ h (2·h0.00625 − h0.0125); both are estimates from two points
        "final": {name: _extrap_to_zero(BLOCK_RUNS[i][1], BLOCK_RUNS[i][2]) for name, i in FINAL_ROWS.items()},
        "sph78": [(a["delta"], a["U_snk1_over_UH"], b["U_snk1_over_UH"]) for a, b in zip(
            _read_e0("fine_direct_L4.8_s7/ipc_hc0.00625_dhat0.00025_sph7_L4.8_snk1_direct.json")["rows"],
            _read_e0("fine_direct_L4.8_s8/ipc_hc0.00625_dhat0.00025_sph8_L4.8_snk1_direct.json")["rows"])],
        # far-mesh check has only d̂ 5e-4 / 2.5e-4, so compare it with the L = 2.4 run extrapolated from the same pair
        "far_mesh_diff": abs(_extrap_to_zero(BLOCK_RUNS[2][1], "U_over_UH")[1]
                             - _extrap_to_zero(BLOCK_RUNS[1][1][:2], "U_over_UH")[1]),
    }


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


def label_section(f):
    """Setting -> parameter table -> expectation -> figures and sphere table -> observations -> conclusion -> explanation."""
    arrow = lambda xs: " → ".join(f"{x:.2f}" for x in xs)
    obs = [f"球面细分 4 → 6 → 7 次（网格 0.0125、d̂ 2.5e-4、原版 IPC），压深 0.004 的比值 "
           f"{arrow([s[1] for s in f['sph']])}：细分 4 次时反常地低于 Hertz，6 → 7 只差 "
           f"{abs(f['sph'][2][1] / f['sph'][1][1] - 1):.1%}",
           f"d̂ 1e-3 → 5e-4 → 2.5e-4（最细网格、原版 IPC），压深 0.004 的比值 {arrow(f['dhat_small'])}，"
           f"压深 0.024 的比值 {arrow(f['dhat_large'])}：d̂ 每减半，多出来的部分大约减半",
           f"网格 0.025 → 0.0125 → 0.00625（d̂ 2.5e-4），压深 0.004 的比值 {arrow(f['mesh_small'])}：网格的影响比 d̂ 小得多",
           f"原版 IPC 和 al-ipc 在同网格同 d̂ 下几乎一样（压深 0.004：d̂ 1e-3 时 {f['ipc_vs_al'][0][0]:.2f} 对 "
           f"{f['ipc_vs_al'][0][1]:.2f}，d̂ 2.5e-4 时 {f['ipc_vs_al'][1][0]:.2f} 对 {f['ipc_vs_al'][1][1]:.2f}），"
           f"但最细网格上 al-ipc 慢约 {f['time_ratio']:.1f} 倍",
           f"照 supervisor 的设置（al-ipc、网格 0.025、d̂ 1e-3、球面细分 4）复现出压深 0.004 的比值 {f['ref_sph4']:.2f}"
           f"（supervisor 记录 {SUPERVISOR_RECORDED:.2f}）；只把球面换成细分 7 次，变成 {f['ref_sph7']:.2f}",
           f"把最细网格的结果按 d̂ 线性外推到 0：压深 0.004 约 {f['extrap'][0]:.2f}，压深 0.024 约 {f['extrap'][1]:.2f}",
           f"块半宽 1.2 → 2.4 → 4.8（外推到 d̂ = 0，压深 0.024）：{arrow([f['block'][i][3] for i in (0, 1, 3)])}；"
           f"远处网格加细只差 {f['far_mesh_diff']:.3f}（两者都用 d̂ 5e-4、2.5e-4 外推）；"
           f"材料参数直写后 {f['block'][3][3]:.2f} → {f['block'][4][3]:.2f}，label 再按 SNK1 算 → {f['block'][5][3]:.2f}",
           "label 按 SNK1 算、外推到 d̂ = 0 后再拆（都是两点估算）：块按 1/L 外推到无限大，压深 0.004 / 0.024 为 "
           + " / ".join(f"{2 * f['final']['L4.8'][k] - f['final']['L2.4'][k]:.3f}" for k in (0, 1))
           + "；块半宽 4.8 时网格从 0.0125 加密到 0.00625，"
           + " / ".join(f"{f['final']['L4.8'][k]:.3f} → {f['final']['L4.8_fine'][k]:.3f}" for k in (0, 1))
           + "，按误差和网格尺寸成正比外推到网格 0："
           + " / ".join(f"{2 * f['final']['L4.8_fine'][k] - f['final']['L4.8'][k]:.3f}" for k in (0, 1)),
           "最细网格上球面再从细分 7 次加到 8 次（d̂ 2.5e-4、SNK1 label）：压深 0.004 / 0.024 只变 "
           + " / ".join(f"{100 * (f['sph78'][k][2] / f['sph78'][k][1] - 1):+.1f}%" for k in (0, -1))
           + "，球面已不是误差来源",
           "细长棒单轴拉伸实测（设定 E = 1e5、ν = 0.3）：照原样传入 E = "
           f"{f['uniaxial']['as_input']['E_meas']:.0f}、ν = {f['uniaxial']['as_input']['nu_meas']:.3f}；直写属性后 "
           f"E = {f['uniaxial']['snk1_direct']['E_meas']:.0f}、ν = {f['uniaxial']['snk1_direct']['nu_meas']:.3f}"]
    figs = "".join(f'<figure><img src="assets/images/{name}" alt="{esc(cap)}"><figcaption class="small">{esc(cap)}'
                   f"</figcaption></figure>" for name, cap in E0_FIGS)
    sph_table = table(["球面细分次数", "压深 0.004 的 U ÷ U_Hertz", "压深 0.012 的 U ÷ U_Hertz"],
                      [[str(n), f"{a:.3f}", f"{b:.3f}"] for n, a, b in f["sph"]])
    parts = ['<section id="label"><h2>造训练数据（label）的收敛检查</h2>',
             f'<p class="setting">{esc(LABEL["setting"])}</p>',
             table(["改什么", "取值", "直观上是什么"], [list(r) for r in LABEL["params"]]),
             f'<p class="expect"><b>按原理期待：</b>{esc(LABEL["expect"])}</p>',
             figs, sph_table,
             table(["块的半宽 L（网格 0.0125、球面细分 7、原版 IPC）", "d̂ 档数", "外推到 d̂ = 0：压深 0.004",
                    "压深 0.024"],
                   [[lbl, str(n), f"{a:.3f}", f"{b:.3f}"] for lbl, n, a, b in f["block"]]),
             '<ul class="obs">' + "".join(f"<li>{esc(o)}</li>" for o in obs) + "</ul>",
             f'<p class="concl"><b>结论：</b>{esc(LABEL["concl"])}</p>',
             *[f'<p class="small">{esc(e)}</p>' for e in LABEL["explain"]],
             "</section>"]
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




def build_page(demos, tests, hydro, gsweeps, cfg, facts, videos, commit):
    n_video = sum(1 for r in demos + tests + [hydro] if r.get("video_web"))
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
               "官方软球 E = 1 kPa 太软，会被压塌，所以主结果用 E = 1e5。"
               "另外跑了 Genesis 自带的 hydroelastic 接触官方测试（SAP 求解器），官方断言"
               f"{OUTCOME_LABEL.get(hydro.get('outcome'), '尚无结果')}。")
    nav = "".join(f'<a href="#{h}">{esc(n)}</a>' for h, n in NAV)
    body = (f'<header class="top"><h1>{esc(PAGE_TITLE)}</h1><p class="summary">{esc(summary)}</p>'
            f'<nav class="toc">{nav}</nav></header>\n'
            + meeting_section() + "\n"
            + videos_section(demos, tests, facts) + "\n" + hydro_section(hydro) + "\n"
            + label_section(label_facts()) + "\n"
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
              for name, _cap in E0_FIGS]
    facts = compute_facts(demos)
    commit = genesis_commit()

    pages = {WEB / "index.html": build_page(demos, tests, hydro, gsweeps, cfg, facts, videos, commit)}

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
