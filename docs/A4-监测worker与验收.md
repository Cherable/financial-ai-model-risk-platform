# A4 监测 worker 与验收

日期：2026 年 11 月 5 日。已完成成员 A 的 A4：独立单 worker、持久化消费去重、窗口指标、规则版本、告警及修订记录。A5 页面和 B 的正式平台集成仍待完成。

## 1 已交付模块

- [worker 入口](../app/worker.py)：独立运行、单次运行、轮询、人工恢复入口。
- [监测存储](../app/modules/monitoring/store.py)：租约令牌、重试、消费标记、窗口修订号、原子发布与心跳。
- [统计计算](../app/modules/monitoring/statistics.py)：原始缺失率、特征/概率 PSI、推理失败率和 P95。
- [监测表定义](../app/modules/monitoring/schema.py)：规则、窗口、指标历史、告警历史和人工处置。
- [告警发布](../app/modules/alerts/service.py)、[规则与处置命令](../app/modules/alerts/commands.py)。
- [监测接口](../app/modules/monitoring/routes.py)：受模型权限保护的查询、规则版本创建、告警认领/关闭与运维状态。
- [A4 测试](../tests/test_monitoring_worker.py)、[独立进程验证脚本](../scripts/verify_a4_worker.py)。

本阶段沿用 requirements-a3.lock.txt，没有新增第三方依赖。API 不会自动启动 worker，两者是不同进程。

## 2 运行步骤

先按 [A3 操作说明](A3-接口操作与交接.md)在终端一启动 API、配置演示令牌并激活模型。终端二切到同一项目根目录，指向同一个数据库：

```powershell
$env:DATABASE_URL = 'sqlite:///./data/runtime/platform.db'
$env:ARTIFACT_ROOT = './artifacts'
.\.venv\Scripts\python.exe -m app.worker
```

默认每五秒轮询，每轮最多领取 100 个事件并发布最多 20 个待计算窗口。Ctrl+C 停止；重新运行会恢复未完成事件和脏窗口，不清空已有记录。

只运行一轮：

```powershell
.\.venv\Scripts\python.exe -m app.worker --once --max-events 1000
```

worker 使用本地数据库和受控模型目录，不需要 HTTP bearer token。该进程不实现 B 的复现任务执行器，只消费 prediction.completed 类型；不会接管其他任务类型的租约。

API 必须先初始化该数据库的版本元数据。如果 worker 提示没有登记版本，先检查两端的 DATABASE_URL、当前工作目录和 A3 启动状态，不能通过新建另一套空数据库来代替。

## 3 事件可靠性

领取在短写事务中完成：pending 或租约已过期且达到重试时间的事件变为 processing，尝试次数加一，并获得独立 lease_token 和三十秒租约。新领取者使用新 token，旧进程即使恢复也不能用原 token 确认或覆盖结果。

消费时先检查协议 1.1、事件与评分/快照关联及输入摘要，再按服务器受理时间定位窗口。在同一个事务内保存 processed_events、增加窗口计数和修订号、确认 outbox 为 done。任一步失败全部回滚。

processed_events 按消费者名称和 event_id 唯一。重复投递已有标记的事件只确认队列状态，不再次累加，不再生成指标或告警。

错误采用有界退避：第一次失败后一秒、第二次两秒、第三次四秒、第四次八秒；第五次失败进入 dead_letter。租约反复领取后仍崩溃也计入五次上限。last_error 只保存归类代码，不保存原始客户输入、令牌或异常路径。

一个无效事件进入延迟重试不会挡住后面的合法事件。数据库不可用时 worker 报告存储不可用，保留原有状态，在后续轮次继续尝试。

## 4 两阶段窗口与计算恢复

窗口固定为 UTC 对齐的六十秒区间 `[start,end)`，结束后再等待十秒迟到宽限。未满足这个时间条件时只消费和计数，不发布指标。

