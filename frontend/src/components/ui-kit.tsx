"use client";

import { cn } from "@/lib/utils";
import { DeltaBadge } from "@/components/dashboard-widgets";

// ─── Page header (consistent title / subtitle / actions across pages) ───

export function PageHeader({
  title,
  subtitle,
  actions,
  className,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <header className={cn("mb-8 flex flex-wrap items-start justify-between gap-4", className)}>
      <div>
        <h1 className="text-[1.75rem] font-semibold leading-tight tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

// ─── Segmented control (pill switcher — currency, view modes, …) ────────

interface SegmentedOption {
  value: string;
  label: string;
}

export function SegmentedControl({
  options,
  value,
  onChange,
  size = "sm",
  className,
}: {
  options: SegmentedOption[];
  value: string;
  onChange: (v: string) => void;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div className={cn("inline-flex rounded-full border border-border bg-card p-0.5 shadow-xs", className)}>
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-full font-medium transition-colors",
            size === "md" ? "px-4 py-1.5 text-sm" : "px-3 py-1.5 text-xs",
            value === o.value
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ─── Stat tile (label + big tabular figure, optional delta / hint) ──────

export function StatTile({
  label,
  value,
  hint,
  loading = false,
  emphasis = false,
  deltaPercent,
  valueClassName,
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  loading?: boolean;
  emphasis?: boolean;
  deltaPercent?: number;
  valueClassName?: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border bg-card p-5 shadow-xs",
        emphasis ? "border-foreground/15 ring-1 ring-foreground/5" : "border-border",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">{label}</p>
        {deltaPercent != null && !loading && <DeltaBadge percent={deltaPercent} />}
      </div>
      {loading ? (
        <div className={cn("skeleton mt-3 h-8", emphasis ? "w-44" : "w-32")} />
      ) : (
        <p
          className={cn(
            "mt-2 font-semibold tabular-nums tracking-tight",
            emphasis ? "text-3xl" : "text-2xl",
            valueClassName,
          )}
        >
          {value}
        </p>
      )}
      {hint && !loading && <div className="mt-2 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}
