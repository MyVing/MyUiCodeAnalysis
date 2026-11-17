import { useState } from "react";

export function UserList() {
  const [expanded, setExpanded] = useState(false);

  return (
    <section>
      <button type="button" onClick={() => setExpanded(!expanded)}>
        {expanded ? "收起" : "展开用户列表"}
      </button>
      {expanded ? <ul><li>alice</li><li>bob</li></ul> : null}
    </section>
  );
}
