"""路径、体积上限、站点标题与周清单：全站共用的常量，不含任何逻辑。"""

import os
from pathlib import Path

# ---------------- 路径 ----------------
WEB = Path(__file__).resolve().parents[2]                    # 本仓库根目录（Neural-IPC-web）
PROJECT = Path("/data/xiaoyingwang/projects/Neural-IPC")    # 讲稿、扫描配置所在的仓库（指向 Neural-IPC-sandbox 的软链接）
OUT_ROOT = Path("/nas/xiaoyingwang/Neural-IPC/outputs")     # NAS 上的仿真结果根目录（只读）
DEMO_ROOT = OUT_ROOT / "ipc_demos"
SWEEP_ROOT = OUT_ROOT / "ipc_sweep"
SWEEP_CONFIGS = PROJECT / "tools" / "ipc_sweep" / "configs.py"
DRAFTS = Path(os.environ.get("NIPC_DRAFTS_DIR", PROJECT / "docs" / "drafts"))   # 每周讲稿 web_<周>.md；可指向导出的已提交版本，避免带上编辑中的半成品
# week3 讲稿配图版目录（图在 figs/<组>/、视频在 videos/<组>/）；讲稿写 [[配图 <组>/<文件>]] 直接取这里的当时版本，
# 图注取 sandbox tools/figs/week3_md/assemble_md.py 读的三份图注清单（draft_md.illustrated）
ILLUSTRATED = OUT_ROOT / "week3_illustrated"

VIDEO_DIR = WEB / "assets" / "videos"
IMAGE_DIR = WEB / "assets" / "images"
BUILD_TMP = WEB / "build_tmp"          # 压缩中间文件；已在 .gitignore
VIDEO_MANIFEST = VIDEO_DIR / "encode_manifest.json"

# ---------------- 体积上限 ----------------
MAX_VIDEO_BYTES = 10 * 1024 * 1024        # 单个视频上限（Artifact 附属文件每个 ≤ 15 MB，留余量）
MAX_TOTAL_VIDEO_BYTES = 60 * 1024 * 1024  # 全部视频加起来的上限
MAX_IMAGE_BYTES = 5 * 1024 * 1024         # 单张图上限

# ---------------- 站点 ----------------
PAGE_TITLE = "Neural-IPC 周汇报"
# 每周一块，新的一周在前；周的分界 = weekly todo/weekN.txt 文件头的日期。锚点只用字母和数字。
# (页面名, 标题, [(节锚点, 节名)])；节名用于首页卡片和该周页面顶部的目录。
# 列表为空的周（week3_4、week1）整页从讲稿生成，目录自动取讲稿的节标题（pages.build_pages）。
# week3 没开会，第 3、4 周合成一页 week3_4（用户 10-22）；旧 week3.html 不再生成、不再链接。
WEEKS = [("week5", "Week 5–6（第 5–6 周）", []),
         ("week3_4", "Week 3–4（第 3–4 周）", []),
         ("week2", "Week 2（TODO 2026-10-04）",
          [("w1", "文献：Hydroelastic"), ("w2", "文献：神经网络接触模型"), ("w3", "看代码"), ("w4", "Demo"),
           ("w5", "实验方案"), ("w6", "问题与待决定")]),
         ("week1", "Week 1（TODO 2026-09-26）", [])]
