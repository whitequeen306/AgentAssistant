import { useState } from "react";
import { Plus, X } from "lucide-react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui";
import { Select } from "@/components/ui/Select";
import { Input } from "@/components/ui";
import { useStore } from "@/lib/store";
import { saveScene } from "@/lib/scenes";
import { paramLabel, toolLabel } from "@/lib/toolLabels";
import type { Scene, SceneAction } from "@/types";

export function SceneEditor({
  scene,
  open,
  onOpenChange,
}: {
  scene: Scene | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const tools = useStore((s) => s.tools);
  const [name, setName] = useState(scene?.name || "");
  const [triggers, setTriggers] = useState((scene?.trigger_phrases || []).join(", "));
  const [actions, setActions] = useState<SceneAction[]>(
    scene?.actions ? scene.actions.map((a) => ({ ...a, args: { ...a.args } })) : [],
  );

  const addAction = () =>
    setActions((a) => [...a, { tool: tools[0]?.name || "", args: {} }]);
  const removeAction = (i: number) => setActions((a) => a.filter((_, idx) => idx !== i));
  const updateAction = (i: number, patch: Partial<SceneAction>) =>
    setActions((a) => a.map((act, idx) => (idx === i ? { ...act, ...patch } : act)));

  const validateActions = (): string | null => {
    if (!actions.length) return "请至少添加一个动作";
    for (let i = 0; i < actions.length; i++) {
      const act = actions[i];
      const tool = tools.find((t) => t.name === act.tool);
      const label = toolLabel(act.tool, tool?.label);
      if (!act.tool) return `第 ${i + 1} 个动作未选择工具`;
      for (const p of tool?.parameters || []) {
        if (!p.required) continue;
        const v = (act.args[p.name] || "").trim();
        if (!v) {
          const pl = paramLabel(p.name, p.label);
          return `「${label}」缺少必填项「${pl}」——例如打开应用需填写应用名（如「逆战」）`;
        }
      }
    }
    return null;
  };

  const save = async () => {
    if (!name.trim()) {
      alert("请填写场景名称");
      return;
    }
    const err = validateActions();
    if (err) {
      alert(err);
      return;
    }
    const trig = triggers.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    await saveScene({
      id: scene?.id,
      name: name.trim(),
      trigger_phrases: trig,
      actions,
    });
    onOpenChange(false);
  };

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title={scene ? "编辑场景" : "添加场景"}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={save}>保存</Button>
        </>
      }
    >
      <label className="flex flex-col gap-1 text-sm text-secondary">
        <span>名称</span>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：游戏模式" />
      </label>
      <label className="flex flex-col gap-1 text-sm text-secondary">
        <span>触发词（逗号分隔）</span>
        <Input
          value={triggers}
          onChange={(e) => setTriggers(e.target.value)}
          placeholder="游戏时间, 开逆战"
        />
      </label>
      <div className="flex flex-col gap-1 text-sm text-secondary">
        <span>动作序列</span>
        <div className="flex flex-col gap-2">
          {actions.map((act, i) => {
            const tool = tools.find((t) => t.name === act.tool);
            return (
              <div key={i} className="rounded-sm border border-border p-2">
                <div className="flex items-center gap-2">
                  <Select
                    value={act.tool}
                    onValueChange={(v) => updateAction(i, { tool: v, args: {} })}
                    options={tools.map((t) => ({
                      value: t.name,
                      label: `${toolLabel(t.name, t.label)}${t.requires_confirm ? " ⚠" : ""}`,
                    }))}
                    className="flex-1 min-w-0"
                  />
                  <button
                    onClick={() => removeAction(i)}
                    className="inline-flex h-7 w-7 items-center justify-center rounded-sm text-tertiary hover:bg-surface-elevated hover:text-error"
                    aria-label="删除动作"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <div className="mt-2 flex flex-col gap-1">
                  {(tool?.parameters || []).map((p) => (
                    <div key={p.name} className="flex items-center gap-2 text-xs">
                      <span
                        className="w-24 shrink-0 truncate text-tertiary"
                        title={p.description || p.name}
                      >
                        {paramLabel(p.name, p.label)}
                        {p.required ? " *" : ""}
                      </span>
                      <Input
                        value={act.args[p.name] || ""}
                        onChange={(e) =>
                          updateAction(i, { args: { ...act.args, [p.name]: e.target.value } })
                        }
                        placeholder={
                          act.tool === "launch_app" && p.name === "name"
                            ? "如：逆战 / 网易云音乐 / Chrome"
                            : p.description || p.type
                        }
                        className="h-[26px] flex-1 text-xs"
                      />
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          <Button variant="ghost" onClick={addAction} className="self-start">
            <Plus className="h-4 w-4" />
            添加动作
          </Button>
        </div>
      </div>
    </Modal>
  );
}
