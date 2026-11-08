"use client";

import { API_BASE_URL } from "@/lib/api";

import React, { useState, useRef, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Upload, FileText, Send, BookOpen, ChevronDown } from "lucide-react";
import CustomsExtractorPanel from "@/components/CustomsExtractorPanel";

// ✅ 添加引用来源的数据结构
interface SourceDoc {
  rank: number;
  // 概览来源没有 rerank 分数，后端返回 null，前端不得当作 0% 相关度展示
  relevance_score: number | null;
  text: string;
  page?: number;
  doc_id: string;
  source_kind?: "rerank" | "overview";
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
  status?: "processing" | "indexed" | "failed" | "unknown";
  error?: string;
}

const FILE_STATUS_META: Record<string, { label: string; className: string }> = {
  indexed: { label: "已索引", className: "bg-green-100 text-green-700" },
  processing: { label: "索引中", className: "bg-blue-100 text-blue-700" },
  failed: { label: "索引失败", className: "bg-red-100 text-red-700" },
  unknown: { label: "状态未知", className: "bg-gray-100 text-gray-600" },
};
const fileStatusMeta = (status?: string) => FILE_STATUS_META[status ?? "unknown"] ?? FILE_STATUS_META.unknown;

interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

// Mirror the backend bounds: keep only recent turns and stay within budget so
// the transcript cannot bloat every request.
const MAX_HISTORY_MESSAGES = 8;
const MAX_HISTORY_MESSAGE_CHARS = 3000;
const MAX_HISTORY_TOTAL_CHARS = 6000;
// Client-side failure text is shown in the bubble but is not a model answer,
// so it must never become conversation context for the next turn.
const NON_HISTORY_ASSISTANT_PREFIXES = ["请求失败", "抱歉，未生成有效的回答"];

function buildChatHistory(messages: Message[]): ChatTurn[] {
  const selected: ChatTurn[] = [];
  let total = 0;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (selected.length >= MAX_HISTORY_MESSAGES) break;
    const message = messages[i];
    let content = message.content.trim();
    if (!content) continue;
    if (
      message.role === "assistant"
      && NON_HISTORY_ASSISTANT_PREFIXES.some((prefix) => content.startsWith(prefix))
    ) continue;
    if (content.length > MAX_HISTORY_MESSAGE_CHARS) {
      const marker = "\n…\n";
      const kept = MAX_HISTORY_MESSAGE_CHARS - marker.length;
      const head = Math.floor((kept * 2) / 3);
      const tail = kept - head;
      content = `${content.slice(0, head)}${marker}${content.slice(-tail)}`;
    }
    if (total + content.length > MAX_HISTORY_TOTAL_CHARS) break;
    selected.push({ role: message.role, content });
    total += content.length;
  }
  return selected.reverse();
}

