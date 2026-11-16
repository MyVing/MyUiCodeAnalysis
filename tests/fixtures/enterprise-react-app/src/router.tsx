/**
 * 路由表：验证对象式路由（`createBrowserRouter`）能被识别成 route 节点。
 *
 * 注意这里的 JSX 写在模块顶层的配置对象里，不归任何组件 —— 因此它**不产生
 * renders 边**（renders 只认组件函数体内的 JSX）。route 节点与 route→页面
 * 的边由 routes.ts 单独产出，两者互不干扰。
 *
 * 覆盖两条路径语义：
 *   "/users"    绝对路径
 *   ":userId"   相对子路径 → /users/:userId
 */

import { createBrowserRouter } from "react-router-dom";

import UserDetail from "./pages/UserDetail";
import UserList from "./pages/UserList";

export const router = createBrowserRouter([
  {
    path: "/users",
    element: <UserList title="用户列表" onSelect={() => {}} />,
    children: [{ path: ":userId", element: <UserDetail userId="1" /> }],
  },
]);

export default router;
