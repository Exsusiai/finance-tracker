"use client";

import { useState, useCallback, useRef } from "react";
import { useAccounts, useStatementsPage, invalidateTransactionGraph } from "@/lib/hooks";
import {
  type PdfImportOut,
  type CsvImportResult,
  uploadPdf,
  uploadCsv,
  commitStatement,
  deleteStatement,
  ApiError,
} from "@/lib/api";
import { formatFileSize, formatDate, cn } from "@/lib/utils";
import { LoadingSpinner, ErrorDisplay } from "@/components/ui-common";

// Bank formats the backend can parse (mirrors _BANK_PARSERS in
// services/pdf_parser/engine.py) + a generic fallback. "auto" lets the
// backend detect from text features (BIC / domain / legal name).
const BANK_FORMAT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "auto", label: "自动识别（推荐）" },
  { value: "n26", label: "N26" },
  { value: "revolut", label: "Revolut" },
  { value: "tfbank", label: "TFBank" },
  { value: "advanzia", label: "Advanzia" },
  { value: "amex_de", label: "American Express" },
  { value: "other", label: "其他（通用规则）" },
];

// Only bank / credit_card accounts can be PDF-statement destinations.
const PDF_ACCOUNT_TYPES = new Set(["bank", "credit_card"]);

const PAGE_SIZE = 50;

