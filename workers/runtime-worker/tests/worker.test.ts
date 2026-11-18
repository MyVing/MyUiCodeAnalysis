import { describe, expect, it } from "vitest";

import { parseServerPorts, toBaseUrl } from "../src/ports.js";
import { normalizeSourceFile, toUnixSeconds } from "../src/normalize.js";
import { parseRequest } from "../src/parse-request.js";
import type { RuntimeWorkerRequest } from "../src/protocol.js";

describe("parseServerPorts", () => {
  it("解析 vite 的 Local 行", () => {
    const output = [
      "  VITE v7.1.2  ready in 312 ms",
      "",
      "  ➜  Local:   http://localhost:5173/",
      "  ➜  Network: use --host to expose",
    ].join("\n");
    expect(parseServerPorts(output)).toEqual([5173]);
  });

  it("端口被占用时 vite 换端口，取解析到的实际端口", () => {
    const output = "  ➜  Local:   http://localhost:5174/\n";
    expect(parseServerPorts(output)).toEqual([5174]);
  });

  it("vite 7 的 ANSI 粗体码隔开端口数字也能解析", () => {
    const output = "  ➜  Local:   http://localhost:\x1b[1m5199\x1b[22m/\n";
    expect(parseServerPorts(output)).toEqual([5199]);
  });

  it("识别 CRA/webpack 风格的 localhost 行", () => {
    const output = "You can now view app in the browser.\n  Local:            http://localhost:3000/\n";
    expect(parseServerPorts(output)).toEqual([3000]);
  });

  it("忽略 network 行中的外网地址，只认回环地址", () => {
    const output = "  ➜  Network: http://192.168.1.5:5173/\n  ➜  Local:   http://127.0.0.1:5173/\n";
    expect(parseServerPorts(output)).toEqual([5173]);
  });

  it("去重且按出现顺序", () => {
    const output = "Local: http://localhost:8000/\nLocal: http://localhost:8000/\n";
    expect(parseServerPorts(output)).toEqual([8000]);
  });

  it("无匹配返回空数组", () => {
    expect(parseServerPorts("nothing here")).toEqual([]);
    expect(parseServerPorts("")).toEqual([]);
  });

  it("拒绝非法端口数字", () => {
    expect(parseServerPorts("Local: http://localhost:99999/\n")).toEqual([]);
    expect(parseServerPorts("Local: http://localhost:abc/\n")).toEqual([]);
  });
});

describe("toBaseUrl", () => {
  it("拼接 base url", () => {
    expect(toBaseUrl("localhost", 5173)).toBe("http://localhost:5173/");
  });
});

describe("normalizeSourceFile", () => {
  const root = "D:/work/project";

  it("vite webroot 相对路径去开头斜杠", () => {
    expect(normalizeSourceFile("/src/App.tsx", root)).toBe("src/App.tsx");
  });

  it("完整 http URL 只留路径", () => {
    expect(normalizeSourceFile("http://localhost:5173/src/pages/UserList.tsx", root)).toBe(
      "src/pages/UserList.tsx",
    );
  });

  it("file:// URL 转为相对 rootPath 的 POSIX 路径", () => {
    expect(normalizeSourceFile("file:///D:/work/project/src/a.tsx", root)).toBe("src/a.tsx");
  });

  it("Windows 盘符被当 webroot（/D:/...）时剥掉盘符段", () => {
    expect(normalizeSourceFile("/D:/work/project/src/a.tsx", root)).toBe("src/a.tsx");
  });

  it("rootPath 内的绝对路径归一化为相对路径", () => {
    expect(normalizeSourceFile("D:/work/project/src/components/ProTable.tsx", root)).toBe(
      "src/components/ProTable.tsx",
    );
  });

  it("大小写不敏感匹配 rootPath", () => {
    expect(normalizeSourceFile("D:/WORK/project/src/a.tsx", root)).toBe("src/a.tsx");
  });

  it("去掉查询串与 hash", () => {
    expect(normalizeSourceFile("/src/App.tsx?t=1700000000#L3", root)).toBe("src/App.tsx");
  });

  it("反斜杠统一为正斜杠", () => {
    expect(normalizeSourceFile("D:\\work\\project\\src\\a.tsx", root)).toBe("src/a.tsx");
  });

  it("webroot 下任意路径按项目内处理（dev server 只服务项目文件）", () => {
    expect(normalizeSourceFile("/etc/passwd", root)).toBe("etc/passwd");
  });

  it("越出 rootPath 的 ../ 路径返回 undefined", () => {
    expect(normalizeSourceFile("/src/../secrets/x.tsx", root)).toBeUndefined();
  });

  it("空值返回 undefined", () => {
    expect(normalizeSourceFile("", root)).toBeUndefined();
  });
});

