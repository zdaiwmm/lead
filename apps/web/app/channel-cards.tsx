"use client";

type Row = Record<string, any>;
export default function ChannelCards({
  cards,
  states,
  busy,
  respond,
  start,
}: {
  cards: (Row | string)[];
  states: Row[];
  busy: boolean;
  respond: (card: Row, accepted: boolean) => void;
  start: () => void;
}) {
  return (
    <>
      {cards
        .filter((card) => card === "start" || typeof card === "object")
        .map((value, i) => {
          const card: Row = typeof value === "string" ? { type: value } : value;
          if (card.type === "channel_target") return null;
          if (card.type === "start")
            return (
              <button
                key={i}
                className="inline-action"
                disabled={busy}
                onClick={start}
              >
                免费启动
              </button>
            );
          const state = states.find((s) => s.id === card.id);
          if (!state) return null;
          const disabled = busy || state.state !== "pending" || !state.valid;
          const label =
            state.state === "accepted"
              ? "已确认"
              : state.state === "canceled"
                ? "已取消"
                : !state.valid
                  ? "已过期"
                  : null;
          return (
            <div className="channel-card" key={card.id}>
              <strong>
                {card.type === "authorize" ? card.title : card.target_name}
              </strong>
              {card.type === "authorize" ? (
                <p>允许发布 · 每天最多 3 条</p>
              ) : (
                <p className="channel-preview">{card.body}</p>
              )}
              {label ? (
                <span className="muted small">{label}</span>
              ) : (
                <div className="channel-actions">
                  <button
                    className="primary"
                    disabled={disabled}
                    onClick={() => respond(card, true)}
                  >
                    {card.type === "authorize" ? "授权运营" : "发布此条验证"}
                  </button>
                  <button
                    disabled={disabled}
                    onClick={() => respond(card, false)}
                  >
                    取消
                  </button>
                </div>
              )}
            </div>
          );
        })}
    </>
  );
}
