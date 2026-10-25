# cleanup 1025 待删清单 agent 进度（只列不删）

- 05:55 已盘点 web_annotate_test/（5 个 site_* worktree + restore_wt + 散文件）与工作区未跟踪 check/、snapshot/；核过 worktree list、site_* 只有 CIFS 权限位差异（core.fileMode=false 后干净）、各 sha 都在 origin/main 上；snapshot/1、2a 与 restore_wt/snapshot/1、2 逐字节相同，2b 不同（未上线）。下一步：notes_*.json 与笔记仓库历史比对。
- 06:05 notes 比对完（115 个历史版本）：6 份可从仓库取回，6 份唯一（保留）。运行生成器 .sh 被 code_run_gate 拦（要求在 ~/.claude/hooks/ 写 receipt，按规矩不碰 ~/.claude），改用任务指定的 find -printf 直接生成。清单 990 行 / 87,336,132 B + 5 个 worktree 91,776,922 B 写入 NAS，README 已写。完成。
