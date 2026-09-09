import { cn } from "@/lib/cn";
import { useStore } from "@/lib/store";

/** 8px status dot: idle gray, thinking amber pulse, listening green pulse. */
export function StatusDot({ className }: { className?: string }) {
  const voice = useStore((s) => s.voiceState);
  const thinking = useStore((s) => s.thinking);
  const listening = voice === "listening";
  const active = thinking && !listening;
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 shrink-0 rounded-full transition-colors duration-[var(--duration-normal)]",
        listening &&
          "bg-[var(--color-dot-listening)] animate-[pulse_0.7s_ease-in-out_infinite]",
        active &&
          "bg-[var(--color-dot-thinking)] animate-[pulse_1.2s_ease-in-out_infinite]",
        !listening && !active && "bg-[var(--color-dot-idle)]",
        className,
      )}
      aria-label={`status: ${listening ? "listening" : active ? "thinking" : "idle"}`}
    />
  );
}
