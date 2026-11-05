# 金融 AI 模型风险监测与审计平台

本目录是《软件架构与设计》课程项目的双人执行方案。项目围绕一个模拟信贷评分模型，完成模型登记、评分留痕、风险监测、告警处置和历史决策审计。成员暂以 A、B 表示，可直接替换为姓名。

英文项目名：Financial AI Model Risk Platform。仓库内容以中文设计文档为主，适用于金融科技与人工智能方向的双人课程项目。

**当前交付状态：成员 A 的 A1—A4 已完成。已具备数据、模型、评分/激活 API、原子留痕、独立监测 worker、规则与告警后端。累计 90 项测试通过；B 的正式平台集成和 A5 页面仍待完成，性能目标仍待实测。**

## A4 实施入口

- [A4 运行、计算口径、恢复与验收](docs/A4-监测worker与验收.md)
- [独立进程验收证据](evidence/A4-worker-smoke.json)
- [完整回归结果](evidence/A4-validation-summary.json)

先启动下方 A3 API，然后在同一项目目录的另一终端运行：

```powershell
$env:DATABASE_URL = 'sqlite:///./data/runtime/platform.db'
$env:ARTIFACT_ROOT = './artifacts'
.\.venv\Scripts\python.exe -m app.worker
```

独立复验使用 `python -m scripts.verify_a4_worker`；该脚本自建临时服务和凭据，包含明确标注的测试时钟推进，不是性能测试。

## A3 实施入口

- [A3 接口操作、事务语义与交接](docs/A3-接口操作与交接.md)
- [当前完整依赖锁文件](requirements-a3.lock.txt)
- [成功、失败与 outbox 的真实 HTTP 样例](evidence/A3-http-smoke.json)

先按 A3 文档配置两枚本地演示令牌，再启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

独立复验（脚本自建临时令牌、服务和数据库并自动关闭）：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_a3_http
.\.venv\Scripts\python.exe -m scripts.run_acceptance
```

## A2 实施入口

- [A2 完成记录、实测指标与交接](docs/A2-完成记录与交接清单.md)
- [固定版本模型包说明](artifacts/README.md)
- [评分示例输入](examples/A2-score-input.json)

项目 `.venv` 已安装模型依赖，在根目录可执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_model --version credit-logreg-v1
.\.venv\Scripts\python.exe -m scripts.score_model --version credit-logreg-v1 --input examples/A2-score-input.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

其他机器使用 [A2 锁文件](requirements-a2.lock.txt)安装依赖并核对 Python 版本；已交付模型的加载环境需与包内 environment.json 一致，详见 A2 说明。

## A1 实施入口

- [A1 完成记录与交接清单](docs/A1-完成记录与交接清单.md)
- [监测需求与五类用例](docs/A1-监测需求与用例.md)
- [合成数据说明与运行命令](data/README.md)

在项目根目录使用 Python 3.12 执行：

```powershell
py -3.12 -m scripts.validate_data
py -3.12 -m unittest discover -s tests -p test_data_preparation.py -v
```

A1 的数据生成与校验只依赖 Python 标准库；A2 和完整测试需要模型依赖。默认数据已生成；重新生成请使用新的 `--output` 目录，详见数据说明。

## 文档导航

1. [项目范围与课程交付](docs/01-项目范围与课程交付.md)：项目边界、用户、最小范围、课程报告要求、八学时安排和原型扩展计划。
2. [双人分工与实施步骤](docs/02-双人分工与实施步骤.md)：逐项责任、输入输出、交接、任务清单、协作规则和答辩分配。
3. [软件环境与操作手册](docs/03-软件环境与操作手册.md)：所需软件、技术选择、Windows 安装检查、虚拟环境、依赖与未来启动方式。
4. [数据与接口约定](docs/04-数据与接口约定.md)：数据字典、数据库实体、状态机、接口、评分事件示例与处理语义。
5. [ASR与验收方案](docs/05-ASR与验收方案.md)：质量属性场景、测量口径、功能验证、异常注入与演示脚本。
6. [五类架构设计](docs/06-五类架构设计.md)：业务、数据、逻辑、技术、部署架构的 Mermaid 图和设计取舍。

协作与公开内容约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 先做什么

- 两人先阅读第 1、2 份文档，确认基本范围和共同接口。
- A 先产出模拟数据、基线模型说明与监测计算样例；B 先产出数据模型、权限规则与审计记录样例。
- 课程以设计材料为第一交付；是否做可运行原型，按实际课外时间决定。不要把完整开发压进八个实验学时。
- 若做原型，先完成一条端到端链路，再补页面和扩展能力。

## 方案基线

- 业务：模拟个人信贷风险评分；不接银行生产系统，不开展真实授信。
- 模型：scikit-learn 的逻辑回归与预处理 Pipeline，使用合成数据；不需要 GPU 或大模型 API。
- 应用：FastAPI、Streamlit、SQLAlchemy、SQLite、独立单 worker 进程。
- 关键机制：评分和审计原子保存、持久化 outbox、幂等消费、模型版本固定、服务端权限检查。
- 扩展：真实标签回流、模型效果监测、PostgreSQL、容器化，均不计入基础版必做范围。

## 材料依据

以下为本地课程参考材料，未随公开仓库发布。远程阅读者可直接使用本仓库中归纳的课程交付清单与项目设计。

- 上级目录《软件架构与设计》-实验大纲-隆承志.doc：功能与质量需求分析、ASR，以及架构设计；实验安排为两个四学时环节。
- 上级目录《软件设计与系统架构》课程实验报告模板.doc：选题背景、需求分析、数据流图、数据字典、ASR、五类架构及分工。
- 上级目录“需求说明书：Dcampus SNMS 2005 软件需求说明书.doc”：参考需求文档的组织方式，不复用其中网管业务和旧技术参数。

编写日期：2026 年 11 月 4 日。所有阈值与性能指标均为课程设计建议值，实施后应以实测结果修订。
