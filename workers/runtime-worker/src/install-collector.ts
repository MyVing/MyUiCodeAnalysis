/**
 * React DevTools hook 垫片：先于页面所有脚本执行（addInitScript）。
 *
 * React 检测到 `__REACT_DEVTOOLS_GLOBAL_HOOK__` 后会注册 renderer 并在每次
 * commit 时回调 onCommitFiberRoot——我们借这个口子收集 fiber root。
 * 必须用 defineProperty 而非直接赋值，否则 React 的检测在部分打包产物下不可靠。
 *
 * 本函数会被 Playwright 序列化后在浏览器执行：必须是自包含的（不能引用
 * 模块级导入或闭包外变量），内部只用运行时判空，不做任何 Node API 调用。
 */
export function installCollector(): void {
  const hook = {
    renderers: new Map<unknown, unknown>(),
    supportsFiber: true,
    inject(internals: unknown): number {
      const id = hook.renderers.size + 1;
      hook.renderers.set(id, internals);
      return id;
    },
    onCommitFiberRoot(_rendererId: unknown, root: unknown): void {
      if (!root) return;
      const w = window as unknown as { __UICA_FIBER_ROOTS__: unknown[] };
      (w.__UICA_FIBER_ROOTS__ ||= []).push(root);
    },
    onScheduleFiberRoot(): void {},
  };
  try {
    Object.defineProperty(window, "__REACT_DEVTOOLS_GLOBAL_HOOK__", {
      value: hook,
      configurable: true,
      writable: true,
    });
  } catch {
    // 某些环境禁止 defineProperty；退化为直接赋值
    (window as unknown as Record<string, unknown>)["__REACT_DEVTOOLS_GLOBAL_HOOK__"] = hook;
  }
}
