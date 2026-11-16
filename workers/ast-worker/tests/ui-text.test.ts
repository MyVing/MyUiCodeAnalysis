/**
 * UI 文案提取测试。
 *
 * 分两部分：
 *  - 真实样本仓：验证 README 里声明的那几条链路上的文案都能提到
 *  - 合成项目：验证真实样本没覆盖到的来源（i18n、模板字符串、白名单外属性、
 *    组件外常量、超长截断等）与边界
 *
 * 合成项目写临时目录而不是内存注入：归属判定依赖真实的文件路径。
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import type { Warning } from "@uicodeanalysis/contracts";

import { discoverComponents } from "../src/components.js";
import { createFullProject } from "../src/project.js";
import { analyzeUiText, type ComponentText } from "../src/ui-text.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(here, "..", "..", "..");
const FIXTURE_ROOT = path.join(REPO_ROOT, "tests", "fixtures", "enterprise-react-app");

interface AnalysisResult {
  byComponent: Map<string, ComponentText>;
  warnings: Warning[];
}

function analyze(rootPath: string): AnalysisResult {
  const { project } = createFullProject(rootPath);
  const discovery = discoverComponents(rootPath, project);
  const uiText = analyzeUiText({
    rootPath,
    components: discovery.components,
    declarations: discovery.declarations,
    project,
  });
  return { byComponent: uiText.byComponent, warnings: uiText.warnings };
}

const tempDirs: string[] = [];

function analyzeSynthetic(files: Record<string, string>): AnalysisResult {
  const dir = mkdtempSync(path.join(tmpdir(), "ast-worker-ui-text-"));
  tempDirs.push(dir);
  for (const [relative, content] of Object.entries(files)) {
    const absolute = path.join(dir, relative);
    mkdirSync(path.dirname(absolute), { recursive: true });
    writeFileSync(absolute, content, "utf8");
  }
  return analyze(dir);
}

afterAll(() => {
  for (const dir of tempDirs) {
    rmSync(dir, { recursive: true, force: true });
  }
});

/** 取某组件的全部文案，便于断言；缺失直接失败并列出实际组件。 */
function textsOf(result: AnalysisResult, componentId: string): ComponentText {
  const entry = result.byComponent.get(componentId);
  if (!entry) {
    throw new Error(
      `缺少组件文案 ${componentId}；实际组件：\n${[...result.byComponent.keys()].join("\n")}`,
    );
  }
  return entry;
}

/** 只取文案原文，断言更直观。 */
function textValues(result: AnalysisResult, componentId: string): string[] {
  return textsOf(result, componentId).texts.map((entry) => entry.text);
}

describe("UI 文案（真实样本仓）", () => {
  let result: AnalysisResult;

  beforeAll(() => {
    result = analyze(FIXTURE_ROOT);
  });

  it("提得到 JSX 文本节点里的文案", () => {
    // UserList 里有 <p>加载中…</p> 与 <h1>{title}</h1>
    const texts = textValues(result, "src/pages/UserList.tsx#UserList");
    expect(texts).toContain("加载中…");
  });

  it("提得到白名单属性里的文案，并记录属性名", () => {
    const entry = textsOf(result, "src/pages/UserList.tsx#UserList");
    const placeholder = entry.texts.find((item) => item.text === "搜索用户");
    expect(placeholder).toBeDefined();
    expect(placeholder?.kind).toBe("attr");
    expect(placeholder?.via).toBe("placeholder");
  });

  it("提得到 columns 配置里的列名（对象字面量的文本键）", () => {
    const texts = textValues(result, "src/pages/UserList.tsx#UserList");
    expect(texts).toContain("姓名");
    expect(texts).toContain("邮箱");
  });

  it("变量形式的文案记为 dynamic，而不是当作没有文案", () => {
    const entry = textsOf(result, "src/pages/UserList.tsx#UserList");
    const dynamic = entry.dynamic.map((item) => item.expr);
    // <h1>{title}</h1> 里的 title 来自 props，字面量不在本文件
    expect(dynamic).toContain("title");
  });

  it("每条文案都带可回溯的行列号", () => {
    const entry = textsOf(result, "src/pages/UserList.tsx#UserList");
    for (const item of entry.texts) {
      expect(item.loc.line).toBeGreaterThan(0);
      expect(item.loc.col).toBeGreaterThan(0);
    }
  });

  it("className 这类非文案属性不被误收", () => {
    const allTexts = [...result.byComponent.values()].flatMap((entry) =>
      entry.texts.map((item) => item.text),
    );
    for (const noise of ["flex", "items-center", "gap-2", "true", "false", "div"]) {
      expect(allTexts).not.toContain(noise);
    }
  });
});

