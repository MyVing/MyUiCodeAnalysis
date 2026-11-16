/**
 * RowItem 独立成文件，并经由 barrel（src/index.ts）转发导出。
 * 用于验证：barrel 解析不会把同一组件重复计为两个节点。
 */

import type { UserRowProps } from "@/types/props";

export function RowItem({ userId, name, email, onClick }: UserRowProps) {
  return (
    <div className="row-item" data-user-id={userId} onClick={() => onClick(userId)}>
      <span>{name}</span>
      <span>{email}</span>
    </div>
  );
}
