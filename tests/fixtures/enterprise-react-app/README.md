# enterprise-react-app 验收样本

M1 静态分析的验收基准。刻意模拟企业项目的真实特征，而不是写一个"能跑通就好"的最小项目。

## 覆盖的验收点

| 验收点 | 文件 | 对应缺陷来源 |
|---|---|---|
| 单文件多组件不合并 | `src/components/TableCard.tsx`（`TableCard` + `CardFooter` + 默认导出） | ReactGraph 实测缺陷 1 |
| `export interface XxxProps` 可提取 | `src/types/props.ts` | ReactGraph 实测缺陷 2 |
| `request({ url })` 封装可识别 | `src/services/request.ts` + `src/pages/UserList.tsx:fetchUsers` | ReactGraph 实测缺陷 3 |
| `@/*` 路径别名精确解析，不得退化为按名称匹配 | `tsconfig.json` paths + 各文件的别名 import | ReactGraph 实测缺陷 4 |
| barrel `export *` 不重复计数 | `src/index.ts` | 解析器去重能力 |
| 二次封装映射 | `src/components/ProTable.tsx` → `antd.Table` | 文档 4.3 一期必须有 |
| 调用链端到端 | `src/pages/UserList.tsx` | 文档 M1 验收标准 |

## 期望的图结构（M1 达成标准）

```
UserList ──renders──> TableCard ──renders──> RowItem
         ──renders──> ProTable  ──renders──> antd.Table
         ──calls────> fetchUsers ──requests──> GET /api/users
```

每条边都必须带 `file` + `loc`，能回溯到上面的源码行列。

## 注意

- `package.json` 声明了 `antd` 依赖但**未安装**。M1 若要读取 antd 的 `*.d.ts`，需在样本目录执行 `npm install`，或由适配器内置元数据。
- `ProTable.tsx` 中的 `useProTableData` 是简化实现，只为让静态分析看到"封装组件内部向服务层下沉"的形态，不是可运行代码。
