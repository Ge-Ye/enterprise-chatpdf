import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const backendBaseUrl = (
    process.env.INTERNAL_API_BASE_URL ||
    process.env.NEXT_PUBLIC_API_BASE_URL ||
    "http://localhost:8000"
  ).replace(/\/+$/, "");

  try {
    const response = await fetch(`${backendBaseUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(await req.json()),
      cache: "no-store",
    });
    const body = await response.text();

    return new NextResponse(body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") || "application/json",
      },
    });
  } catch {
    return NextResponse.json({ detail: "Unable to reach the chat backend" }, { status: 502 });
  }
}
