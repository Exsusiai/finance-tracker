"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

// ─── prefers-reduced-motion ─────────────────────────────────────────────

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(m.matches);
    const handler = () => setReduced(m.matches);
    m.addEventListener("change", handler);
    return () => m.removeEventListener("change", handler);
  }, []);
  return reduced;
}

// ─── Count-up (the dashboard's one signature entrance) ──────────────────

/** Animates a number 0→target once with ease-out-quart. Jumps instantly when
 *  reduced motion is requested. Re-runs when target meaningfully changes. */
export function useCountUp(target: number, duration = 900): number {
  const [value, setValue] = useState(0);
  const reduced = usePrefersReducedMotion();
  const fromRef = useRef(0);

  useEffect(() => {
    if (reduced || !isFinite(target)) {
      setValue(target);
      return;
    }
    const from = fromRef.current;
    let raf = 0;
    let startTs = 0;
    const tick = (now: number) => {
      if (!startTs) startTs = now;
      const t = Math.min(1, (now - startTs) / duration);
      const eased = 1 - Math.pow(1 - t, 4);
      const next = from + (target - from) * eased;
      setValue(next);
      if (t < 1) {
        raf = requestAnimationFrame(tick);
      } else {
        fromRef.current = target;
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration, reduced]);

  return value;
}

// ─── Sparkline (axis-less area trend) ───────────────────────────────────

interface SparklineProps {
  data: number[];
  className?: string;
  height?: number;
  /** unique id for the gradient fill */
  gradientId?: string;
}

export function Sparkline({ data, className, height = 48, gradientId = "spark" }: SparklineProps) {
  if (!data || data.length < 2) return null;
  const W = 240;
  const H = height;
  const pad = 3;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const stepX = (W - pad * 2) / (data.length - 1);
  const pts = data.map((v, i) => {
    const x = pad + i * stepX;
    const y = pad + (H - pad * 2) * (1 - (v - min) / span);
    return [x, y] as const;
  });
  const line = pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${H - pad} L${pts[0][0].toFixed(1)},${H - pad} Z`;
  const last = pts[pts.length - 1];

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className={cn("w-full", className)}
      style={{ height }}
      aria-hidden
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--chart-ink)" stopOpacity={0.16} />
          <stop offset="100%" stopColor="var(--chart-ink)" stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradientId})`} />
      <path d={line} fill="none" stroke="var(--chart-ink)" strokeWidth={1.5} vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={last[0]} cy={last[1]} r={2.5} fill="var(--chart-ink)" />
    </svg>
  );
}

// ─── Delta badge (gain/loss) ────────────────────────────────────────────

export function DeltaBadge({ percent, className }: { percent: number; className?: string }) {
  if (!isFinite(percent)) return null;
  const up = percent >= 0;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium tabular-nums",
        up ? "bg-gain/10 text-gain" : "bg-loss/10 text-loss",
        className,
      )}
    >
      <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" aria-hidden>
        <path
          d={up ? "M6 2.5 L9.5 7 H2.5 Z" : "M6 9.5 L2.5 5 H9.5 Z"}
          fill="currentColor"
        />
      </svg>
      {up ? "+" : ""}
      {percent.toFixed(1)}%
    </span>
  );
}

// ─── Allocation bar (single stacked bar, Apple-storage style) ───────────

export interface AllocSegment {
  key: string;
  label: string;
  value: number;
  percent: number;
  currency: string;
  color: string;
}

interface AllocationBarProps {
  segments: AllocSegment[];
  formatValue: (v: number, currency: string) => string;
}

export function AllocationBar({ segments, formatValue }: AllocationBarProps) {
  const [hover, setHover] = useState<string | null>(null);
  if (segments.length === 0) return null;

  return (
    <div>
      <div className="flex h-3 w-full gap-[3px] overflow-hidden rounded-full">
        {segments.map((s) => (
          <div
            key={s.key}
            className="h-full rounded-[2px] transition-opacity duration-200"
            style={{
              width: `${Math.max(s.percent, 1.5)}%`,
              backgroundColor: s.color,
              opacity: hover && hover !== s.key ? 0.35 : 1,
            }}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
          />
        ))}
      </div>
      <div className="mt-5 grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-3">
        {segments.map((s) => (
          <div
            key={s.key}
            className="flex items-center gap-2.5 transition-opacity duration-200"
            style={{ opacity: hover && hover !== s.key ? 0.4 : 1 }}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
          >
            <span className="h-2.5 w-2.5 shrink-0 rounded-[3px]" style={{ backgroundColor: s.color }} />
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <span className="truncate text-sm text-foreground">{s.label}</span>
                <span className="text-xs tabular-nums text-muted-foreground">{s.percent.toFixed(1)}%</span>
              </div>
              <div className="text-sm font-medium tabular-nums text-foreground">
                {formatValue(s.value, s.currency)}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Proportion row (label · amount · thin bar) ─────────────────────────

export function ProportionRow({
  label,
  amount,
  percent,
  emphasis = false,
}: {
  label: string;
  amount: string;
  percent: number;
  emphasis?: boolean;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-sm text-muted-foreground">{label}</span>
        <span className={cn("tabular-nums", emphasis ? "text-base font-semibold text-foreground" : "text-sm font-medium text-foreground")}>
          {amount}
        </span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-700 ease-out"
          style={{ width: `${Math.min(Math.max(percent, 0), 100)}%` }}
        />
      </div>
    </div>
  );
}

// ─── Tile (the one reusable surface for the bento) ──────────────────────

export function Tile({
  className,
  children,
  delay = 0,
  interactive = false,
}: {
  className?: string;
  children: React.ReactNode;
  delay?: number;
  interactive?: boolean;
}) {
  return (
    <section
      className={cn(
        "animate-fade-in-up rounded-2xl border border-border bg-card p-6 shadow-xs",
        interactive && "transition-shadow duration-300 hover:shadow-md",
        className,
      )}
      style={{ animationDelay: `${delay}ms` }}
    >
      {children}
    </section>
  );
}
