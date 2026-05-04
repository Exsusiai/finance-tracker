"use client";

import { useMemo, useState } from "react";
import { mutate as swrMutate } from "swr";
import { useCategories } from "@/lib/hooks";
import {
  ApiError,
  type CategoryOut,
  createCategory,
  deleteCategory,
  updateCategory,
} from "@/lib/api";
import { LoadingSpinner } from "@/components/ui-common";
import { cn } from "@/lib/utils";

const KIND_LABELS: Record<string, string> = {
  expense: "支出",
  income: "收入",
  transfer: "转账",
};
const KINDS = ["expense", "income", "transfer"] as const;

/**
 * 两层分类管理：左侧一级 (parent) 列表，右侧选中一级下的二级 (child) 列表。
 * 一级与二级都支持「+ 新建 / 重命名 / 删除」。
 */
export function CategoryManager() {
  const [activeKind, setActiveKind] = useState<string>("expense");
  const [selectedParentId, setSelectedParentId] = useState<number | null>(null);

  const { data: categories, isLoading, mutate: refresh } = useCategories();

  const refreshAll = () => {
    refresh();
    swrMutate(
      (k) => typeof k === "string" && (k.startsWith("categories") || k.startsWith("transactions") || k.startsWith("inbox")),
      undefined,
      { revalidate: true },
    );
  };

  const { parents, children } = useMemo(() => {
    if (!categories) return { parents: [] as CategoryOut[], children: [] as CategoryOut[] };
    const ofKind = categories.filter((c) => c.kind === activeKind);
    const parents = ofKind.filter((c) => c.parent_id == null).sort(_byNameZh);
    const children = ofKind
      .filter((c) => c.parent_id === selectedParentId)
      .sort(_byNameZh);
    return { parents, children };
  }, [categories, activeKind, selectedParentId]);

  // Auto-select first parent when kind changes
  if (parents.length > 0 && !parents.some((p) => p.id === selectedParentId)) {
    queueMicrotask(() => setSelectedParentId(parents[0].id));
  }

  if (isLoading) return <LoadingSpinner />;

  return (
    <div className="space-y-4">
      {/* Kind tabs */}
      <div className="inline-flex rounded-lg border border-border bg-card p-1">
        {KINDS.map((k) => (
          <button
            key={k}
            onClick={() => {
              setActiveKind(k);
              setSelectedParentId(null);
            }}
            className={cn(
              "px-3 py-1.5 text-xs font-medium rounded-md transition-colors",
              activeKind === k
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {KIND_LABELS[k]}
          </button>
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {/* ─── Parents ─── */}
        <CategoryColumn
          title="一级分类"
          items={parents}
          selectedId={selectedParentId}
          onSelect={setSelectedParentId}
          onCreate={async (name) => {
            const created = await createCategory({ name, kind: activeKind, parent_id: null });
            setSelectedParentId(created.id);
            refreshAll();
          }}
          onRename={async (id, name) => {
            await updateCategory(id, { name });
            refreshAll();
          }}
          onDelete={async (id) => {
            await deleteCategory(id);
            if (selectedParentId === id) setSelectedParentId(null);
            refreshAll();
          }}
          showSelector
          createPlaceholder="如：住家"
        />

        {/* ─── Children of selected parent ─── */}
        <CategoryColumn
          title={
            selectedParentId
              ? `二级分类 — ${parents.find((p) => p.id === selectedParentId)?.name ?? ""}`
              : "二级分类（先选一个一级）"
          }
          items={children}
          selectedId={null}
          onSelect={() => {}}
          onCreate={
            selectedParentId
              ? async (name) => {
                  await createCategory({ name, kind: activeKind, parent_id: selectedParentId });
                  refreshAll();
                }
              : null
          }
          onRename={async (id, name) => {
            await updateCategory(id, { name });
            refreshAll();
          }}
          onDelete={async (id) => {
            await deleteCategory(id);
            refreshAll();
          }}
          showSelector={false}
          createPlaceholder={selectedParentId ? "如：房租" : "请先在左边选择一级分类"}
        />
      </div>
    </div>
  );
}

function _byNameZh(a: CategoryOut, b: CategoryOut): number {
  return a.name.localeCompare(b.name, "zh");
}

interface ColumnProps {
  title: string;
  items: CategoryOut[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  onCreate: ((name: string) => Promise<void>) | null;
  onRename: (id: number, name: string) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  showSelector: boolean;
  createPlaceholder: string;
}

function CategoryColumn({
  title,
  items,
  selectedId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  showSelector,
  createPlaceholder,
}: ColumnProps) {
  const [newName, setNewName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim() || !onCreate) return;
    setError(null);
    try {
      setSubmitting(true);
      await onCreate(newName.trim());
      setNewName("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "创建失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleRenameSubmit = async (id: number) => {
    if (!renameValue.trim()) return;
    setError(null);
    try {
      await onRename(id, renameValue.trim());
      setRenamingId(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "重命名失败");
    }
  };

  const handleDelete = async (item: CategoryOut) => {
    if (!confirm(`确定删除「${item.name}」？\n如有子分类或被交易引用，删除可能失败。`)) return;
    setError(null);
    try {
      await onDelete(item.id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "删除失败（可能仍被使用）");
    }
  };

  return (
    <div className="rounded-xl border border-border bg-card">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      </div>

      <ul className="divide-y divide-border max-h-[420px] overflow-y-auto">
        {items.length === 0 ? (
          <li className="px-4 py-6 text-center text-sm text-muted-foreground">
            暂无分类
          </li>
        ) : (
          items.map((c) => {
            const isSelected = showSelector && selectedId === c.id;
            const isRenaming = renamingId === c.id;
            return (
              <li
                key={c.id}
                className={cn(
                  "flex items-center justify-between gap-2 px-4 py-2.5 transition-colors",
                  isSelected && "bg-primary/5",
                  showSelector && !isSelected && "hover:bg-muted/40 cursor-pointer",
                )}
                onClick={() => !isRenaming && showSelector && onSelect(c.id)}
              >
                {isRenaming ? (
                  <input
                    autoFocus
                    type="text"
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleRenameSubmit(c.id);
                      if (e.key === "Escape") setRenamingId(null);
                    }}
                    onBlur={() => handleRenameSubmit(c.id)}
                    className="flex-1 px-2 py-1 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                ) : (
                  <span className={cn("text-sm flex-1 truncate", isSelected ? "font-medium text-foreground" : "text-foreground")}>
                    {c.name}
                  </span>
                )}
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setRenamingId(c.id);
                      setRenameValue(c.name);
                    }}
                    className="text-[10px] px-2 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted"
                  >
                    重命名
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(c);
                    }}
                    className="text-[10px] px-2 py-0.5 rounded text-rose-600 dark:text-rose-400 hover:bg-rose-500/10"
                  >
                    删除
                  </button>
                </div>
              </li>
            );
          })
        )}
      </ul>

      {onCreate && (
        <form onSubmit={handleCreate} className="flex gap-2 p-3 border-t border-border">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={createPlaceholder}
            className="flex-1 px-2.5 py-1.5 text-sm rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            type="submit"
            disabled={submitting || !newName.trim()}
            className="text-xs px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {submitting ? "…" : "+ 新建"}
          </button>
        </form>
      )}

      {error && (
        <p className="px-4 py-2 text-xs text-destructive border-t border-destructive/20 bg-destructive/5">
          {error}
        </p>
      )}
    </div>
  );
}
