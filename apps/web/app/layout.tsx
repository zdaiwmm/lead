import type { Metadata } from "next";
import "@fontsource-variable/inter";
import "./styles.css";
export const metadata: Metadata = {
  title: "lead · 你的 AI 运营人员",
  description: "让 AI 自主运营社媒，清楚查看获客效果与每一项真实工作。",
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
