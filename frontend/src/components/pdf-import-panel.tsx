"use client";

import { useState, useCallback, useRef } from "react";
import { useAccounts, useStatements } from "@/lib/hooks";
import {
  type PdfImportOut,
  uploadPdf,
  confirmStatement,
  deleteStatement,
  ApiError,
} from "@/lib/api";
import { formatFileSize, formatDate, cn } from "@/lib/utils";
import { LoadingSpinner, ErrorDisplay } from "@/components/ui-common";

const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
  pending: { label: "等待中", color: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400" },
  parsing: { label: "解析中", color: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
  success: { label: "成功", color: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400" },
  failed: { label: "失败", color: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400" },
};

export function PdfImportPanel() {
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<PdfImportOut | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<number | undefined>();
  const [dragOver, setDragOver] = useState(false);
  const [confirmingId, setConfirmingId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const { data: accounts } = useAccounts(true);
  const {
    data: statements,
    error,
    isLoading,
    mutate: refreshStatements,
  } = useStatements(20);

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
        const result = await uploadPdf(file, selectedAccountId);
        setUploadResult(result);
        refreshStatements();
      } catch (e) {
        if (e instanceof ApiError) {
          setUploadError(e.message);
        } else {
          setUploadError("上传失败，请重试");
        }
      } finally {
        setUploading(false);
      }
    },
    [selectedAccountId, refreshStatements]
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

  const handleConfirm = useCallback(
    async (importId: number) => {
      setConfirmingId(importId);
      try {
        await confirmStatement(importId);
        refreshStatements();
        if (uploadResult?.id === importId) {
          setUploadResult(null);
        }
      } catch (e) {
        console.error("Confirm failed:", e);
      } finally {
        setConfirmingId(null);
      }
    },
    [refreshStatements, uploadResult]
  );

  const handleDelete = useCallback(
    async (importId: number) => {
      try {
        await deleteStatement(importId);
        refreshStatements();
        if (uploadResult?.id === importId) {
          setUploadResult(null);
        }
      } catch (e) {
        console.error("Delete failed:", e);
      }
    },
    [refreshStatements, uploadResult]
  );

  return (
    <div className="space-y-6">
      {/* ─── Upload area ─────────────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-card p-6">
        <div className="mb-4">
          <label className="block text-sm font-medium mb-2">关联账户（可选）</label>
          <select
            value={selectedAccountId || ""}
            onChange={(e) =>
              setSelectedAccountId(e.target.value ? Number(e.target.value) : undefined)
            }
            className="w-full sm:w-auto px-3 py-2 text-sm rounded-lg border border-border bg-background text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">未选择</option>
            {accounts?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
          </select>
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => fileInputRef.current?.click()}
          className={cn(
            "relative flex flex-col items-center justify-center gap-3 py-12 rounded-xl border-2 border-dashed cursor-pointer transition-all",
            dragOver
              ? "border-primary bg-primary/5"
              : "border-border hover:border-muted-foreground/30 hover:bg-muted/30",
            uploading && "pointer-events-none opacity-60"
          )}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf"
            onChange={handleInputChange}
            className="hidden"
          />

          {uploading ? (
            <>
              <LoadingSpinner />
              <p className="text-sm text-muted-foreground">正在上传并解析…</p>
            </>
          ) : (
            <>
              <svg
                className={cn(
                  "h-10 w-10",
                  dragOver ? "text-primary" : "text-muted-foreground/50"
                )}
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                />
              </svg>
              <div className="text-center">
                <p className="text-sm font-medium text-foreground">
                  拖拽 PDF 文件到这里，或点击上传
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  支持：N26、Revolut、TFBank、advanzia、AMEX等格式
                </p>
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

      {/* ─── Upload result / Preview ──────────────────────────────── */}
      {uploadResult && (
        <div className="rounded-xl border border-border bg-card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold">解析结果</h3>
            <span className={cn("px-2 py-0.5 text-xs font-medium rounded-full", STATUS_CONFIG[uploadResult.status]?.color)}>
              {STATUS_CONFIG[uploadResult.status]?.label || uploadResult.status}
            </span>
          </div>

          <div className="space-y-3 mb-4">
            <InfoRow label="文件名" value={uploadResult.filename} />
            <InfoRow label="文件大小" value={formatFileSize(uploadResult.file_size)} />
            {uploadResult.detected_bank && (
              <InfoRow label="识别银行" value={uploadResult.detected_bank} />
            )}
            {uploadResult.statement_period && (
              <InfoRow label="账单周期" value={uploadResult.statement_period} />
            )}
            <InfoRow label="交易笔数" value={`${uploadResult.transactions_count} 笔`} />
          </div>

          {uploadResult.error_message && (
            <div className="mb-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20 text-sm text-destructive">
              {uploadResult.error_message}
            </div>
          )}

          {uploadResult.preview && uploadResult.preview.length > 0 && (
            <div className="mb-4">
              <h4 className="text-sm font-medium mb-2 text-muted-foreground">交易预览</h4>
              <div className="rounded-lg border border-border overflow-hidden">
                <div className="hidden sm:grid grid-cols-4 gap-2 px-3 py-2 text-xs font-medium text-muted-foreground bg-muted/30 border-b border-border">
                  <div>日期</div>
                  <div>描述</div>
                  <div>对方</div>
                  <div className="text-right">金额</div>
                </div>
                {uploadResult.preview.map((tx, i) => (
                  <div
                    key={i}
                    className="grid grid-cols-2 sm:grid-cols-4 gap-2 px-3 py-2 text-sm border-b border-border last:border-b-0"
                  >
                    <div className="text-muted-foreground">{formatDate(tx.occurred_at)}</div>
                    <div className="truncate">{tx.description || "—"}</div>
                    <div className="truncate text-muted-foreground">{tx.counterparty || "—"}</div>
                    <div className="text-right font-medium">
                      <span
                        style={{
                          color:
                            tx.type === "expense"
                              ? "hsl(340, 70%, 55%)"
                              : tx.type === "income"
                              ? "hsl(160, 60%, 45%)"
                              : undefined,
                        }}
                      >
                        {tx.type === "expense" ? "-" : tx.type === "income" ? "+" : ""}
                        {parseFloat(tx.amount).toFixed(2)}
                        {tx.currency}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {uploadResult.status === "success" && uploadResult.transactions_count > 0 && (
            <div className="flex gap-3">
              <button
                onClick={() => handleConfirm(uploadResult.id)}
                disabled={confirmingId === uploadResult.id}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                {confirmingId === uploadResult.id ? "确认中…" : "确认导入"}
              </button>
              <button
                onClick={() => setUploadResult(null)}
                className="px-4 py-2.5 text-sm font-medium rounded-lg border border-border hover:bg-muted transition-colors"
              >
                暂不导入
              </button>
            </div>
          )}

          {uploadResult.status !== "success" && (
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
          <h2 className="text-lg font-semibold">导入记录</h2>
          <button
            onClick={() => refreshStatements()}
            className="p-2 rounded-lg hover:bg-muted transition-colors text-muted-foreground"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>

        {error && !isLoading && (
          <ErrorDisplay
            message={error instanceof ApiError ? error.message : "加载失败"}
            onRetry={() => refreshStatements()}
          />
        )}

        {isLoading && <LoadingSpinner />}

        {!isLoading && !error && (!statements || statements.length === 0) && (
          <div className="text-center py-12 text-muted-foreground text-sm">
            暂无导入记录
          </div>
        )}

        {!isLoading && !error && statements && statements.length > 0 && (
          <div className="space-y-2">
            {statements.map((stmt) => (
              <div
                key={stmt.id}
                className="rounded-xl border border-border bg-card p-4 hover:bg-muted/30 transition-colors"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3 min-w-0">
                    <svg className="h-8 w-8 text-muted-foreground shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-foreground truncate">
                        {stmt.filename}
                      </p>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <span>{formatDate(stmt.created_at)}</span>
                        <span>·</span>
                        <span>{formatFileSize(stmt.file_size)}</span>
                        {stmt.detected_bank && (
                          <>
                            <span>·</span>
                            <span>{stmt.detected_bank}</span>
                          </>
                        )}
                        <span>·</span>
                        <span>{stmt.transactions_count} 笔</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 ml-3">
                    <span className={cn("px-2 py-0.5 text-xs font-medium rounded-full", STATUS_CONFIG[stmt.status]?.color)}>
                      {STATUS_CONFIG[stmt.status]?.label || stmt.status}
                    </span>
                    {stmt.status === "success" && (
                      <button
                        onClick={() => handleConfirm(stmt.id)}
                        disabled={confirmingId === stmt.id}
                        className="p-1.5 rounded-md hover:bg-green-50 dark:hover:bg-green-950/30 transition-colors text-green-600"
                        title="确认导入"
                      >
                        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                        </svg>
                      </button>
                    )}
                    <button
                      onClick={() => handleDelete(stmt.id)}
                      className="p-1.5 rounded-md hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors text-muted-foreground hover:text-destructive"
                      title="删除"
                    >
                      <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                  </div>
                </div>
                {stmt.error_message && (
                  <p className="mt-2 text-xs text-destructive truncate">
                    {stmt.error_message}
                  </p>
                )}
              </div>
            ))}
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
      <span className="text-sm font-medium text-foreground">{value}</span>
    </div>
  );
}