const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  pending: { label: "等待中", color: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400" },
  parsing: { label: "解析中", color: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
  awaiting_review: { label: "待确认", color: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400" },
  awaiting_account: { label: "待选账户", color: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400" },
  success: { label: "已导入", color: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400" },
  failed: { label: "失败", color: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400" },
};

export function PdfImportPanel() {
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<PdfImportOut | null>(null);
  // Account chosen in the preview (defaults to the backend's candidate).
  const [previewAccountId, setPreviewAccountId] = useState<number | undefined>();
  const [bankFormat, setBankFormat] = useState<string>("auto");
  const [dragOver, setDragOver] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [listLimit, setListLimit] = useState(PAGE_SIZE);
  // Per-row account choice for awaiting_review records in the history list.
  const [rowAccount, setRowAccount] = useState<Record<number, number | undefined>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  // CSV import (PayPal): account chosen up front, direct import + dedup.
  const [csvAccountId, setCsvAccountId] = useState<number | undefined>();
  const [csvUploading, setCsvUploading] = useState(false);
  const [csvError, setCsvError] = useState<string | null>(null);
  const [csvResult, setCsvResult] = useState<CsvImportResult | null>(null);
  const csvInputRef = useRef<HTMLInputElement>(null);

  const { data: accounts } = useAccounts(true);
  const {
    data: page,
    error,
    isLoading,
    mutate: refreshStatements,
  } = useStatementsPage(listLimit);

  const statements = page?.items ?? [];
  const total = page?.total ?? 0;
  const pdfAccounts = (accounts ?? []).filter((a) => PDF_ACCOUNT_TYPES.has(a.type));

  const handleFile = useCallback(
    async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setUploadError("仅支持 PDF 文件");
        return;
      }
      setUploading(true);
      setUploadError(null);
      setUploadResult(null);
      try {
        // No insert happens here — the backend parses + stages (awaiting_review).
        const result = await uploadPdf(file, undefined, bankFormat);
        setUploadResult(result);
        setPreviewAccountId(result.account_id ?? undefined);
        refreshStatements();
      } catch (e) {
        setUploadError(e instanceof ApiError ? e.message : "上传失败，请重试");
      } finally {
        setUploading(false);
      }
    },
    [bankFormat, refreshStatements]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files[0];
      if (file) handleFile(file);
    },
    [handleFile]
  );

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) handleFile(file);
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
    [handleFile]
  );

  // CSV import (PayPal): direct import into the chosen account, row-level
  // dedup makes re-uploading overlapping months safe.
  const handleCsvFile = useCallback(
    async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".csv")) {
        setCsvError("仅支持 CSV 文件");
        return;
      }
      if (!csvAccountId) {
        setCsvError("请先选择要导入到的账户。");
        return;
      }
      setCsvUploading(true);
      setCsvError(null);
      setCsvResult(null);
      try {
        const result = await uploadCsv(file, csvAccountId);
        setCsvResult(result);
        refreshStatements();
        invalidateTransactionGraph();
      } catch (e) {
        setCsvError(e instanceof ApiError ? e.message : "上传失败，请重试");
      } finally {
        setCsvUploading(false);
        if (csvInputRef.current) csvInputRef.current.value = "";
      }
    },
    [csvAccountId, refreshStatements]
  );

  // Commit a staged import → actually inserts the transactions.
  const handleCommit = useCallback(
    async (importId: number, accountId: number | undefined) => {
      if (!accountId) {
        setUploadError("请先选择要导入到的账户。");
        return;
      }
      setBusyId(importId);
      try {
        await commitStatement(importId, accountId);
        refreshStatements();
        invalidateTransactionGraph();
        if (uploadResult?.id === importId) setUploadResult(null);
      } catch (e) {
        setUploadError(e instanceof ApiError ? e.message : "导入失败");
      } finally {
        setBusyId(null);
      }
    },
    [refreshStatements, uploadResult]
  );

  // Cancel/revert: removes the import record + its transactions (and the
  // stored file). For staged imports this leaves no trace.
  const handleDelete = useCallback(
    async (importId: number, committed: boolean) => {
      const msg = committed
        ? "撤销这次导入？该 PDF 的所有交易记录都会被删除，且影响现金流统计。"
        : "取消这次导入？暂存的解析结果会被丢弃，不留记录。";
      if (!confirm(msg)) return;
      setBusyId(importId);
      try {
        await deleteStatement(importId);
        refreshStatements();
        invalidateTransactionGraph();
        if (uploadResult?.id === importId) setUploadResult(null);
      } catch (e) {
        setUploadError(e instanceof ApiError ? e.message : "删除失败");
      } finally {
        setBusyId(null);
      }
    },
    [refreshStatements, uploadResult]
  );

  return (
    <div className="space-y-6">
      {/* ─── Upload area ─────────────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-card p-6">
        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">银行格式</label>
          <select
            value={bankFormat}
            onChange={(e) => setBankFormat(e.target.value)}
            className="w-full sm:w-1/2 px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          >
            {BANK_FORMAT_OPTIONS.map((b) => (
              <option key={b.value} value={b.value}>{b.label}</option>
            ))}
          </select>
          <p className="mt-1.5 text-xs text-muted-foreground">
            默认自动识别；识别错误时可手动指定。上传后会先<strong>预览</strong>，确认无误再导入——
            取消则不留任何记录。
          </p>
        </div>

        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => fileInputRef.current?.click()}
          className={cn(
            "relative flex flex-col items-center justify-center gap-3 py-12 rounded-xl border-2 border-dashed transition-all cursor-pointer",
            dragOver
              ? "border-primary bg-primary/5"
              : "border-border hover:border-muted-foreground/30 hover:bg-muted/30",
            uploading && "pointer-events-none opacity-60",
          )}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf"
            onChange={handleInputChange}
            disabled={uploading}
            className="hidden"
          />
          {uploading ? (
            <>
              <LoadingSpinner />
              <p className="text-sm text-muted-foreground">正在上传并解析…</p>
            </>
          ) : (
            <>
              <svg className={cn("h-10 w-10", dragOver ? "text-primary" : "text-muted-foreground/50")} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
              </svg>
              <div className="text-center">
                <p className="text-sm font-medium text-foreground">拖拽 PDF 文件到这里，或点击上传</p>
                <p className="text-xs text-muted-foreground mt-1">支持：N26、Revolut、TFBank、Advanzia、AMEX 等格式</p>
              </div>
            </>
          )}
        </div>

        {uploadError && (
          <div className="mt-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
            {uploadError}
          </div>
        )}
      </div>

      {/* ─── CSV import (PayPal) ──────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-card p-6">
        <h3 className="text-base font-semibold mb-1">CSV 导入（PayPal）</h3>
        <p className="text-xs text-muted-foreground mb-4">
          从 PayPal 网页版导出 CSV（Activity → Reports → Statements）。可一次上传
          多个月、任意日期范围；<strong>按交易号自动去重</strong>，重复上传重叠月份不会重复入账。
        </p>
        <div className="flex flex-col sm:flex-row gap-2">
          <select
            value={csvAccountId ?? ""}
            onChange={(e) => setCsvAccountId(e.target.value ? Number(e.target.value) : undefined)}
            className="px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring sm:w-1/2"
          >
            <option value="">导入到账户…</option>
            {pdfAccounts.map((a) => (
              <option key={a.id} value={a.id}>{a.name} ({a.currency})</option>
            ))}
          </select>
          <button
            onClick={() => csvInputRef.current?.click()}
            disabled={csvUploading || !csvAccountId}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {csvUploading ? "导入中…" : "选择 CSV 上传"}
          </button>
          <input
            ref={csvInputRef}
            type="file"
            accept=".csv"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) handleCsvFile(f); }}
            disabled={csvUploading}
            className="hidden"
          />
        </div>
        {csvError && (
          <div className="mt-3 p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
            {csvError}
          </div>
        )}
        {csvResult && (
          <div className="mt-3 p-3 rounded-lg bg-green-500/10 border border-green-500/20 text-sm text-foreground">
            导入完成（{csvResult.detected_source ?? "csv"}{csvResult.period ? ` · ${csvResult.period}` : ""}）：
            新增 <strong>{csvResult.imported}</strong> 笔
            {csvResult.skipped_duplicate > 0 && <>，跳过重复 {csvResult.skipped_duplicate} 笔</>}
            （共解析 {csvResult.parsed} 笔）。
          </div>
        )}
      </div>

      {/* ─── Preview (awaiting_review) ─────────────────────────────── */}
      {uploadResult && (
        <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold">导入预览 · 确认前不会入库</h3>
            <span className={cn("px-2 py-0.5 text-xs font-medium rounded-full", STATUS_CONFIG[uploadResult.status]?.color)}>
              {STATUS_CONFIG[uploadResult.status]?.label || uploadResult.status}
            </span>
          </div>

          <div className="space-y-2 mb-4">
            <InfoRow label="文件名" value={uploadResult.filename} />
            <InfoRow label="识别银行" value={uploadResult.detected_bank || "未识别（通用规则）"} />
            <InfoRow label="账单周期" value={uploadResult.statement_period || "—"} />
            <InfoRow label="交易笔数" value={`${uploadResult.transactions_count} 笔`} />
          </div>

          {uploadResult.error_message && (
            <div className="mb-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
              {uploadResult.error_message}
            </div>
          )}

          {uploadResult.status === "awaiting_review" && (
            <>
              {/* Account picker */}
              <div className="mb-4">
                <label className="block text-sm font-medium mb-1.5">
                  导入到账户 <span className="text-destructive">*</span>
                </label>
                <select
                  value={previewAccountId ?? ""}
                  onChange={(e) => setPreviewAccountId(e.target.value ? Number(e.target.value) : undefined)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value="">请选择账户…</option>
                  {pdfAccounts.map((a) => (
                    <option key={a.id} value={a.id}>{a.name} ({a.currency})</option>
                  ))}
                </select>
                {uploadResult.account_id && (
                  <p className="mt-1 text-xs text-muted-foreground">已根据识别结果预选，可修改。</p>
                )}
              </div>

              {/* Full parsed preview (scrollable) */}
              {uploadResult.parsed_preview.length > 0 && (
                <div className="mb-4">
                  <h4 className="text-sm font-medium mb-2 text-muted-foreground">
                    交易明细（{uploadResult.parsed_preview.length} 笔）
                  </h4>
                  <div className="rounded-lg border border-border overflow-hidden">
                    <div className="grid grid-cols-[5rem_1fr_6rem] gap-2 px-3 py-2 text-xs font-medium text-muted-foreground bg-muted/40 border-b border-border">
                      <div>日期</div><div>描述</div><div className="text-right">金额</div>
                    </div>
                    <div className="max-h-80 overflow-y-auto">
                      {uploadResult.parsed_preview.map((tx, i) => (
                        <div key={i} className="grid grid-cols-[5rem_1fr_6rem] gap-2 px-3 py-1.5 text-sm border-b border-border last:border-b-0">
                          <div className="text-muted-foreground text-xs tabular-nums">{(tx.occurred_at || "").slice(0, 10)}</div>
                          <div className="truncate" title={tx.description || ""}>{tx.description || "—"}</div>
                          <div className="text-right font-medium tabular-nums">
                            <span style={{ color: tx.type === "expense" ? "hsl(340,70%,55%)" : tx.type === "income" ? "hsl(160,60%,45%)" : undefined }}>
                              {tx.type === "expense" ? "-" : tx.type === "income" ? "+" : ""}
                              {parseFloat(tx.amount).toFixed(2)} {tx.currency}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              <div className="flex gap-3">
                <button
                  onClick={() => handleCommit(uploadResult.id, previewAccountId)}
                  disabled={busyId === uploadResult.id || !previewAccountId}
                  className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                  {busyId === uploadResult.id ? "导入中…" : "确认导入"}
                </button>
                <button
                  onClick={() => handleDelete(uploadResult.id, false)}
                  disabled={busyId === uploadResult.id}
                  className="px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
                >
                  取消
                </button>
              </div>
            </>
          )}

          {uploadResult.status !== "awaiting_review" && (
            <button
              onClick={() => setUploadResult(null)}
              className="w-full px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
            >
              关闭
            </button>
          )}
        </div>
      )}

      {/* ─── Import history ───────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">
            导入记录 {total > 0 && <span className="text-sm font-normal text-muted-foreground">（{statements.length}/{total}）</span>}
          </h2>
          <button onClick={() => refreshStatements()} className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>

        {error && !isLoading && (
          <ErrorDisplay message={error instanceof ApiError ? error.message : "加载失败"} onRetry={() => refreshStatements()} />
        )}
        {isLoading && <LoadingSpinner />}
        {!isLoading && !error && statements.length === 0 && (
          <div className="text-center py-12 text-muted-foreground text-sm">暂无导入记录</div>
        )}

        {!isLoading && !error && statements.length > 0 && (
          <div className="space-y-2">
            {statements.map((stmt) => {
              const staged = stmt.status === "awaiting_review" || stmt.status === "awaiting_account";
              const committed = stmt.status === "success";
              const acctForRow = rowAccount[stmt.id] ?? stmt.account_id ?? undefined;
              return (
                <div key={stmt.id} className="rounded-xl border border-border bg-card p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-3 min-w-0">
                      <svg className="h-8 w-8 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                      </svg>
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-foreground truncate">{stmt.filename}</p>
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                          <span>{formatDate(stmt.created_at)}</span>
                          <span>·</span>
                          <span className="font-medium text-foreground/70">
                            {stmt.detected_bank || "未识别"}
                          </span>
                          {stmt.statement_period && (
                            <>
                              <span>·</span>
                              <span title="账单月份">📅 {stmt.statement_period}</span>
                            </>
                          )}
                          <span>·</span>
                          <span>{stmt.transactions_count} 笔</span>
                          <span>·</span>
                          <span>{formatFileSize(stmt.file_size)}</span>
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className={cn("px-2 py-0.5 text-xs font-medium rounded-full", STATUS_CONFIG[stmt.status]?.color)}>
                        {STATUS_CONFIG[stmt.status]?.label || stmt.status}
                      </span>
                      <button
                        onClick={() => handleDelete(stmt.id, committed)}
                        disabled={busyId === stmt.id}
                        className="p-1.5 rounded-md hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors text-muted-foreground hover:text-destructive disabled:opacity-50"
                        title={committed ? "撤销导入（删除其全部交易）" : "取消/删除"}
                      >
                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      </button>
                    </div>
                  </div>

                  {/* Staged: pick account + 确认导入 */}
                  {staged && (
                    <div className="mt-3 pt-3 border-t border-border flex flex-wrap items-center gap-2">
                      <select
                        value={acctForRow ?? ""}
                        onChange={(e) => setRowAccount((m) => ({ ...m, [stmt.id]: e.target.value ? Number(e.target.value) : undefined }))}
                        className="px-2 py-1.5 text-xs rounded-md border border-border bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                      >
                        <option value="">选择账户…</option>
                        {pdfAccounts.map((a) => (
                          <option key={a.id} value={a.id}>{a.name} ({a.currency})</option>
                        ))}
                      </select>
                      <button
                        onClick={() => handleCommit(stmt.id, acctForRow)}
                        disabled={busyId === stmt.id || !acctForRow}
                        className="px-3 py-1.5 text-xs font-medium rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                      >
                        {busyId === stmt.id ? "导入中…" : "确认导入"}
                      </button>
                    </div>
                  )}

                  {stmt.error_message && (
                    <p className="mt-2 text-xs text-destructive">{stmt.error_message}</p>
                  )}
                </div>
              );
            })}

            {statements.length < total && (
              <button
                onClick={() => setListLimit((l) => l + PAGE_SIZE)}
                className="w-full py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors text-muted-foreground"
              >
                加载更多（还有 {total - statements.length} 条）
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between items-center gap-4">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm font-medium text-foreground text-right truncate">{value}</span>
    </div>
  );
}
