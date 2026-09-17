"""考研（研究生入学考试）目标轨道配置。

注册年份约定：``cycle_year`` 指**考研年份**（例：2027 考研 = 2026 年 12 月初试）。
因此初试及之前的节点 ``year_offset = -1``，出分及之后为 0。
"""

from __future__ import annotations

from agent_assistant.goals.models import Milestone, TrackKind, TrackSpec

RESEARCH_TEMPLATE = """\
本次调研属于【研究生择校】场景。最终报告必须严格按下面三段输出，不要自行改动结构。

## 〇、年度口径（最高优先，动手前先读完）
本次调研服务于 **{cycle_year} 考研**（报名在 {cycle_prev_year} 年 9–10 月，初试在 {cycle_prev_year} 年 12 月，{cycle_year} 年入学）。
- **优先查找 {cycle_year} 年招生目录 / 招生简章**（通常 {cycle_prev_year} 年 9 月起陆续发布）。已发布的必须以它为准。
- {cycle_year} 年目录尚未发布时，**必须显式写「{cycle_year} 年目录未发布，以下为 {cycle_prev_year} 年数据」**，再引用历史数据。
- 「近三年分数线」指 {cycle_prev_years} 三个招生年度。
- **禁止把历史年度的数据当作本年度数据呈现**——每个数字都必须带年度标签。
- 查询词里请**显式带上年份**（例如「哈尔滨工业大学{cycle_year}年硕士研究生招生专业目录」），不要用无年份的泛查询。

## 一、对比表
输出 Markdown 表格，列顺序固定为：
| 院校 | 专业(代码) | 院校层次 | 学科评估 | 招生人数(推免占比) | 初试科目(代码) | 参考书目 | 近三年复试线 | 复试权重 | 就业去向 |
待比较的目标共 {count} 个，一行一个，禁止合并单元格、禁止省略列。

## 二、关键差异解读
只挑真正影响决策的差异，优先这三类：
1. **初试科目代码差异**（例：408 计算机学科专业基础 vs 829 数据工程基础）——它直接决定复习计划，必须点明；
2. **推免占比过高**导致统考名额极少的风险；
3. **近三年分数线的波动方向与幅度**，以及它与招生人数变化的联动。
每一条都要写清"对谁有影响、影响多大"，不要复述表格里已有的数字。

## 三、风险与数据缺口
- 任何字段在多个来源之间不一致时，**必须显式标注冲突并列出两个来源**，禁止静默取其中一个；
- 查不到的字段写「未获取到」，**严禁推测、严禁补全**；
- 列出本次调研未能覆盖、但会影响决策的信息（如复试差额比、导师方向、是否歧视本科出身）。

硬性要求：
- 对比表每一格后面必须附来源链接（行内 Markdown 链接），无来源的格子写「未获取到」；
- 来源必须是院校研究生院 / 研招网等官方页面或权威媒体；社交平台经验贴只能作为补充且须显式标注；
- 所有日期、分数线、招生人数必须标明所属年度（例：2026 年招生目录）。
"""

POSTGRAD = TrackSpec(
    kind=TrackKind.POSTGRAD,
    label="考研",
    research_template=RESEARCH_TEMPLATE,
    output_fields=(
        "院校层次",
        "学科评估",
        "招生人数(含推免占比)",
        "初试科目代码",
        "参考书目",
        "近三年复试线",
        "复试权重",
        "就业去向",
    ),
    milestones=(
        Milestone(
            key="pre_signup",
            label="预报名",
            month=9,
            day=24,
            year_offset=-1,
            note="通常 9 月下旬，以研招网当年公告为准",
            remind_before_days=3,
        ),
        Milestone(
            key="signup",
            label="正式报名",
            month=10,
            day=8,
            year_offset=-1,
            note="通常 10 月中下旬，逾期不可补报",
            remind_before_days=7,
        ),
        Milestone(
            key="confirm",
            label="网上确认",
            month=11,
            day=5,
            year_offset=-1,
            note="各省时间不同，以报考点公告为准",
            remind_before_days=3,
        ),
        Milestone(
            key="exam",
            label="初试",
            month=12,
            day=19,
            year_offset=-1,
            note="通常 12 月第三个周末",
            remind_before_days=14,
        ),
        Milestone(
            key="score",
            label="初试成绩公布",
            month=2,
            day=26,
            year_offset=0,
            note="通常 2 月下旬",
            remind_before_days=2,
        ),
        Milestone(
            key="national",
            label="国家线公布",
            month=3,
            day=12,
            year_offset=0,
            note="通常 3 月中旬，随后各校公布复试线",
            remind_before_days=3,
        ),
        Milestone(
            key="adjust",
            label="调剂系统开放",
            month=4,
            day=8,
            year_offset=0,
            note="调剂意向采集通常早于此一周，务必提前联系",
            remind_before_days=7,
        ),
    ),
    prompt_hint=(
        "用户正在准备研究生入学考试，常见话题包括择校对比、初试科目与参考书目、"
        "分数线与报录比、复试准备、调剂窗口期。涉及这些信息时必须标明年度与来源，"
        "数据冲突时提示人工核验，不要给出确定性的唯一答案。"
    ),
    chip_hints=(
        "对比我的目标院校近三年复试线",
        "解读目标专业的初试科目代码差异",
        "整理目标院校参考书目清单",
    ),
    draft=False,
)

__all__ = ["POSTGRAD", "RESEARCH_TEMPLATE"]
