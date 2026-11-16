/** layout 不是页面，不产生 route 节点 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html><body>{children}</body></html>;
}
