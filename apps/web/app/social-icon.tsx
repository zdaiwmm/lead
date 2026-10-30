import { Globe, Linkedin } from "lucide-react";
import {
  siTelegram,
  siX,
  siFacebook,
  siInstagram,
  siYoutube,
  siTiktok,
  siReddit,
  siPinterest,
  siThreads,
  siWhatsapp,
} from "simple-icons";
const icons = {
  telegram: siTelegram,
  x: siX,
  facebook: siFacebook,
  instagram: siInstagram,
  youtube: siYoutube,
  tiktok: siTiktok,
  reddit: siReddit,
  pinterest: siPinterest,
  threads: siThreads,
  whatsapp: siWhatsapp,
};
export const socialNames: Record<string, string> = {
  telegram: "Telegram",
  x: "X",
  linkedin: "LinkedIn",
  facebook: "Facebook",
  instagram: "Instagram",
  youtube: "YouTube",
  tiktok: "TikTok",
  reddit: "Reddit",
  pinterest: "Pinterest",
  threads: "Threads",
  whatsapp: "WhatsApp",
};
export default function SocialIcon({
  platform,
  size = 22,
  connected = true,
}: {
  platform: string;
  size?: number;
  connected?: boolean;
}) {
  if (platform === "linkedin")
    return (
      <Linkedin
        size={size}
        color={connected ? "#0A66C2" : "var(--text-muted)"}
        aria-hidden="true"
      />
    );
  const icon = icons[platform as keyof typeof icons];
  if (!icon)
    return <Globe size={size} color="var(--text-muted)" aria-hidden="true" />;
  const monochrome = ["x", "threads", "tiktok"].includes(platform);
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={
        connected
          ? monochrome
            ? "var(--text)"
            : `#${icon.hex}`
          : "var(--text-muted)"
      }
      aria-hidden="true"
    >
      <path d={icon.path} />
    </svg>
  );
}
