import { useEffect, useState } from "react";
import { Plus, Play, Pencil, Trash2, Zap } from "lucide-react";
import { Button } from "@/components/ui";
import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/ui/EmptyState";
import { SceneEditor } from "@/components/SceneEditor";
import { useStore } from "@/lib/store";
import { refreshScenes, deleteScene, testScene } from "@/lib/scenes";
import { toolLabel } from "@/lib/toolLabels";
import type { Scene } from "@/types";

export function ScenesPage() {
  const scenes = useStore((s) => s.scenes);
  const tools = useStore((s) => s.tools);
  const [editing, setEditing] = useState<Scene | null>(null);
  const [open, setOpen] = useState(false);

  const actionSummary = (sc: Scene) => {
    const acts = sc.actions || [];
    if (!acts.length) return "0 个动作";
    const names = acts.map((a) => {
      const t = tools.find((x) => x.name === a.tool);
      return toolLabel(a.tool, t?.label);
    });
    return names.join(" → ");
  };

  useEffect(() => {
    refreshScenes();
  }, []);

  const add = () => {
    setEditing(null);
    setOpen(true);
  };
  const edit = (sc: Scene) => {
    setEditing(sc);
    setOpen(true);
  };

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader title="场景模式">
        <Button variant="ghost" onClick={add} className="h-8 py-1">
          <Plus className="h-4 w-4" />
          添加场景
        </Button>
        <DockMinimizeButton />
      </PageHeader>
      <div className="flex-1 overflow-y-auto p-4">
        {scenes.length === 0 ? (
          <EmptyState
            icon={Zap}
            title="还没有场景"
            hint="场景可以把「触发词 → 一串动作」自动化。点击右上角「添加场景」创建一个。"
          />
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-3">
            {scenes.map((sc) => (
              <div
                key={sc.id}
                className="lift rounded-lg border border-border bg-surface-elevated p-3.5"
              >
                <div className="mb-1.5 flex items-center gap-2">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent-strong">
                    <Zap className="h-3.5 w-3.5" />
                  </span>
                  <span className="text-md font-semibold text-primary">{sc.name}</span>
                </div>
                <div className="mb-1 flex flex-wrap items-center gap-1">
                  <span className="text-xs text-tertiary">触发词</span>
                  {(sc.trigger_phrases || []).length ? (
                    (sc.trigger_phrases || []).map((p) => (
                      <span
                        key={p}
                        className="rounded-pill bg-surface-sunken px-2 py-0.5 text-xs text-secondary"
                      >
                        {p}
                      </span>
                    ))
                  ) : (
                    <span className="text-xs text-tertiary">（无）</span>
                  )}
                </div>
                <div className="mb-3 text-xs text-tertiary">动作：{actionSummary(sc)}</div>
                <div className="flex gap-2">
                  <Button variant="ghost" className="h-7 py-1" onClick={() => sc.id && testScene(sc.id)}>
                    <Play className="h-3.5 w-3.5" />
                    测试
                  </Button>
                  <Button variant="ghost" className="h-7 py-1" onClick={() => edit(sc)}>
                    <Pencil className="h-3.5 w-3.5" />
                    编辑
                  </Button>
                  <Button
                    variant="danger"
                    className="h-7 py-1"
                    onClick={async () => {
                      if (!sc.id || !confirm(`删除场景「${sc.name}」？`)) return;
                      await deleteScene(sc.id);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                    删除
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      {/* key forces re-init per target scene */}
      <SceneEditor key={editing?.id || "new"} scene={editing} open={open} onOpenChange={setOpen} />
    </section>
  );
}
