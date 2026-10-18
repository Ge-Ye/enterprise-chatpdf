// app/context/ChatContext.tsx

import React, { createContext, useContext, useState, useCallback, ReactNode } from "react";

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
}

interface ChatContextType {
  messages: Message[];
  addMessage: (msg: Message) => void;
  clearMessages: () => void;
  currentFileId: string | null;
  setCurrentFileId: (id: string | null) => void;
}

const ChatContext = createContext<ChatContextType | undefined>(undefined);

export function ChatProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [currentFileId, setCurrentFileId] = useState<string | null>(null);

  // ✅ 使用 useCallback 避免每次渲染都创建新函数
  const addMessage = useCallback((msg: Message) => {
    setMessages((prev) => [...prev, msg]);
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  return (
    <ChatContext.Provider 
      value={{ 
        messages, 
        addMessage, 
        clearMessages,
        currentFileId,
        setCurrentFileId
      }}
    >
      {children}
    </ChatContext.Provider>
  );
}

export function useChat() {
  const context = useContext(ChatContext);
  if (!context) throw new Error("useChat must be used within ChatProvider");
  return context;
}