"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import {
  ApiError,
  fetchLLMCost,
  fetchLLMSettings,
  type LLMSettingsOut,
  updateLLMSettings,
} from "@/lib/api";
import { ErrorDisplay, LoadingSpinner } from "@/components/ui-common";

const _MODEL_CHOICES = [
  { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash（推荐，便宜快速）" },
  { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro（更准但贵 16 倍）" },
  { value: "gemini-2.0-flash", label: "Gemini 2.0 Flash" },
];

export function LLMSettingsForm() {
  const {
    data: settings,
    error,
    isLoading,
    mutate,
  } = useSWR<LLMSettingsOut>("llm-settings", fetchLLMSettings, {
    revalidateOnFocus: false,
  });
  const { data: cost, mutate: refreshCost } = useSWR("llm-cost", fetchLLMCost, {
    refreshInterval: 30_000,
  });

  const [draft, setDraft] = useState<LLMSettingsOut | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [keyFlash, setKeyFlash] = useState<string | null>(null);
  const [showKey, setShowKey] = useState(false);

  useEffect(() => {
    if (settings) setDraft(settings);
  }, [settings]);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <ErrorDisplay message={error instanceof Error ? error.message : "加载失败"} />;
  if (!draft) return null;

  const dirty = settings ? JSON.stringify(draft) !== JSON.stringify(settings) : false;

  const handleSave = async () => {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    try {
      await updateLLMSettings({
        enabled: draft.enabled,
        model: draft.model,
        monthly_usd_budget: draft.monthly_usd_budget,
        confidence_threshold: draft.confidence_threshold,
        use_grounding: draft.use_grounding,
        max_notes_in_prompt: draft.max_notes_in_prompt,
      });
      await mutate();
      await refreshCost();
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 2000);
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const usedPct =
    cost && cost.budget_usd > 0
      ? Math.min(100, Math.round((cost.used_usd / cost.budget_usd) * 100))
      : 0;
  const usedColor =
    usedPct >= 90 ? "bg-destructive" : usedPct >= 70 ? "bg-amber-500" : "bg-emerald-500";

  const handleSaveKey = async () => {
    setSavingKey(true);
    setKeyFlash(null);
    try {
      await updateLLMSettings({ gemini_api_key: apiKeyInput.trim() });
      await mutate();
      setApiKeyInput("");
      setKeyFlash(apiKeyInput.trim() ? "已保存" : "已清除");
      setTimeout(() => setKeyFlash(null), 2500);
    } catch (e) {
      setKeyFlash(e instanceof ApiError ? e.message : "保存失败");
    } finally {
      setSavingKey(false);
    }
  };

  const handleClearKey = async () => {
    if (!confirm("确认清除已保存的 Gemini API key?")) return;
    setSavingKey(true);
    setKeyFlash(null);
    try {
      await updateLLMSettings({ gemini_api_key: "" });
      await mutate();
      setKeyFlash("已清除");
      setTimeout(() => setKeyFlash(null), 2500);
    } catch (e) {
      setKeyFlash(e instanceof ApiError ? e.message : "清除失败");
    } finally {
      setSavingKey(false);
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-5">
      {/* API Key input */}
      <div className="rounded-md border border-border bg-muted/30 p-3 space-y-2.5">
        <div className="flex items-center justify-between gap-2">
          <div>
            <label className="text-sm font-medium">Gemini API Key</label>
            <p className="text-xs text-muted-foreground mt-0.5">
              当前状态:{" "}
              {draft.api_key_present ? (
                <span className="text-emerald-600 dark:text-emerald-400">已保存</span>
              ) : (
                <span className="text-amber-600 dark:text-amber-400">未设置</span>
              )}
              {" · "}
              <a
                href="https://aistudio.google.com/apikey"
                target="_blank"
                rel="noreferrer"
                className="underline hover:text-foreground"
              >
                获取 key
              </a>
            </p>
          </div>
          {keyFlash && (
            <span className="text-xs text-emerald-600 dark:text-emerald-400">{keyFlash}</span>
          )}
        </div>
        {/* form wrapper silences the orphan password-field DOM warning
            + lets browsers wire up password-manager affordances. */}
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (apiKeyInput.trim() && !savingKey) handleSaveKey();
          }}
        >
          <input
            type={showKey ? "text" : "password"}
            value={apiKeyInput}
            onChange={(e) => setApiKeyInput(e.target.value)}
            placeholder={draft.api_key_present ? "输入新 key 替换现有的…" : "粘贴 Gemini API key…"}
            autoComplete="off"
            spellCheck={false}
            className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm font-mono"
          />
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="px-2.5 py-2 text-xs rounded-md border border-input hover:bg-muted transition-colors"
            title={showKey ? "隐藏" : "显示"}
          >
            {showKey ? "🙈" : "👁️"}
          </button>
          <button
            type="submit"
            disabled={!apiKeyInput.trim() || savingKey}
            className="px-3 py-2 text-xs font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-40"
          >
            {savingKey ? "保存中…" : "保存 key"}
          </button>
          {draft.api_key_present && (
            <button
              type="button"
              onClick={handleClearKey}
              disabled={savingKey}
              className="px-3 py-2 text-xs font-medium rounded-md border border-destructive/40 text-destructive hover:bg-destructive/10 transition-colors disabled:opacity-40"
            >
              清除
            </button>
          )}
        </form>
        <p className="text-[10px] text-muted-foreground">
          Key 经 FINANCE_BANK_ENCRYPTION_KEY 加密 (AES-256-GCM) 后入库, 永不返回到 API 响应中。
          若 .env 也设置了 GEMINI_API_KEY, 此处保存的值优先生效。
        </p>
      </div>

      {!draft.api_key_present && (
        <div className="rounded-md bg-amber-500/10 border border-amber-500/30 p-3 text-xs text-amber-600 dark:text-amber-300">
          ⚠️ 还没有保存 Gemini API key, LLM 分类将无法启用。在上方输入框粘贴你的 key 并点击「保存 key」。
        </div>
      )}

      {/* enabled toggle */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <label className="text-sm font-medium">启用 LLM 智能分类</label>
          <p className="text-xs text-muted-foreground mt-0.5">
            关闭后, L1 关键词 miss 的交易直接进 inbox 等人工分类
          </p>
        </div>
        <button
          onClick={() => setDraft({ ...draft, enabled: !draft.enabled })}
          disabled={!draft.api_key_present}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition disabled:opacity-50 ${
            draft.enabled ? "bg-primary" : "bg-input"
          }`}
        >
          <span
            className={`h-4 w-4 transform rounded-full bg-white shadow-sm transition ${
              draft.enabled ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </div>

      {/* model */}
      <div>
        <label className="text-sm font-medium">模型</label>
        <select
          value={draft.model}
          onChange={(e) => setDraft({ ...draft, model: e.target.value })}
          className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        >
          {_MODEL_CHOICES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </div>

      {/* threshold */}
      <div>
        <div className="flex items-center justify-between">
          <label className="text-sm font-medium">置信度阈值</label>
          <span className="text-xs text-muted-foreground tabular-nums">
            {draft.confidence_threshold.toFixed(2)}
          </span>
        </div>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={draft.confidence_threshold}
          onChange={(e) =>
            setDraft({ ...draft, confidence_threshold: Number(e.target.value) })
          }
          className="mt-1 w-full"
        />
        <p className="text-xs text-muted-foreground mt-0.5">
          LLM 给出的置信度 ≥ 此值才自动落分类, 否则进 inbox 显示为推荐
        </p>
      </div>

      {/* budget */}
      <div>
        <label className="text-sm font-medium">月度预算 (USD)</label>
        <input
          type="number"
          min={0}
          step={0.5}
          value={draft.monthly_usd_budget}
          onChange={(e) =>
            setDraft({ ...draft, monthly_usd_budget: Number(e.target.value) })
          }
          className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
        <p className="text-xs text-muted-foreground mt-0.5">
          单月成本超额后自动降级 (停调 LLM, 等下月重置)
        </p>
      </div>

      {/* grounding */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <label className="text-sm font-medium">允许联网搜索 (Google Search Grounding)</label>
          <p className="text-xs text-muted-foreground mt-0.5">
            LLM 在知识库不足时可联网核实商户性质 (会增加少量调用成本)
          </p>
        </div>
        <button
          onClick={() => setDraft({ ...draft, use_grounding: !draft.use_grounding })}
          className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition ${
            draft.use_grounding ? "bg-primary" : "bg-input"
          }`}
        >
          <span
            className={`h-4 w-4 transform rounded-full bg-white shadow-sm transition ${
              draft.use_grounding ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </div>

      {/* knowledge base context size */}
      <div>
        <label className="text-sm font-medium">注入 LLM 的知识库条目上限</label>
        <input
          type="number"
          min={0}
          max={100}
          step={1}
          value={draft.max_notes_in_prompt}
          onChange={(e) =>
            setDraft({ ...draft, max_notes_in_prompt: Number(e.target.value) })
          }
          className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        />
      </div>

      {/* cost */}
      {cost && (
        <div className="rounded-md bg-muted/50 p-3">
          <div className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">
              本月 ({cost.period}) 已用
            </span>
            <span className="tabular-nums font-medium">
              ${cost.used_usd.toFixed(4)} / ${cost.budget_usd.toFixed(2)}
            </span>
          </div>
          <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-input">
            <div
              className={`h-full transition-all ${usedColor}`}
              style={{ width: `${usedPct}%` }}
            />
          </div>
        </div>
      )}

      {saveError && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 p-2.5 text-xs text-destructive">
          {saveError}
        </div>
      )}

      <div className="flex items-center justify-end gap-3">
        {savedFlash && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400">已保存</span>
        )}
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className="px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-40"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
