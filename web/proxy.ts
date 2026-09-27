import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";

// The chat spends your fal and OpenRouter credit through the API key its server holds, so anyone who can open it can
// spend. With MASTERSMITH_WEB_PASSWORD set, every page and /api route asks for it (HTTP basic auth: the browser shows
// its own prompt and resends it for downloads and the viewer; any user name). Unset, the chat is open, which is fine
// while it listens on 127.0.0.1 only (npm run dev, docker-compose); set it before exposing the chat to anyone else.
export function proxy(req: NextRequest) {
  const password = process.env.MASTERSMITH_WEB_PASSWORD;
  if (!password) return NextResponse.next();
  const header = req.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    const decoded = Buffer.from(header.slice(6), "base64").toString("utf8");
    const given = Buffer.from(decoded.slice(decoded.indexOf(":") + 1));
    const want = Buffer.from(password);
    if (given.length === want.length && timingSafeEqual(given, want)) return NextResponse.next();
  }
  return new NextResponse("Master Smith: password required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Master Smith", charset="UTF-8"' },
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
