import type { Metadata } from "next";
import type { ReactNode } from "react";
import Shell from "@/components/Shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Kronos Command Center",
  description: "Read-only monitoring of the Kronos shadow forward tests (XAUUSD, BTC/USDT).",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
