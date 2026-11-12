# -*- coding: utf-8 -*-
"""按课程实验报告模板生成 Word 报告。

用法：python build_report.py
输入：报告模板.docx（由课程提供的 .doc 转换）
输出：金融AI模型风险监测与审计平台-实验报告.docx

封面信息与组内分工为占位符，提交前需替换为实际信息。
"""
import copy
import os
import sys

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TPL = os.path.join(HERE, "报告模板.docx")
OUT = os.path.join(HERE, "金融AI模型风险监测与审计平台-实验报告.docx")
FIG = os.path.join(ROOT, "docs", "figures")

BODY_FONT = "仿宋_GB2312"
HEAD_FONT = "微软雅黑"
BODY_PT = 12
FIG_WIDTH = Cm(14.5)


# ---------- 基础工具 -------------------------------------------------
def set_run_font(run, name, size=None, bold=None, color=None):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = RGBColor(*color)


def body_para(doc, text, *, indent=True, size=BODY_PT, align=None, space_after=6):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(space_after)
    pf.widow_control = True
    if indent:
        pf.first_line_indent = Pt(size * 2)
    if align is not None:
        p.alignment = align
    r = p.add_run(text)
    set_run_font(r, BODY_FONT, size)
    return p


def heading(doc, text, level=1):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(14 if level == 1 else 10)
    pf.space_after = Pt(8 if level == 1 else 6)
    pf.line_spacing = 1.3
    pf.keep_with_next = True
    pf.widow_control = True
    r = p.add_run(text)
    set_run_font(r, HEAD_FONT, 16 if level == 1 else 14, bold=True)
    return p


def caption(doc, text, keep_next=False):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(10)
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.keep_with_next = keep_next
    r = p.add_run(text)
    set_run_font(r, BODY_FONT, 10.5)
    return p


def picture(doc, filename, caption_text=None, width=None):
    path = os.path.join(FIG, filename)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.keep_with_next = True
    p.paragraph_format.line_spacing = 1.0
    p.add_run().add_picture(path, width=width or FIG_WIDTH)
    if caption_text:
        caption(doc, caption_text)
    return p


def _set_cell_border(cell):
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tcPr.append(borders)


def table(doc, headers, rows, widths=None, fs=10.5, caption_text=None):
    if caption_text:
        caption(doc, caption_text, keep_next=True)
    t = doc.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    tblPr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    if widths:
        tblW = OxmlElement("w:tblW")
        tblW.set(qn("w:type"), "dxa")
        tblW.set(qn("w:w"), str(int(sum(widths) * 567)))
        tblPr.append(tblW)
    t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        p = hdr[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(h)
        set_run_font(r, HEAD_FONT, fs, bold=True)
        _set_cell_border(hdr[i])
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), "EEF2F7")
        hdr[i]._tc.get_or_add_tcPr().append(shd)
    for row in rows:
        tr = t.add_row()
        trPr = tr._tr.get_or_add_trPr()
        trPr.append(OxmlElement("w:cantSplit"))
        cells = tr.cells
        for i, val in enumerate(row):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(str(val))
            set_run_font(r, BODY_FONT, fs)
            _set_cell_border(cells[i])
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)
    return t


def replace_text(p, text):
    """替换段落文字，保留首个 run 的格式。"""
    if not p.runs:
        r = p.add_run(text)
        return
    first = p.runs[0]
    first.text = text
    for r in p.runs[1:]:
        r.text = ""


def add_section(doc, builder, next_anchor=None):
    """在文末生成内容，再整体移动到 next_anchor 之前；next_anchor 为空时保留在文末。"""
    existing = set(doc.element.body.iterchildren())
    builder(doc)
    new = [el for el in doc.element.body.iterchildren() if el not in existing]
    if next_anchor is not None:
        for el in new:
            next_anchor._p.addprevious(el)
    return new


# ---------- 封面 -----------------------------------------------------
COVER = {
    "学  院": "待填写",
    "专  业": "待填写",
    "组长姓名": "待填写",
    "组内成员": "待填写",
    "指导教师": "隆承志",
    "课程编号": "待填写",
    "课程学分": "2",
    "起始日期": "待填写",
}


