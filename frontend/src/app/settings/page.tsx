"use client";

import { useEffect, useState } from "react";
import { CategoryManager } from "@/components/category-manager";
import { LLMSettingsForm } from "@/components/llm-settings-form";
import { CategorizationNotesTable } from "@/components/categorization-notes-table";

export default function SettingsPage() {
  return (
    <div className="min-h-screen bg-background text-foreground pb-16 md:pb-0">
      <div className="mx-auto max-w-4xl px-4 py-6 md:px-6 lg:px-8">
        <div className="mb-10">
          <h1 className="text-[1.75rem] font-semibold leading-tight tracking-tight">设置</h1>
          <p className="text-sm text-muted-foreground mt-1">
            分类、智能分类与 API Token。账户管理已统一移至
            <a href="/assets" className="font-medium underline underline-offset-2 hover:text-foreground mx-1">资产页</a>。
          </p>
        </div>

        <section>
          <div className="mb-3">
            <h2 className="text-base font-semibold">分类管理</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              一级分类 → 二级分类两层结构。系统已预置 9 大类（住家 / 日常生活 / …）共 30 个二级，可随意增删改。
            </p>
          </div>
          <CategoryManager />
        </section>

        <section className="mt-10">
          <div className="mb-3">
            <h2 className="text-base font-semibold">智能分类（LLM）</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              当 L1 关键词规则未命中或命中需要 LLM 复核的规则时, 异步调用 LLM 兜底分类。
            </p>
          </div>
          <LLMSettingsForm />
        </section>

        <section className="mt-10">
          <div className="mb-3">
            <h2 className="text-base font-semibold">分类知识库</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              用户维护的分类规则, 自动注入 LLM prompt 作为 few-shot 上下文。
            </p>
          </div>
          <CategorizationNotesTable />
        </section>

        <section className="mt-10">
          <div className="mb-3">
            <h2 className="text-base font-semibold">API Token</h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              当后端开启鉴权（AUTH_DISABLED=false）时需要。本地 loopback +
              AUTH_DISABLED=true 模式下可留空。Token 存于浏览器
              localStorage，刷新后保留；任何拥有此设备访问权的人都能读到。
            </p>
          </div>
          <ApiTokenInput />
        </section>
      </div>
    </div>
  );
}


// Sprint 3 FIX-18 (review V2 §V2-P2-4): UI for the user to paste their API
// token instead of building it into the public bundle via NEXT_PUBLIC_*.
function ApiTokenInput() {
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);
  const [hasStored, setHasStored] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const existing = window.localStorage.getItem("finance_api_token") ?? "";
    setHasStored(existing.length > 0);
  }, []);

  const handleSave = () => {
    if (typeof window === "undefined") return;
    if (token.trim()) {
      window.localStorage.setItem("finance_api_token", token.trim());
      setHasStored(true);
    } else {
      window.localStorage.removeItem("finance_api_token");
      setHasStored(false);
    }
    setToken("");
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  };

  const handleClear = () => {
    if (typeof window === "undefined") return;
    window.localStorage.removeItem("finance_api_token");
    setHasStored(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  };

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-3">
      <div className="text-xs text-muted-foreground">
        当前状态：
        {hasStored ? (
          <span className="ml-1 text-emerald-600 dark:text-emerald-400">已保存 token</span>
        ) : (
          <span className="ml-1 text-amber-600 dark:text-amber-400">未设置</span>
        )}
      </div>
      {/* Wrapping in a <form> silences the DOM warning about an orphan
          password field + lets browsers/password managers recognise the
          input. Submit handler stops the default page reload. */}
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          handleSave();
        }}
      >
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={hasStored ? "输入新 token 以替换" : "粘贴 32 字节 hex token"}
          className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          spellCheck={false}
          autoComplete="off"
        />
        <button
          type="submit"
          className="px-3 py-2 text-sm font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
        >
          保存
        </button>
        {hasStored && (
          <button
            type="button"
            onClick={handleClear}
            className="px-3 py-2 text-sm font-medium rounded-md border border-border hover:bg-muted transition-colors"
          >
            清除
          </button>
        )}
      </form>
      {saved && (
        <p className="text-xs text-emerald-600 dark:text-emerald-400">已保存，刷新页面后生效</p>
      )}
    </div>
  );
}
