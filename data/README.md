# A1 合成数据使用说明

数据全部由本项目脚本生成，没有真实客户、银行流水或外部数据集。用途是开发与演示监测、审计及复现流程，不用于实际授信，也不能说明模型在真实金融场景的效果。

## 已生成内容

目录为 `synthetic/v1/`，共六个 CSV、10,000 行数据（不计表头），以及记录参数、独立子种子、摘要与统计的 `manifest.json`：

- `train.csv`：5,000 行，带 `synthetic_default_label`，供 A2 训练。
- `test.csv`：1,000 行，带模拟标签，留出评估；不做拟合和阈值调优。
- `reference.csv`：1,000 行，无标签，固定监测分箱与参考分布。
- `stream_normal.csv`：1,000 行，无标签，正常输入场景。
- `stream_drift.csv`：1,000 行，无标签，收入下移、负债率上移场景。
- `stream_missing.csv`：1,000 行，无标签，恰好 100 行月收入为空。

三组 stream CSV 是“待回放的特征行”，尚无评分、事件编号、真实受理时间或推理耗时。A3 实现后由回放工具逐行生成请求，服务器生成这些元数据；不能把 CSV 直接当成已发生的评分事件。

## 字段与单位

CSV 为 UTF-8、逗号分隔、LF 换行；金额用两位小数字符串序列化。Git 属性固定 CSV/JSON 的 LF，避免 Windows 检出换行转换导致 manifest 摘要变化。

- `customer_ref`：形如 `SYN-train-000001`，虚构编号；按集合命名，不能作为特征。
- `age`：18—80 岁整数。
- `monthly_income`：月收入，单位元；原始基线按对数正态分布生成，限制在 1,500—150,000 元，漂移场景再乘 0.55。
- `debt_income_ratio`：比例，基线使用 Beta(2,5) × 1.8 并限制在 0—2.5，漂移场景增加 0.6 并截到 3。
- `past_due_count`：模拟逾期次数，指数分布采样后取整，范围 0—12。
- `loan_amount`：单位元，基线月收入的随机 2—24 倍并限制在 1,000—5,000,000 元；漂移时保留先生成的贷款额，因此负担同步变重。
- `employment_years`：就业年限，0 到 `min(age-18,60)` 之间，两位小数。
- `synthetic_default_label`：仅训练和测试集出现，0/1 模拟结果；没有真实违约观察期或业务含义。

数据分布是教学假设，不是对真实客户总体分布的估计。除缺失场景的 monthly_income 外，所有生成字段均完整。字段允许范围仍以 [接口契约](../docs/04-数据与接口约定.md) 为准。

## 缺失如何处理

CSV 空字段表示缺失，不是 0、字符串 `null` 或空白文本。Python `csv.DictReader` 读取为 `""`，组装 JSON 时转为 Python None；pandas 可把空字段读取为 NaN。后续原始快照保留 null 和缺失掩码，不能在监测前用均值覆盖原始数据。

缺失数量为 `ROUND_HALF_UP(样本数 × missing_rate)`，默认 1,000 × 0.1 = 100；使用单独随机流选择位置，支持 0 和 1 两个边界。若修改 missing_rate 后的比例不超过预警阈值，不应再声称该样本必然触发缺失告警。

## 模拟标签生成

对训练和测试集已序列化的完整特征计算：

```text
r = min(loan_amount / (monthly_income × 12), 5)
z = -2.4 + 2.4 × debt_income_ratio + 0.45 × past_due_count
    + 0.8 × r - 0.07 × employment_years
p = 1 / (1 + exp(-z))
synthetic_default_label ~ Bernoulli(p)
```

每组标签使用独立随机流；年龄不直接进入标签公式。公式不是已经训练好的信贷模型，不能把生成 p 当作 A2 的模型预测；p 本身不导出到 CSV，避免被误作输入。训练正标签 1,387 条，测试正标签 259 条，均为本次默认生成结果。

## 可复现与隔离约定

默认主种子 20261104；子种子由生成器版本、主种子、集合名称的 SHA-256 前八字节导出。特征、标签和缺失掩码各使用局部随机实例，不依赖全局随机状态或运行顺序。

相同生成器版本、Python 运行时和参数下，输出 CSV 与 manifest 字节一致；manifest 记录精确 Python 版本。跨 Python 版本不要无条件假定随机分布函数输出相同，应复用当前 CSV 或核对摘要。

六组独立采样、编号互不相交；校验器额外检查重复特征行，避免只检查编号造成虚假的隔离结论。分布有意相同不等于样本重复；在线流与参考样本不包含标签。

## 运行方式

在仓库根目录执行，Python 3.12 标准库即可，不需要安装 pandas、scikit-learn、付费 API 或其他第三方包：

```powershell
# 查看参数
py -3.12 -m scripts.generate_data --help

# 已有默认数据，不覆盖它；在另一目录复现
py -3.12 -m scripts.generate_data --output data/synthetic/reproduced

# 校验默认已交付数据，并输出机器可读结果
py -3.12 -m scripts.validate_data --report evidence/A1-data-validation.json

# 运行自动化验收
py -3.12 -m unittest discover -s tests -v
```

如果机器上没有 py 启动器，用对应 Python 可执行文件完整路径替换。确需替换目标目录中的六个 CSV 与 manifest 时，显式传 `--overwrite`；默认遇到已有目标文件直接失败，不会静默覆盖。该选项不递归删除目录，也不删除其他文件。写入中断后先核对 manifest 摘要，不能把半完成目录交给 B。

## A2 和 B 的接入方式

A2 训练时明确只选六个特征列，标签仅取训练集的 `synthetic_default_label`。在 train 上拟合预处理与模型，在 test 上评估；reference 只用于固定监测分箱。模型训练属于下一步，本次没有保存模型文件。

B 在 `evidence/A1-handoff-sample.json` 中检查已抽出的十条虚构样本，重点核对字段单位、金额精度、缺失含义、就业年限与年龄关系。机器校验已完成，B 的人工检查仍待其实际执行，不能代签。
