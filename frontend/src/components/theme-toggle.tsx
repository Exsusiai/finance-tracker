"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";

type Theme = "light" | "dark";

const STORAGE_KEY = "finance_theme";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/**
 * Low-key light/dark switch. Defaults to light (the project's primary theme);
 * the choice persists in localStorage and is applied pre-paint by the inline
 * bootstrap script in layout.tsx, so this component only reflects/sets state.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? "light";
    setTheme(stored);
    setMounted(true);
  }, []);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem(STORAGE_KEY, next);
    applyTheme(next);
  };

  const isDark = theme === "dark";

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={isDark ? "切换到亮色" : "切换到暗色"}
      title={isDark ? "切换到亮色" : "切换到暗色"}
      className={cn(
        "inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground",
        "transition-colors hover:bg-accent hover:text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        !mounted && "opacity-0",
        className,
      )}
    >
      {isDark ? (
        // Sun — switch to light
        <svg className="h-[18px] w-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="4" strokeWidth={1.6} />
          <path
            strokeWidth={1.6}
            strokeLinecap="round"
            d="M12 2v2m0 16v2M4.93 4.93l1.41 1.41m11.32 11.32l1.41 1.41M2 12h2m16 0h2M4.93 19.07l1.41-1.41m11.32-11.32l1.41-1.41"
          />
        </svg>
      ) : (
        // Moon — switch to dark
        <svg className="h-[18px] w-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
        </svg>
      )}
    </button>
  );
}
