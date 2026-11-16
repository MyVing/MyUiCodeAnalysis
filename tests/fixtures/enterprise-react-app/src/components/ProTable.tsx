/**
 * 二次封装样本：ProTable 封装 antd.Table。
 *
 * 预期映射（M1 需在 mapping.json 中声明，不得写死在核心分析器里）：
 *   ProTable            -> antd.Table
 *   props.request       -> dataSource（内部产生一次性 GET 请求）
 *   props.onRowClick    -> onRow
 *   props.columns       -> columns（直传）
 *
 * 核心分析器不允许出现 `if (tag === "ProTable")` 之类的判断。
 */

import { Table } from "antd";

import { request } from "@/services/request";
import type { ProTableProps } from "@/types/props";

export function ProTable<T extends Record<string, unknown>>({
  request: loadData,
  columns,
  rowKey = "id",
  onRowClick,
}: ProTableProps<T>) {
  const rows = loadData ? useProTableData(loadData) : [];

  return (
    <Table
      dataSource={rows}
      columns={columns}
      rowKey={rowKey}
      onRow={(record) => ({ onClick: () => onRowClick?.(record) })}
    />
  );
}

/** 内部 Hook：把 request 声明式地转成一次 GET 请求。 */
function useProTableData<T>(loadData: () => Promise<{ data: T[]; total: number }>): T[] {
  return useProTableRequest(loadData).data;
}

function useProTableRequest<T>(loadData: () => Promise<{ data: T[]; total: number }>) {
  // 真实项目此处是 useRequest / useSWR；这里保持同步签名以便静态分析识别调用链
  return { data: [] as T[], total: 0, request: loadData };
}

export { request };
