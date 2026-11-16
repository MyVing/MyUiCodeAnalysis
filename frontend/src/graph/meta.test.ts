import { describe, expect, it } from "vitest";

import { formatConfidence, primarySource } from "./meta";

describe("primarySource", () => {
  it("prefers runtime over static over ai", () => {
    expect(
      primarySource([
        { source: "ai", confidence: 0.5 },
        { source: "static", confidence: 1 },
        { source: "runtime", confidence: 1 },
      ]),
    ).toBe("runtime");

    expect(
      primarySource([
        { source: "ai", confidence: 0.5 },
        { source: "static", confidence: 0.9 },
      ]),
    ).toBe("static");

    expect(primarySource([{ source: "ai", confidence: 0.5 }])).toBe("ai");
  });

  it("formats confidence as a rounded percentage", () => {
    expect(formatConfidence(0.555)).toBe("56%");
    expect(formatConfidence(1)).toBe("100%");
  });
});
