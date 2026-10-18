import { NextResponse } from "next/server";

export async function POST(req: Request) {
  const { message } = await req.json();
  // 模拟AI回复
  return NextResponse.json({ reply: `你问的是：“${message}” —— 暂时还没连后端哦 ?` });
}
