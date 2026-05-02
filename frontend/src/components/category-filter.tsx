"use client";

import { cn } from "@/lib/utils";

interface CategoryOut {
  id: number;
  name: string;
  kind: string;
  icon: string | null;
  color: string | null;
  sort_order: number;
  is_system: boolean;
  created_at: string;
}

interface CategoryFilterProps {
  categories: CategoryOut[];
  selected: number | undefined;
  onSelect: (id: number | undefined) => void;
}

export function CategoryFilter({ categories, selected, onSelect }: CategoryFilterProps) {
  if (categories.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5">
      <button
        onClick={() => onSelect(undefined)}
        className={cn(
          "px-2.5 py-1 text-xs font-medium rounded-full transition-colors",
          selected === undefined
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-muted-foreground hover:text-foreground"
        )}
      >
        全部
      </button>
      {categories.map((cat) => (
        <button
          key={cat.id}
          onClick={() => onSelect(cat.id === selected ? undefined : cat.id)}
          className={cn(
            "px-2.5 py-1 text-xs font-medium rounded-full transition-colors",
            cat.id === selected
              ? "bg-primary text-primary-foreground"
              : "bg-muted text-muted-foreground hover:text-foreground"
          )}
          style={
            cat.id === selected && cat.color
              ? { backgroundColor: cat.color, color: "#fff" }
              : undefined
          }
        >
          {cat.icon && <span className="mr-1">{cat.icon}</span>}
          {cat.name}
        </button>
      ))}
    </div>
  );
}
