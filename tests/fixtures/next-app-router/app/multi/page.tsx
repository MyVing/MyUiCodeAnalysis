/**
 * 同文件多组件：页面组件必须取 default export，而不是"文件里第一个组件"。
 * 若取错，route 会连到 PageHeader 上。
 */
export function PageHeader() {
  return <h1>多组件页面</h1>;
}

export default function MultiPage() {
  return (
    <section>
      <PageHeader />
    </section>
  );
}
