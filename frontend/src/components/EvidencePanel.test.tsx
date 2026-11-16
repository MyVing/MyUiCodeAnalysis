import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EvidencePanel } from "@/components/EvidencePanel";
import type { Evidence } from "@uicodeanalysis/contracts";

const evidence: Evidence[] = [
  { source: "static", confidence: 0.9, file: "src/Table.tsx", loc: { line: 12, col: 3 } },
  { source: "runtime", confidence: 1, traceId: "trc_1" },
  { source: "ai", confidence: 0.55 },
];

describe("EvidencePanel", () => {
  it("keeps facts and AI inference visually distinguishable", () => {
    render(<EvidencePanel evidence={evidence} />);

    expect(screen.getByText("静态事实")).toBeInTheDocument();
    expect(screen.getByText("运行时事实")).toBeInTheDocument();
    expect(screen.getByText("AI 推断")).toBeInTheDocument();

    expect(screen.getByText(/src\/Table\.tsx:12:3/)).toBeInTheDocument();
    expect(screen.getByText(/trace: trc_1/)).toBeInTheDocument();
    expect(screen.getByText(/置信度 55%/)).toBeInTheDocument();
    expect(screen.getByText("推断结论，需人工确认或驳回。")).toBeInTheDocument();
  });
});
