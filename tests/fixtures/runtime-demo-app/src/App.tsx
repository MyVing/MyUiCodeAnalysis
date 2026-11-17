import { useEffect, useState } from "react";

import { UserList } from "./components/UserList";

export function App() {
  const [demo, setDemo] = useState<string>("");

  useEffect(() => {
    // 被动采集的网络证据：页面自己发起的请求会被记录（404 也是事实）
    fetch("/api/demo", { headers: { Accept: "application/json" } }).catch(() => undefined);
    setDemo("runtime-demo");
  }, []);

  return (
    <main>
      <h1>Runtime Demo App</h1>
      <span data-testid="demo">{demo}</span>
      <UserList />
    </main>
  );
}
