"""Model-generated opening chips for empty conversations (profile-aware).

One lightweight LLM call turns the study profile (+ library titles) into
1-3 concrete chips — e.g. goal「求职Agent工程师」→ draft chip
「深度调研：使用率较高的Agent开发框架」. Best-effort by design: any
failure returns [] and the frontend silently keeps its instant local chips.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你为大学生学习助手生成空会话的开场建议（chips）。"
    "只输出一个 JSON 对象，禁止 markdown 代码块、禁止解释文字。"
)


def build_chips_messages(
    profile: dict[str, str],
    note_titles: list[str],
    kb_titles: list[str],
) -> list[dict[str, str]]:
    """Build (system, user) messages grounding the model in profile + library."""
    lines: list[str] = []
    if profile.get("grade"):
        lines.append(f"年级：{profile['grade']}")
    if profile.get("major"):
        lines.append(f"专业：{profile['major']}")
    if profile.get("goal"):
        lines.append(f"目标：{profile['goal']}")
    if profile.get("note"):
        lines.append(f"补充：{profile['note']}")
    profile_block = (
        "用户画像：\n" + "\n".join(f"- {x}" for x in lines) + "\n\n" if lines else ""
    )

    # 目标轨道（考研/考公/求职…）来自 TrackSpec.chip_hints —— 建了轨道就该让
    # 开场建议贴合该场景，否则用户建了「2027 考研」看到的还是通用建议。
    hints = profile.get("track_hints")
    track_block = ""
    if profile.get("track"):
        track_block = f"用户当前目标轨道：{profile['track']}\n"
        if isinstance(hints, list) and hints:
            track_block += (
                "该场景值得参考的方向（据此改写得更具体，不要照抄）："
                + "、".join(str(h) for h in hints)
                + "\n"
            )
        track_block += "\n"

    library: list[str] = []
    if note_titles:
        library.append("笔记：" + "、".join(f"《{t}》" for t in note_titles))
    if kb_titles:
        library.append("知识库：" + "、".join(f"《{t}》" for t in kb_titles))
    library_block = (
        "学习库（真实存在的材料，禁止编造其他材料名）：\n"
        + "\n".join(f"- {x}" for x in library)
        + "\n\n"
        if library
        else "学习库暂空。\n\n"
    )

    user = (
        f"{profile_block}{track_block}{library_block}"
        "请按以下构成生成开场建议（chips）：\n"
        "1. 第 1 条「深度调研」：把用户的目标/专业转化为一个当下值得调研的具体主题，"
        "放在 draft 字段，文本以「深度调研：」开头——"
        "例如目标为「求职Agent工程师」→「深度调研：使用率较高的Agent开发框架」。\n"
        "2. 第 2 条画像相关的个性化请求：message 字段，用户视角的一句话，"
        "贴合专业/年级/目标/补充说明，具体不空泛，不要与第 1 条主题重复。\n"
        "3. 第 3 条（可选）：仅当学习库中有材料与用户画像明显相关时生成——"
        "把该材料标题与画像目标结合起来（例：目标求职后端 + 笔记《Redis持久化》→"
        "「结合《Redis持久化》帮我整理后端面试高频考点」）；没有明显关联就省略这条。\n"
        "4. label 是按钮短语（不超过 18 字）；draft/message 是用户视角的完整文本；"
        "禁止编造学习库中不存在的材料名；画像为空时输出空数组。\n"
        '输出格式：{"chips": ['
        '{"label": "调研主题短语", "draft": "深度调研：…"}, '
        '{"label": "建议短语", "message": "…"}, '
        '{"label": "关联短语", "message": "…"}]}'
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


def _parse_chips(raw: str) -> list[dict[str, str]]:
    """Tolerant parse + light validation; keep at most 3 sane chips."""
    cleaned = re.sub(r"```(?:json)?", "", raw or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []
    raw_chips = data.get("chips")
    if not isinstance(raw_chips, list):
        return []
    chips: list[dict[str, str]] = []
    for item in raw_chips[:3]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:24]
        draft = str(item.get("draft") or "").strip()[:200]
        message = str(item.get("message") or "").strip()[:200]
        if not label:
            continue
        if draft:
            chips.append({"label": label, "draft": draft})
        elif message:
            chips.append({"label": label, "message": message})
    return chips


def generate_smart_chips(
    profile: dict[str, str],
    note_titles: list[str],
    kb_titles: list[str],
) -> list[dict[str, str]]:
    """One LLM call, best-effort; returns [] on any failure."""
    from agent_assistant.llm.client import llm_client

    try:
        response = llm_client.chat(
            messages=build_chips_messages(profile, note_titles, kb_titles),
            temperature=0.8,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001 — chips are cosmetic, never fatal
        logger.warning("start-chips generation failed: %s", e)
        return []
    return _parse_chips(raw)


__all__ = ["build_chips_messages", "generate_smart_chips"]
