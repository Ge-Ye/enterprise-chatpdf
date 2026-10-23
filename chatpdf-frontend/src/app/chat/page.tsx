"use client";

import { API_BASE_URL } from "@/lib/api";

import React, { useState, useRef, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Upload, FileText, Send, BookOpen, ChevronDown } from "lucide-react";

// ✅ 添加引用来源的数据结构
interface SourceDoc {
  rank: number;
  relevance_score: number;
  text: string;
  page?: number;
  doc_id: string;
}

function formatCitationText(text: string): string {
  return (text || "")
    .replace(/(?<!^)##\s/gm, "\n## ")
    .replace(/\.\s+-\s+/g, ".\n- ");
}

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: SourceDoc[];  // ✅ 添加引用来源字段
  status?: string;
}

interface FileItem {
  id: string;
  name: string;
  alias?: string;
}



function ChatPageContent() {
  const [expandedPanels, setExpandedPanels] = useState<Record<string, boolean>>({});
  const [expandedCards, setExpandedCards] = useState<Record<string, boolean>>({});
  const searchParams = useSearchParams();
  const initialFileId = searchParams.get("fileId");

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [currentFileId, setCurrentFileId] = useState<string | null>(initialFileId);
  const [availableFiles, setAvailableFiles] = useState<FileItem[]>([]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortControllerRef.current?.abort(), []);

  // 加载可用文件列表
  useEffect(() => {
    const fetchFiles = async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/files`);
        if (!res.ok) throw new Error("加载文件失败");
        const data = await res.json();
        setAvailableFiles(data);
      } catch (err) {
        console.error("加载文件失败:", err);
      }
    };
    fetchFiles();
  }, []);

  // 自动滚动到消息底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim()) return;

    if (!currentFileId) {
      alert("请先选择一个文件！");
      return;
    }

    const userMsg: Message = {
      id: Date.now().toString(),
      role: "user",
      content: input,
    };

    const assistantId = `${Date.now()}-assistant`;
    const assistantMsg: Message = { id: assistantId, role: "assistant", content: "", sources: [] };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setInput("");
    setIsLoading(true);
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const payload = {
        query: input,
        file_id: currentFileId,
      };

      console.log("📤 发送请求:", payload);

      const res = await fetch(`${API_BASE_URL}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });

      if (!res.ok) throw new Error(`请求失败：${res.status}`);
      if (!res.body) throw new Error("响应不包含可读取的数据流");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let receivedChunk = false;
      let streamDone = false;

      const handleEvent = (rawEvent: string) => {
        let eventName = "message";
        const dataLines: string[] = [];
        for (const line of rawEvent.split(/\r?\n/)) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
        }
        if (!dataLines.length) return;
        const rawData = dataLines.join("\n");
        let data: Record<string, unknown> = {};
        try {
          const parsed: unknown = JSON.parse(rawData);
          if (typeof parsed === "object" && parsed !== null) data = parsed as Record<string, unknown>;
          else data = { text: String(parsed) };
        } catch { data = { text: rawData }; }

        if (eventName === "status") {
          setMessages((prev) => prev.map((message) => message.id === assistantId
            ? { ...message, status: (typeof data.message === "string" ? data.message : "正在处理...") } : message));
        } else if (eventName === "sources") {
          setMessages((prev) => prev.map((message) => message.id === assistantId
            ? { ...message, sources: Array.isArray(data.source_docs) ? data.source_docs as SourceDoc[] : [] } : message));
        } else if (eventName === "chunk") {
          const text = typeof data.text === "string" ? data.text : "";
          if (text) {
            receivedChunk = true;
            setMessages((prev) => prev.map((message) => message.id === assistantId
              ? { ...message, content: message.content + text, status: undefined } : message));
          }
        } else if (eventName === "error") {
          const message = typeof data.message === "string" ? data.message : "流式生成失败。";
          setMessages((prev) => prev.map((item) => item.id === assistantId
            ? { ...item, content: item.content || `请求失败：${message}`, status: undefined } : item));
          streamDone = true;
        } else if (eventName === "done") {
          setMessages((prev) => prev.map((message) => message.id === assistantId
            ? { ...message, status: undefined } : message));
          streamDone = true;
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const completeEvent = buffer.slice(0, boundary).replace(/\r$/, "");
          buffer = buffer.slice(boundary + 2);
          if (completeEvent.trim()) handleEvent(completeEvent);
          boundary = buffer.indexOf("\n\n");
        }
        if (done) {
          buffer += decoder.decode();
          if (buffer.trim()) handleEvent(buffer);
          break;
        }
        if (streamDone) {
          await reader.cancel();
          break;
        }
      }
      if (!receivedChunk) {
        setMessages((prev) => prev.map((message) => message.id === assistantId && !message.content
          ? { ...message, content: "抱歉，未生成有效的回答。" } : message));
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      console.error("消息发送失败：", err);
      setMessages((prev) => prev.map((message) => message.id === assistantId
        ? { ...message, content: message.content || "请求失败，请检查后端服务是否正常运行。" } : message));
    } finally {
      if (abortControllerRef.current === controller) abortControllerRef.current = null;
      setIsLoading(false);
    }
  };

  return (
    <main className="max-w-6xl mx-auto mt-10 h-[calc(100vh-120px)]">
      {/* 页头 */}
      <div className="flex items-center justify-between mb-6">
        <Link
          href="/"
          className="flex items-center text-gray-600 hover:text-gray-900 transition"
        >
          <ArrowLeft className="w-5 h-5 mr-2" />
          返回首页
        </Link>

        <div className="flex items-center space-x-4">
          {currentFileId && (
            <div className="flex items-center bg-green-100 px-4 py-2 rounded-lg">
              <FileText className="w-4 h-4 text-green-600 mr-2" />
              <span className="text-sm text-green-700">当前文件</span>
            </div>
          )}
          <Link
            href="/upload"
            className="flex items-center bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition"
          >
            <Upload className="w-4 h-4 mr-2" />
            上传文件
          </Link>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6 h-full">
        {/* 文件选择侧边栏 */}
        <div className="lg:col-span-1 bg-white rounded-xl shadow-lg p-4 overflow-y-auto">
          <h2 className="font-semibold mb-4">选择文件</h2>

          {availableFiles.length === 0 ? (
            <div className="text-center py-8">
              <FileText className="w-12 h-12 text-gray-300 mx-auto mb-3" />
              <p className="text-sm text-gray-500 mb-4">暂无可用文件</p>
              <Link
                href="/upload"
                className="block bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition"
              >
                上传文件
              </Link>
            </div>
          ) : (
            <div className="space-y-2">
              {availableFiles.map((file) => (
                <div
                  key={file.id}
                  onClick={() => {
                    setCurrentFileId(file.id);
                    setMessages([]);
                  }}
                  className={`p-3 border-2 rounded-lg cursor-pointer transition ${
                    file.id === currentFileId
                      ? "bg-blue-50 border-blue-500"
                      : "bg-gray-50 border-gray-200 hover:border-blue-300"
                  }`}
                >
                  <div className="flex items-center">
                    <FileText className="w-5 h-5 text-blue-600 mr-2" />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">{file.alias || file.name}</p>
                      <p className="text-xs text-gray-500 truncate">{file.id}</p>
                    </div>
                  </div>
                </div>
              ))}

              <button
                onClick={() => {
                  setCurrentFileId(null);
                  setMessages([]);
                }}
                className="w-full text-sm text-red-600 hover:text-red-700 py-2"
              >
                清除选择
              </button>
            </div>
          )}

          <div className="mt-6 pt-6 border-t">
            <h3 className="text-sm font-semibold mb-2">💡 使用提示</h3>
            <ul className="text-xs text-gray-600 space-y-1">
              <li>• 上传 PDF 文件后可开始聊天</li>
              <li>• 提问应与所选文件内容相关</li>
              <li>• AI 将基于文件内容进行回答</li>
            </ul>
          </div>
        </div>

        {/* 聊天区域 */}
        <div className="lg:col-span-3 flex flex-col bg-white rounded-xl shadow-lg">
          {!currentFileId && (
            <div className="bg-yellow-50 border-b border-yellow-200 px-6 py-3 text-sm text-yellow-700">
              ⚠️ 请先选择或上传一个文件
            </div>
          )}

          <div className="flex-1 overflow-y-auto p-6 space-y-4">
            {messages.length === 0 ? (
              <div className="text-center py-12">
                <div className="text-6xl mb-4">💬</div>
                <h3 className="text-xl font-semibold mb-2">开始聊天</h3>
                <p className="text-gray-600">
                  {currentFileId ? "请提出与 PDF 内容相关的问题" : "请先上传或选择一个文件"}
                </p>
              </div>
            ) : (
              messages.map((m) => (
                <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div className="max-w-[85%]">
                    {/* 消息气泡 */}
                    <div
                      className={`p-4 rounded-2xl ${
                        m.role === "user" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-800"
                      }`}
                    >
                      <div className="whitespace-pre-wrap min-h-6">
                        {m.content || (m.role === "assistant" && isLoading
                          ? <span className="text-gray-500">{m.status || "正在连接..."} <span className="animate-pulse">▍</span></span>
                          : null)}
                      </div>
                    </div>

                    {/* ✅ 显示引用来源 */}
                    {m.role === "assistant" && m.sources && m.sources.length > 0 && (
                      <div className="mt-3 bg-gradient-to-r from-blue-50 to-indigo-50 rounded-xl p-4 border border-blue-200">
                        <button type="button" className="flex w-full items-center justify-between mb-3 text-left" onClick={() => setExpandedPanels((s) => ({ ...s, [m.id]: !s[m.id] }))} aria-expanded={!!expandedPanels[m.id]}>
                          <span className="flex items-center">
                          <BookOpen className="w-4 h-4 text-blue-600 mr-2" />
                          <span className="font-semibold text-gray-800 text-sm">
                            📖 引用来源（Rerank 排序）
                          </span>
                          <span className="ml-2 text-xs text-gray-600">共 {m.sources.length} 条</span>
                          </span>
                          <ChevronDown className={`h-4 w-4 transition-transform ${expandedPanels[m.id] ? "rotate-180" : ""}`} />
                        </button>
                        
                        {expandedPanels[m.id] && <>
                        <div className="space-y-3">
                          {m.sources.map((source) => (
                            <div 
                              key={source.doc_id} 
                              className="bg-white rounded-lg p-3 border-l-4 border-blue-500 shadow-sm hover:shadow-md transition"
                            >
                              {/* 排名和相关度 */}
                              <div className="flex items-center justify-between mb-2">
                                <div className="flex items-center space-x-2">
                                  <span className="inline-flex items-center justify-center w-6 h-6 bg-blue-600 text-white text-xs font-bold rounded-full">
                                    {source.rank}
                                  </span>
                                  <span className="text-xs font-medium text-gray-700">
                                    相关度排名 #{source.rank}
                                  </span>
                                </div>
                                <span className="text-xs font-semibold text-blue-600 bg-blue-100 px-2 py-1 rounded">
                                  {(source.relevance_score * 100).toFixed(1)}%
                                </span>
                              </div>

                              {/* 文本内容 */}
                              <div className="mb-2 rounded bg-gray-50 p-2">
                                <div className={`whitespace-pre-line text-sm leading-relaxed text-gray-700 ${expandedCards[`${m.id}:${source.doc_id}`] ? "" : "line-clamp-3 overflow-hidden"}`}>
                                  {formatCitationText(source.text)}
                                </div>
                              </div>
                              {source.text.length > 220 && <button type="button" className="mb-2 text-xs text-blue-700" onClick={() => setExpandedCards((s) => ({ ...s, [`${m.id}:${source.doc_id}`]: !s[`${m.id}:${source.doc_id}`] }))}>{expandedCards[`${m.id}:${source.doc_id}`] ? "收起 ▴" : "展开详情 ▾"}</button>}

                              {/* 页码和文档ID */}
                              <div className="flex items-center justify-between text-xs text-gray-500">
                                {source.page !== null && source.page !== undefined ? (
                                  <span className="flex items-center">
                                    <FileText className="w-3 h-3 mr-1" />
                                    第 {source.page + 1} 页
                                  </span>
                                ) : (
                                  <span>未知页码</span>
                                )}
                                <span className="text-gray-400 truncate max-w-[150px]" title={source.doc_id}>
                                  ID: {source.doc_id.substring(0, 8)}...
                                </span>
                              </div>
                            </div>
                          ))}
                        </div>
                        </>}
                      </div>
                    )}
                  </div>
                </div>
              ))
            )}

            {isLoading && (
              <div className="flex justify-start">
                <div className="bg-gray-100 p-4 rounded-2xl">
                  <div className="flex space-x-2">
                    <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"></div>
                    <div
                      className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                      style={{ animationDelay: "0.2s" }}
                    ></div>
                    <div
                      className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"
                      style={{ animationDelay: "0.4s" }}
                    ></div>
                  </div>
                </div>
              </div>
            )}

            <div ref={bottomRef}></div>
          </div>

          <div className="border-t bg-gray-50 p-4">
            <div className="flex items-center space-x-3">
              <input
                type="text"
                className="flex-1 rounded-xl border px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder={currentFileId ? "请输入您的问题..." : "请先选择一个文件"}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                disabled={!currentFileId || isLoading}
              />
              <button
                onClick={handleSend}
                disabled={!currentFileId || isLoading || !input.trim()}
                className="bg-blue-600 text-white p-3 rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
              >
                <Send className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}

export default function ChatPage() {
  return (
    <React.Suspense fallback={<main className="min-h-screen" />}>
      <ChatPageContent />
    </React.Suspense>
  );
}
