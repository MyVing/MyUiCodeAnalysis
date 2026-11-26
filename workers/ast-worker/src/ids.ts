/**
 * 业务 id 的定长收敛。
 *
 * 节点 id 形如 `相对文件#组件/jsx:标签/处理器`，可读性来自「路径 + 名字」，
 * 但深目录 + 长组件名/标签名的代码生成项目会超过行表 `node_id VARCHAR(512)`
 * 的硬上限，导致 MySQL 直接报 `Data too long for column 'node_id'`。
 *
 * 这里对超长 id 做**确定性**收敛：保留可读前缀，追加原 id 的 sha256 短哈希。
 * 同一个原始 id 永远得到同一个结果，因此：
 * - 多次分析、跨版本比较稳定；
 * - 边端点、`attrs` 内的 id 引用只要都由同一个生成函数产出，就天然一致。
 *
 * 未超长的 id 原样返回，因此对现有图完全无影响；函数也是幂等的。
 */

import { createHash } from "node:crypto";

/**
 * 节点 id 上限。
 *
 * 边 id 由两端拼接：`e_<kind>_<from>__<to>`，最长 kind（navigates）时
 * 约 `len(from) + len(to) + 14`；240 时仍 < 512，无需再单独截断边 id。
 */
export const MAX_NODE_ID_LENGTH = 240;

/** 哈希后缀（含分隔符 `~`）的固定长度。 */
const HASH_SUFFIX_LENGTH = 13;
const HASH_HEX_LENGTH = HASH_SUFFIX_LENGTH - 1;

/** 把业务 id 收敛到 MAX_NODE_ID_LENGTH 内；未超长时原样返回。 */
export function canonicalId(full: string): string {
  if (full.length <= MAX_NODE_ID_LENGTH) return full;
  const digest = createHash("sha256").update(full).digest("hex").slice(0, HASH_HEX_LENGTH);
  return `${full.slice(0, MAX_NODE_ID_LENGTH - HASH_SUFFIX_LENGTH)}~${digest}`;
}
