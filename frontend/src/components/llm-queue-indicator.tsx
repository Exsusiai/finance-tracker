"use client";

import useSWR from "swr";
import { fetchLLMQueue, type LLMQueueStatus } from "@/lib/api";

/**
 * "AI 处理中" banner for the inbox. The L2 LLM classifier drains a paced
 * single-worker queue (~1 every few seconds to stay under the Gemini
 * free-tier rate limit), so bulk imports / refresh-matching take a while
 * to clear. This polls the queue depth and shows progress so the user
 * knows classification is still running rather than stuck.
 *
 * Polls every 3s only while there's outstanding work; when the queue
 * drains it falls back to a slow 30s heartbeat (cheap) so a freshly
 * started batch is picked up without a manual refresh.
 */
export function LlmQueueIndicator() {
  const { data } = useSWR<LLMQueueStatus>("llm-queue", fetchLLMQueue, {
    refreshInterval: (latest) =>
      latest && latest.outstanding > 0 ? 3000 : 30000,
    revalidateOnFocus: true,
  });

  if (!data || data.outstanding <= 0) return null;

  return (
    <div className="mb-4 flex items-center gap-3 rounded-lg border border-primary/30 bg-primary/5 px-4 py-2.5 text-sm">
      <span className="inline-block h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-primary border-t-transparent" />
      <span className="text-foreground">
        ✨ AI 智能分类处理中
        <span className="ml-2 text-muted-foreground">
          剩 {data.outstanding} 笔
          {data.in_flight > 0 && `（处理中 ${data.in_flight}）`}
        </span>
      </span>
      <span className="ml-auto text-xs text-muted-foreground">
        为避开免费额度限速，逐笔处理，请稍候
      </span>
    </div>
  );
}
