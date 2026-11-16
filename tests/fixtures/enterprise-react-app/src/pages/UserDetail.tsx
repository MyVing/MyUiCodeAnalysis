/**
 * 第二个页面：证明「多页面可区分」与动态路由参数（`/users/:userId`）。
 *
 * 刻意**不被 UserList 引用**，且只渲染宿主元素：
 *  - 不被引用 → 不产生新的 renders 边（renders.test.ts 的边数断言依赖这一点）
 *  - 只渲染宿主元素 → 即使被引用也不产边
 *
 * 它同时提供若干条可见文案（"用户详情" / "编号" / "状态" / "待审核"），
 * 用于验证需求召回能否只凭页面文案区分两个页面。
 */

export interface UserDetailProps {
  userId: string;
}

export default function UserDetail({ userId }: UserDetailProps) {
  return (
    <section>
      <h2>用户详情</h2>
      <dl>
        <dt>编号</dt>
        <dd>{userId}</dd>
        <dt>状态</dt>
        <dd>待审核</dd>
      </dl>
    </section>
  );
}
