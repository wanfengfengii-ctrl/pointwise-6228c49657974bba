# 日志提取依赖迁移契约

共同业务结果：升级后，同一固定任务仍向同一业务字段交付相同的完整捕获；不能以“仍然匹配”代替字段兼容。

## 已有能力与本轮交付
README.md、FORMATS.md 和全部 tests/ 是原有业务契约及原回归，保持原字节。warehouse.samples.build_legacy 可构造真实旧库，不是目标库样板；vendor/ 已提供两个离线依赖，不修改它们。新建空库继续支持旧流程，显式 upgrade 才切换。

A：把全部历史计划修订、当前指针和固定任务绑定迁入可独立执行的目标存储。
B：让批加工消费目标捕获表示，并让旧写入及原生图写入共用修订、任务和发布流程。
两项共同保全批次原文、全部 ID、组身份、字段映射、状态、错误、历史成果和请求回执。同名替换不改变旧任务；重放不重新绑定，也不移动当前指针。

## 入口与切换
新增 warehouse.migration.upgrade(path, *, checkpoint=None)，path 为 SQLite 文件路径；成功返回 None。维护窗口内单工作者，允许全量复制和全量重新匹配，不要求在线迁移、并发竞争或性能优化。原来的 Warehouse、Registry、Database 接口和错误类别继续有效。

完整迁入后 metadata.storage_version 为字符串 "2"。目标 Schema 及可执行计划的内部表示由交付者设计；可保留解释性源码供旧 get 返回，但不得靠它执行。原来的 revisions、heads、batches、raw_lines、jobs、results、result_rows、requests 留作只读审计；切换后业务读写使用目标表，不能读取这些旧表来解释任务。既有 Warehouse/Registry 对象在下一次公开调用时也使用目标表；关闭重开相同。无需协调跨进程的旧写入者。

升级必须将目标数据和版本标记同次事务提交。异常原样传播，提交前失败不留下目标半成品或改变旧库；重开重试能够完成；重复升级不改变业务状态。checkpoint 为可选观察/故障回调：每个修订目标转换已写入但未提交时调用 ("migration.converted", revision_id)，所有目标及版本标记写完、提交前调用 ("migration.before_commit", None)；回调抛 Exception 即回滚。已是版本 2 时直接返回，不触发回调。回调不重入或改写数据库。

切换后，每条记录的实际状态转移和寄存器写入经未修改的 sdk2.Graph.transitions，接受标志经 is_accepting 解释。可以保存必要的目标控制元数据，也可以在转换期改写图。不可调用 SDK1、re 或其他正则匹配器执行记录，不可把旧语法树/旧解释器作为执行旁路。转换期可解析旧语法；SDK1 只供旧流程、兼容对照及异常类型引用。不能只换版本号、只存源码或在读取时重新转换历史计划。

## 旧接口兼容
旧语法、Unicode 码点坐标和所有原错误见 FORMATS.md 与 README.md。最左起点、分支顺序、贪婪回退、重复组最后参与捕获、未参与 null、参与空串以及可空重复的 Python 3.11 规则全部保留。全部捕获均输出，字段从原文区间切片；不匹配行仍保留原位置。

Registry.get/history 对旧修订仍返回原来的公开 plan 对象；这是描述性兼容视图，不是执行载荷。旧 Registry.put 仍接收 source 并产生可由 SDK2 消费的新修订。同一修订内组 ID 不因图节点、寄存器或目录排列改变含义。保留 seal/batch/create_job/job/run/read、请求命名空间、输入冲突、成功幂等和整批发布边界；升级不重新发布历史成功成果。

## 原生图写入
新增 Registry.put_graph(name, graph, groups, request_id)，仅版本 2 可用，版本 1 抛 Invalid。graph 是 FORMATS.md 的完整 symbol-graph/2 对象；groups 是有序列表，每项恰为 {"id": 非空字符串, "start": 寄存器ID, "end": 寄存器ID}。组 ID 唯一，两个寄存器均须存在。节点列表顺序没有优先意义，组身份也不由寄存器排列推导。图结构错误抛 sdk2.GraphError，组目录错误或 epsilon 子图含环抛 Invalid，不留下修订、head 或请求回执。

原生输入限定 epsilon 子图无环；消费符号的环允许。每个候选起点按边列表顺序深度优先回退，节点的接受标志在全部出边之后尝试，取第一个成功结果；按码点递增尝试起点。路径各自持有寄存器，回退不能泄漏失败路径写入。所有寄存器初始为 null。成功路径中一组的两端均 null 表示未参与；两端均为整数且 start<=end 时表示区间；其他配对抛 Invalid，整批失败，不跳到另一条匹配路径掩盖错误。

返回修订对象仍为 revision、name、plan；原生 plan 为 {"format":"native-capture/2","graph": graph,"groups": groups} 的隔离副本。put_graph 与 put 共用计划请求命名空间；其请求载荷为 {"name":name,"graph":graph,"groups":groups}，同请求同载荷返回原修订，不回退 head，同请求不同载荷抛 Conflict。原生修订可用于相同的 create_job/run/read，mapping 引用 groups 中的 id。

migration_samples.py 提供两份完整原生图和手算预期：共前缀分支对 aa 取 a；带消费环的图对 噪甲甲尾 返回 [1,3]。它们不是迁移器或兼容执行器。

## 核验和交付
运行原完整回归：python3.11 -B -m unittest discover -s tests -v。
联合验证入口：python3.11 -B migration_validation.py verify。
真实业务演示入口：python3.11 -B migration_validation.py demo。
联合验证先执行全部原回归，再实际构造文件旧库、升级、在旧对象和重开对象上加工固定任务、替换同名计划、原生续写、阻断旧表与 SDK1 旁路，并注入转换中及提交前失败后重试。有限差分语料是回归样本，不缩小公开语法范围。

保留全部原断言、依赖和样本；可添加验证但不改写既有测试。交付迁移与消费适配、简短的表示/捕获回退不变量说明和独立兼容对照。将既有 .pointwise-runtime.json 的 verify/demo 接到上述联合验证及真实迁移演示；保留 version=1、kind=cli、setup 为 argv 数组列表、verify/demo 为 argv 数组的运行格式。验证结果须区分已运行、失败和受环境限制未验证，不把源环境限制当业务偏差。
