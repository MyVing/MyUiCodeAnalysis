/**
 * 缺陷 2：`export interface XxxProps` 必须能被提取。
 * ReactGraph 因为只遍历顶层节点、漏掉 ExportNamedDeclaration 包裹的类型声明，
 * 在此样本上会得到空 props。
 */

export interface UserListProps {
  /** 列表标题 */
  title: string;
  /** 是否显示筛选栏 */
  showFilter?: boolean;
  /** 选中回调，由页面注入 */
  onSelect: (userId: string) => void;
}

export interface UserRowProps {
  userId: string;
  name: string;
  email: string;
  onClick: (userId: string) => void;
}

export interface ProTableProps<T> {
  /** 二次封装的请求方法：内部会产生一次 GET 请求 */
  request?: () => Promise<{ data: T[]; total: number }>;
  columns?: Array<{ title: string; dataIndex: string }>;
  rowKey?: string;
  onRowClick?: (row: T) => void;
}
