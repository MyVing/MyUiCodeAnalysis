/**
 * props 分析测试。
 *
 * 重点不是"能展开"，而是几个容易做错的判定：
 *  - 可选性必须来自问号，不能来自类型文本里的 `undefined`
 *  - 类型文本要保留源码写法（命名类型不被展开成匿名结构）
 *  - 默认值只能来自解构，类型系统不记录它
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, afterAll, describe, expect, it } from "vitest";

import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";
import { analyzeProps, type ComponentProps, type PropsAnalysisResult } from "../src/props.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

function analyze(rootPath: string): PropsAnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  return analyzeProps({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
  });
}

/** 按组件 id 取 props；不存在直接失败，避免断言静默跳过。 */
function propsOf(result: PropsAnalysisResult, componentId: string): ComponentProps {
  const props = result.byComponent.get(componentId);
  if (!props) {
    throw new Error(
      `缺少组件 ${componentId} 的 props；实际：\n${[...result.byComponent.keys()].join("\n")}`,
    );
  }
  return props;
}

/** 按字段名取字段，并把类型收窄掉可选性，省掉每处 `?.`。 */
function fieldOf(props: ComponentProps, name: string) {
  const field = props.fields.find((f) => f.name === name);
  if (!field) {
    throw new Error(`缺少字段 ${name}；实际：${props.fields.map((f) => f.name).join(",")}`);
  }
  return field;
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): PropsAnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-props-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir);
}

afterAll(() => {
  for (const dir of tempDirs) rmSync(dir, { recursive: true, force: true });
});

describe("props 展开（真实样本仓）", () => {
  let result: PropsAnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("跨文件的 export interface 可展开，并记录类型来源文件", () => {
    const props = propsOf(result, "src/pages/UserList.tsx#UserList");
    expect(props.typeName).toBe("UserListProps");
    expect(props.typeFile).toBe("src/types/props.ts");
    expect(props.fields.map((f) => f.name)).toEqual(["title", "showFilter", "onSelect"]);
  });

  it("字段类型保留源码写法，不展开命名类型", () => {
    const tableCard = propsOf(result, "src/components/TableCard.tsx#TableCard");
    // 必须是 UserRowProps[]，而不是展开后的 { userId: string; ... }[]
    expect(fieldOf(tableCard, "rows").type).toBe("UserRowProps[]");
  });

  it("可选性来自问号，且类型文本不掺入 undefined", () => {
    const userList = propsOf(result, "src/pages/UserList.tsx#UserList");

    const optionalField = fieldOf(userList, "showFilter");
    expect(optionalField.optional).toBe(true);
    // `?` 引入的 undefined 是类型系统的产物，不该展示给用户
    expect(optionalField.type).toBe("boolean");

    const requiredField = fieldOf(userList, "title");
    expect(requiredField.optional).toBe(false);
    expect(requiredField.type).toBe("string");
  });

  it("函数类型字段完整保留签名", () => {
    const userList = propsOf(result, "src/pages/UserList.tsx#UserList");
    expect(fieldOf(userList, "onSelect").type).toBe("(userId: string) => void");
  });

  it("默认值从解构取到，且是源码文本", () => {
    const userList = propsOf(result, "src/pages/UserList.tsx#UserList");
    expect(fieldOf(userList, "showFilter").defaultValue).toBe("true");

    const proTable = propsOf(result, "src/components/ProTable.tsx#ProTable");
    expect(fieldOf(proTable, "rowKey").defaultValue).toBe('"id"');

    // 没有默认值的字段不能编造
    expect(fieldOf(proTable, "columns").defaultValue).toBeNull();
  });

  it("JSDoc 注释作为字段说明", () => {
    const userList = propsOf(result, "src/pages/UserList.tsx#UserList");
    expect(fieldOf(userList, "title").description).toBe("列表标题");
    expect(fieldOf(userList, "showFilter").description).toBe("是否显示筛选栏");
  });

  it("泛型 props 标记为 generic，并展开全部字段", () => {
    const proTable = propsOf(result, "src/components/ProTable.tsx#ProTable");
    expect(proTable.typeName).toBe("ProTableProps");
    expect(proTable.generic).toBe(true);
    expect(proTable.fields.map((f) => f.name)).toEqual([
      "request",
      "columns",
      "rowKey",
      "onRowClick",
    ]);
  });

  it("内联类型字面量也能展开，但 typeName 为 null", () => {
    const cardFooter = propsOf(result, "src/components/TableCard.tsx#CardFooter");
    expect(cardFooter.typeName).toBeNull();
    expect(cardFooter.fields).toHaveLength(1);
    expect(fieldOf(cardFooter, "total").type).toBe("number");
  });

  it("字段位置可回溯到声明处", () => {
    const userList = propsOf(result, "src/pages/UserList.tsx#UserList");
    const title = fieldOf(userList, "title");
    expect(title.file).toBe("src/types/props.ts");
    expect(title.loc).toEqual({ line: 9, col: 3 });
  });

  it("同一 props 类型被多个组件复用时不串数据", () => {
    // RowItem 直接用 UserRowProps；TableCard 的字段引用了它但不该把其字段并入自己
    const rowItem = propsOf(result, "src/components/RowItem.tsx#RowItem");
    expect(rowItem.fields.map((f) => f.name)).toEqual(["userId", "name", "email", "onClick"]);

    const tableCard = propsOf(result, "src/components/TableCard.tsx#TableCard");
    expect(tableCard.fields.map((f) => f.name)).toEqual(["rows", "onSelect"]);
  });

  it("每个项目内组件都有 props 分析结果（哪怕是空字段列表）", () => {
    for (const id of [
      "src/pages/UserList.tsx#UserList",
      "src/components/TableCard.tsx#TableCard",
      "src/components/TableCard.tsx#CardFooter",
      "src/components/RowItem.tsx#RowItem",
      "src/components/ProTable.tsx#ProTable",
    ]) {
      expect(result.byComponent.has(id)).toBe(true);
    }
  });
});

