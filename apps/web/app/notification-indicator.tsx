"use client";

import { useEffect, useState } from "react";
import { Bell } from "lucide-react";
import { api } from "./api-client";

export default function NotificationIndicator() {
  const [unread, setUnread] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api("/admin/notifications")
        .then((items: Record<string, any>[]) => {
          if (active) setUnread(items.filter((item) => !item.read_at).length);
        })
        .catch(() => {
          if (active) setUnread(null);
        });
    void refresh();
    const timer = setInterval(refresh, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);
  return (
    <a
      href="/admin#notifications"
      className="notification-entry"
      title={unread === null ? "查看站内通知" : `最近通知中有 ${unread} 条未读`}
    >
      <Bell size={16} />
      通知
      {unread !== null && unread > 0 && (
        <span className="notification-count">
          {unread >= 50 ? "50+" : unread}
        </span>
      )}
    </a>
  );
}
