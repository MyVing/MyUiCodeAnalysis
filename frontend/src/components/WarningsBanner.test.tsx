import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { WarningsBanner } from "@/components/WarningsBanner";

import type { Warning } from "@uicodeanalysis/contracts";

const infoWarning: Warning = {
  code: "analysis_not_implemented",
  severity: "info",
  stage: "static",
  message: "静态分析将在 M1 实现",
};

const parseError: Warning = {
  code: "parse_error",
  severity: "error",
  stage: "static",
  file: "src/broken.tsx",
  loc: { line: 12, col: 3 },
  message: "无法解析该文件",
};

describe("WarningsBanner", () => {
  it("没有告警时不占用任何空间", () => {
    const { container } = render(<WarningsBanner warnings={[]} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("单条告警直接展示内容与文件位置", () => {
    render(<WarningsBanner warnings={[parseError]} />);

    expect(screen.getByText("错误")).toBeInTheDocument();
    expect(screen.getByText("无法解析该文件")).toBeInTheDocument();
    expect(screen.getByText(/src\/broken\.tsx:12:3/)).toBeInTheDocument();
    // 只有一条时不需要"明细"入口
    expect(screen.queryByText("明细")).not.toBeInTheDocument();
  });

  it("多条告警时首条展示最严重的一条，展开后按严重程度排序", async () => {
    const user = userEvent.setup();
    render(<WarningsBanner warnings={[infoWarning, parseError]} />);

    // 折叠态只显示最严重的一条，info 不占据头部空间
    const header = document.body.textContent ?? "";
    expect(header).toContain("无法解析该文件");
    expect(header).not.toContain("静态分析将在 M1 实现");

    await user.click(screen.getByText("明细"));

    const expanded = document.body.textContent ?? "";
    expect(expanded.indexOf("无法解析该文件")).toBeLessThan(
      expanded.indexOf("静态分析将在 M1 实现"),
    );
  });

  it("明细默认折叠，点击后展开全部告警", async () => {
    const user = userEvent.setup();
    render(<WarningsBanner warnings={[infoWarning, parseError]} />);

    expect(screen.getByText(/2 条分析告警/)).toBeInTheDocument();
    expect(screen.queryByText("[analysis_not_implemented]")).not.toBeInTheDocument();

    await user.click(screen.getByText("明细"));

    expect(screen.getByText("[analysis_not_implemented]")).toBeInTheDocument();
    expect(screen.getByText("[parse_error]")).toBeInTheDocument();
  });
});
