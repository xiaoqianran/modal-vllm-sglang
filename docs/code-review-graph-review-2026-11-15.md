# 图谱追溯与异常恢复修复（2026-11-15）

## 范围与方法

审查当前 main 工作区，保留已有未提交改动。使用本机安装的
`code-review-graph`，通过其 Python 工具入口和 CLI 查询
`.code-review-graph/graph.db`；未使用另一产品的 `.codegraph/` 数据。
调用了 `get_minimal_context_tool`、调用者/被调用者查询、`get_flow_tool`
和 `get_impact_radius_tool`。发现索引落后于源码后执行增量更新。

重点检查新增云端编排、SDK 调度和 telemetry 回收链路。图谱用于确定
影响范围，缺陷结论来自源码检查和故障注入测试；本次不是全项目无缺陷证明。
图谱中的动态调用有未解析边，未把按名称匹配的边视作运行时证据。

## 追溯链路

结合图谱与源码确认的主链路：

```text
cloud_client.worker → tick_shot → Backend.submit_workflow
  → Modal run_shot → cloud.run → sdk.tick
  → Backend.submit / Backend.poll → 云端结果
  → tick_shot → sdk.collect_outputs → Backend.download
```

`run_shot` 的图谱执行流包含 35 个节点、15 个文件、深度 5。
两处生产文件的影响查询覆盖 38 个节点和 12 个额外文件（包含未解析调用）。

运行记录回收链路：

```text
Writer 发布清单 → 云端/本地任务状态保存清单引用
  → Collector._run 或显式 collect → collect_cloud
  → download_volume_file → ingest → SQLite 事件索引
```

`collect_cloud` 未生成独立的命名 flow；其入口通过 callers_of 和源码确认。

## 已修复问题

### P2：云端 checkpoint 失败绕过资源清理

位置：`orchestration/cloud.py::run`。
首次 checkpoint 原本在 try/finally 外，失败后留下 writer 线程和
checkpoint 回调；退出阶段 checkpoint 失败也会跳过 writer.close。

修复：初始状态验证及写入完成后才创建 recorder；首次 checkpoint 纳入
try/finally；在最外层 finally 清除回调并关闭 writer。
持久化失败仍向调用方报错，没有增加任何模型提交重试。

回归测试：`CloudTests.test_checkpoint_failure_closes_writer_and_releases_callback`。
分别注入首次 commit 失败、调度异常后最终 commit 失败，验证 writer 已关闭、
线程退出、回调清除且未提交模型任务。两个子场景修复前均失败。

### P2：损坏的本地 telemetry 缓存无法恢复

位置：`runtime/telemetry/collector.py::collect_cloud`。
旧实现仅在缓存不存在时下载。缓存截断或内容损坏后，每次收集只会再次
报告校验失败，后台重试和显式 collect 都无法恢复。

修复：复用缓存前检查字节数和 SHA-256；不匹配时从云端重新下载。
继续使用已有的大小限制、摘要验证及原子替换，不直接写入最终文件。

回归测试：`TelemetryTests.test_cloud_collection_repairs_corrupt_local_cache`。
先正常收集，再分别制造截断和同长度内容损坏，验证重新下载、恢复正确
摘要，并保持事件去重（新增事件数为 0）。两个子场景修复前均失败。

## 验证

- 修改前：179 项离线测试通过。
- 故障注入：2 项新增测试、4 个子场景在修复前失败。
- 修复后：云端编排及 telemetry 24 项测试通过。
- 全量：`python -m unittest discover -s tests -q`，181 项测试通过。
- `git diff --check` 通过（Git 提示已有部分文件将转换换行符）。

未部署 Modal、未运行真实 GPU 推理；云端 Volume 和后台故障通过本地替身
模拟，因此本结果不代表真实云端端到端验收。此次仅修改两个生产文件及
两个对应测试文件，并新增本报告；未提交或推送代码。


## 后续：编排依赖方向重构

删除 sdk.worker 的云端客户端转发，由 runner 的 worker 命令直接调用
cloud_client.worker。当时的本地 SDK 执行通过 sdk.execute / execute_many；这两个入口已在后续精简中删除，当前任务统一由云端 worker 链路执行。
APP_NAME、VERSION、run_id、state_path 原样移到 protocol，cloud_client
不再导入 cloud；cloud_app 分别从 cloud 导入执行入口、从 protocol 导入协议定义。
任务身份算法、协议版本、状态路径及模型部署配置未改变。

依赖方向为 runner → cloud_client → sdk，cloud_app → cloud → sdk；
执行模块共同依赖 protocol，protocol 不反向导入编排执行模块。
新增 test_orchestration_dependencies.py，检查函数内部导入在内的依赖方向、
两种顺序的独立进程导入、CLI worker 路由和同一配置的模型句柄复用。
针对性回归 30 项通过；模型句柄复用测试不等同于 Modal 热容器实测。
