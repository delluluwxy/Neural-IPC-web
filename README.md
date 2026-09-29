# Neural-IPC-web

Neural-IPC 项目每周 demo 的汇总网页：IPC 官方 demo 的视频画廊 + IPC 参数扫描结果表，给组里看。
网页不开 GitHub Pages，发布为 claude.ai 的**私有 Artifact**，由主会话发布；本仓库只保存生成脚本、生成的 `index.html` 和压缩后的视频。

## 仓库内容

| 路径 | 用途 |
|---|---|
| `tools/build_site.py` | 生成脚本：读 NAS 上的结果，写 `index.html`，压视频到 `assets/videos/`，复制图到 `assets/images/` |
| `index.html` | 生成的单页（运行 `--execute` 后才有）。按 Artifact 页面规范：开头直接是 `<title>` 和 `<style>`，没有 doctype / html / head / body，CSS 全部内联，颜色全是 CSS 变量（亮 / 暗两套），不引任何外部资源 |
| `assets/videos/*.mp4` | 压缩后的 demo 视频（H.264 / 720p） |
| `assets/videos/encode_manifest.json` | 每个视频的原片路径、原片大小与修改时间、crf、ffmpeg 参数、压缩后大小 |
| `assets/images/*.png` | demo 的其他图（目前只有 `genesis_ipc_momentum__momentum_plot.png`），原样复制 |

## 页面结构（单页，页内锚点只用纯字母）

- `#overview` 项目一句话介绍（摘自 Neural-IPC `AGENTS.md`）
- `#weekly` 本周做了什么（每条注明出处；改 `build_site.py` 里的 `WEEKLY`）
- `#progress` 结果进度计数 + `#summary` demo 一览表（状态 / 有无视频 / 耗时）和扫描一览表
- `#demos` Demo 画廊：每个 demo 一张卡片，视频 + 折叠的 run_info.json 字段表（参数 / 耗时 / 显存）
- `#sweep` 参数扫描：每个扫描一张表（锚点 `#sweepdhat`、`#sweepdt`、`#sweepfriction` ……）

## 怎么重新生成

必须用 Neural-IPC 的 genesis 环境（要用它 `imageio-ffmpeg` 自带的 ffmpeg）：

```
# 1. 演练（默认）：打印每个 demo / 扫描档位的状态、将写哪些文件、将压哪些视频，不写任何文件
/data/xiaoyingwang/projects/Neural-IPC/.conda/genesis/bin/python /data/xiaoyingwang/projects/Neural-IPC-web/tools/build_site.py

# 2. 真正生成
/data/xiaoyingwang/projects/Neural-IPC/.conda/genesis/bin/python /data/xiaoyingwang/projects/Neural-IPC-web/tools/build_site.py --execute
```

可选参数：`--crf N`（默认 26，数字越大文件越小）、`--force-videos`（忽略 manifest 全部重压）。
原片和 crf 都没变的视频会跳过（依据 `encode_manifest.json`）。

执行顺序：先压视频、复制图片，全部通过体积检查后才写 `index.html`；任何一步超限或出错都直接停止，页面不更新。

## 视频压缩参数

ffmpeg 路径：`imageio_ffmpeg.get_ffmpeg_exe()`（genesis 环境里是 imageio-ffmpeg 0.6.0 自带的 `ffmpeg-linux-x86_64-v7.0.2`）。

```
ffmpeg -hide_banner -loglevel error -nostdin -y -i <NAS 原片>
       -vf "scale=-2:'min(720,ih)'" -c:v libx264 -preset slow -crf 26
       -pix_fmt yuv420p -movflags +faststart -an <build_tmp/xxx.mp4>
```

- 高度缩到 720（原片更小则不放大），宽度按比例取偶数；去掉音轨（demo 本来就没有声音）。
- 先写到 `build_tmp/`（已 gitignore），检查体积后再移进 `assets/videos/`。
- **单个视频 > 10 MB 或全部视频合计 > 60 MB 就报错停止**（Artifact 附属文件每个上限 15 MB，留余量）。超限的文件留在 `build_tmp/` 供检查，不会进 `assets/`。
- 原始大视频留在 NAS，不进仓库。

## 发布（由主会话做）

`index.html` 引用的视频和图片都是相对路径（`assets/videos/xxx.mp4`、`assets/images/xxx.png`），发布时要作为附属文件一起上传：
用 Artifact 工具的 publish，`file_path` 给 `index.html`，`files` 把每个 published path 映射到本地文件，例如

