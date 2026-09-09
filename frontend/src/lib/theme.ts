import { useEffect, useState } from "react";
import { actions, getState, useStore } from "@/lib/store";
import { getApi } from "@/lib/bridge";
import type { ThemePref } from "@/types";

/** Apply theme (system/light/dark) + custom accent to <html>. */
export function applyTheme(): void {
  const pref: ThemePref = (getState().settings.theme as ThemePref) || "system";
  let dark: boolean;
  if (pref === "dark") dark = true;
  else if (pref === "light") dark = false;
  else dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);

  const accent = getState().settings.accent || "";
  if (/^#[0-9A-Fa-f]{6}$/.test(accent)) {
    const root = document.documentElement;
    root.style.setProperty("--color-accent", accent);
    root.style.setProperty("--color-accent-strong", accent);
    root.style.setProperty(
      "--color-accent-soft",
      `color-mix(in srgb, ${accent} 14%, var(--color-surface))`,
    );
  }
}

/** Toggle between light/dark (persists the choice). */
export function useThemeToggle() {
  const theme = useStore((s) => s.settings.theme as ThemePref | undefined);
  const [resolvedDark, setResolvedDark] = useState(false);

  useEffect(() => {
    let dark: boolean;
    if (theme === "dark") dark = true;
    else if (theme === "light") dark = false;
    else dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    setResolvedDark(dark);
  }, [theme]);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (!theme || theme === "system") applyTheme();
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const toggle = () => {
    const next: ThemePref = resolvedDark ? "light" : "dark";
    actions.setSetting("theme", next);
    getApi()?.save_setting("theme", next).catch(() => {});
    applyTheme();
  };

  return { isDark: resolvedDark, toggle };
}
