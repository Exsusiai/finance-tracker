import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/sidebar";

export const metadata: Metadata = {
  title: "Finance Tracker",
  description: "个人资金管理与记账系统",
};

function DevTokenBootstrap() {
  // In dev mode, auto-set the API token so the frontend works out of the box
  if (typeof window !== "undefined" && !localStorage.getItem("finance_api_token")) {
    localStorage.setItem("finance_api_token", "dev-token-123");
  }
  return null;
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `if(!localStorage.getItem("finance_api_token"))localStorage.setItem("finance_api_token","dev-token-123");`,
          }}
        />
      </head>
      <body className="antialiased">
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