第一阶段事务完成后，事件可标记 done，但窗口的 revision 大于 computed_revision，表示还有持久化的待计算任务。即使进程此时终止，重启后也会扫描这类窗口补算，不能仅依赖内存任务列表。

第二阶段读取窗口快照和已消费记录，在写事务外计算。提交前再核对修订号；如果计算过程中有迟到事件改变 revision，则丢弃旧计算，在下一轮按新快照重算。指标、指标历史、告警修订和 computed_revision 在同一事务提交。

迟到事件仍归原窗口；每次发布保留 metric_revisions，而不是悄悄覆盖旧依据。若参考模型包暂不可读，窗口进入独立退避；五次计算失败后显示 calculation_failed，等待人工检查。其他窗口和事件仍可继续处理。

“computed”表示截至该消费检查点已计算，不代表数据库里永远不会再出现迟到数据。GET /metrics 同时返回 latest_window_revision、unconsumed_events 和 stale；已知积压未消费、存在隔离事件或新修订未发布时，旧指标不会被标成当前完整结果。

## 5 指标口径

每个窗口产生十五项指标记录：

- 六项 missing_rate：仅对成功评分的原始规范化输入统计 null；分母是成功评分数，不是填补后的矩阵。
- 六项 feature_psi：使用 A2 包内各特征固定分箱。
- 一项 prediction_psi：使用 A2 包内概率固定分箱。
- 一项 failure_rate：推理失败次数 / 实际推理尝试次数；模型加载失败不进入分母。
- 一项 inference_p95：只对成功推理的耗时计算，单位毫秒，使用固定 linear 分位数方法。

PSI 成功样本至少 200 条；不足时 value=null、status=insufficient_samples，不生成 PSI 告警。无分母时标记 no_data，不写伪造的零值。

固定分箱沿用左闭右开区间，等于切点时进入右侧分箱；首尾覆盖超出参考范围的输入，缺失单独一箱。参考和当前占比各加 A2 指定的 epsilon=1e-6，再分别归一化后计算 PSI，避免零占比产生无穷值。

没有真实结果标签，因此本模块不计算实时准确率、AUC 或宣称模型效果退化。数据漂移只代表输入/输出分布变化。

## 6 规则与告警

课程默认规则为缺失率大于 5%、PSI 大于 0.2、推理失败率大于 1%、成功推理 P95 大于 500 ms。它们是演示阈值，不是行业标准，也不等于端到端 API 时延要求。

默认规则使用明确的创始基线时间，用于课程历史样本；用户新增规则只允许未来 UTC 整分钟生效。规则按“模型版本、指标、特征”递增版本，旧版本不更新。每个窗口首次消费时，固定窗口起点已生效的规则快照。

告警按窗口和规则版本唯一。迟到重算会追加 alert_revisions，并更新 condition_active 和当前值；不会删除原告警、覆盖人工处置结论或自动重开已关闭告警。若新数据使原条件不再成立，保留告警并标记 condition_active=false，供审计解释。

人工流程为 open → acknowledged → closed。关闭需填写说明，只允许认领人或管理员关闭；命令有幂等回执，重复调用不重复记录处置。角色和模型范围均在后端检查。

## 7 查询与操作

沿用 A3 终端中的私有令牌文件，查询 worker 状态与某模型的指标：

```powershell
$taskCredentials = Get-Content -Raw data/runtime/demo-credentials.json | ConvertFrom-Json
$taskHeaders = @{ Authorization = "Bearer $($taskCredentials.admin_token)" }
$taskVersions = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/model-versions -Headers $taskHeaders
$taskVersion = $taskVersions.items | Where-Object { $_.version -eq 'credit-logreg-v1' }
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/monitor/status -Headers $taskHeaders
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/metrics?model_version_id=$($taskVersion.model_version_id)&limit=100" -Headers $taskHeaders
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/alerts?model_version_id=$($taskVersion.model_version_id)&limit=100" -Headers $taskHeaders
```

