"use client";

import { useEffect, useState } from "react";
import { Bell } from "lucide-react";
import { api, getWorkspace } from "./api-client";

export default function NotificationIndicator() {
  const [stale, setStale] = useState(false);
  const [unread, setUnread] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      if (!getWorkspace()) return;
      return api("/admin/notifications")
        .then((items: Record<string, any>[]) => {
          if (active) { setUnread(items.filter((item) => !item.read_at && !item.muted).length); setStale(false); }
        })
        .catch(() => {
          if (active) setStale(true);
        });
    };
    void refresh();
    const timer = setInterval(refresh, 15000);
    window.addEventListener("lead:updated", refresh);
    window.addEventListener("lead:workspace-ready", refresh);
    return () => {
      active = false;
      clearInterval(timer);
      window.removeEventListener("lead:updated", refresh);
      window.removeEventListener("lead:workspace-ready", refresh);
    };
  }, []);
  return (
    <a
      href="/admin#notifications"
      className="notification-entry"
      title={stale ? "通知暂未更新，正在自动重试" : unread === null ? "查看站内通知" : `最近通知中有 ${unread} 条未读`}
    >
      <Bell size={16} />
      通知{stale ? " · 待更新" : ""}
      {unread !== null && unread > 0 && (
        <span className="notification-count">
          {unread >= 50 ? "50+" : unread}
        </span>
      )}
    </a>
  );
}
