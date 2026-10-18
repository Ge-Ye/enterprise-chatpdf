import "./globals.css";
import Providers from "@/components/Providers";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-gray-100 min-h-screen p-8">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

