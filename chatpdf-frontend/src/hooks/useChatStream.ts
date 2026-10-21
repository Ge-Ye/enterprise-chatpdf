import { API_BASE_URL } from "@/lib/api";
// app/hooks/useChatStream.ts

import { useState } from "react";
import { Message } from "../context/ChatContext";



export function useChatStream(addMessage: (msg: Message) => void) {
  const [isLoading, setIsLoading] = useState(false);

  const sendMessage = async (query: string, fileId: string) => { // ✅ fileId 必传
    setIsLoading(true);

    try {
      const payload = { 
        query, 
        file_id: fileId 
      };

      console.log("📤 发送请求:", payload); // ✅ 调试日志

      const res = await fetch(`${API_BASE_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        console.error("❌ 请求失败:", res.status, errorData);
        throw new Error(`请求失败: ${res.status}`);
      }

      const data = await res.json();
      console.log("✅ 收到响应:", data);
      
      const assistantMsg: Message = {
        id: Date.now().toString(),
        role: "assistant",
        content: data.answer || "抱歉，未能生成回答",
      };
      
      addMessage(assistantMsg);
    } catch (err) {
      console.error("❌ 发送消息失败:", err);
      const errorMsg: Message = {
        id: Date.now().toString(),
        role: "assistant",
        content: "❌ 请求失败，请检查后端服务",
      };
      addMessage(errorMsg);
    } finally {
      setIsLoading(false);
    }
  };

  return { sendMessage, isLoading };
}