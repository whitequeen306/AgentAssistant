import { forwardRef } from "react";
import * as RSelect from "@radix-ui/react-select";
import { ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/cn";

export interface SelectOption {
  value: string;
  label: string;
}

/** Native-feeling dropdown select built on Radix Select. */
export const Select = forwardRef<
  HTMLButtonElement,
  {
    value: string;
    onValueChange: (v: string) => void;
    options: SelectOption[];
    placeholder?: string;
    className?: string;
    "aria-label"?: string;
  }
>(({ value, onValueChange, options, placeholder, className, ...rest }, _ref) => (
  <RSelect.Root value={value} onValueChange={onValueChange}>
    <RSelect.Trigger
      className={cn(
        "inline-flex h-9 min-w-[160px] items-center justify-between gap-2 rounded-md border border-border bg-surface-sunken px-3 text-sm text-primary outline-none transition-[border-color,box-shadow] hover:bg-surface-elevated focus:border-accent focus:shadow-[var(--shadow-focus)] data-[placeholder]:text-tertiary",
        className,
      )}
      {...(rest as object)}
    >
      <RSelect.Value placeholder={placeholder} />
      <ChevronDown className="h-4 w-4 text-tertiary" />
    </RSelect.Trigger>
    <RSelect.Portal>
      <RSelect.Content
        position="popper"
        sideOffset={6}
        className="glass-panel-strong z-[100] max-h-60 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-md"
      >
        <RSelect.Viewport className="p-1">
          {options.map((o) => (
            <RSelect.Item
              key={o.value}
              value={o.value}
              className="relative flex cursor-pointer select-none items-center rounded-sm px-8 py-1.5 text-sm text-primary outline-none data-[highlighted]:bg-accent-soft data-[state=checked]:text-accent-strong data-[state=checked]:font-medium"
            >
              <RSelect.ItemIndicator className="absolute left-2">
                <Check className="h-3.5 w-3.5" />
              </RSelect.ItemIndicator>
              <RSelect.ItemText>{o.label}</RSelect.ItemText>
            </RSelect.Item>
          ))}
        </RSelect.Viewport>
      </RSelect.Content>
    </RSelect.Portal>
  </RSelect.Root>
));
Select.displayName = "Select";
