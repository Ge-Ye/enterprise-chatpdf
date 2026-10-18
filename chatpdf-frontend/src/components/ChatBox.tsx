"use client";
import React, { useState } from "react";
import { useChat } from "@/context/ChatContext";
import MessageBubble from "./MessageBubble";
import { useMutation } from "@tanstack/react-query";
import { sendChat } from "@/lib/api";

export default function ChatBox() {
  const { messages, addMessage } = useChat();
  const [input, setInput] = useState("");

  const chatMutation = useMutation({
  mutationFn: ({ fileId, question }: { fileId: string; question: string }) =>
    sendChat(fileId, question),
  onSuccess: (reply) => {
    addMessage({ id: Date.now().toString(), role: "assistant", content: reply });
  },
});

  const handleSend = async () => {
  if (!input.trim()) return;
  const userMsg = { id: Date.now().toString(), role: "user" as const, content: input };
  addMessage(userMsg);
  setInput("");
  
  // 假设 fileId 来自某个上下文或状态，例如 props 或 context
  const fileId = "example-file-id"; // 替换为你实际获取 fileId 的方式

  chatMutation.mutate({ fileId, question: input });
};

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto p-4 bg-gray-50 rounded-xl">
        {messages.map((m) => (
          <MessageBubble key={m.id} role={m.role} content={m.content} />
        ))}
      </div>
      <div className="mt-4 flex">
        <input
          className="flex-1 border rounded-l-xl px-3 py-2"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="请输入问题..."
        />
        <button
          onClick={handleSend}
          className="bg-blue-600 text-white px-4 py-2 rounded-r-xl hover:bg-blue-700"
        >
          发送
        </button>
      </div>
    </div>
  );
}
