"""考公 / 求职 / 考研 轨道骨架。

每个 spec 文件只导出一个 ``TrackSpec`` 常量（大写同名），registry 负责汇总。
新增目标 = 新建一个 spec 文件 + 在 registry 里加一行，其余代码零改动。
"""

from agent_assistant.goals.specs.civil_service import CIVIL_SERVICE
from agent_assistant.goals.specs.job import JOB
from agent_assistant.goals.specs.postgrad import POSTGRAD

__all__ = ["CIVIL_SERVICE", "JOB", "POSTGRAD"]
