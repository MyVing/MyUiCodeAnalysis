/**
 * 缺陷 3：服务层 `request({ url })` 必须被识别为接口调用。
 * ReactGraph 只白名单 fetch / useSWR / useQuery，此处完全识别不到。
 */

export interface RequestOptions {
  url: string;
  method?: "GET" | "POST" | "PUT" | "DELETE";
  params?: Record<string, unknown>;
  data?: unknown;
}

/**
 * 统一的请求封装：所有业务接口都经由此处发出。
 * 解析器需要把调用此函数的地方与最终 URL 关联起来。
 */
export async function request<T>(options: RequestOptions): Promise<T> {
  const { url, method = "GET", params, data } = options;
  const query = params ? `?${new URLSearchParams(params as Record<string, string>).toString()}` : "";

  const response = await fetch(`${url}${query}`, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(data ? { body: JSON.stringify(data) } : {}),
  });

  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`);
  }
  return (await response.json()) as T;
}
