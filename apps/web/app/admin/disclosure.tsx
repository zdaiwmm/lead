import { ReactNode } from "react";

export default function Disclosure({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <details
      className="admin-disclosure"
      onInvalidCapture={(event) => {
        // Required fields remain editable even when a browser validation error is inside a closed group.
        let element: HTMLElement | null = event.target as HTMLElement;
        while (element && element !== event.currentTarget.parentElement) {
          if (element instanceof HTMLDetailsElement) element.open = true;
          element = element.parentElement;
        }
      }}
    >
      <summary>{title}</summary>
      <div className="admin-disclosure-content">{children}</div>
    </details>
  );
}
