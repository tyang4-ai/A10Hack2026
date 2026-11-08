import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "katex/dist/katex.min.css";
import { DEFAULT_THEME, THEME_BOOT } from "@/lib/theme";
import "./globals.css";

const plexSans = IBM_Plex_Sans({ variable: "--font-plex-sans", subsets: ["latin"], weight: ["400", "500", "600"] });
const plexMono = IBM_Plex_Mono({ variable: "--font-plex-mono", subsets: ["latin"], weight: ["400", "500", "600"] });

export const metadata: Metadata = {
  title: "Axelrod",
  description: "Self-verifying research harness",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // data-theme is replaced before paint by the saved choice, hence suppressHydrationWarning.
    <html lang="en" data-theme={DEFAULT_THEME} suppressHydrationWarning className={`${plexSans.variable} ${plexMono.variable} h-full antialiased`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body className="min-h-full">{children}</body>
    </html>
  );
}
