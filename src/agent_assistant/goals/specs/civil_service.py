"""考公（公务员 / 事业单位）目标轨道配置 —— 骨架占位。

``draft=True``：字段与节点已就位，调研模板仍是通用版（未针对国考/省考/
事业编的差异细化）。UI 应标灰并提示"尚未完善"，暂不建议用户启用。
"""

from __future__ import annotations

from agent_assistant.goals.models import Milestone, TrackKind, TrackSpec

RESEARCH_TEMPLATE = """\
本次调研属于【考公选岗】场景（该轨道的输出模板仍在完善，结构以本节为准）。

输出 Markdown 对比表，列顺序固定为：
| 招录单位 | 岗位(代码) | 考试类型 | 专业要求 | 学历要求 | 政治面貌 | 基层工作年限 | 招录人数 | 历年进面分 | 工作地点 |
待比较的目标共 {count} 个，一行一个，禁止省略列。

硬性要求：
- 每格后附来源链接；查不到写「未获取到」，禁止推测；
- 专业要求、政治面貌、基层经历直接决定能否报考，来源冲突时必须显式标注，禁止静默取值；
- 所有数据标明所属年度与考试批次（国考 / 省考 / 事业编）。
"""

CIVIL_SERVICE = TrackSpec(
    kind=TrackKind.CIVIL_SERVICE,
    label="考公",
    research_template=RESEARCH_TEMPLATE,
    output_fields=(
        "考试类型",
        "招录单位",
        "岗位代码",
        "专业要求",
        "学历要求",
        "政治面貌",
        "基层工作年限",
        "招录人数",
        "历年进面分",
        "工作地点",
    ),
    milestones=(
        Milestone(key="announce", label="招考公告发布", month=10, day=14, year_offset=-1,
                  note="国考通常 10 月中旬；省考各省差异大", remind_before_days=3),
        Milestone(key="signup", label="报名", month=10, day=15, year_offset=-1,
                  note="国考报名窗口约 10 天，逾期不可补报", remind_before_days=5),
        Milestone(key="written", label="笔试", month=11, day=29, year_offset=-1,
                  note="国考通常 11 月底或 12 月初", remind_before_days=14),
        Milestone(key="score", label="笔试成绩公布", month=1, day=10, year_offset=0,
                  note="通常次年 1 月上旬", remind_before_days=2),
        Milestone(key="interview", label="面试", month=3, day=1, year_offset=0,
                  note="各部门时间不一，通常次年 2-4 月", remind_before_days=7),
    ),
    prompt_hint=(
        "用户正在准备公务员或事业单位考试，常见话题包括岗位筛选与报考条件核对、"
        "行测申论备考、进面分数、面试形式。报考条件（专业、学历、政治面貌、基层经历）"
        "必须逐项核对官方职位表，不得给出确定性结论。"
    ),
    chip_hints=(
        "核对我的条件能报哪些岗位",
        "对比目标岗位历年进面分",
        "整理行测各模块提分策略",
    ),
    draft=True,
)

__all__ = ["CIVIL_SERVICE", "RESEARCH_TEMPLATE"]