def fill_cover(doc):
    for p in doc.paragraphs[:20]:
        txt = p.text.strip()
        for label, value in COVER.items():
            if txt.replace(" ", "") == label.replace(" ", ""):
                # 模板的下划线是带 single 下划线的空格 run，直接把值写进去
                target = None
                for r in p.runs:
                    rPr = r._element.find(qn("w:rPr"))
                    if rPr is not None and rPr.find(qn("w:u")) is not None:
                        target = r
                if target is not None:
                    target.text = value
                    set_run_font(target, BODY_FONT, 14)
                else:
                    p.add_run("  " + value)
                break
        if "题目" in txt and "在此填" in txt:
            replace_text(p, "题目：金融 AI 模型风险监测与审计平台")
            for r in p.runs:
                set_run_font(r, HEAD_FONT, 20, bold=True)


def fill_division_table(doc):
    t = doc.tables[0]
    cell = t.rows[0].cells[1]
    cell.text = ""
    lines = [
        ("组内分工", True),
        ("成员 A：合成数据与校验、固定版本模型包、评分接口与原子留痕、监测 Worker、"
         "规则与告警、三个业务页面；主笔业务架构、逻辑架构与一级数据流图。", False),
        ("成员 B：数据架构与数据字典、技术架构与部署架构；定义审计、权限与复现需求；"
         "负责报告版式与图形打印化重排。", False),
        ("排名及理由", True),
        ("待两位成员按实际交付与集成记录共同确认后填写。", False),
    ]
    first = True
    for text, bold in lines:
        p = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.15
        r = p.add_run(text)
        set_run_font(r, BODY_FONT, 10, bold=bold)


# ---------- 正文内容 -------------------------------------------------
def sec_background(doc):
    body_para(doc, "信贷评分模型投入服务后，输入缺失、数据分布变化和推理故障会直接影响评分结果的可信度。"
                   "如果系统只保存最终的风险概率，那么当一次评分出现争议、面临审计问询或触发风险事件时，"
                   "就无法回答三个基本问题：这次评分当时使用的是哪个模型版本；模型接收到的原始输入是什么；"
                   "风险信号出现后由谁、依据什么规则进行了处置。本课题以模拟个人信贷评分为业务场景，"
                   "设计一个金融 AI 模型风险监测与审计平台，围绕上述三个问题组织功能与架构。")
    body_para(doc, "本课题需要解决的主要问题及技术要求包括：（1）模型版本的受控管理，只有经验证并登记的固定版本"
                   "可用于评分，历史版本不得被覆盖；（2）评分证据的原子留存，输入快照、评分结果、审计记录与下游"
                   "事件必须在同一事务中提交，任一步失败整体回滚；（3）风险监测与评分请求隔离，监测进程停机不得"
                   "导致评分失败，恢复后能够补算；（4）幂等与去重，网络重试与进程中断不得产生重复评分或重复统计；"
                   "（5）历史可复现，切换激活版本后，仍应能使用原模型包与原输入复现历史评分的结果。")
    body_para(doc, "本设计的指导思想是质量属性驱动架构。先识别影响架构的需求（ASR），再由这些需求推导进程划分、"
                   "数据模型与部署形态，最后用最小可运行原型验证关键架构决策。原型使用合成数据与固定版本的逻辑回归 "
                   "Pipeline，不接入真实信贷系统，也不声称具备生产级高可用、合规认证或分布式部署能力；"
                   "这些边界在需求与架构章节中逐条写明。")
    body_para(doc, "项目按两名成员分工完成。成员 A 负责合成数据、固定模型、评分与监测链路、规则告警及业务页面；"
                   "成员 B 负责数据架构、身份权限、审计导出与历史复现的设计，以及技术架构、部署架构和报告汇总。"
                   "本文中标注为“设计”的内容尚未实现，不得计为已完成功能；其余条目均已实现并有自动化测试。")


