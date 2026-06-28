import type { Metadata } from "next";
import "./globals.css";
import { Sidebar, MobileNav } from "@/components/sidebar";

export const metadata: Metadata = {
  title: "Finance Tracker",
  description: "个人资金管理与记账系统",
};

// Apply the persisted theme before first paint to avoid a light→dark flash.
const themeBootstrap = `(function(){try{var t=localStorage.getItem('finance_theme')||'light';var r=document.documentElement;if(t==='dark'){r.classList.add('dark');}r.style.colorScheme=t;}catch(e){}})();`;

// Sprint 3 FIX-18 (review V2 §V2-P2-4 closes V1 P2-7 partial):
// the previous build inlined `NEXT_PUBLIC_API_TOKEN` into the JS bundle and
// auto-set it into localStorage on every page load. NEXT_PUBLIC_* values
// are visible to anyone who downloads the bundle, so this leaked the token
// to the public web.
//
// Local-first deployment now relies on either:
//   1. AUTH_DISABLED=true + loopback (dev convenience, no token needed), or
//   2. The user manually pasting their token into Settings → API Token
//      input box (stored in localStorage["finance_api_token"]).
//
// The bundle no longer contains the token, and there is no auto-bootstrap.

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootstrap }} />
      </head>
      <body className="font-sans antialiased">
        <div className="flex h-screen overflow-hidden bg-background">
          <Sidebar />
          <main className="flex-1 overflow-y-auto pb-16 md:pb-0">
            {children}
          </main>
          <MobileNav />
        </div>
      </body>
    </html>
  );
}