describe("props 展开的边界（合成项目）", () => {
  it("无 props 参数的组件返回空字段列表而不是缺失", () => {
    const result = analyzeSynthetic({
      "src/NoProps.tsx": `export function NoProps() { return <div />; }`,
    });

    const props = propsOf(result, "src/NoProps.tsx#NoProps");
    expect(props.fields).toEqual([]);
    expect(props.typeName).toBeNull();
  });

  it("无类型标注但解构了参数时，字段类型来自推断", () => {
    const result = analyzeSynthetic({
      "src/Inferred.tsx": `export function Inferred({ label }: { label: string }) { return <span>{label}</span>; }`,
    });

    const props = propsOf(result, "src/Inferred.tsx#Inferred");
    // 内联字面量：字段可展开，但没有类型名
    expect(props.typeName).toBeNull();
    expect(fieldOf(props, "label").type).toBe("string");
    expect(fieldOf(props, "label").optional).toBe(false);
  });

  it("解构重命名时以对外属性名为准，默认值仍能对上", () => {
    const result = analyzeSynthetic({
      "src/Rename.tsx": `
        interface RenameProps { onDone?: () => void; }
        export function Rename({ onDone: callback = () => {} }: RenameProps) {
          return <button onClick={callback} />;
        }
      `,
    });

    const props = propsOf(result, "src/Rename.tsx#Rename");
    const field = fieldOf(props, "onDone");
    // 属性名是 onDone，不是解构出来的局部名 callback
    expect(field.defaultValue).toBe("() => {}");
  });

  it("type alias 写的 props 也能展开", () => {
    const result = analyzeSynthetic({
      "src/Alias.tsx": `
        export type AliasProps = { size: number; label?: string };
        export function Alias(props: AliasProps) { return <div>{props.label}</div>; }
      `,
    });

    const props = propsOf(result, "src/Alias.tsx#Alias");
    expect(props.fields.map((f) => f.name)).toEqual(["size", "label"]);
    expect(fieldOf(props, "label").optional).toBe(true);
    expect(fieldOf(props, "label").type).toBe("string");
  });

  it("显式可空（非可选）不被误判为可选", () => {
    const result = analyzeSynthetic({
      "src/Nullable.tsx": `
        export interface NullableProps { value: string | undefined; }
        export function Nullable({ value }: NullableProps) { return <div>{value}</div>; }
      `,
    });

    const props = propsOf(result, "src/Nullable.tsx#Nullable");
    const field = fieldOf(props, "value");
    // 没有问号 = 调用方必须传（哪怕可以传 undefined）
    expect(field.optional).toBe(false);
    expect(field.type).toBe("string | undefined");
  });

  it("多个组件共用同一 props 类型时各自独立展开", () => {
    const result = analyzeSynthetic({
      "src/Shared.tsx": `
        export interface SharedProps { id: number; }
        export function First({ id }: SharedProps) { return <div>{id}</div>; }
        export function Second({ id = 1 }: SharedProps) { return <span>{id}</span>; }
      `,
    });

    const first = propsOf(result, "src/Shared.tsx#First");
    const second = propsOf(result, "src/Shared.tsx#Second");

    // 默认值属于各自签名，不能互相串
    expect(fieldOf(first, "id").defaultValue).toBeNull();
    expect(fieldOf(second, "id").defaultValue).toBe("1");
  });
});