function ChatPageContent() {
  const [activeRightTab, setActiveRightTab] = useState<"chat" | "customs">("chat");
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

  const refreshFiles = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/files`);
      if (!res.ok) throw new Error("加载文件失败");
      const data = (await res.json()) as FileItem[];
      setAvailableFiles(Array.isArray(data) ? data : []);
    } catch (err) {
      console.error("加载文件失败:", err);
    }
  }, []);

  // 加载可用文件列表（每条记录带索引状态）
  useEffect(() => {
    void refreshFiles();
  }, [refreshFiles]);

  const selectedFile = availableFiles.find((file) => file.id === currentFileId);
  const selectedStatus = selectedFile?.status;
  const isFileReady = !currentFileId || selectedStatus === "indexed" || selectedStatus === "unknown";

  // 选中文件仍在索引时轮询文件列表，直到 indexed / failed
  useEffect(() => {
    if (selectedStatus !== "processing") return;
    const timer = window.setInterval(() => void refreshFiles(), 2000);
    return () => window.clearInterval(timer);
  }, [selectedStatus, refreshFiles]);

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

    if (!isFileReady) {
      alert(selectedStatus === "failed" ? "该文件索引失败，请重新上传后再提问。" : "文件正在建立索引，请稍候再提问。");
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

    // Prior turns only: the current question is already `query`, and sending it
    // twice would make the model treat it as old context.
    const history = buildChatHistory(messages);

    try {
      const payload = {
        query: input,
        file_id: currentFileId,
        history,
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
              {availableFiles.map((file) => {
                const meta = fileStatusMeta(file.status);
                const unavailable = file.status === "processing" || file.status === "failed";
                return (
                  <div
                    key={file.id}
                    onClick={() => {
                      if (unavailable) return;
                      setCurrentFileId(file.id);
                      setMessages([]);
                    }}
                    title={unavailable ? (file.status === "failed" ? "索引失败，请重新上传该文件" : "索引完成后可选择") : undefined}
                    className={`p-3 border-2 rounded-lg transition ${
                      unavailable ? "cursor-not-allowed opacity-60" : "cursor-pointer"
                    } ${
                      file.id === currentFileId
                        ? "bg-blue-50 border-blue-500"
                        : "bg-gray-50 border-gray-200 hover:border-blue-300"
                    }`}
                  >
                    <div className="flex items-center">
                      <FileText className="w-5 h-5 text-blue-600 mr-2" />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate">{file.alias || file.name}</p>
                        <div className="mt-1 flex items-center justify-between gap-2">
                          <p className="text-xs text-gray-500 truncate">{file.id}</p>
                          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${meta.className}`}>{meta.label}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}

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
              <li>• 上传后需等待索引完成再提问</li>
              <li>• 提问应与所选文件内容相关</li>
              <li>• AI 将基于文件内容进行回答</li>
            </ul>
          </div>
        </div>

        {/* 聊天区域 */}
        <div className="lg:col-span-3 flex flex-col bg-white rounded-xl shadow-lg">
          <div className="flex gap-2 border-b border-gray-200 px-4 pt-4" role="tablist" aria-label="右侧工作区">
            <button type="button" role="tab" aria-selected={activeRightTab === "chat"} onClick={() => setActiveRightTab("chat")} className={`rounded-t-lg px-4 py-2 text-sm font-medium transition ${activeRightTab === "chat" ? "border-b-2 border-blue-600 text-blue-700" : "text-gray-500 hover:text-gray-800"}`}>
              💬 智能问答 (RAG)
            </button>
            <button type="button" role="tab" aria-selected={activeRightTab === "customs"} onClick={() => setActiveRightTab("customs")} className={`flex items-center gap-2 rounded-t-lg px-4 py-2 text-sm font-medium transition ${activeRightTab === "customs" ? "border-b-2 border-blue-600 text-blue-700" : "text-gray-500 hover:text-gray-800"}`}>
              📋 关务单据提取与质检 <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-bold text-blue-700">AI质检</span>
            </button>
          </div>
          <div hidden={activeRightTab !== "chat"} className="flex min-h-0 flex-1 flex-col">
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
                            {m.sources.some((source) => source.source_kind === "overview")
                              ? "📖 引用来源（含文档首部概览）"
                              : "📖 引用来源（Rerank 排序）"}
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
                                    {source.source_kind === "overview" ? "片段顺序" : "相关度排名"} #{source.rank}
                                  </span>
                                </div>
                                {source.source_kind === "overview" || typeof source.relevance_score !== "number" ? (
                                  <span
                                    className="text-xs font-semibold text-amber-700 bg-amber-100 px-2 py-1 rounded"
                                    title="文档首部片段，未参与 Rerank 排序，因此没有相关度分数"
                                  >
                                    概览片段 · 文档首部
                                  </span>
                                ) : (
                                  <span className="text-xs font-semibold text-blue-600 bg-blue-100 px-2 py-1 rounded">
                                    {(source.relevance_score * 100).toFixed(1)}%
                                  </span>
                                )}
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
                                    第 {source.page} 页
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

          {currentFileId && !isFileReady && (
            <div
              role="status"
              className="mx-4 mb-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800"
            >
              {selectedStatus === "failed"
                ? "该文件索引失败，无法提问，请重新上传该文件。"
                : "文件正在建立索引，完成后才能提问；就绪后此处会自动消失。"}
            </div>
          )}

          <div className="border-t bg-gray-50 p-4">
            <div className="flex items-center space-x-3">
              <input
                type="text"
                className="flex-1 rounded-xl border px-4 py-3 focus:outline-none focus:ring-2 focus:ring-blue-500"
                placeholder={!currentFileId
                  ? "请先选择一个文件"
                  : !isFileReady
                    ? (selectedStatus === "failed" ? "文件索引失败，请重新上传" : "文件索引中，请稍候…")
                    : "请输入您的问题..."}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                disabled={!currentFileId || !isFileReady || isLoading}
              />
              <button
                onClick={handleSend}
                disabled={!currentFileId || !isFileReady || isLoading || !input.trim()}
                className="bg-blue-600 text-white p-3 rounded-xl hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
              >
                <Send className="w-5 h-5" />
              </button>
            </div>
            {currentFileId && isFileReady && (
              <p className="mt-2 px-1 text-xs text-gray-500">
                支持多轮追问，例如“它有什么限制？”“那第二条呢？”
              </p>
            )}
          </div>
          </div>
          <div hidden={activeRightTab !== "customs"} className="min-h-0 flex-1">
            <CustomsExtractorPanel fileId={currentFileId} fileName={availableFiles.find((file) => file.id === currentFileId)?.alias || availableFiles.find((file) => file.id === currentFileId)?.name || null} />
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
