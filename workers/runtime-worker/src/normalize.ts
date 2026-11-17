/**
 * 采集值归一化：浏览器侧拿到的 file / URL / 时间戳与后端契约、静态图之间
 * 需要一个确定的换算规则——这条规则与 merge 的证据匹配强相关。
 */

/** Windows 盘符路径（file:///D:/... 或 /D:/... 归一化后的形态）。 */
function isDrivePath(path: string): boolean {
  return /^[A-Za-z]:\//.test(path);
}

/**
 * 把 _debugSource.fileName 归一化为相对项目根目录的 POSIX 路径。
 *
 * vite dev 下的 fileName 有多种形态：
 *   /src/App.tsx                         —— dev server webroot 相对路径（最常见）
 *   http://localhost:5173/src/App.tsx    —— 某些插件注入的完整 URL
 *   file:///D:/work/project/src/App.tsx  —— 本地文件协议（绝对路径）
 *   /D:/work/project/src/App.tsx         —— Windows 盘符路径被当成 webroot
 * 规则：能落到 rootPath 内的一律相对化；盘符路径落在 rootPath 外返回
 * undefined（宁缺勿错）；其余按 webroot 相对路径处理。
 */
export function normalizeSourceFile(
  fileName: string,
  rootPath: string,
): string | undefined {
  let candidate = fileName.trim();
  if (!candidate) {
    return undefined;
  }

  // 去掉 URL 前缀：file:///D:/x/y → D:/x/y；http://host:port/src/a.tsx → /src/a.tsx
  const fileUrl = /^file:\/\/\/(.*)$/i.exec(candidate);
  const httpUrl = /^https?:\/\/[^/]+(\/.*)$/i.exec(candidate);
  if (fileUrl !== null) {
    candidate = decodeURIComponent(fileUrl[1] ?? "");
  } else if (httpUrl !== null) {
    candidate = decodeURIComponent(httpUrl[1] ?? "");
  } else {
    candidate = decodeURIComponent(candidate);
  }

  // 去查询串与 hash（vite 可能带 ?t=时间戳 或 #hash）
  candidate = candidate.replace(/[?#].*$/, "");
  if (!candidate) {
    return undefined;
  }

  // 统一斜杠方向；盘符前的 webroot 斜杠（/D:/...）一并剥掉
  candidate = candidate.replaceAll("\\", "/");
  candidate = candidate.replace(/^\/(?=[A-Za-z]:\/)/, "");

  const root = rootPath.replaceAll("\\", "/").replace(/\/$/, "").toLowerCase();
  const lower = candidate.toLowerCase();

  if (root && lower.startsWith(`${root}/`)) {
    // 落在 rootPath 内（绝对或相对形态均可）→ 相对化
    candidate = candidate.slice(root.length + 1);
  } else if (isDrivePath(candidate)) {
    // 绝对盘符路径但不在 rootPath 内：拒绝归一化
    return undefined;
  } else {
    // webroot 相对路径：剥掉开头斜杠
    candidate = candidate.replace(/^\/+/, "");
  }

  if (!candidate || candidate.includes("../")) {
    return undefined;
  }
  return candidate;
}

/** 毫秒时间戳 → Unix 秒（契约要求整数秒）。 */
export function toUnixSeconds(ms: number): number {
  return Math.floor(ms / 1000);
}
