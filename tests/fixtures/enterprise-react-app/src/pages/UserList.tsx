/**
 * 端到端断言入口：组合别名 import、barrel、单文件多组件、二次封装、服务层请求。
 *
 * M1 验收时，从这个文件出发应能还原出：
 *   UserList -> TableCard -> RowItem
 *   UserList -> ProTable -> antd.Table
 *   UserList -> fetchUsers -> request -> GET /api/users
 * 且每条边都能回溯到本文件中的具体行列号。
 */

import { useCallback, useEffect, useState } from "react";

import { TableCard } from "@/components/TableCard";
import { ProTable } from "@/components/ProTable";
import { request } from "@/services/request";
import type { UserListProps, UserRowProps } from "@/types/props";

/** 缺陷 3 的调用点：经 request 封装发出，url 以字面量给出。 */
async function fetchUsers(): Promise<UserRowProps[]> {
  const result = await request<{ data: UserRowProps[] }>({
    url: "/api/users",
    method: "GET",
  });
  return result.data;
}

export default function UserList({ title, showFilter = true, onSelect }: UserListProps) {
  const [rows, setRows] = useState<UserRowProps[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetchUsers()
      .then(setRows)
      .finally(() => setLoading(false));
  }, []);

  const handleSelect = useCallback(
    (userId: string) => {
      onSelect(userId);
    },
    [onSelect],
  );

  return (
    <section>
      <h1>{title}</h1>
      {showFilter && <input placeholder="搜索用户" />}
      {loading ? (
        <p>加载中…</p>
      ) : (
        <TableCard rows={rows} onSelect={handleSelect} />
      )}
      <ProTable<UserRowProps>
        columns={[
          { title: "姓名", dataIndex: "name" },
          { title: "邮箱", dataIndex: "email" },
        ]}
        rowKey="userId"
        onRowClick={(row) => handleSelect(row.userId)}
      />
    </section>
  );
}