新增/查询规则使用 POST/GET /api/v1/monitor-rules；请求样式见 [A4-rule-request.json](../examples/A4-rule-request.json)，其中 effective_at 必须换成未来 UTC 整分钟，model_version_id 从实际版本列表获取。POST 需要新的 Idempotency-Key；同键修改内容返回 409。

认领使用 POST /api/v1/alerts/{alert_id}/acknowledge，关闭使用 POST /api/v1/alerts/{alert_id}/close，请求体均为 `{"note":"具体说明"}`；GET /api/v1/alerts/{alert_id} 可查看最多一百条修订和处置记录。实际 role 为 admin 或 risk_manager 才能修改，普通 scorer 不能处置。

队列状态、死信摘要、计算错误列表和最近完成轮次由 GET /api/v1/monitor/status 返回，仅管理员可访问。心跳十五秒内标 fresh，超过为 stale，未运行过为 never_started；它是最近轮次的证据，不是进程永远存活的保证。

## 8 人工恢复

先检查状态接口中的 dead_letters 或 window_errors，修复协议、数据或模型目录问题，再以持有本地数据库访问权限的操作员身份显式恢复：

```powershell
# 将下面的编号替换为状态接口返回的真实 UUID；先修复原因，再恢复。
.\.venv\Scripts\python.exe -m app.worker --retry-event '真实事件UUID' --reason '已修复事件协议并核验源评分'
.\.venv\Scripts\python.exe -m app.worker --retry-window '真实窗口UUID' --reason '已恢复对应版本的受控参考包'
.\.venv\Scripts\python.exe -m app.worker --once --max-events 1000
```

恢复只接受 dead_letter 事件或 calculation_failed 窗口，重置当前重试预算并记录先前错误、次数和人工原因；不会清空消费标记或历史指标。CLI 属于本地运维工具，其审计主体为监测系统身份与人工说明，不等于 B 尚未提供的个人实名认证。

## 9 验收证据与结果

- [A4-validation-summary.json](../evidence/A4-validation-summary.json)：完整 A1—A4 回归结果。
- [A4-worker-smoke.json](../evidence/A4-worker-smoke.json)：真实本机 API 与两个独立 worker 进程的验收。
- A4 新增 35 项测试，连同既有 55 项共 90 项；包括租约并发和过期接管、旧 token 隔离、消费去重、两阶段回滚、重启补算、迟到修订、规则冻结、200 条门槛、参考包失败与人工恢复、UTC 边界、分页和权限。
- 端到端输入为四十条真实 HTTP 合成评分，其中十条收入缺失。worker 未启动时保留四十条 pending；第一独立进程消费四十条，第二次启动消费零条。推进测试时钟后，本次生成一个窗口、十五项指标记录和一条缺失率告警，并完成认领与关闭。
- 该端到端样本不足二百，因此七项 PSI 记录应为 insufficient_samples；达到门槛后的 PSI 数值计算由两百条可控记录的专项测试验证。

复验命令：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_a4_worker
.\.venv\Scripts\python.exe -m scripts.run_acceptance
```

端到端脚本不会改写源评分时间；只在最后发布阶段将 worker 的测试时钟前移一百二十秒，避免等待真实分钟窗口。这验证流程和边界，不证明十五秒告警时效、吞吐或生产高可用已经达标。重跑时窗口数量可能随真实请求跨分钟而变化。

API 启动和 worker 初始化只执行增量建表，并对旧 A3 outbox 补充 nullable lease_token；不删除原表或评分数据。专项测试已验证旧结构升级后事件仍可消费。B 若替换适配器，必须保持租约 fencing、消费标记、修订号和同事务提交语义；不能只复制表名。

当前正式用户/JWT、审计平台与 A5 页面仍待完成；其他任务类型的 outbox 不由本 worker 接管。现有依赖组合的 Starlette 测试客户端兼容提示与 A3 一致，不影响本次通过结果。

参考：[SQLAlchemy SQLite 事务说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)、[NumPy quantile 的 linear 方法](https://numpy.org/doc/stable/reference/generated/numpy.quantile.html)。
