"use client";

type Row = Record<string, any>;
import { AssetPreview, MediaVerification } from "./media-upload";
export default function ChannelCards({
  cards,
  states,
  busy,
  respond,
  start,
  aid,
  onComplete,
}: {
  cards: (Row | string)[];
  states: Row[];
  busy: boolean;
  respond: (card: Row, accepted: boolean) => void;
  start: () => void;
  aid: string;
  onComplete: () => void;
}) {
  return (
    <>
      {cards
        .filter((card) => card === "start" || typeof card === "object")
        .map((value, i) => {
          const card: Row = typeof value === "string" ? { type: value } : value;
          if (card.type === "media_verification")
            return (
              <MediaVerification
                key={card.target_id}
                aid={aid}
                card={card}
                onComplete={onComplete}
              />
            );
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
                <>
                  <p className="channel-preview">
                    {state.payload?.title && (
                      <strong>
                        {state.payload.title}
                        <br />
                      </strong>
                    )}
                    {card.body}
                  </p>
                  {(state.payload?.media || []).map((asset: Row) => (
                    <AssetPreview
                      key={asset.id}
                      aid={aid}
                      assetId={asset.id}
                      kind={asset.kind}
                    />
                  ))}
                  {state.payload?.made_for_kids != null && (
                    <p>面向儿童：{state.payload.made_for_kids ? "是" : "否"}</p>
                  )}
                  {(state.payload?.media || []).some(
                    (asset: Row) => asset.ai_generated != null,
                  ) && (
                    <p>
                      包含 AI 生成素材：
                      {state.payload.media.some(
                        (asset: Row) => asset.ai_generated,
                      )
                        ? "是"
                        : "否"}
                    </p>
                  )}
                  {state.payload?.commercial_disclosure === "own_brand" &&
                    state.payload?.media?.length > 0 && <p>推广自己的业务</p>}
                </>
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
