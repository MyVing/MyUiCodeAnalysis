/**
 * dev server 端口解析：从启动日志里找到实际监听端口。
 *
 * 为什么不能信任配置端口：vite 会因占用自动 +1（strictPort 未开时），
 * 用户项目也可能用 CRA/webpack 等其他工具，监听端口以日志为准。
 */

const URL_LINE_PATTERN = /https?:\/\/(?:localhost|127\.0\.0\.1|\[::1\]):(\d{2,5})/;

/** vite 7 的端口数字会被 ANSI 粗体码隔开（localhost:\x1b[1m5199），先剥掉再匹配。 */
function stripAnsi(line: string): string {
  // eslint-disable-next-line no-control-regex -- 控制字符就是这里的目标
  return line.replace(/\x1b\[[0-9;]*m/g, "");
}

/**
 * 从 stdout 累积文本中解析本机回环地址端口。
 * 返回去重后的端口列表，按出现顺序；空数组表示还没打印出来。
 */
export function parseServerPorts(text: string): number[] {
  const ports: number[] = [];
  for (const line of text.split(/\r?\n/)) {
    const clean = stripAnsi(line);
    // vite 的 "Local:" 行是权威信号；其他工具没有这个标记，
    // 就退化为匹配任意 localhost URL 行。
    const isLocalLine = /local/i.test(clean) || URL_LINE_PATTERN.test(clean);
    if (!isLocalLine) {
      continue;
    }
    const match = URL_LINE_PATTERN.exec(clean);
    const port = match?.[1];
    if (port !== undefined) {
      const parsed = Number.parseInt(port, 10);
      if (Number.isInteger(parsed) && parsed >= 1 && parsed <= 65535 && !ports.includes(parsed)) {
        ports.push(parsed);
      }
    }
  }
  return ports;
}

/** 把 base URL 与端口拼成规范的页面入口地址。 */
export function toBaseUrl(host: string, port: number): string {
  return `http://${host}:${port}/`;
}
