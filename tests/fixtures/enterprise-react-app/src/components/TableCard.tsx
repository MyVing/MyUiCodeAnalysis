/**
 * 缺陷 1：单文件多组件必须全部保留。
 * ReactGraph 按文件生成节点 id，去重后只剩 1 个组件。
 *
 * 同一文件内有三个导出组件：TableCard（默认）、RowItem、CardFooter。
 */

import type { UserRowProps } from "@/types/props";

import { RowItem } from "./RowItem";

export interface TableCardProps {
  rows: UserRowProps[];
  onSelect: (userId: string) => void;
}

export function CardFooter({ total }: { total: number }) {
  return <div className="card-footer">共 {total} 条</div>;
}

export function TableCard({ rows, onSelect }: TableCardProps) {
  return (
    <div className="table-card">
      {rows.map((row) => (
        <RowItem key={row.userId} {...row} onClick={onSelect} />
      ))}
      <CardFooter total={rows.length} />
    </div>
  );
}

export default TableCard;