describe("UI 文案的边界（合成项目）", () => {
  it("模板字符串只取静态片段，整体记为 dynamic", () => {
    const result = analyzeSynthetic({
      "src/Count.tsx": `
        export function Count({ total }: { total: number }) {
          return <p>{\`共\${total}条记录\`}</p>;
        }
      `,
    });

    const texts = textValues(result, "src/Count.tsx#Count");
    expect(texts).toContain("共");
    expect(texts).toContain("条记录");
    // 插值部分定不下来，整体表达式必须记成 dynamic
    expect(textsOf(result, "src/Count.tsx#Count").dynamic.length).toBe(1);
  });

  it("i18n 取词调用记下键名", () => {
    const result = analyzeSynthetic({
      "src/I18n.tsx": `
        import { useTranslation } from "react-i18next";
        export function I18n() {
          const { t } = useTranslation();
          return <div><span>{t("user.list.name")}</span></div>;
        }
      `,
    });

    const entry = textsOf(result, "src/I18n.tsx#I18n");
    const hit = entry.texts.find((item) => item.text === "user.list.name");
    expect(hit?.kind).toBe("i18n");
  });

  it("formatMessage 的对象形态取 id 字段", () => {
    const result = analyzeSynthetic({
      "src/Intl.tsx": `
        export function Intl({ intl }: { intl: { formatMessage: (m: unknown) => string } }) {
          return <div>{intl.formatMessage({ id: "order.status.pending" })}</div>;
        }
      `,
    });

    const texts = textValues(result, "src/Intl.tsx#Intl");
    expect(texts).toContain("order.status.pending");
  });

  it("字符串数组字面量收为选项文案", () => {
    const result = analyzeSynthetic({
      "src/Select.tsx": `
        export function Select() {
          const options = ["正常", "禁用", "待审核"];
          return <div data-options={options} />;
        }
      `,
    });

    const texts = textValues(result, "src/Select.tsx#Select");
    expect(texts).toEqual(expect.arrayContaining(["正常", "禁用", "待审核"]));
  });

  it("纯英文单词组成的配置数组不被误收", () => {
    const result = analyzeSynthetic({
      "src/Units.tsx": `
        export function Units() {
          const units = ["px", "rem", "em"];
          return <div data-units={units} />;
        }
      `,
    });

    // 三个都是全小写单次，没有空格、没有中文、没有大写开头 → 不是 UI 文案
    expect(result.byComponent.has("src/Units.tsx#Units")).toBe(false);
  });

  it("数字与纯符号文本不被误收", () => {
    const result = analyzeSynthetic({
      "src/Stars.tsx": `
        export function Stars() {
          return <div><span>*****</span><span>12345</span><span>—</span></div>;
        }
      `,
    });

    expect(result.byComponent.has("src/Stars.tsx#Stars")).toBe(false);
  });

  it("同一个文案在一个组件里只记一次，位置取首次出现", () => {
    const result = analyzeSynthetic({
      "src/Dup.tsx": `
        export function Dup() {
          return <div><span>保存</span><span>保存</span></div>;
        }
      `,
    });

    const texts = textValues(result, "src/Dup.tsx#Dup");
    expect(texts.filter((item) => item === "保存")).toHaveLength(1);
  });

  it("组件外的模块级常量不归属任何组件，并记 info 告警", () => {
    const result = analyzeSynthetic({
      "src/Columns.tsx": `
        const columns = [{ title: "模块级列名" }];
        export function Columns() {
          return <div data-columns={columns} />;
        }
      `,
    });

    // 不猜测归属：这个组件自身没有可见文案
    expect(result.byComponent.has("src/Columns.tsx#Columns")).toBe(false);
    const warning = result.warnings.find((item) => item.code === "ui_text_unassigned");
    expect(warning?.severity).toBe("info");
  });

  it("HTML 实体解码后再入库", () => {
    const result = analyzeSynthetic({
      "src/Entity.tsx": `
        export function Entity() {
          return <div><span>保存 &amp; 提交</span></div>;
        }
      `,
    });

    expect(textValues(result, "src/Entity.tsx#Entity")).toContain("保存 & 提交");
  });

  it("JSX 多行文本折叠为单空格", () => {
    const result = analyzeSynthetic({
      "src/Multi.tsx": `
        export function Multi() {
          return (
            <p>
              第一行
              第二行
            </p>
          );
        }
      `,
    });

    expect(textValues(result, "src/Multi.tsx#Multi")).toContain("第一行 第二行");
  });

  it("超过上限时截断并如实告警，不静默丢弃", () => {
    const items = Array.from({ length: 100 }, (_, index) => `<span>第${index}项</span>`).join("\n");
    const result = analyzeSynthetic({
      "src/Many.tsx": `
        export function Many() {
          return <div>
            ${items}
          </div>;
        }
      `,
    });

    const entry = textsOf(result, "src/Many.tsx#Many");
    expect(entry.texts).toHaveLength(80);
    expect(entry.truncated).toBe(20);

    const warning = result.warnings.find((item) => item.code === "ui_text_truncated");
    expect(warning?.severity).toBe("info");
  });

  it("普通的 className / id / data-* 属性不被当作文案", () => {
    const result = analyzeSynthetic({
      "src/Attrs.tsx": `
        export function Attrs() {
          return <div className="flex gap-2" id="root" data-testid="panel" title="面板标题" />;
        }
      `,
    });

    const texts = textValues(result, "src/Attrs.tsx#Attrs");
    expect(texts).toEqual(["面板标题"]);
  });
});
