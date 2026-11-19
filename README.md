# 金融 AI 模型风险监测与审计平台

本组围绕模拟个人信贷评分完成模型登记与激活、评分证据留存、风险监测、告警处置、身份授权、审计导出及历史评分复现。系统使用合成数据与固定版本逻辑回归模型，采用 FastAPI、Streamlit、SQLAlchemy、SQLite WAL 和独立 Worker。

整合版本包含 A1—A5、B1—B5、GitHub 合作分支 26d1e4b 和本地回执、恢复及性能修正。158 项自动化测试全部通过；真实 HTTP、负载、浏览器和进程恢复结果分别保存在 `evidence/acceptance-20261119`，各项结论以对应实测记录为准。

## 运行

运行环境为 Windows AMD64、CPython 3.12.14，依赖见 `requirements-a5.lock.txt`，模型环境与包内清单严格一致。

```powershell
python -m pip install -r requirements-a5.lock.txt
python -m scripts.run_demo
```

默认页面为 http://127.0.0.1:8501 ，API 文档为 http://127.0.0.1:8000/docs 。首次运行创建 admin、developer、risk、auditor 四类账号，密码由本地终端输出。账号、JWT 密钥及运行数据库不纳入版本控制。

## 验证

```powershell
python -m scripts.validate_data
python -m scripts.verify_model --version credit-logreg-v1
python -m unittest discover -s tests -v
```

`acceptance_live`、`acceptance_load`、`acceptance_asr` 和 `acceptance_recovery` 用于隔离演示库的实测，使用 18100 端口及测试账号。负载测试包含 5 并发 500 次基准和三轮 20 并发各 100 次测试，保留每次请求样本。恢复实验只终止隔离测试进程。

## 架构与协作

- A：合成数据、固定模型、评分留痕、监测规则与告警、模型及监测页面。
- B：身份认证、角色及模型授权、审计查询与导出、历史复现和审计页面。
- 评分、输入快照、审计和 outbox 在同一事务提交；监测和复现由单一 Worker 异步处理。
- 模型每次使用前核验文件清单、摘要和环境；解析与推理使用同一批已校验字节。
- 复现按原模型版本执行，同键回执在完成后仍可查询，中断任务在单 Worker 重启时恢复。
- SQLite 单写者和单 Worker 是当前部署约束，未覆盖跨机高可用或生产信贷。

完整设计见 `docs/`，本次整合记录见 `docs/最终整合记录-20261119.md`。

## 最终课程文档

- [课程实验报告](report/金融AI模型风险监测与审计平台-实验报告.docx)
- [软件需求说明书 V1.5](docs/金融AI模型风险监测与审计平台软件需求说明书.docx)

最终 Word 沿用课程模板，包含实际页面截图和 20261119 的验收结果；当前需求正文同步保存于 docs/软件需求说明书.md。report/build_report.py 与 docs/build_requirements_docx.py 为早期版本生成器，现有最终 Word 为本版依据。