def sec_requirements(doc):
    heading(doc, "2.1 用户与角色", 2)
    body_para(doc, "系统设置四类业务角色。角色由服务端在每次请求时校验，页面隐藏按钮不构成授权。"
                   "四类是系统角色，不要求四个人操作，演示时使用独立测试账号切换。")
    table(doc,
          ["角色", "职责", "主要操作", "可见范围"],
          [["模型开发人员", "登记与查看模型版本", "查看版本、元数据、指标与解释", "获授权模型的元数据与指标"],
           ["风险管理人员", "配置规则并处置告警", "创建规则版本、认领与关闭告警", "获授权版本的指标、规则与告警"],
           ["审计人员", "查询证据并复现历史决策", "查询评分证据、发起复现、导出报告", "获授权范围内的评分证据；不可修改原始记录"],
           ["管理员", "管理版本与授权", "激活版本、管理用户与模型授权", "全部模型的管理权限"]],
          widths=[3.2, 3.4, 4.4, 3.8],
          caption_text="表 2-1 系统角色与权限")

    heading(doc, "2.2 功能需求", 2)
    body_para(doc, "需求编号 FR01—FR19 覆盖数据准备、模型管理、评分留痕、风险监测、告警处置、身份权限、"
                   "审计查询与历史复现八个部分。其中 FR01—FR09 由成员 A 实现；FR10—FR17 由成员 B 实现，"
                   "审计与复现页面（FR18、FR19）仍待完成。")
    table(doc,
          ["编号", "功能", "状态"],
          [["FR01", "合成数据生成与校验", "已实现"],
           ["FR02", "模型版本登记与查询", "已实现"],
           ["FR03", "模型激活与幂等回执", "已实现"],
           ["FR04", "评分与原子留痕", "已实现"],
           ["FR05", "事件消费与恢复", "已实现"],
           ["FR06", "窗口指标监测", "已实现"],
           ["FR07", "监测规则配置", "已实现"],
           ["FR08", "告警生成与处置", "已实现"],
           ["FR09", "令牌会话与角色展示", "已实现"],
           ["FR10", "登录与令牌签发", "已实现"],
           ["FR11", "角色与模型授权", "已实现"],
           ["FR12", "服务端授权执行", "已实现"],
           ["FR13", "评分证据查询", "已实现"],
           ["FR14", "审计记录检索", "已实现"],
           ["FR15", "审计导出（单条证据）", "已实现"],
           ["FR16", "复现任务创建", "已实现"],
           ["FR17", "复现执行与比对", "已实现"],
           ["FR18", "审计页面", "设计"],
           ["FR19", "复现页面", "设计"]],
          widths=[1.8, 8.0, 4.0],
          caption_text="表 2-2 功能需求与实现状态")
    body_para(doc, "完整的功能描述、输入输出、处理流程、参数约束与异常处理见单独提交的《软件需求规格说明书》，"
                   "本文只给出与架构相关的摘要。")

    heading(doc, "2.3 数据流图", 2)
    body_para(doc, "上下文图给出系统与五类外部实体之间的数据交换，不展开内部处理。"
                   "一级数据流图按评分与激活、监测与处置两条主线拆分绘制，"
                   "处理编号 1.0—6.0 与数据存储编号 D1—D5 在两图中保持一致。")
    picture(doc, "B-上下文图.png", "图 2-1 系统上下文图")
    picture(doc, "B-DFD-评分与激活.png", "图 2-2 一级数据流图（一）：评分与模型激活")
    body_para(doc, "图 2-2 中，评分调用方提交版本标识与合成特征，处理 1.0 完成输入校验、推理与解释计算，"
                   "并在同一事务内写入评分快照、审计记录与完成事件（D2 存储）。管理员通过处理 6.0 激活受信任版本，"
                   "激活结果回写版本索引（D1）并产生审计回执。")
    picture(doc, "B-DFD-监测与处置.png", "图 2-3 一级数据流图（二）：监测计算与告警处置")
    body_para(doc, "图 2-3 中，处理 2.0 认领 D2 中的完成事件并归入固定窗口（D3），处理 3.0 按窗口起点冻结的"
                   "规则版本（D4）计算缺失率、PSI、失败率与推理耗时 P95，结果写入指标与告警历史（D5）。"
                   "风险管理人员通过处理 4.0 查询证据并填写处置说明，通过处理 5.0 创建不可覆盖的规则新版本。")

    heading(doc, "2.4 数据字典摘要", 2)
    body_para(doc, "系统已实现 24 张数据表，按写入者划分为模型与版本、评分证据、监测与告警、身份与审计四个域。字段级数据字典（类型、可空性、取值范围、单位、写入方与去向）"
                   "见《数据架构与数据字典》。此处列出核心实体及其关键约束。")
    table(doc,
          ["实体", "作用", "关键约束"],
          [["model_versions", "模型版本元数据与包摘要", "同一模型版本号唯一；历史版本不可覆盖"],
           ["model_activations", "每个模型的当前激活指针", "以 model_id 为主键，每次切换只更新一行"],
           ["input_snapshots", "原始输入与规范化输入快照", "同一评分一份快照；原始与规范化输入分列保存"],
           ["predictions", "评分结果与解释快照", "主体、接口、幂等键联合唯一；状态与概率由 CHECK 约束绑定"],
           ["audit_logs", "业务操作审计", "只追加，不提供更新与删除"],
           ["outbox_events", "持久化完成事件", "每评分一个事件；租约令牌防止旧消费者确认"],
           ["monitor_windows", "固定窗口计数与修订号", "版本与窗口起点唯一；冻结规则快照"],
           ["metrics", "窗口指标（每窗口 15 项）", "窗口、指标名、特征唯一；不可计算时写空值加状态"],
           ["alerts", "告警证据与处置状态", "窗口与规则唯一；触发条件与处置状态两个维度分开保存"]],
          widths=[3.6, 5.0, 5.8],
          caption_text="表 2-3 核心数据实体与约束摘要")

    heading(doc, "2.5 安全性与数据完整性要求", 2)
    body_para(doc, "安全性方面：身份与模型授权在后端校验，请求体中的角色或用户字段不作为授权依据；"
                   "密码使用 Argon2id 哈希，日志与错误响应不得包含令牌、密码或完整原始输入；"
                   "令牌与数据库文件不纳入版本控制。")
    body_para(doc, "数据完整性方面：评分事务的四张表同提交或同回滚；幂等通过“接口层校验、记录层唯一约束、"
                   "语义层回执重放”三层实现；审计、指标与告警均保留修订历史，业务接口不提供修改历史记录的路径；"
                   "数据库文件启用外键与 WAL。")

    heading(doc, "2.6 运行环境与性能要求", 2)
    body_para(doc, "运行环境为 CPython 3.12.14、Windows AMD64，主要依赖锁定为 scikit-learn 1.9.1、NumPy 2.5.4、"
                   "FastAPI 0.162.0、SQLAlchemy 2.0.57、httpx 0.28.1、Streamlit 1.66.1。"
                   "评分与监测分进程运行，监测采用 60 秒非重叠窗口并在窗口结束后保留 10 秒宽限。")
    body_para(doc, "查询接口默认每页 20 条、单页最大 100 条，页面每页 30 条；审计检索必须限定时间范围。"
                   "监测为异步计算，指标在窗口闭合后约 70 秒稳定，不占用评分请求的响应时间。"
                   "这些是当前配置约束，性能指标应以实际测量记录为准，不以设计目标冒充实测结果。")


