# 日志批次入仓

本项目登记日志提取计划、封存有序原文、创建固定修订的加工任务，再整批发布结构化成果。仅依赖 Python 3.11 标准库和 SQLite；每个数据库限定单工作者，不提供并发调度。

## 运行

在项目根目录执行：

```sh
python3.11 -B -m unittest discover -s tests -v
python3.11 -B -m warehouse.demo
```

demo 在项目内创建临时目录，构造包含两个计划修订、原文批次、已发布/待执行/失败任务的完整档案，重开后完成重试并打印真实成果，退出时清理目录。测试也只在项目内的测试临时目录建库。

## 公开调用

```python
from warehouse import Warehouse, Registry

with Warehouse("archive.sqlite") as w:
    revision = w.registry.put("meters", "((甲|乙)*)=((0|1)*)", "put-1")
    batch = w.seal(["前缀甲乙甲=010尾", "unparsed", "="], "batch-1")
    job = w.create_job(
        batch["batch"], {"device": "g1", "value": "g3", "last_digit": "g4"},
        "job-1", name="meters")
    receipt = w.run(job["job"])
    result = w.read(job["job"])

# 目录也可独立打开同一数据库。
with Registry("archive.sqlite") as registry:
    saved = registry.get(revision=revision["revision"])
```

Registry.put(name, source, request_id) 返回包含 revision、name、plan 的修订对象。Registry.get(name) 读取当前修订；get(revision=id) 读取固定修订；history(name) 按修订 ID 返回全部版本。修订不可变，同名写入只移动当前指针。

Warehouse.seal(lines, request_id) 接受有限的单遍字符串迭代源，返回 batch、digest、row_count。字符串不做换行拆分或 Unicode 归一化。输入摘要为原序字符串列表的规范 ASCII JSON 的 SHA-256；重放判断比较完整请求，不依赖摘要相等。Warehouse.batch(id) 返回批次信息和原序 lines。原文以可逆 JSON 字符串保存，保留换行、零字符与 Python Unicode 字符串。

Warehouse.create_job(batch, mapping, request_id, name=...) 或 revision=... 固定批次、确切修订及字段映射；两个选择器必须恰给一个。mapping 是业务字段名到捕获 ID 的字典，例如 {"device":"g1"}；多个字段可以绑定同一组，允许空映射。捕获身份由确切修订与组 ID 共同确定，不能跨修订解释裸组 ID。job(id) 返回固定绑定、state、error。

Warehouse.run(job, checkpoint=None) 返回成功回执：job、result、state、row_count。result 的身份等于 job ID。已成功任务重复执行返回原回执；失败任务可按原绑定重试。Warehouse.read(job) 返回任务绑定、状态、错误、result 和完整 rows。rows 按输入位置排列，成功空批次为 []，未成功任务为 null。

每条成果包含 position、matched、span、captures、fields。span 与每组区间均为原输入字符串上的左闭右开 Unicode 码点区间。未匹配行仍占原位置，matched=false，span=null，每组和每字段均为 null。匹配行未参与组为 null，参与的空组为 [p,p]，相应字段是空字符串；其他字段由原文区间切片生成。成果包含全部捕获，即使只有部分组被映射。

## 捕获规则

SDK1 只接受字面字符、连接、括号捕获、分支、贪婪星号和可选项；反斜杠使紧接的一个字符成为字面量。空表达式、空分支和空组合法。未转义的 . + [ ] { } ^ $ 是不支持的运算符。没有字符类、通配符、反向引用、环视、命名组或懒惰量词。详见 FORMATS.md。

匹配选择最左起点，在该起点按分支书写顺序及贪婪回退选择结果，不使用最长匹配准则。重复组返回最后一次参与捕获；某个后续迭代未参与该组时不清除前次捕获。可空重复体严格沿用 Python 3.11 re 的终止和捕获规则；sdk1 对有限语法校验后使用该引擎。tests/test_capture_semantics.py 的手算表固定这些规则。

## 请求、错误和发布

put、seal、create_job 的 request_id 各在自己的操作命名空间内唯一。同请求同输入返回原身份；同请求不同输入抛 Conflict。字段映射的字典键顺序不影响重放，输入行顺序影响重放。任务按计划名称创建的请求重放仍返回原绑定，即使该名称当前指针已经移动。计划请求重放不会把当前指针退回旧修订。

语法错误抛 vendor.sdk1.SyntaxError；输入、映射错误抛 Invalid；未知计划、批次和任务抛 NotFound。来源异常原样传播。失败的登记、封存、任务创建均不留新业务对象或成功请求回执，修正后可复用该请求 ID。

加工采用一个发布事务，全部行、成果身份与 succeeded 状态一起提交。执行异常回滚全部成果，随后单独记录 failed 与异常类型/消息，并原样抛出异常；历史成功任务不变。未成功成果不会伪装为空列表。进程异常退出时依赖 SQLite 原子事务；这不等于提供了多工作者协调。

run 的可选 checkpoint 是真实发布流程中的观察/故障回调：每行完成后调用 ("processing.row", position)，提交前调用 ("processing.before_commit", job_id)。普通 Exception 触发上述失败记录与重试行为。回调不得重入或改写本库。

## 代码和数据

- warehouse/registry.py：计划校验、不可变修订、当前指针。
- warehouse/batches.py：有限来源封存、原序原文、精确请求重放。
- warehouse/jobs.py：固定修订、字段映射、任务回执。
- warehouse/processing.py：SDK1 提取、字段投影、整批发布、成果查询。
- warehouse/db.py：存储 Schema、请求设施和事务。
- warehouse/samples.py：build_legacy(path_or_database) 通过公开接口构造完整旧格式库，要求空库，不覆盖已有档案。
- vendor/sdk1.py、vendor/sdk2.py：离线双代基础端口，格式及不同端口能力见 FORMATS.md。

生产业务链只使用 SDK1。SDK2 的独立端口测试验证图存储、语言成员判定和单边寄存器转移。数据库 metadata.storage_version 为 1；完整旧格式数据可由 samples 构造，包含历史成果、全部修订、head、固定任务、原文与请求回执。

完整原回归共 22 项，覆盖正常链路、手算捕获、端口校验、来源异常、无效绑定、提交前失败、落盘重开重试和历史成果保全。tests/test_normal_flows.py 使用内存 SQLite；tests/test_failure_boundaries.py 使用真实临时文件库。
