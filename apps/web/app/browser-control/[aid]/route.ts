import sharp from "sharp";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Context = { params: Promise<{ aid: string }> };
const privateHeaders = { "Cache-Control": "private, no-store" };

async function interact(request: Request, context: Context) {
  const { aid } = await context.params;
  const takeover = new URL(request.url).searchParams.get("takeover") || "";
  if (!uuid.test(aid) || !uuid.test(takeover))
    return Response.json({ detail: "无效的接管会话" }, { status: 400 });

  const base =
    (process.env.LEAD_API_ORIGIN || "http://127.0.0.1:8088") +
    "/api/agents/" +
    aid +
    "/browser";
  const headers = new Headers();
  for (const name of ["cookie", "origin", "x-lead-workspace", "x-lead-request"])
    if (request.headers.has(name))
      headers.set(name, request.headers.get(name)!);
  const options = { headers, cache: "no-store", redirect: "error" } as const;

  if (request.method === "POST") {
    const length = Number(request.headers.get("content-length"));
    if (!Number.isSafeInteger(length) || length <= 0 || length > 65536)
      return Response.json(
        { detail: "输入内容过长或长度无效" },
        { status: 413 },
      );
    const input = await request.json().catch(() => null);
    if (!input || input.takeover !== takeover)
      return Response.json({ detail: "无效的接管操作" }, { status: 400 });
    headers.set("Content-Type", "application/json");
    const applied = await fetch(base + "/input", {
      ...options,
      method: "POST",
      body: JSON.stringify(input),
    });
    if (!applied.ok)
      return new Response(applied.body, {
        status: applied.status,
        headers: { ...privateHeaders, "Content-Type": "application/json" },
      });
    await applied.arrayBuffer();
  }
  // The existing screenshot endpoint checks membership, permission and takeover again.
  const previous = request.headers.get("x-lead-frame-tag");
  if (previous) headers.set("If-None-Match", previous);
  const frame = await fetch(
    base + "/screenshot?format=jpeg&takeover=" + takeover,
    options,
  );
  if (frame.status === 304)
    return new Response(null, { status: 204, headers: privateHeaders });
  if (!frame.ok)
    return new Response(frame.body, {
      status: frame.status,
      headers: { ...privateHeaders, "Content-Type": "application/json" },
    });

  const jpeg = Buffer.from(await frame.arrayBuffer());
  const source = sharp(jpeg);
  const { width, height } = await source.metadata();
  const webp = await source
    .resize({ width: 832, withoutEnlargement: true })
    .webp({ quality: 44, effort: 1 })
    .toBuffer()
    .catch(() => null);
  const useWebp = webp !== null && webp.length < jpeg.length;
  return new Response(new Uint8Array(useWebp ? webp : jpeg), {
    headers: {
      ...privateHeaders,
      "Content-Type": useWebp ? "image/webp" : "image/jpeg",
      "X-Lead-Frame-Width": String(width),
      "X-Lead-Frame-Height": String(height),
      "X-Lead-Frame-Tag": frame.headers.get("etag") || "",
    },
  });
}

export { interact as GET, interact as POST };