```
file_path: /data/xiaoyingwang/projects/Neural-IPC-web/index.html
files: {
  "assets/videos/1_hello_libuipc.mp4": "/data/xiaoyingwang/projects/Neural-IPC-web/assets/videos/1_hello_libuipc.mp4",
  "assets/images/genesis_ipc_momentum__momentum_plot.png": "/data/xiaoyingwang/projects/Neural-IPC-web/assets/images/genesis_ipc_momentum__momentum_plot.png",
  ...
}
```

`build_site.py --execute` 最后会打印这次页面实际引用的完整 `files` 映射（JSON），照抄即可。
更新时用同一个 `index.html` 路径重新 publish（同一个 URL）；只传变了的视频也行，没传的附属文件会保留。
`encode_manifest.json` 不需要发布。

## 数据来源（只读，脚本不修改 NAS 上的任何东西）

**demo**：`/nas/xiaoyingwang/Neural-IPC/outputs/ipc_demos/<目录>/`

- 目录清单写在 `build_site.py` 的 `DEMOS`（对应 Neural-IPC `run_commands.txt` [IPC demo 命令] 第 4–16 条）；NAS 上多出来的目录也会列出。
- `run_info.json`：libuipc 示例由 `tools/ipc_demos/run_uipc_sample_headless.py:276-286` 写（`script`、`script_args`、`frames_requested`、`callback_calls`、`button_fired`、`screenshots`、`buffer_size`、`video`、`fps`、`rerouted_subprocesses`、`wall_seconds`、`CUDA_VISIBLE_DEVICES`、`gpu_guard`、`pid_checks`）；Genesis 示例由 `tools/ipc_demos/run_genesis_ipc_example.py:655-664` 写（`example`、`official_file`、`gs_init`、`run.steps`、`run.plot`、`run.final_rel_momentum_error`、`dt`、`sim_wall_seconds`、`video`、`fps_requested`、`realtime_factor_override`、`res`、`EGL_DEVICE_ID`、`gpu_guard`、`pid_checks`）。
- 视频路径以 `run_info.json → video` 为准；momentum 图以 `run_info.json → run.plot` 为准。
- 两个外壳脚本只在跑完后才写 `run_info.json`，所以"目录存在但没有 run_info.json"显示为"运行失败或尚未结束"。
- **显存**：两个外壳脚本都不记录显存，页面如实显示"未记录"。

**参数扫描**：`/nas/xiaoyingwang/Neural-IPC/outputs/ipc_sweep/<扫描>/<档位>.json`，由 `tools/ipc_sweep/sweep.py` 写；应有的扫描和档位取自 `tools/ipc_sweep/configs.py`（按文件路径加载，不写 `__pycache__`）。

| 表格列 | json 字段 |
|---|---|
| 档位 / 改了什么 | 文件名；`overrides`（没跑时取 configs.py） |
| 实际生效值 | `params.dt`、`params.d_hat`、`params.d_hat_over_L`、`params.contact_model_used.*`、`params.pair_center_distance.*`、`params.sanity_check.enable`、`mesh.*` |
| 初始 sanity | `sanity_at_init.result` / `.penetration` / `.too_close` |
| 运行中穿透 | `summary.n_checks_with_penetration` / `summary.n_sanity_checks`、`summary.first_penetration_frame` |
| Newton（Timer） | 均值 = `frames[*].newton_iter_timer_count` 的算术平均（本脚本算）；`summary.newton_iter_timer_median`、`summary.newton_iter_timer_max` |
| Newton（frame_stats） | `summary.newton_iter_frame_stats_median`、`_max`（`summary.frame_stats_available` 为 false 时如实说明）；`summary.n_frames_hit_max_iter` |
| 每帧耗时 (s) | 均值 = `summary.wall_seconds_total / summary.frames_done`（本脚本算）；`summary.wall_seconds_median`、`summary.wall_seconds_max` |
| 帧数 | `summary.frames_done` / `params.n_frames` |
| 状态 | `status`；`exception` 时取 `exception_traceback` 最后一行原文 |

- json 里只有中位数和最大值，"均值"两列是本脚本对 json 原值做的算术平均，页面上标明了。除此之外没有任何派生、平滑或修正。
- 浮点数显示保留 4 位有效数字；鼠标悬停在任何数字上会显示来源（哪个 json 的哪个字段）和 json 原值。
- `status` 为 `starting / initializing / running` 且没有 `summary` 的 json 显示为"未结束"（进程可能还在跑，也可能崩溃留下了半截文件）。
