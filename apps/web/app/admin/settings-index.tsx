import { ArrowRight } from "lucide-react";
import { Navigate, settingsGroups } from "./navigation";

export default function SettingsIndex({ navigate }: { navigate: Navigate }) {
  return (
    <div className="admin-settings-index">
      {settingsGroups.map((group) => (
        <section className="admin-settings-group" key={group.label}>
          <h2>{group.label}</h2>
          <ul>
            {group.items.map((item) => (
              <li key={item.id}>
                <button onClick={() => navigate(item.id)}>
                  <span>
                    <strong>{item.label}</strong>
                    <small>{item.description}</small>
                  </span>
                  <ArrowRight size={16} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
