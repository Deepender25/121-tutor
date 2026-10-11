// Theme: "system" (follows the OS), "light" or "dark". Stored in localStorage and applied as
// <html data-theme>; an inline script in the root layout applies it before first paint.

export type Theme = "system" | "light" | "dark";

export const THEME_SCRIPT =
  'try{var t=localStorage.getItem("vt-theme");if(t)document.documentElement.dataset.theme=t}catch(e){}';

export function getTheme(): Theme {
  return (document.documentElement.dataset.theme as Theme) || "system";
}

export function isDark(): boolean {
  const t = getTheme();
  return t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
}

export function setTheme(theme: Theme): void {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    if (theme === "system") localStorage.removeItem("vt-theme");
    else localStorage.setItem("vt-theme", theme);
  } catch { /* storage blocked */ }
  window.dispatchEvent(new CustomEvent("vt-theme"));
}
