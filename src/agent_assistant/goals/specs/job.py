"""求职（校招 / 实习）目标轨道配置 —— 骨架占位。

``draft=True``：字段与节点已就位，调研模板仍是通用版。UI 应标灰并提示"尚未完善"。
"""

from __future__ import annotations

from agent_assistant.goals.models import Milestone, TrackKind, TrackSpec

RESEARCH_TEMPLATE = """\
本次调研属于【求职选岗】场景（该轨道的输出模板仍在完善，结构以本节为准）。

输出 Markdown 对比表，列顺序固定为：
| 公司 | 岗位 | 技术栈要求 | 学历要求 | 薪资区间 | 招聘批次 | 笔试形式 | 面试轮次 | 投递截止 |
待比较的目标共 {count} 个，一行一个，禁止省略列。

硬性要求：
- 每格后附来源链接；查不到写「未获取到」，禁止推测；
- 薪资区间须注明来源口径（OfferShow / 脉脉 / 官方 JD），并标注统计样本量；
- 投递截止时间是硬约束，来源冲突时取**最早**的那个并标注冲突。
"""

JOB = TrackSpec(
    kind=TrackKind.JOB,
    label="求职",
    research_template=RESEARCH_TEMPLATE,
    output_fields=(
        "公司",
        "岗位",
        "技术栈要求",
        "学历要求",
        "薪资区间",
        "招聘批次",
        "笔试形式",
        "面试轮次",
        "投递截止",
    ),
    milestones=(
        Milestone(key="autumn_open", label="秋招开启", month=8, day=1, year_offset=-1,
                  note="提前批通常 7 月即开始", remind_before_days=7),
        Milestone(key="autumn_peak", label="网申高峰", month=9, day=15, year_offset=-1,
                  note="多数大厂 9-10 月截止网申", remind_before_days=7),
        Milestone(key="written", label="笔试 / 在线测评", month=10, day=1, year_offset=-1,
                  note="批次多、窗口短，注意邮件与短信", remind_before_days=3),
        Milestone(key="interview", label="面试集中期", month=10, day=20, year_offset=-1,
                  note="通常 2-4 轮，周期 2-6 周", remind_before_days=3),
        Milestone(key="offer", label="Offer 集中发放", month=11, day=20, year_offset=-1,
                  note="注意意向书与三方的截止时间", remind_before_days=5),
        Milestone(key="spring", label="春招补录", month=3, day=1, year_offset=0,
                  note="规模远小于秋招", remind_before_days=7),
    ),
    prompt_hint=(
        "用户正在准备校园招聘或实习求职，常见话题包括岗位匹配度分析、技术栈查漏补缺、"
        "笔试面试准备、简历与项目包装、Offer 比较。涉及薪资与招聘时间的信息必须标注来源"
        "与统计口径，不要给出未经核实的唯一结论。"
    ),
    chip_hints=(
        "对比目标岗位的技术栈要求",
        "按我的简历找出能力缺口",
        "整理目标公司面试轮次与时间线",
    ),
    draft=True,
)

__all__ = ["JOB", "RESEARCH_TEMPLATE"]
