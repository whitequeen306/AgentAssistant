import * as Switch from "@radix-ui/react-switch";
import { cn } from "@/lib/cn";

/** Accessible on/off switch — iOS-style white thumb, accent track when on. */
export function Toggle({
  checked,
  onCheckedChange,
  disabled,
  "aria-label": ariaLabel,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
  "aria-label": string;
}) {
  return (
    <Switch.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      aria-label={ariaLabel}
      className={cn(
        "relative h-[22px] w-[38px] shrink-0 rounded-pill border border-border bg-[color-mix(in_srgb,var(--color-tertiary)_38%,transparent)] transition-colors duration-[var(--duration-fast)] data-[state=checked]:border-accent-strong data-[state=checked]:bg-accent-strong disabled:opacity-50",
      )}
    >
      <Switch.Thumb className="block h-[16px] w-[16px] translate-x-[2px] rounded-full bg-white shadow-[0_1px_3px_rgba(18,28,52,0.35)] transition-transform duration-[var(--duration-fast)] ease-[var(--ease-standard)] data-[state=checked]:translate-x-[18px]" />
    </Switch.Root>
  );
}
