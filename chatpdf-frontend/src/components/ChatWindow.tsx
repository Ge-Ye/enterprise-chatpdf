"use client";
import React, { useState, useRef, useEffect } from "react";
import { useChat } from "../context/ChatContext";
import { Message } from "../context/ChatContext";
import { useChatStream } from "../hooks/useChatStream";

export default function ChatWindow() {
  const { messages, addMessage, currentFileId } = useChat(); // ✅ 获取 currentFileId
  const [input, setInput] = useState("");
  const { sendMessage, isLoading } = useChatStream(addMessage);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const handleSend = () => {
    if (!input.trim()) return;

    // ✅ 添加调试日志
    console.log("📝 准备发送消息:", {
      input,
      currentFileId,
      hasFileId: !!currentFileId
    });
    
    // ✅ 检查是否有上传的文件
    if (!currentFileId) {
      alert("⚠️ 请先上传 PDF 文件！");
      return;
    }

    const userMsg: Message = { 
      id: Date.now().toString(),
      role: "user", 
      content: input 
    };
    
    addMessage(userMsg);
    
    // ✅ 传递 currentFileId
    if (currentFileId) {
      sendMessage(input, currentFileId);
    } else {
      console.warn("⚠️ currentFileId 为空，请先上传文件");
      alert("请先上传 PDF 文件");
    }
    
    setInput("");
  };

  useEffect(() => {
    if (isLoading) {
      const assistantMsg: Message = {
        id: Date.now().toString(),
        role: "assistant",
        content: "正在思考...",
      };
      addMessage(assistantMsg);
    }
  }, [isLoading,addMessage]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex flex-col h-[600px] border rounded-2xl bg-white shadow-lg">
      {/* ✅ 添加文件状态提示 */}
      {!currentFileId && (
        <div className="bg-yellow-50 border-b border-yellow-200 px-4 py-2 text-sm text-yellow-700">
          ⚠️ 请先上传 PDF 文件再开始对话
        </div>
      )}
      
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.map((m, i) => (
          <div
            key={i}
            className={`p-3 rounded-2xl max-w-[80%] ${
              m.role === "user"
                ? "bg-blue-500 text-white self-end ml-auto"
                : "bg-gray-100 text-gray-800 self-start"
            }`}
          >
            {m.content}
          </div>
        ))}
        <div ref={bottomRef}></div>
      </div>
      
      <div className="p-3 border-t bg-gray-50 flex items-center">
        <input
          className="flex-1 rounded-xl border px-3 py-2 mr-2 focus:outline-none focus:ring focus:ring-blue-200"
          placeholder={currentFileId ? "💡 询问 PDF 内容..." : "⚠️ 请先上传文件"}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSend()}
          disabled={!currentFileId} // ✅ 未上传文件时禁用输入
        />
        <button
          onClick={handleSend}
          disabled={isLoading || !currentFileId}
          className="bg-blue-600 text-white px-4 py-2 rounded-xl hover:bg-blue-700 disabled:opacity-50"
        >
          发送
        </button>
      </div>
    </div>
  );
}