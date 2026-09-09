import {
  forwardRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type TextareaHTMLAttributes,
  type HTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

type ButtonVariant = "primary" | "ghost" | "icon" | "danger";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

const buttonBase =
  "interactive-morph inline-flex items-center justify-center gap-2 font-medium focus-visible:outline-none disabled:opacity-40 disabled:pointer-events-none select-none";

const buttonVariants: Record<ButtonVariant, string> = {
  primary: cn(
    buttonBase,
    "bg-accent-strong text-accent-contrast rounded-md px-4 py-2 text-sm shadow-[var(--shadow-ambient)] hover:brightness-110",
  ),
  ghost: cn(
    buttonBase,
    "bg-transparent text-secondary border border-border rounded-md px-3 py-2 text-sm hover:bg-surface-elevated hover:text-primary",
  ),
  icon: cn(
    buttonBase,
    "bg-surface text-secondary rounded-sm p-1.5 hover:text-primary",
  ),
  danger: cn(
    buttonBase,
    "bg-transparent text-error border border-border rounded-md px-3 py-2 text-sm hover:bg-[var(--color-error-soft)]",
  ),
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants[variant], className)} {...props} />
  ),
);
Button.displayName = "Button";

/** Round icon button (send / voice / window chrome). */
export const IconButton = forwardRef<
  HTMLButtonElement,
  { variant?: "solid" | "ghost"; active?: boolean } & ButtonHTMLAttributes<HTMLButtonElement>
>(({ className, variant = "ghost", active, ...props }, ref) => (
  <button
    ref={ref}
    className={cn(
      "interactive-morph inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-pill focus-visible:outline-none",
      variant === "solid" &&
        "bg-accent-strong text-accent-contrast shadow-[var(--shadow-ambient)] hover:brightness-110",
      variant === "ghost" && "text-secondary hover:bg-surface-elevated hover:text-primary",
      variant === "ghost" && active && "bg-accent-strong text-accent-contrast",
      className,
    )}
    {...props}
  />
));
IconButton.displayName = "IconButton";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "h-9 w-full rounded-md border border-border bg-surface-sunken px-3 text-base text-primary placeholder:text-tertiary outline-none transition-[border-color,box-shadow] duration-[var(--duration-fast)] focus:border-accent focus:shadow-[var(--shadow-focus)]",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "w-full resize-none rounded-md border border-border bg-surface-sunken px-3 py-2 text-base leading-relaxed text-primary placeholder:text-tertiary outline-none transition-[border-color,box-shadow] duration-[var(--duration-fast)] focus:border-accent focus:shadow-[var(--shadow-focus)]",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export const Spinner = ({ className }: { className?: string }) => (
  <span
    className={cn(
      "inline-block h-3 w-3 animate-spin rounded-full border-2 border-accent-soft border-t-accent",
      className,
    )}
  />
);

export const Surface = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement> & { shadow?: boolean }>(
  ({ className, shadow = true, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "rounded-xl border border-border bg-surface",
        shadow && "shadow-[var(--shadow-drop)]",
        className,
      )}
      {...props}
    />
  ),
);
Surface.displayName = "Surface";
