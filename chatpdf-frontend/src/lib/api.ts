// src/lib/api.ts
const API_BASE_URL =
  process.env.NODE_ENV === "development"
    ? "http://127.0.0.1:8000/api" // ✅ 本地后端带 /api
    : "https://yourdomain.com/api"; // ✅ 部署后地址

console.log("🚀 当前 API_BASE_URL =", process.env.NEXT_PUBLIC_API_BASE_URL);

export async function uploadPDF(formData: FormData) {
  const res = await fetch(`${API_BASE_URL}/upload`, {
    method: "POST",
    body: formData,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Upload failed (${res.status}): ${text}`);
  }

  return res.json();
}
/**
 * 向后端 /api/chat 发送聊天请求
 */
export async function sendChat(fileId: string, question: string) {
  const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/api";
  console.log("🛰️ Chat API ->", `${API_BASE_URL}/chat`);

  const res = await fetch(`${API_BASE_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ file_id: fileId, question }),
  });

  if (!res.ok) {
    const errorText = await res.text();
    console.error("❌ Chat request failed:", res.status, errorText);
    throw new Error(`Chat request failed: ${res.status}`);
  }

  return res.json();
}