def sec_asr(doc):
    body_para(doc, "ASR 由质量属性场景推导。下表中 ASR-01 至 ASR-03 直接决定了系统的进程划分与数据模型，"
                   "ASR-04 至 ASR-07 约束了授权、性能、时效与可扩展性。验证方式一列描述的是验收方法，"
                   "其中尚未执行的项在实验报告中标注为“待验证”。")
    table(doc,
          ["编号", "场景", "响应目标", "设计决策", "验证方式"],
          [["ASR-01", "监测进程停止，评分继续", "评分与审计能正常提交，事件保留，恢复后补算且不重复",
            "独立 Worker 进程 + 持久化 outbox + 租约与租约令牌", "停 Worker 后提交 100 条请求；恢复后核对消费次数"],
           ["ASR-02", "网络重试或进程中途中止", "重复请求返回原结果；重复事件不重复计数",
            "幂等键三层实现 + 业务唯一约束 + 事务", "同键重复十次；同键异内容返回 409；注入提交前异常验证回滚"],
           ["ASR-03", "激活新版本后审计旧评分", "使用原模型包复现，误差不超过 1e-8 且判断一致",
            "模型包固定版本目录 + 清单摘要 + 环境清单；复现禁止取激活版本", "两版各抽十条评分，切换激活后复现"],
           ["ASR-04", "未登录、越权或越模型访问", "认证与授权在 API 层校验，分别返回 401/403 并留痕",
            "服务端 Principal + 角色与模型范围双重校验", "直接 HTTP 调用覆盖未登录、无角色、无模型权限"],
           ["ASR-05", "五个并发客户端持续评分", "预热后端到端 P95 不超过 500 ms，服务错误率低于 1%",
            "模型常驻内存 + 单事务写入 + 索引", "不少于 500 次请求，记录 P50/P95/最大值与失败数"],
           ["ASR-06", "窗口闭合且样本充足", "窗口闭合后 15 秒内生成指标与告警",
            "60 秒窗口 + 10 秒宽限 + 5 秒轮询", "记录事件完成时间与告警时间"],
           ["ASR-07", "新增指标或调整阈值", "不改评分接口；旧告警保留原阈值与规则版本",
            "指标统一接口 + 规则版本化 + 窗口冻结规则快照", "新增预测 PSI 规则，查询旧告警核对证据"]],
          widths=[2.0, 2.9, 3.4, 3.4, 2.7], fs=9.5,
          caption_text="表 3-1 影响架构的需求列表（ASR）")
    body_para(doc, "从表中可以看出，ASR-01 导致了“评分进程与监测进程分离 + outbox”这一结构决策，"
                   "ASR-02 导致了幂等回执表与唯一约束，ASR-03 导致了“模型包不进数据库、按版本目录固定”的存储形态。"
                   "这三条是本系统与普通 CRUD 应用最本质的差别。")