describe("toUnixSeconds", () => {
  it("毫秒转秒并向下取整", () => {
    expect(toUnixSeconds(1700000000123)).toBe(1700000000);
  });
});

describe("parseRequest", () => {
  const base: Partial<RuntimeWorkerRequest> = {
    projectId: "prj_1",
    rootPath: process.cwd(),
    traceId: "trace_1",
  };

  it("url 与 command 缺一不可", () => {
    expect(() => parseRequest(JSON.stringify(base))).toThrow("url 与 command");
  });

  it("url 模式合法", () => {
    const request = parseRequest(JSON.stringify({ ...base, url: "http://localhost:5173/" }));
    expect(request.url).toBe("http://localhost:5173/");
  });

  it("command 模式合法", () => {
    const request = parseRequest(JSON.stringify({ ...base, command: "npm run dev" }));
    expect(request.command).toBe("npm run dev");
  });

  it("缺必填字段报错", () => {
    expect(() => parseRequest(JSON.stringify({ traceId: "t" }))).toThrow("projectId");
  });

  it("rootPath 不存在报错", () => {
    expect(() =>
      parseRequest(JSON.stringify({ ...base, url: "http://x/", rootPath: "D:/__no_such_dir__" })),
    ).toThrow("rootPath");
  });

  it("非法 JSON 抛 SyntaxError", () => {
    expect(() => parseRequest("{bad json")).toThrow();
  });

  describe("explore 模式", () => {
    const url = { url: "http://localhost:5173/" };
    const exploreBase = { ...base, ...url, mode: "explore", artifactsDir: process.cwd() };
    const step = {
      index: 0,
      action: "click",
      selector: { strategy: "role", value: "button", name: "查询" },
    };

    it("explore 缺 steps 报错", () => {
      expect(() => parseRequest(JSON.stringify(exploreBase))).toThrow("steps");
    });

    it("explore 缺 artifactsDir 报错", () => {
      const { artifactsDir: _dir, ...withoutDir } = exploreBase;
      expect(() =>
        parseRequest(JSON.stringify({ ...withoutDir, steps: [step] })),
      ).toThrow("artifactsDir");
    });

    it("合法计划解析成功并保留 mode", () => {
      const request = parseRequest(
        JSON.stringify({ ...exploreBase, steps: [step, { index: 1, action: "wait", value: "500" }] }),
      );
      expect(request.mode).toBe("explore");
      expect(request.steps).toHaveLength(2);
    });

    it("目标动作缺 selector 报错", () => {
      expect(() =>
        parseRequest(
          JSON.stringify({ ...exploreBase, steps: [{ index: 0, action: "click" }] }),
        ),
      ).toThrow("selector");
    });

    it("fill 缺 value 报错", () => {
      expect(() =>
        parseRequest(
          JSON.stringify({
            ...exploreBase,
            steps: [
              {
                index: 0,
                action: "fill",
                selector: { strategy: "role", value: "textbox", name: "搜索" },
              },
            ],
          }),
        ),
      ).toThrow("value");
    });

    it("assert 缺 assertion 报错", () => {
      expect(() =>
        parseRequest(
          JSON.stringify({ ...exploreBase, steps: [{ index: 0, action: "assert" }] }),
        ),
      ).toThrow("assertion");
    });

    it("非法 action 报错", () => {
      expect(() =>
        parseRequest(
          JSON.stringify({ ...exploreBase, steps: [{ index: 0, action: "hover" }] }),
        ),
      ).toThrow("action");
    });

    it("步数超过 50 报错", () => {
      const steps = Array.from({ length: 51 }, (_, i) => ({ index: i, action: "wait", value: "10" }));
      expect(() => parseRequest(JSON.stringify({ ...exploreBase, steps }))).toThrow("上限");
    });
  });

  describe("discover 模式", () => {
    it("合法 discover 请求不需要 steps", () => {
      const request = parseRequest(
        JSON.stringify({ ...base, url: "http://localhost:5173/", mode: "discover" }),
      );
      expect(request.mode).toBe("discover");
    });

    it("非法 mode 报错", () => {
      expect(() =>
        parseRequest(JSON.stringify({ ...base, url: "http://x/", mode: "aggressive" })),
      ).toThrow("mode");
    });
  });
});
