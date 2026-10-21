// This module is used by browser components; it must use the browser-reachable URL.
const configuredApiBase = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
export const API_BASE_URL = configuredApiBase.endsWith("/api") ? configuredApiBase : `${configuredApiBase}/api`;

export async function uploadPDF(formData: FormData) {
  const res = await fetch(`${API_BASE_URL}/upload`, { method: "POST", body: formData });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Upload failed (${res.status}): ${text}`);
  }
  return res.json();
}

export async function sendChat(fileId: string, question: string) {
  const res = await fetch(`${API_BASE_URL}/chat`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_id: fileId, query: question }),
  });
  if (!res.ok) throw new Error(`Chat request failed: ${res.status}`);
  return res.json();
}
