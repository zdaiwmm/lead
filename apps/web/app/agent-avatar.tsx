type Row = Record<string, any>;
export function avatarSeed(agent?: Row) {
  return agent?.onboarding_state?.avatar_seed ?? 0;
}
export default function AgentAvatar({
  agent,
  seed,
  size = 40,
}: {
  agent?: Row;
  seed?: number;
  size?: number;
}) {
  const value = seed ?? avatarSeed(agent);
  const asset = seed == null && agent?.onboarding_state?.avatar_asset_id;
  return (
    <img
      className={`pixel-avatar ${asset ? "uploaded" : ""}`}
      src={
        asset
          ? `/api/agents/${agent!.id}/assets/${asset}`
          : "/images/agent-pixel.png"
      }
      alt=""
      width={size}
      height={size}
      style={
        asset
          ? undefined
          : {
              filter: `hue-rotate(${[0, 190, 40, 280, 135, 325, 240, 75][value % 8]}deg)`,
            }
      }
    />
  );
}