def sec_architecture(doc):
    body_para(doc, "本章给出五类架构图。业务、逻辑架构与一级数据流图由成员 A 绘制，数据、技术与部署架构由成员 B 绘制；"
                   "为适应 A4 纸张打印，业务架构图与一级数据流图在不改变内容与编号的前提下重新排版。")
    heading(doc, "4.1 业务架构", 2)
    picture(doc, "B-业务架构图.png", "图 4-1 业务架构图")
    body_para(doc, "业务按六个阶段组织为一条价值链：模型接入、受控激活、评分为证、风险监测、告警处置、审计追溯。"
                   "每个阶段左侧标注主要承担角色。前五个阶段已由原型实现；第六阶段（审计追溯）的后端由成员 B 实现，"
                   "查询与复现接口可用，页面待接入。这一划分的依据是“一次评分的证据链在哪里产生、在哪里被使用”，"
                   "而不是按页面或按技术分层。")

    heading(doc, "4.2 数据架构", 2)
    picture(doc, "B1-data-architecture.png", "图 4-2 数据架构图")
    body_para(doc, "数据按写入者划分为四个域。模型与版本域由启动登记与激活命令写入，版本永久保留、激活指针可切换；"
                   "评分证据域由评分事务写入，只追加不修改，包含输入快照、评分、审计与完成事件；"
                   "监测与告警域由独立 Worker 通过两段事务写入，按窗口保留修订历史；"
                   "身份与审计域由成员 B 实现，包含用户、角色、模型授权、复现任务与导出记录。")
    body_para(doc, "三个跨域规则决定了数据流：模型域只被引用不被修改；评分域到监测域只通过 outbox 传递，"
                   "Worker 不扫描评分表；监测域不复制原始事实，需要输入时按评分编号回查快照。"
                   "这些规则使评分请求路径与监测计算路径互不阻塞。")

    heading(doc, "4.3 逻辑架构", 2)
    picture(doc, "A5-logical.png", "图 4-3 逻辑架构图")
    body_para(doc, "逻辑架构分为六层：表现层（Streamlit 三个页面）、接口层（FastAPI 路由与校验）、"
                   "应用服务层（评分、监测、告警命令）、领域逻辑（特征规范化、推理与解释、窗口统计、规则求值）、"
                   "适配器边界（PredictionStore 与 Authenticator 协议）以及资源层（SQLite 与模型包目录）。"
                   "领域逻辑层不依赖数据库与 Web 框架，因此统计口径可以在脱离数据库的情况下单独验证。")
    body_para(doc, "两个适配器协议是成员 B 的接入点：替换认证实现与存储实现不需要改动评分、监测与统计代码。"
                   "当前身份由服务端配置的静态令牌提供，存储为 SQLite 参考实现。")

    heading(doc, "4.4 技术架构", 2)
    picture(doc, "B5-technical.png", "图 4-4 技术架构图", width=Cm(11.4))
    body_para(doc, "技术选型遵循“不引入课程原型无法承担的运维组件”这一约束：Web 接口用 FastAPI + Pydantic，"
                   "数据访问用 SQLAlchemy Core，数据库用 SQLite（WAL），模型运行时用 scikit-learn Pipeline + joblib，"
                   "页面用 Streamlit。消息队列、缓存、独立模型服务与前端构建链均未引入，理由在技术架构文档中逐条说明。")
    body_para(doc, "关键设计决策包括：评分与监测分进程、以 outbox 连接（TD-01）；模型包固定版本目录且先校验后反序列化"
                   "（TD-02、TD-03）；幂等键与回执表（TD-04）；窗口冻结规则快照（TD-06）；指标与告警保留修订历史（TD-07）。"
                   "每项决策的替代方案与代价见技术架构文档。")

    heading(doc, "4.5 部署架构", 2)
    picture(doc, "B5-deployment.png", "图 4-5 系统部署架构图")
    body_para(doc, "部署形态为单机三进程加共享文件：API 进程（127.0.0.1:8000）、监测 Worker 进程、"
                   "页面进程（127.0.0.1:8501），三者共享同一个 SQLite 文件与模型包目录。所有服务只绑定回环地址。")
    body_para(doc, "三个进程各自独立启动、独立崩溃、独立重启。Worker 停机只影响指标更新，评分照常提交；"
                   "API 停机则评分与查询中断，但已提交事务不受影响；数据库文件被占用时在 5 秒超时内重试，"
                   "超时返回 503 并允许用同一幂等键重试。单机部署不具备高可用能力，进程崩溃期间服务不可用，"
                   "这一点在报告中如实写明。")

    heading(doc, "4.6 架构决策与需求的对应", 2)
    table(doc,
          ["架构决策", "支撑的需求 / ASR", "落地位置"],
          [["评分与监测分进程，以 outbox 连接", "FR05、ASR-01", "app/worker.py、outbox_events"],
           ["模型包固定版本目录，先校验后加载", "FR02、FR03、ASR-03", "artifacts/、app/modules/models/bundle.py"],
           ["幂等键三层实现", "FR03、FR04、ASR-02", "contracts.py、各 *_receipts 表"],
           ["窗口冻结规则快照", "FR06、FR07、FR08", "monitor_windows.rule_snapshot"],
           ["指标与告警保留修订历史", "FR06、FR07", "metrics / metric_revisions、alerts / alert_revisions"],
           ["服务端授权，角色不入令牌", "FR09、ASR-04", "app/main.py、app/demo_auth.py（B 将替换）"],
           ["只追加的审计记录", "FR03—FR09", "audit_logs、存储层不提供更新方法"]],
          widths=[4.6, 3.9, 5.7], fs=9,
          caption_text="表 4-1 架构决策与需求对应")
    body_para(doc, "最后说明本报告的边界：原型已验证评分留痕、监测计算、告警处置与幂等恢复等核心链路，"
                   "审计与复现页面尚未实现，其余需求均有实现与自动化测试；性能指标为待测目标，"
                   "未执行的验证在验收材料中标注为“待验证”，不以设计目标替代实测结果。")


