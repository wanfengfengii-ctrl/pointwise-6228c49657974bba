# 离线依赖端口与存储格式

## SDK1：ordered-capture/1

compile_plan(source) 返回 JSON 对象：

```json
{"format":"ordered-capture/1","source":"(a|b)*","groups":[{"id":"g1","number":1}]}
```

组按左括号出现顺序编号，g1、g2 等为本修订内的稳定 ID。load(plan) 重新校验源码和组目录完全一致；search(plan, text) 返回 null 或 {"span":[start,end],"captures":{"g1":null或[start,end]}}。每次执行验证持久计划。没有隐含的组 0，整段匹配另存 span。

语法为 expression := sequence ("|" sequence)*；sequence := atom*；atom 是字面字符或 "(" expression ")"，后面可有一个 "*" 或 "?"。反斜杠把紧接一个字符当字面量，例如反斜杠加 1 匹配字符 1，并非反向引用。其他受限运算符及错误见 README。Unicode 不归一化。

Python 3.11 的有序回退语义决定可空循环：允许终结前的空参与捕获，但不能无限重复同一个无进展循环。以下手算样例固定实际端口行为，区间单位为码点：

| 源码 | 文本 | 整段区间 | 各组区间 |
| --- | --- | --- | --- |
| (a\|aa)(a?) | aa | [0,2] | [0,1]；[1,2] |
| (a*)a | aaa | [0,3] | [0,2] |
| (a\|b)* | aba | [0,3] | [2,3] |
| (a(b)?)* | aba | [0,3] | [2,3]；[1,2] |
| (a)?(b*) | 空串 | [0,0] | null；[0,0] |
| (a?)* | aa | [0,2] | [2,2] |
| (()?)* | 空串 | [0,0] | [0,0]；[0,0] |

源码中的实际分支字符是竖线；表内转义用于 Markdown。tests/test_capture_semantics.py 还包含分支公共前缀、可空嵌套、贪婪回退、Unicode 和有限小语料终止检查。

## SDK2：symbol-graph/2

Graph(payload) 校验并隔离复制 JSON 图；dump() 返回不共享可变容器的序列化对象。顶层恰含 format、entry、registers、nodes。节点 ID 与寄存器 ID 是不同的字符串命名空间；节点列表顺序不赋予编号意义。节点须恰含 id、accept、edges；边须恰含 to、symbol、writes。symbol=null 是 epsilon 边，其他符号必须恰为一个 Unicode 码点。

```json
{
  "format":"symbol-graph/2",
  "entry":"entry",
  "registers":["left","right"],
  "nodes":[
    {"id":"entry","accept":false,"edges":[
      {"to":"finish","symbol":"甲","writes":[["left","before"],["right","after"]]}
    ]},
    {"id":"finish","accept":true,"edges":[]}
  ]
}
```

is_accepting(state) 查询接受标志。transitions(state,text,position,registers=None) 枚举当前可走的一条边，输出 (edge_index, next_state, next_position, new_registers)。输入状态必须存在，position 是 0 到 len(text) 的整数；传入寄存器时键集合必须完整，值为 null 或有效码点边界。symbol 边消费一个码点，epsilon 边不消费。writes 按列表顺序执行：before 写入边前位置，after 写入边后位置，clear 写 null。返回寄存器与输入容器隔离。同一目标的多条边仍分别枚举。

边的列表顺序与边索引在存储往返中保留；transitions 仅枚举单步，不裁定接受路径优先级或合并捕获历史。accepts(text) 仅判断整个字符串的语言成员性，使用无序状态集合与 epsilon 闭包，忽略寄存器写入。它不返回子串、捕获、路径顺序或业务字段。epsilon 环在成员判定中可终止。寄存器本身没有捕获组或字段含义。

tests/test_sdk2.py 独立验证本格式、单步状态转移、寄存器复制/清除、epsilon 自环、校验错误和存储往返。

## 正常仓库存储

warehouse/db.py 的 SCHEMA 是可执行旧库定义，metadata 的 storage_version 为字符串 1。revisions.plan 保存完整 SDK1 JSON，heads 指向某个修订 ID。batches 保存有序输入的摘要及数量；raw_lines 以 (batch,position) 唯一，text 存可逆 JSON 字符串。jobs 保存固定 batch、revision 与 field-to-group JSON，state 为 pending、failed 或 succeeded。results 记录唯一 job 成果和行数；result_rows 以 (job,position) 保存完整成果 JSON。requests 以 (scope,request) 唯一，payload 为完整规范请求 JSON，result 为对应业务 ID。所有数值 ID 均由 SQLite 产生。

encode 使用 ensure_ascii=true、sort_keys=true、紧凑分隔符与 allow_nan=false。成功请求、对象与其所有关联行同次事务写入。失败执行仅记录任务错误，无部分 result。批次和修订没有覆盖/删除接口。原文和历史成果不随计划 head 变化。

构造完整文件库的命令（目标应为不存在的文件或空库）：

```sh
python3.11 -B -c 'from warehouse.samples import build_legacy; print(build_legacy("old.sqlite"))'
```

build_legacy 也接受共享 Database 对象，便于内存内构造。固定样例同时保存重复捕获、未匹配行、空捕获、Unicode、两个同名修订、已发布成果、待执行和失败任务；demo 使用该构造器。
