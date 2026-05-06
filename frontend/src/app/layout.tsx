import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";
import { Sidebar } from "@/components/sidebar";

export const metadata: Metadata = {
  title: "Finance Tracker",
  description: "个人资金管理与记账系统",
};

// Sprint 2 FIX-12 (review V1 §P2-7): the previous version defined an unused
// `DevTokenBootstrap` component (never rendered) AND an inline script that
// referenced `process.env` directly inside browser HTML — `process` is
// undefined in the browser, so the script silently errored.
//
// Next.js inlines `NEXT_PUBLIC_*` env vars into the build at compile time,
// so we read it via the standard `process.env.NEXT_PUBLIC_API_TOKEN` access
// (Next replaces the literal at build) wrapped in a `<Script>` so it runs
// after hydration without polluting the head.
const tokenBootstrapSrc = `
  try {
    var key = "finance_api_token";
    if (!localStorage.getItem(key)) {
      var t = ${JSON.stringify(process.env.NEXT_PUBLIC_API_TOKEN || "")};
      if (t) localStorage.setItem(key, t);
    }
  } catch (e) { /* localStorage may be unavailable in some contexts */ }
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="antialiased">
        <Script id="dev-token-bootstrap" strategy="beforeInteractive">
          {tokenBootstrapSrc}
        </Script>
        <div className="flex h-screen overflow-hidden bg-background">
          <Sidebar />
          <main className="flex-1 overflow-y-auto">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
