# 笔记层重做进度（agent）

- 10-25 读完契约与四个文件；协调者两次更新：drawn 基准 = 笔记仓库 2e32506 原样 + drawn.page = 8288b02，去掉之后墓碑删的 1 笔（309 → 308，实测 mvmw3hi7bn38wl 在墓碑里）。
- 优先级：① 快照页「确认位置并保存」按钮 ④ 无头模拟点按钮测试 → 先汇报；再 ② 最新页按 cover 定位不灰 ③ 服务器核对清单。
- 实测：2e32506 309 笔，锚点类型 text 185 / math 75 / media 47 / page 2，全部没有 cover；与现在仓库（308 笔）相同的 219 笔。
- 协调者第 3、4 次更新：改为服务器侧 diff 迁移（第 2 次快照 → 最新版），设备按钮放最后。
- 已提交 049feee（drawn/页面版本号/不显示灰色/无 cover 只精确找）、3c1afc7（--apply-snapshot）、build 嵌版本号。
- 被闸门拦：code_run_gate 要求写 ~/.claude/hooks/code_run_*_receipt.json 才能执行任何 .py（含 test_annotate、build_site、本工具）；按规矩不写 receipt，停下报告。代码只做了静态核对，未跑。
- 票据已签。--apply-snapshot（equal 段 ≥ 8 字 / 前后各 4 字；公式符号段 ≥ 3；挪位公式按唯一同 TeX 对上）：308 笔，迁过去 250（text 135 / math 69 / media 45 / page 1），隐藏 58（text 50 / math 6 / media 2）；最新版核对不符 0，可见灰色 0。
- --check-drawn：308 笔 drawn 与 2e32506 逐字节不同 0；drawn.page 全是 1fd94db1f374（= 8288b02 页面哈希）；墓碑与 c13089b 相同（39 条）。
- 下一步：重建页面 → 在重建页上重跑 --apply-snapshot → test_annotate。
- 重建页面（0d245e8，只有标注层与版本号标签变化，三页 diff 行数相同）后重跑 --apply-snapshot：数字不变（迁过去 250 / 隐藏 58，核对不符 0，可见灰色 0）；--check-drawn 逐字节不同 0。
- test_annotate：60 项，失败 0（新增 a7、f2、h3 灰色、h5、m1、k1、k2）。
- 未做：快照页「确认位置并保存」按钮（等协调者再说）；--snapshots 改为从 drawn 生成。
- 按协调者改：正文按「这笔实际压住的字」（iPad 上锚字宽 orig.w × 笔画横向范围）判，全在同一 equal 段且段 ≥ 8 才迁；跨行 4 笔退回段边规则（全部迁过去）。图上标注加 letter_maps 字母换名。
- 重跑：迁过去 256（text 140 / math 69 / media 46 / page 1），隐藏 52（text 45 / math 6 / media 1）；核对不符 0，可见灰色 0；比上次 hidden→moved 6 笔，反向 0；drawn 逐字节不同 0。
- 第一次重跑 test_annotate 与迁移同时跑，Page.goto 30 s 超时（并发负载），单独重跑中。
- test_annotate 单独重跑：60 项，失败 0。
