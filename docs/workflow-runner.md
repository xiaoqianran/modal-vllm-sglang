# SDK Workflow / Task

跨模型编排默认使用 Modal SDK，本机只负责调度，不需要 MOSS_ENDPOINT、PIKA_ENDPOINT 或 HTTP Proxy Token。
单模型与组合任务都使用同一个入口；各模型 run.py 已移除，run 只有异步登记一种行为。

独立场次（单模型、每镜头恰好一个步骤且无步骤依赖）由本机 worker 直接向 GPU App
提交 detached generate_one，不部署或调用 CPU run_shot。已接受的调用在本机退出后仍继续；
尚未提交的调用需要恢复 worker。批次固定部署版本及缓存指纹，提交账本防止重复生成，
共享 admission 在确定终态前阻止滚动部署替换 GPU 服务。依赖工作流和已有 cloud_runs
记录继续走原云端编排路径，避免中途切换。

## 滚动部署与产物传输

- 源码指纹统一 Python/JSON 的 CRLF/LF 行尾并按 POSIX 相对路径排序，避免 Windows 和 Linux 的相同 Git 提交被误判为不同源码。其余内容修改仍然改变指纹。
- 托管部署先检查当前 App 清单中是否存在对应源码/缓存哈希的专属 CPU 探针，再调用当前 App 中该专属函数（不依赖付费的历史 revision 保留功能）。新版本未发布时不调用旧的固定名称探针；旧探针仅用于诊断。探针使用单次容器、2 秒缩容窗口和有界调用，校验前后核对 revision，避免滚动更新或回滚竞态。
- 就绪记录保存通过校验的 Modal revision。CPU 工作流使用专属哈希名称，GPU 保持稳定类名但在执行入口强制匹配完整部署哈希；已经接受的调用仍按原 call ID 查询。滚动部署继续保留，禁止用强制重建来打断在途任务。
- Volume V2 大文件（至少 8 MiB，且有期望大小及摘要）以 3 个数据块并行下载，单块 60 秒总超时、20 秒读空闲超时、最多 3 次尝试；仅失败块重新获取下载链接。临时文件及块摘要清单可跨 worker 重启恢复，验证通过才原子发布。
- 完整 SHA-256 按文件顺序在下载/恢复的数据流中计算，首次下载不再整文件二次读取。恢复时只重读本地已保留的块以验证并重建摘要。Modal Volume 的块传输不同于 HF Xet；不把 HF Xet 用作产物校验替代。小文件或非 V2 后端仍使用公共 SDK 流式读取。

## Task 006：图片在云端交给视频，本地下载独立进行

```text
MOSS generate_one → 图片写入 modal-vllm-sglang Volume 并 commit
                                    ├→ 图片引用解锁对应 Pika generate_one
                                    │       → Pika 云端校验并读取首尾帧 → 生成视频 → commit
                                    └→ 后台下载到本地（最多 8 个下载线程）
```

图片引用包含 Volume 内相对路径、字节数和 SHA256。Pika 在自己的云端容器中通过 SDK
读取已提交的 Volume 文件，校验格式、大小和 SHA256，再交给视频引擎；图片不经过本地电脑回传。
不调用模型挂载的 reload，避免为了读取新图片而刷新已有权重 mmap。
本地下载慢或失败不会阻止视频提交；云端完成状态与本地下载状态分开保存。

MOSS 和 Pika 是两个独立服务，各允许一个 GPU 容器，SDK 推理在各自容器中串行。
重叠执行两个模型通常需要两张云端 GPU；这不是在同一张卡上同时运行两个模型。

## 首次使用新的 Volume

统一存储名为 `modal-vllm-sglang`。旧 `modal-forge` Volume 不会被修改、删除或自动复制。
新的 Volume 没有旧盘的缓存，必须先在新盘准备权重，再部署本次更新后的服务：

```powershell
uv run python models/001-moss-image/download_model.py --execute
uv run python models/002-pika-h3-flex/download_model.py --execute
uv run python -m modal deploy models/001-moss-image/app.py
uv run python -m modal deploy models/002-pika-h3-flex/app.py
```

MiniMax 如需使用，也要在新盘准备并重新部署。新盘为空时不要使用 `--migrate-only`，
该参数仅转换同一盘内已有旧清单。模型内的 cache_directory 保留原布局，所在 Volume 已更换。
model.json 增加 volume_name 进入部署/请求签名，旧部署会拒绝新配置的生成调用。
GPU 快照仍分别受各模型 memory_snapshot 配置控制，空闲缩容窗口仍为 30 秒。

