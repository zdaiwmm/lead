import type { Metadata } from "next";
import "./styles.css";
export const metadata: Metadata = {
  title: "lead · 你的 AI 运营人员",
  description: "与 AI 运营人员对话，了解每一项真实工作。",
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
