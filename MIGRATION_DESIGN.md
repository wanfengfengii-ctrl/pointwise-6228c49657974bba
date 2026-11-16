# 迁移适配说明：有序捕获 → 显式符号图

本文说明版本 1 到版本 2 的存储与执行表示、保留的捕获回退不变量，以及兼容对照的
运行方式。业务契约仍以 README.md、FORMATS.md、MIGRATION.md 为准；这里只描述交付
实现。

## 1. 存储切换

- `warehouse/migration.py` 的 `upgrade(path, *, checkpoint=None)` 是唯一入口。
- 目标表为 `target_revisions`、`target_heads`、`target_batches`、
  `target_raw_lines`、`target_jobs`、`target_results`、`target_result_rows`、
  `target_requests`，形状与旧表一一对应，ID 由旧表原值显式搬入，不做重编号。
- 每个修订在 `target_revisions.plan` 保存公开描述（旧修订仍是
  `ordered-capture/1` 对象），在 `target_revisions.execution` 保存可执行载荷：
  `{"format":"graph-capture/2","graph":<symbol-graph/2>,"groups":[...]}`。
  执行载荷不含源码、旧语法树或解释器入口。
- 一次 `BEGIN IMMEDIATE` 事务内完成全部目标行写入与
  `metadata.storage_version='2'` 标记；`migration.converted` 在该修订行已写入、
  事务未提交时回调，`migration.before_commit` 在全部行与版本标记写完后回调。
  回调抛异常即整体回滚，旧库原样可用。已是版本 2 时直接返回，不写库、不回调。
- 旧表（`revisions`、`heads`、`batches`、`raw_lines`、`jobs`、`results`、
  `result_rows`、`requests`）保留为只读审计。版本 2 的业务代码只读写 `target_*`
  与 `metadata`，迁移后即使篡改旧表也不影响读取（见
  `tests/test_migration_delivery.py`）。

## 2. 版本 2 的读写路径

`Database.is_v2()` 在每次公开调用时读取 `metadata.storage_version`，因此升级前
已经打开的 `Warehouse`/`Registry` 对象在下一次调用起自动使用目标表；关闭重开后
行为相同。`put`、`put_graph`、`seal`、`create_job`、`run`、`read`、`get`、
`history` 全部按版本选择表名，公共返回结构与错误类型不变。

`Registry.put` 在版本 2 下仍然接收旧语法源码：注册时解析一次，同时生成公开的
`ordered-capture/1` 描述和内部图载荷。之后执行只依赖图载荷。`Registry.put_graph`
只在版本 2 可用（版本 1 抛 `Invalid`），校验顺序为：`sdk2.Graph` 结构
（`GraphError`）→ 组目录（`Invalid`）→ epsilon 子图无环（`Invalid`）。全部校验
和重放检查都在写事务内完成，失败不留下修订、head 或请求回执。`put` 与
`put_graph` 共用 `plan` 请求命名空间，载荷分别为
`{"name","source"}` 与 `{"name","graph","groups"}`。

## 3. 组身份与寄存器

每个捕获组 `gN` 独占两个寄存器 `gN.start`、`gN.end`：

- 组入口 epsilon 边写 `gN.start = before`（进入组前的位置）；
- 组出口 epsilon 边写 `gN.end = after`（离开组后的位置）。

因此一对寄存器只有三种状态：两端均 null（未参与，映射为 `null` 捕获与 `null`
字段）、两端均为整数且 `start<=end`（区间，含 `[p,p]` 空串参与），其他配对是
非法成果（原生图下整批抛 `Invalid`）。组身份来自 `groups` 目录中的 ID，与节点、
寄存器、节点列表顺序无关；迁移后的组 ID 与旧修订的 `g1…gN` 完全一致，因此字段
映射无需重写，固定任务原绑定照旧。

## 4. 翻译不变量

`warehouse/translation.py` 把旧语法树逐节点转为图：

| 旧构造 | 图构造 | 保留的语义 |
| --- | --- | --- |
| 字面字符 | 消费该码点符号的单边 | 码点偏移，不归一化 |
| 连接 `AB` | A 的出口接 B 入口 | 原序 |
| 分支 `A\|B` | 按书写顺序排列的 epsilon 出边 | 分支优先，深度优先回退 |
| 分组 `(A)` | 入口写 start、出口写 end | 最后一次参与值保留（写入覆盖） |
| `A?` | body 边在前、跳过边在后 | 贪婪优先、可跳过 |
| `A*`（body 必消费） | body 回到 head，head 先走 body 再走出口 | 贪婪回退 |
| `A*`（body 可空） | 两层 body 副本：不消费层只能走一次，消费层回到 head | 见下 |

可空重复是唯一需要展开的构造。Python 3.11 的重复允许一次“无进展”的空参与写入
捕获（例如 `(a?)*` 对 `aa` 的捕获为 `[2,2]`），但绝不无限重复。为避免 epsilon
环（原生图写入明确禁止），可空 body 生成两份副本：`零` 层把消费边指向 `一` 层，
自身只能经由 epsilon 走到出口；`一` 层消费后回到 head。head 先尝试零层再尝试
出口，于是：退出前保留最后一次空参与捕获、消费迭代正常循环、层数有限。
`_minimum` 判定 body 是否必然消费，避免不必要的展开。

节点可达但无接受意义的最终节点 `accept=True`；所有边按构造顺序写入
`nodes[].edges`，`sdk2.Graph.transitions` 的枚举顺序即分支/贪婪优先顺序。

## 5. 执行算法（仅 SDK2）

`translation.search` 只调用 `sdk2.Graph.transitions` 与 `is_accepting`：

1. 起点从 0 到 `len(text)` 递增，第一个有成果的起点即最左起点；
2. 每个状态按边索引顺序深度优先展开，`writes` 由 SDK2 在 `before/after/clear`
   上执行，返回的寄存器容器与输入隔离，失败路径不泄漏；
3. 节点的接受标志只在全部出边失败后尝试，对应 Python 的“先吞更多再回退”；
4. 匹配范围是 `[起点, 接受位置]`，捕获直接从寄存器读出。

它不调用 `sdk1`、`re` 或任何正则匹配器，也不读取执行载荷以外的源码。

## 6. 语义对照与核验

- 原回归：`python3.11 -B -m unittest discover -s tests -v`（原 22 项保持原字节，
  另有新增交付测试）。
- 联合验证：`python3.11 -B migration_validation.py verify`（先跑全部原回归，再跑
  真实升级、固定任务、同名替换、原生续写、只读旧表/禁用 SDK1 的旁路阻断和失败
  重试）。
- 真实演示：`python3.11 -B migration_validation.py demo`。
- 独立兼容对照：`python3.11 -B compatibility_check.py`。它单独构造旧库、真实
  升级并逐条对比：固定任务升级前后成果、旧语法经图执行的有限差分（左最起点、
  分支优先、贪婪回退、重复组、可空循环、Unicode）、原生样例手算行、`null` 与
  空串差异。非 Python 3.11 解释器下，差分部分标记为“受环境限制未验证”，不当作
  业务偏差；其余结构性与固定任务检查照常执行。

报告字段：`passed` 为失败数为零；`sections` 分节统计 `executed/failed/
unverified`；`failed` 列出具体不符；`environment_limited` 列出未验证项及原因。
"已运行"仅指实际执行并通过，"失败"与"受环境限制"分别单列。