# ---------- 组装 -----------------------------------------------------
def build():
    doc = Document(TPL)
    fill_cover(doc)
    fill_division_table(doc)

    paras = doc.paragraphs
    title_p = next(p for p in paras if p.text.strip() == "课题名称")
    replace_text(title_p, "金融 AI 模型风险监测与审计平台")

    anchors = {}
    for p in doc.paragraphs:
        t = p.text.strip()
        if t.startswith("一、选题背景"):
            anchors["一"] = p
        elif t.startswith("二、需求分析"):
            anchors["二"] = p
        elif t.startswith("三、ASR"):
            anchors["三"] = p
        elif t.startswith("四、架构图"):
            anchors["四"] = p

    sections = [
        ("一", sec_background, "二"),
        ("二", sec_requirements, "三"),
        ("三", sec_asr, "四"),
        ("四", sec_architecture, None),
    ]
    # 删除模板中的说明性段落
    for p in list(doc.paragraphs):
        t = p.text.strip()
        if t.startswith("说明本课题") or t.startswith("包括：用户的基本需求") \
                or t.startswith("根据软件需求分析说明书") or t.startswith("画出软件系统业务架构"):
            p._p.getparent().remove(p._p)

    for key, fn, nxt in sections:
        add_section(doc, fn, anchors[nxt] if nxt else None)

    doc.save(OUT)
    print("saved", OUT)


if __name__ == "__main__":
    build()
