/**
 * discover 模式：枚举页面上真实存在的可交互元素。
 *
 * 与 collect.ts 同一被动原则：加载页面后只观察、不操作。枚举结果是探索
 * 计划的唯一素材——计划只允许引用这里出现过的元素，从源头杜绝模型幻觉
 * 出页面上不存在的控件。
 *
 * 页内枚举逻辑必须自包含（page.evaluate 只序列化函数本体）。
 */

import type { DiscoverElement, DiscoverOutcome } from "./protocol.js";

export class DiscoverError extends Error {
  constructor(
    readonly code: "page_load_failed" | "discover_failed",
    message: string,
  ) {
    super(message);
  }
}

/** 页内执行的枚举函数：返回原始元素信息，selector 在 Node 侧组装。 */
function enumerateInteractive(maxElements: number): {
  ok: boolean;
  elements?: Array<Record<string, unknown>>;
  error?: string;
} {
  type Raw = {
    tag: string;
    role: string;
    name: string;
    type?: string;
    testId?: string;
    href?: string;
    visible: boolean;
    id?: string;
    testIdAttr?: string;
    position: number;
  };

  const CANDIDATE =
    "a[href], button, input, select, textarea, [role], [onclick], [contenteditable], [tabindex]";

  const IMPLICIT_ROLE: Readonly<Record<string, string | undefined>> = {
    A: "link",
    BUTTON: "button",
    SELECT: "combobox",
    TEXTAREA: "textbox",
  };

  function inputRole(type: string): string {
    switch (type) {
      case "button":
      case "submit":
      case "reset":
        return "button";
      case "checkbox":
        return "checkbox";
      case "radio":
        return "radio";
      default:
        return "textbox";
    }
  }

  function resolveRole(el: Element): string {
    const explicit = el.getAttribute("role");
    if (explicit) {
      return explicit;
    }
    const tag = el.tagName.toUpperCase();
    const byTag = IMPLICIT_ROLE[tag];
    if (byTag) {
      return byTag;
    }
    if (tag === "INPUT") {
      return inputRole((el as HTMLInputElement).type || "text");
    }
    if (el.getAttribute("contenteditable") === "true" || el.getAttribute("contenteditable") === "") {
      return "textbox";
    }
    return "generic";
  }

  function resolveName(el: Element): string {
    const labeled = el.getAttribute("aria-label");
    if (labeled && labeled.trim()) {
      return labeled.trim().slice(0, 120);
    }
    if (el instanceof HTMLElement) {
      const text = (el.innerText || "").trim().replace(/\s+/g, " ");
      if (text) {
        return text.slice(0, 120);
      }
    }
    if (el instanceof HTMLInputElement) {
      const byValue = (el.value || "").trim();
      if (byValue) {
        return byValue.slice(0, 120);
      }
      const byPlaceholder = (el.placeholder || "").trim();
      if (byPlaceholder) {
        return byPlaceholder.slice(0, 120);
      }
    }
    const byTitle = el.getAttribute("title");
    if (byTitle && byTitle.trim()) {
      return byTitle.trim().slice(0, 120);
    }
    if (el instanceof HTMLImageElement && el.alt) {
      return el.alt.slice(0, 120);
    }
    return "";
  }

  function isVisible(el: Element): boolean {
    if (typeof (el as HTMLElement).checkVisibility === "function") {
      return (el as HTMLElement).checkVisibility();
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  try {
    const all = Array.from(document.querySelectorAll(CANDIDATE)) as HTMLElement[];
    const raws: Raw[] = [];
    let position = 0;
    for (const el of all) {
      if (raws.length >= maxElements) {
        break;
      }
      if (!isVisible(el)) {
        continue;
      }
      const tag = el.tagName.toLowerCase();
      const role = resolveRole(el);
      if (role === "generic" && tag !== "input") {
        // 无语义的包装元素（div[tabindex] 等）对计划没有价值，跳过
        continue;
      }
      const testId = el.getAttribute("data-testid");
      const input = el as HTMLInputElement;
      const anchor = el as HTMLAnchorElement;
      const raw: Raw = {
        tag,
        role,
        name: resolveName(el),
        visible: true,
        position: position++,
      };
      if (tag === "input" || tag === "textarea") {
        raw.type = input.type;
      }
      if (testId) {
        raw.testId = testId;
        raw.testIdAttr = "data-testid";
      }
      if (tag === "a" && anchor.href) {
        raw.href = anchor.href;
      }
      if (el.id) {
        raw.id = el.id;
      }
      raws.push(raw);
    }
    return { ok: true, elements: raws as unknown as Array<Record<string, unknown>> };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

interface RawElement {
  tag: string;
  role: string;
  name: string;
  type?: string;
  testId?: string;
  href?: string;
  visible: boolean;
  id?: string;
  position: number;
}

/**
 * selector 组装（Node 侧）：testId > role+name > text > css。
 * role+name 走 Playwright 的 getByRole 语义，css 只在元素有 id 时给
 * 简单形式——无 id 时 Playwright 的 getByRole/text 兜底已覆盖绝大多数场景。
 */
function buildSelector(raw: RawElement): DiscoverElement["selector"] {
  if (raw.testId) {
    return { strategy: "testId", value: raw.testId };
  }
  if (raw.role !== "generic" && raw.name) {
    return { strategy: "role", value: raw.role, name: raw.name };
  }
  if (raw.name && !["input", "select", "textarea"].includes(raw.tag)) {
    return { strategy: "text", value: raw.name };
  }
  if (raw.id) {
    return { strategy: "css", value: `#${raw.id}` };
  }
  // 兜底：role 无名称（无文本图标按钮等）
  if (raw.role !== "generic") {
    return { strategy: "role", value: raw.role };
  }
  return { strategy: "css", value: raw.tag };
}

export interface DiscoverRequestLike {
  maxElements?: number;
}

export async function discoverElements(
  page: import("playwright-core").Page,
  maxElements: number,
): Promise<{ elements: DiscoverElement[]; title?: string; pageUrl: string }> {
  const result = (await page.evaluate(enumerateInteractive, maxElements)) as unknown as {
    ok: boolean;
    elements?: RawElement[];
    error?: string;
  } | null;
  if (result === null || typeof result !== "object") {
    throw new DiscoverError("discover_failed", "元素枚举未返回结果（脚本未生效）");
  }
  if (!result.ok) {
    throw new DiscoverError("discover_failed", `元素枚举失败：${result.error ?? "未知错误"}`);
  }

  const elements: DiscoverElement[] = (result.elements ?? []).map((raw, position) => ({
    index: position,
    tag: raw.tag,
    role: raw.role,
    name: raw.name,
    ...(raw.type !== undefined ? { type: raw.type } : {}),
    ...(raw.testId !== undefined ? { testId: raw.testId } : {}),
    ...(raw.href !== undefined ? { href: raw.href } : {}),
    visible: raw.visible,
    selector: buildSelector(raw),
  }));
  const title = await page.title().catch(() => undefined);
  return {
    elements,
    ...(title !== undefined ? { title } : {}),
    pageUrl: page.url(),
  };
}

export type { DiscoverOutcome };
