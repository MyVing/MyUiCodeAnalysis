import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("startAnalysis returns the created task", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ id: "task_1", status: "queued", analysisId: "an_1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const task = await api.startAnalysis("prj_1", {
      requirement: "用户列表增加状态筛选",
      baseCommit: "abc123",
      targetCommit: "def456",
    });

    expect(task).toMatchObject({ id: "task_1", status: "queued" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/projects/prj_1/analyses");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        requirement: "用户列表增加状态筛选",
        baseCommit: "abc123",
        targetCommit: "def456",
      }),
    );
  });

  it("startAnalysis omits the body when no advanced options are set", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 202, json: async () => ({ id: "task_2" }) });
    vi.stubGlobal("fetch", fetchMock);

    await api.startAnalysis("prj_1");

    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("getTask polls the task endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: "task_1", status: "succeeded" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const task = await api.getTask("task_1");

    expect(task.status).toBe("succeeded");
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/v1/tasks/task_1");
  });

  it("surfaces the FastAPI detail on error responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ detail: "该项目已有进行中的分析任务" }),
      }),
    );

    await expect(api.startAnalysis("prj_1")).rejects.toMatchObject({
      status: 409,
      message: "该项目已有进行中的分析任务",
    });
  });

  it("sends project payloads as JSON with camelCase fields", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 201, json: async () => ({ id: "prj_1" }) });
    vi.stubGlobal("fetch", fetchMock);

    const created = await api.createProject({
      name: "订单平台",
      rootPath: "D:/work/orders",
      framework: "next",
    });

    expect(created).toEqual({ id: "prj_1" });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/projects");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({ name: "订单平台", rootPath: "D:/work/orders", framework: "next" }),
    );
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });
});
