import { DockMinimizeButton } from "@/components/DockMinimizeButton";
import { PageHeader } from "@/components/PageHeader";
import { useStore } from "@/lib/store";

export function AboutPage() {
  const version = useStore((s) => s.version);
  const model = useStore((s) => s.model);

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <PageHeader title="关于">
        <DockMinimizeButton />
      </PageHeader>
      <div className="flex flex-1 flex-col items-center justify-center gap-2.5 px-6 text-center">
        <div className="brand-mark flex h-16 w-16 items-center justify-center rounded-xl text-xl font-bold text-white shadow-[var(--shadow-lift)]">
          AA
        </div>
        <div>
          <h2 className="font-[family-name:var(--font-display)] text-xl font-semibold text-primary">
            AgentAssistant
          </h2>
          <p className="mt-0.5 text-sm text-secondary">
            v{version}
            {model ? ` · ${model}` : ""}
          </p>
        </div>
        <p className="text-sm text-secondary">Windows 桌面智能体助手 · Liquid Frost 玻璃拟态</p>
        <p className="text-xs text-tertiary">开源项目 · MIT License</p>
      </div>
    </section>
  );
}
