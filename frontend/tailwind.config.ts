import type { Config } from "tailwindcss";

/**
 * Redesign: graphite monochrome system.
 *
 * Two mechanisms collapse the old rainbow into "neutral + money semantics":
 *  1. Semantic tokens (background/foreground/primary/…) read OKLCH CSS vars with
 *     `<alpha-value>` so opacity modifiers (bg-primary/10) keep working.
 *  2. The literal Tailwind palettes used across the app are REMAPPED here:
 *     every decorative hue (blue/violet/teal/slate/…) → the graphite neutral
 *     ramp; emerald/green → tuned gain-green; rose/red → loss-red; amber/yellow
 *     → warning-amber. This re-skins existing className colors with zero edits.
 */

const token = (name: string) => `oklch(var(--${name}) / <alpha-value>)`;

// Full OKLCH ramps with the alpha placeholder so `/opacity` modifiers work.
const ramp = (
  stops: Record<number, [number, number, number]>,
): Record<string, string> =>
  Object.fromEntries(
    Object.entries(stops).map(([k, [l, c, h]]) => [
      k,
      `oklch(${l} ${c} ${h} / <alpha-value>)`,
    ]),
  );

// Graphite — true neutral (chroma 0). Absorbs every decorative hue.
const NEUTRAL = ramp({
  50: [0.985, 0, 0],
  100: [0.967, 0, 0],
  200: [0.922, 0, 0],
  300: [0.87, 0, 0],
  400: [0.708, 0, 0],
  500: [0.556, 0, 0],
  600: [0.439, 0, 0],
  700: [0.371, 0, 0],
  800: [0.269, 0, 0],
  900: [0.205, 0, 0],
  950: [0.145, 0, 0],
});

// Gain — restrained money green (the only "positive" color).
const GAIN = ramp({
  50: [0.96, 0.02, 155],
  100: [0.93, 0.04, 155],
  200: [0.88, 0.06, 155],
  300: [0.8, 0.09, 155],
  400: [0.72, 0.12, 155],
  500: [0.64, 0.13, 155],
  600: [0.58, 0.13, 155],
  700: [0.5, 0.115, 155],
  800: [0.42, 0.1, 155],
  900: [0.36, 0.08, 155],
  950: [0.26, 0.06, 155],
});

// Loss — restrained red (negative / destructive).
const LOSS = ramp({
  50: [0.96, 0.02, 27],
  100: [0.93, 0.05, 27],
  200: [0.88, 0.08, 27],
  300: [0.8, 0.12, 27],
  400: [0.7, 0.17, 27],
  500: [0.62, 0.2, 27],
  600: [0.55, 0.2, 27],
  700: [0.48, 0.18, 27],
  800: [0.41, 0.15, 27],
  900: [0.36, 0.12, 27],
  950: [0.26, 0.09, 27],
});

// Warning — amber, used only for reconciliation / pending / caution chrome.
const WARNING = ramp({
  50: [0.97, 0.03, 80],
  100: [0.94, 0.06, 82],
  200: [0.9, 0.09, 82],
  300: [0.85, 0.12, 80],
  400: [0.8, 0.14, 75],
  500: [0.74, 0.14, 70],
  600: [0.66, 0.13, 62],
  700: [0.55, 0.11, 56],
  800: [0.45, 0.09, 55],
  900: [0.38, 0.07, 55],
  950: [0.27, 0.05, 55],
});

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "SF Pro Text",
          "SF Pro Display",
          "system-ui",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SF Mono",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
      colors: {
        border: token("border"),
        input: token("input"),
        ring: token("ring"),
        background: token("background"),
        foreground: token("foreground"),
        primary: {
          DEFAULT: token("primary"),
          foreground: token("primary-foreground"),
        },
        secondary: {
          DEFAULT: token("secondary"),
          foreground: token("secondary-foreground"),
        },
        muted: {
          DEFAULT: token("muted"),
          foreground: token("muted-foreground"),
        },
        accent: {
          DEFAULT: token("accent"),
          foreground: token("accent-foreground"),
        },
        popover: {
          DEFAULT: token("popover"),
          foreground: token("popover-foreground"),
        },
        card: {
          DEFAULT: token("card"),
          foreground: token("card-foreground"),
        },
        destructive: {
          DEFAULT: token("destructive"),
          foreground: token("destructive-foreground"),
        },
        // Financial + state semantics (the only chromatic tokens).
        gain: {
          DEFAULT: token("gain"),
          foreground: token("gain-foreground"),
        },
        loss: {
          DEFAULT: token("loss"),
          foreground: token("loss-foreground"),
        },
        warning: {
          DEFAULT: token("warning"),
          foreground: token("warning-foreground"),
        },

        // ── Palette remap: legacy literal classes → monochrome + money ──
        // Decorative hues all collapse to the graphite neutral ramp.
        gray: NEUTRAL,
        slate: NEUTRAL,
        zinc: NEUTRAL,
        neutral: NEUTRAL,
        stone: NEUTRAL,
        blue: NEUTRAL,
        indigo: NEUTRAL,
        violet: NEUTRAL,
        purple: NEUTRAL,
        fuchsia: NEUTRAL,
        pink: NEUTRAL,
        sky: NEUTRAL,
        cyan: NEUTRAL,
        teal: NEUTRAL,
        lime: NEUTRAL,
        orange: NEUTRAL,
        // Semantic hues keep meaning, re-tuned to the restrained ramps.
        emerald: GAIN,
        green: GAIN,
        rose: LOSS,
        red: LOSS,
        amber: WARNING,
        yellow: WARNING,
      },
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        // Soft, low-spread elevation — closer to macOS than Material.
        xs: "0 1px 2px 0 oklch(0 0 0 / 0.04)",
        sm: "0 1px 3px 0 oklch(0 0 0 / 0.06), 0 1px 2px -1px oklch(0 0 0 / 0.05)",
        DEFAULT:
          "0 2px 6px -1px oklch(0 0 0 / 0.07), 0 1px 3px -1px oklch(0 0 0 / 0.05)",
        md: "0 6px 16px -4px oklch(0 0 0 / 0.08), 0 2px 6px -2px oklch(0 0 0 / 0.05)",
        lg: "0 12px 32px -8px oklch(0 0 0 / 0.12), 0 4px 10px -4px oklch(0 0 0 / 0.06)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
      },
      animation: {
        "fade-in": "fade-in 0.3s ease-out both",
      },
    },
  },
  plugins: [],
};

export default config;
