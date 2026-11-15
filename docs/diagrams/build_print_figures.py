# -*- coding: utf-8 -*-
"""生成报告用的打印版架构图（SVG），供 Word 报告引用。

A5 的 .mmd 源文件在 GitHub 网页上横向滚动阅读正常，但按 A4 纵向印出时
业务架构图与一级数据流图的文字只有约 3—4pt。本脚本按相同的节点与数据流
重新排版，只调整布局分组，不改任何业务内容或编号。

用法：python build_print_figures.py
然后用浏览器或矢量工具把同目录下的 .svg 转成 PNG。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figlib import SVG, write

HERE = os.path.dirname(os.path.abspath(__file__))

BUSINESS_ROWS = [
    ("模型开发人员", "① 模型接入", "离线训练与锁定版本 · 模型包验证与登记", False),
    ("管理员", "② 受控激活", "激活受信任版本", False),
    ("模拟评分调用方", "③ 评分为证", "评分与解释快照 · 评分 审计 Outbox 同事务留存", False),
    ("风险管理人员", "④ 风险监测", "配置未来规则版本 · 独立监测与窗口结算 · 生成或修订告警证据", False),
    ("风险管理人员", "⑤ 告警处置", "认领 核查 说明后关闭 · 保留修订与处置历史", False),
    ("审计人员", "⑥ 审计追溯", "正式审计查询与复现", False),
]


def business():
    W, ROW_H, GAP, TOP = 900, 88, 30, 18
    H = TOP * 2 + len(BUSINESS_ROWS) * ROW_H + (len(BUSINESS_ROWS) - 1) * GAP
    s = SVG(W, H)
    AX, AW, BX = 16, 122, 150
    BW = W - BX - 16
    for i, (actor, stage, acts, dashed) in enumerate(BUSINESS_ROWS):
        y = TOP + i * (ROW_H + GAP)
        s.actor(AX, y, AW, ROW_H, [actor], fs=15)
        s.rect(BX, y, BW, ROW_H, (), fill="#f8fafc" if not dashed else "#fdfaf5",
               stroke="#475569" if not dashed else "#b45309", rx=8,
               dash="7,5" if dashed else None)
        s.parts.append(f'<text x="{BX+20}" y="{y+ROW_H/2+8}" font-family="Microsoft YaHei, SimHei, sans-serif" '
                       f'font-size="20" font-weight="600" fill="#0f172a">{stage}</text>')
        s.parts.append(f'<text x="{BX+140}" y="{y+ROW_H/2+7}" font-family="Microsoft YaHei, SimHei, sans-serif" '
                       f'font-size="17" fill="#334155">{acts}</text>')
        if i < len(BUSINESS_ROWS) - 1:
            cx = BX + BW / 2
            s.arrow([(cx, y + ROW_H), (cx, y + ROW_H + GAP - 3)])
    write(s, os.path.join(HERE, "B-业务架构图.svg"))
    return W, H


def dfd_scoring():
    W, H = 900, 400
    s = SVG(W, H)
    E1x, P1x, D2x = 40, 330, 610
    s.actor(E1x, 50, 140, 60, ["模拟评分调用方"])
    s.process(P1x, 50, 160, 60, ["1.0 校验并评分"])
    s.store(D2x, 48, 160, 64, ["D2 评分证据与 Outbox"])
    s.store(E1x, 250, 160, 64, ["D1 版本索引与模型包"])
    s.process(P1x, 250, 160, 60, ["6.0 验证并激活版本"])
    s.actor(D2x, 250, 140, 60, ["管理员"])
    s.arrow([(180, 72), (330, 72)], "版本 ID 与合成特征", ly=62)
    s.arrow([(330, 96), (180, 96)], "概率 决策 解释与记录 ID", ly=116)
    s.arrow([(150, 250), (400, 112)], "阈值 模型 参数", lx=250, ly=170)
    s.arrow([(490, 72), (610, 72)], ["同事务评分快照", "审计 完成事件"], ly=44)
    s.arrow([(610, 268), (490, 268)], "激活版本 ID", ly=258)
    s.arrow([(490, 292), (610, 292)], "激活结果", ly=314)
    s.arrow([(200, 272), (330, 272)], "受信任包摘要与状态", ly=262)
    s.arrow([(330, 296), (200, 296)], "激活指针与审计回执", ly=318)
    write(s, os.path.join(HERE, "B-DFD-评分与激活.svg"))
    return W, H


def dfd_monitoring():
    W, H = 900, 690
    s = SVG(W, H)
    s.store(40, 50, 160, 64, ["D2 评分证据与 Outbox"])
    s.process(310, 50, 160, 60, ["2.0 消费并归入窗口"])
    s.store(580, 50, 160, 64, ["D3 窗口与检查点"])
    s.store(40, 250, 160, 64, ["D1 版本索引与模型包"])
    s.process(310, 250, 160, 60, ["3.0 计算指标并求值"])
    s.store(580, 250, 160, 64, ["D4 规则版本"])
    s.process(580, 400, 160, 60, ["5.0 创建规则版本"])
    s.store(190, 400, 160, 64, ["D5 指标与告警历史"])
    s.process(190, 560, 160, 60, ["4.0 查询与处置告警"])
    s.actor(470, 560, 140, 60, ["风险管理人员"])
    s.arrow([(200, 74), (310, 74)], ["待消费事件", "及原始评分证据"], ly=30)
    s.arrow([(470, 74), (580, 74)], ["消费检查点", "窗口修订"], ly=30)
    s.arrow([(660, 114), (470, 250)], "待结算窗口与修订号", lx=590, ly=180)
    s.arrow([(120, 114), (320, 250)], "已消费样本", lx=150, ly=190)
    s.arrow([(200, 280), (310, 280)], "固定参考分布", ly=270)
    s.arrow([(580, 280), (470, 280)], "窗口开始时生效规则", ly=270)
    s.arrow([(390, 310), (280, 400)], "指标 告警与修订", lx=300, ly=360)
    s.arrow([(280, 464), (280, 560)], "指标 规则快照与历史", lx=288, ly=520, anchor="start")
    s.arrow([(262, 560), (262, 464)], "处置与操作留痕", lx=254, ly=520, anchor="end")
    s.arrow([(470, 575), (350, 575)], "查询条件及处置说明", ly=553)
    s.arrow([(350, 598), (470, 598)], ["窗口 样本数", "触发原因 状态"], ly=636)
    s.arrow([(575, 560), (640, 460)], "阈值与未来生效时间", lx=650, ly=520)
    s.arrow([(610, 560), (680, 460)], "规则版本与生效时间", lx=760, ly=500)
    s.arrow([(660, 400), (660, 314)], "不可覆盖规则版本", lx=668, ly=360, anchor="start")
    write(s, os.path.join(HERE, "B-DFD-监测与处置.svg"))
    return W, H


if __name__ == "__main__":
    for fn in (business, dfd_scoring, dfd_monitoring):
        print(fn.__name__, fn())
