"use client";
import { useState } from "react";
import { api } from "./api-client";
type Row = Record<string, any>;
const roles: Row = { owner: "所有者", operator: "运营成员", viewer: "仅查看" };
export default function WorkspaceSettings({ user }: { user: Row }) {
  const [spaces, setSpaces] = useState<Row[]>([]);
  const [members, setMembers] = useState<Row[]>([]);
  const [subject, setSubject] = useState("");
  const [role, setRole] = useState("operator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const load = async () => {
    setSpaces(await api("/workspaces"));
    if (user.role === "owner") setMembers(await api("/workspace/members"));
  };
  return (
    <details
      className="advanced"
      onToggle={(e) => {
        if (e.currentTarget.open) perform(load);
      }}
    >
      <summary>工作室与成员</summary>
      <div className="advanced-content">
        {error && <p role="alert">{error}</p>}
        <label className="media-question">
          工作室
          <select
            aria-label="工作室"
            value={user.tenant_id}
            disabled={busy}
            onChange={(e) =>
              perform(async () => {
                await api("/workspace/switch", { tenant_id: e.target.value });
                window.location.reload();
              })
            }
          >
            {spaces.map((space) => (
              <option key={space.id} value={space.id}>
                {space.name}
              </option>
            ))}
          </select>
        </label>
        <div className="setting-row">
          <span>我的权限</span>
          <span>{roles[user.role]}</span>
        </div>
        <div className="setting-row">
          <span>我的成员编号</span>
          <button
            onClick={() =>
              perform(async () => {
                await navigator.clipboard.writeText(user.subject);
              })
            }
          >
            复制
          </button>
        </div>
        {user.role === "owner" && (
          <details className="advanced">
            <summary>管理成员</summary>
            <div className="advanced-content">
              {members
                .filter((member) => member.status === "active")
                .map((member) => (
                  <div className="member-row" key={member.subject}>
                    <span dir="auto">{member.subject}</span>
                    <select
                      aria-label={member.subject + "的权限"}
                      value={member.role}
                      disabled={busy}
                      onChange={(e) =>
                        perform(async () => {
                          await api("/workspace/members", {
                            subject: member.subject,
                            role: e.target.value,
                          });
                          await load();
                        })
                      }
                    >
                      {Object.entries(roles).map(([value, label]) => (
                        <option value={value} key={value}>
                          {String(label)}
                        </option>
                      ))}
                    </select>
                    <button
                      disabled={busy}
                      onClick={() =>
                        perform(async () => {
                          await api("/workspace/members", {
                            subject: member.subject,
                            role: member.role,
                            status: "revoked",
                          });
                          await load();
                        })
                      }
                    >
                      移除
                    </button>
                  </div>
                ))}
              <form
                className="member-add"
                onSubmit={(e) => {
                  e.preventDefault();
                  perform(async () => {
                    await api("/workspace/members", {
                      subject: subject.trim(),
                      role,
                    });
                    setSubject("");
                    await load();
                  });
                }}
              >
                <label htmlFor="member-id">成员编号</label>
                <input
                  id="member-id"
                  value={subject}
                  maxLength={500}
                  required
                  onChange={(e) => setSubject(e.target.value)}
                />
                <select
                  aria-label="新成员权限"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                >
                  <option value="operator">运营成员</option>
                  <option value="viewer">仅查看</option>
                </select>
                <button disabled={busy || !subject.trim()} type="submit">
                  添加
                </button>
              </form>
            </div>
          </details>
        )}
        <button
          disabled={busy}
          onClick={() =>
            perform(async () => {
              await api("/auth/logout", {});
              window.location.reload();
            })
          }
        >
          退出登录
        </button>
      </div>
    </details>
  );
}
