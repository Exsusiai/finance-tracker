"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  ApiError,
  createCategorizationNote,
  deleteCategorizationNote,
  fetchCategorizationNotes,
  type CategorizationNoteOut,
  updateCategorizationNote,
} from "@/lib/api";
import { useCategories } from "@/lib/hooks";
import { ErrorDisplay, LoadingSpinner } from "@/components/ui-common";

interface NoteFormState {
  id?: number;
  category_id: number | null;
  trigger_text: string;
  note_text: string;
  enabled: boolean;
}

const _empty: NoteFormState = {
  category_id: null,
  trigger_text: "",
  note_text: "",
  enabled: true,
};

export function CategorizationNotesTable() {
  const { data: notes, error, isLoading, mutate } = useSWR(
    "categorization-notes",
    () => fetchCategorizationNotes(),
    { revalidateOnFocus: false },
  );
  const { data: categories } = useCategories();

  const [editing, setEditing] = useState<NoteFormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error instanceof Error ? error.message : "加载失败"} />;

  const items = notes ?? [];

  const handleSave = async () => {
    if (!editing) return;
    if (editing.category_id == null) {
      setOpError("请选择分类");
      return;
    }
    if (!editing.trigger_text.trim() || !editing.note_text.trim()) {
      setOpError("trigger 和 note 都不能为空");
      return;
    }
    setBusy(true);
    setOpError(null);
    try {
      if (editing.id) {
        await updateCategorizationNote(editing.id, {
          category_id: editing.category_id,
          trigger_text: editing.trigger_text,
          note_text: editing.note_text,
          enabled: editing.enabled,
        });
      } else {
        await createCategorizationNote({
          category_id: editing.category_id,
          trigger_text: editing.trigger_text,
          note_text: editing.note_text,
          enabled: editing.enabled,
        });
      }
      setEditing(null);
      await mutate();
    } catch (e) {
      setOpError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const handleToggle = async (note: CategorizationNoteOut) => {
    setBusy(true);
    try {
      await updateCategorizationNote(note.id, { enabled: !note.enabled });
      await mutate();
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (note: CategorizationNoteOut) => {
    if (!confirm(`删除知识库条目「${note.trigger_text.slice(0, 40)}」?`)) return;
    setBusy(true);
    try {
      await deleteCategorizationNote(note.id);
      await mutate();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div>
          <h3 className="text-sm font-medium">知识库条目</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            LLM 在分类前会读这些条目作为上下文。在 Inbox 改分类时填写备注会自动入库。
          </p>
        </div>
        <button
          onClick={() => setEditing({ ..._empty })}
          className="px-3 py-1.5 text-xs font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          + 新建条目
        </button>
      </div>

      {items.length === 0 ? (
        <div className="px-4 py-8 text-center text-sm text-muted-foreground">
          暂无知识库条目。可在 Inbox 改分类时写备注（自动入库），或点击「新建条目」手动添加。
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-xs">
              <tr>
                <th className="px-3 py-2 text-left font-medium">触发条件</th>
                <th className="px-3 py-2 text-left font-medium">分类</th>
                <th className="px-3 py-2 text-left font-medium">备注</th>
                <th className="px-3 py-2 text-right font-medium">命中</th>
                <th className="px-3 py-2 text-center font-medium">启用</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((n) => (
                <tr key={n.id} className="border-t border-border">
                  <td className="px-3 py-2 max-w-xs">
                    <div className="text-xs font-mono truncate" title={n.trigger_text}>
                      {n.trigger_text}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-xs">{n.category_name ?? `#${n.category_id}`}</td>
                  <td className="px-3 py-2 max-w-md">
                    <div className="text-xs text-muted-foreground truncate" title={n.note_text}>
                      {n.note_text}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right text-xs tabular-nums">{n.usage_count}</td>
                  <td className="px-3 py-2 text-center">
                    <button
                      onClick={() => handleToggle(n)}
                      disabled={busy}
                      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
                        n.enabled ? "bg-primary" : "bg-input"
                      }`}
                    >
                      <span
                        className={`h-3.5 w-3.5 transform rounded-full bg-white shadow-sm transition ${
                          n.enabled ? "translate-x-5" : "translate-x-1"
                        }`}
                      />
                    </button>
                  </td>
                  <td className="px-3 py-2 text-right space-x-2">
                    <button
                      onClick={() =>
                        setEditing({
                          id: n.id,
                          category_id: n.category_id,
                          trigger_text: n.trigger_text,
                          note_text: n.note_text,
                          enabled: n.enabled,
                        })
                      }
                      className="text-xs text-primary hover:underline"
                    >
                      编辑
                    </button>
                    <button
                      onClick={() => handleDelete(n)}
                      disabled={busy}
                      className="text-xs text-destructive hover:underline disabled:opacity-50"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <NoteEditDialog
          editing={editing}
          categories={categories ?? []}
          onChange={setEditing}
          onSave={handleSave}
          onCancel={() => {
            setEditing(null);
            setOpError(null);
          }}
          busy={busy}
          error={opError}
        />
      )}
    </div>
  );
}

interface NoteEditDialogProps {
  editing: NoteFormState;
  categories: { id: number; name: string; kind: string; parent_id: number | null }[];
  onChange: (next: NoteFormState) => void;
  onSave: () => void;
  onCancel: () => void;
  busy: boolean;
  error: string | null;
}

function NoteEditDialog({
  editing,
  categories,
  onChange,
  onSave,
  onCancel,
  busy,
  error,
}: NoteEditDialogProps) {
  // Group categories by kind for display in optgroups
  const grouped = ["expense", "income", "transfer"].map((kind) => ({
    kind,
    cats: categories
      .filter((c) => c.kind === kind && c.parent_id !== null)
      .map((child) => {
        const parent = categories.find((c) => c.id === child.parent_id);
        return {
          id: child.id,
          label: parent ? `${parent.name} · ${child.name}` : child.name,
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label, "zh")),
  }));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-black/50 backdrop-blur-sm" onClick={onCancel} />
      <div className="relative w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-xl space-y-4">
        <h3 className="text-lg font-semibold">{editing.id ? "编辑" : "新建"}知识库条目</h3>

        <div>
          <label className="text-xs font-medium">分类</label>
          <select
            value={editing.category_id ?? ""}
            onChange={(e) =>
              onChange({ ...editing, category_id: e.target.value ? Number(e.target.value) : null })
            }
            className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          >
            <option value="">— 选择分类 —</option>
            {grouped.map((g) =>
              g.cats.length > 0 ? (
                <optgroup key={g.kind} label={g.kind}>
                  {g.cats.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </optgroup>
              ) : null,
            )}
          </select>
        </div>

        <div>
          <label className="text-xs font-medium">触发条件 (LLM 检索 keyword)</label>
          <input
            value={editing.trigger_text}
            onChange={(e) => onChange({ ...editing, trigger_text: e.target.value })}
            placeholder='例如: PayPal 每月 2.99 EUR'
            className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          />
        </div>

        <div>
          <label className="text-xs font-medium">备注内容 (LLM 推理依据)</label>
          <textarea
            value={editing.note_text}
            onChange={(e) => onChange({ ...editing, note_text: e.target.value })}
            placeholder="例如: 这是 X 服务的订阅费用, 每月固定 2.99 EUR"
            rows={3}
            className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
          />
        </div>

        <div className="flex items-center gap-2">
          <input
            id="note-enabled"
            type="checkbox"
            checked={editing.enabled}
            onChange={(e) => onChange({ ...editing, enabled: e.target.checked })}
            className="h-4 w-4 rounded border-input"
          />
          <label htmlFor="note-enabled" className="text-xs">启用 (供 LLM 读取)</label>
        </div>

        {error && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-2.5 text-xs text-destructive">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onCancel}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors disabled:opacity-50"
          >
            取消
          </button>
          <button
            onClick={onSave}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}
