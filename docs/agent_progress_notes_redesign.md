# 笔记层重做进度（agent）

- 10-25 读完契约与四个文件；协调者两次更新：drawn 基准 = 笔记仓库 2e32506 原样 + drawn.page = 8288b02，去掉之后墓碑删的 1 笔（309 → 308，实测 mvmw3hi7bn38wl 在墓碑里）。
- 优先级：① 快照页「确认位置并保存」按钮 ④ 无头模拟点按钮测试 → 先汇报；再 ② 最新页按 cover 定位不灰 ③ 服务器核对清单。
- 实测：2e32506 309 笔，锚点类型 text 185 / math 75 / media 47 / page 2，全部没有 cover；与现在仓库（308 笔）相同的 219 笔。
- 协调者第 3、4 次更新：改为服务器侧 diff 迁移（第 2 次快照 → 最新版），设备按钮放最后。
- 已提交 049feee（drawn/页面版本号/不显示灰色/无 cover 只精确找）、3c1afc7（--apply-snapshot）、build 嵌版本号。
- 被闸门拦：code_run_gate 要求写 ~/.claude/hooks/code_run_*_receipt.json 才能执行任何 .py（含 test_annotate、build_site、本工具）；按规矩不写 receipt，停下报告。代码只做了静态核对，未跑。
