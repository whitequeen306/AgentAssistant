import { useEffect, useState } from "react";
import { Loader2, UserRoundPlus } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { Select } from "@/components/ui/Select";
import { Modal } from "@/components/ui/Modal";
import { getApi, prefetchStartChips } from "@/lib/bridge";
import { actions, useStore } from "@/lib/store";

const GRADES = ["", "大一", "大二", "大三", "大四", "研一", "研二", "研三"];
const GRADE_LABELS = ["未填写", "大一", "大二", "大三", "大四", "研一", "研二", "研三"];

/**
 * 学习画像弹窗（侧边栏入口）：专业/年级/目标/补充说明。
 * 保存进 ui settings 表 → 下一轮对话即时注入 SystemPrompt（背景参考）。
 */
export function StudyProfileDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const settings = useStore((s) => s.settings);
  const [major, setMajor] = useState("");
  const [grade, setGrade] = useState("");
  const [goal, setGoal] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setMajor(settings.profile_major || "");
    setGrade(settings.profile_grade || "");
    setGoal(settings.profile_goal || "");
    setNote(settings.profile_note || "");
  }, [open, settings]);

  const save = async () => {
    setSaving(true);
    try {
      const entries = [
        ["profile_major", major.trim()],
        ["profile_grade", grade],
        ["profile_goal", goal.trim()],
        ["profile_note", note.trim()],
      ] as const;
      // Store first (UI reflects immediately), then persist to the settings table.
      for (const [k, v] of entries) actions.setSetting(k, v);
      const api = getApi();
      await Promise.all(entries.map(([k, v]) => api?.save_setting(k, v)));
      // 画像变更 → 后台预生成开场 chips（防抖），新会话秒出。
      prefetchStartChips();
      onOpenChange(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={
        <span className="flex items-center gap-2">
          <UserRoundPlus className="h-5 w-5 text-accent" />
          补充一下你自己，让我更好地认识你
        </span>
      }
    >
      <p className="-mt-1 text-xs leading-relaxed text-tertiary">
        这些信息只会保存在你自己的电脑上，用于让助手更贴合你的专业和目标说话、出题、给建议；留空也不影响使用。
      </p>
      <div className="flex flex-col gap-3">
        <Field label="专业">
          <Input
            value={major}
            onChange={(e) => setMajor(e.target.value)}
            placeholder="例如：计算机科学与技术"
            className="w-full"
          />
        </Field>
        <Field label="年级">
          <Select
            value={grade}
            onValueChange={setGrade}
            options={GRADES.map((g, i) => ({ value: g, label: GRADE_LABELS[i] }))}
          />
        </Field>
        <Field label="目标">
          <Input
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="例如：考研（408 方向）/ 考公 / 求职后端开发"
            className="w-full"
          />
        </Field>
        <Field label="补充说明">
          <Input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="例如：二战，数学薄弱，喜欢费曼式讲解"
            className="w-full"
          />
        </Field>
      </div>
      <div className="mt-1 flex justify-end gap-2">
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          取消
        </Button>
        <Button onClick={() => void save()} disabled={saving}>
          {saving && <Loader2 className="h-4 w-4 animate-spin" />}
          保存
        </Button>
      </div>
    </Modal>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-secondary">{label}</span>
      {children}
    </label>
  );
}