## 先离线检查

```powershell
uv run python run.py plan tasks/006-moss-pika-demo.json
uv run python run.py run tasks/006-moss-pika-demo.json --dry-run
```

以上不连接 Modal、不写运行记录、不启动 GPU。示例有两个镜头、四张图片和两段视频。
示例提示词用于验证管线，不保证独立图片之间的角色或场景一致性。

## 持续接收新任务（推荐）

终端 A 保持运行调度器：

```powershell
uv run python run.py worker
```

终端 B 注册一个或多个 Task：

```powershell
uv run python run.py run tasks/006-moss-pika-demo.json
# 多份真实 Task 可一次注册：run tasks/A.json tasks/B.json
```

`run` 仅持久化工作流计划，然后返回，不访问云端。worker 发现新场次/追加镜头后提交就绪工作。
worker 会启动付费推理，只把准备执行的计划登记到所监视的 outputs 中。
新任务默认到下一轮扫描进入调度，不等旧视频结束。worker 监视范围可用 --output 更换。
同场次追加：保留原 Task 的所有 item 和定义，追加新 item，再执行同一条 run。
修改已有镜头需新建场次；同一场次不能切换 SDK/HTTP、模型配置或 Volume。

调度按镜头优先把首尾帧入队；某镜头的两张图云端完成后，就提交对应视频，不等整批图片完成。
同模型多个调用已提交时会排队；不保证多个客户端之间的严格 FIFO。

## 查询与恢复

```powershell
uv run python run.py status outputs/006-moss-pika-demo/001-main
uv run python run.py resume outputs/006-moss-pika-demo/001-main
```

run 可一次登记多份 Task，登记后不等待执行。resume 也只写入恢复请求并返回，
通知运行中的 worker 继续查询原调用、重新校验并补下载，不会将失败/未知生成重新提交。
重启 worker 也会从持久化状态继续。不要把关闭客户端理解为取消云端调用。
云端已接受的 GPU 调用可继续运行；已接受的 run_shot 也会在云端继续调度其依赖。没有 worker 时，尚未提交的独立镜头不会自动入队。
本地下载失败后可执行 resume，无需为了重试下载而重启 worker。

计划位于 outputs/<project>/<scene>/plan.json，状态在 state.json。
`artifacts` 是云端引用；`local_artifact` 是本地下载记录；`download_error` 是下载错误。
report.json 和命令结果中的 counts 表示云端工作状态，local_downloaded 表示本地已收集数量。
查询 status 不访问云端，显示最后一次调度保存的状态。
run/resume 返回表示登记成功，不代表云端生成或本地下载成功；通过 status 查看 counts、download_errors 和 local_downloaded。
恢复时重新校验已下载文件；内容冲突拒绝覆盖，缺失文件重新下载。
SDK 不自动采用没有记录的现有同名输出，已有本地首尾帧请用 {"file":"inputs/..."} 显式引入。

## 锁与不确定提交

根目录锁仅用于短暂的场次注册；场次锁仅用于读写计划和状态。
网络请求、推理等待与下载都不持有这些锁。每个 job 有独立提交锁，避免两个调度器重复 spawn；
下载也有各自文件锁。不同 job、不同场次可以继续推进。

先持久化 submitting 再 spawn，获得 Modal call_id 后保存 queued。
提交结果不明时保留 uncertain，不自动重新付费生成；若后来能查到已提交的云端结果可恢复。
拿到 call_id 的任务只查询原调用。Pika 的视频 job_id 另由云端 generate_one 记录与恢复。
停止/重启引擎可能使引擎内部视频 ID 失效；本地记录不能提供远端恰好执行一次的绝对保证。

## 部署自检

```powershell
uv run python run.py check-deployment moss pika --dry-run
uv run python run.py check-deployment moss pika
```

--dry-run 完全离线；真实自检启动 GPU 并核对配置签名、缓存指纹、云端素材能力和 Volume，
不生成媒体。自检是诊断操作，不是第二种生成提交方式。
业务 CLI 不提供 HTTP 传输选项或同步提交选项。底层引擎内部仍使用 HTTP 推理协议。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
```

离线测试验证云端引用交接、慢下载不阻塞视频、慢视频期间的新任务/追加、锁粒度和故障恢复。
真实双模型重叠执行、GPU 快照恢复与性能需要部署后在云端另行验收。
