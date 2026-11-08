"use client";

import { API_BASE_URL } from "@/lib/api";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ArrowLeft, CheckCircle2, FileText, Loader2 } from "lucide-react";

type IndexState = "idle" | "polling" | "indexed" | "failed";

export default function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number>(0);
  const [status, setStatus] = useState<"idle" | "uploading" | "success" | "error">("idle");
  const [uploadedFileId, setUploadedFileId] = useState<string | null>(null);
  const [uploadedFileName, setUploadedFileName] = useState<string | null>(null);
  const [indexState, setIndexState] = useState<IndexState>("idle");
  const [indexNote, setIndexNote] = useState("");
  const pollTimer = useRef<number | null>(null);

  const stopPolling = () => {
    if (pollTimer.current !== null) {
      window.clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
  };

  useEffect(() => () => {
    if (pollTimer.current !== null) window.clearInterval(pollTimer.current);
  }, []);

  // 上传接口返回 processing；必须轮询到 indexed 才放行“开始对话”，
  // 否则会出现“刚上传就问，回答没有内容”的假故障。
  const startPolling = (fileId: string) => {
    stopPolling();
    setIndexState("polling");
    setIndexNote("已上传，正在等待后台处理…");
    const startedAt = Date.now();
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/files/${encodeURIComponent(fileId)}/status`);
        if (res.ok) {
          const data = (await res.json()) as { status?: string; stage?: string; error?: string; chunk_count?: number };
          if (data.status === "indexed") {
            stopPolling();
            setIndexState("indexed");
            setIndexNote(`索引完成${typeof data.chunk_count === "number" ? `（${data.chunk_count} 个片段）` : ""}，可以开始对话`);
            return;
          }
          if (data.status === "failed") {
            stopPolling();
            setIndexState("failed");
            setIndexNote(data.error || "后台索引失败，请重新上传或联系管理员");
            return;
          }
          if (data.status === "unknown") {
            stopPolling();
            setIndexState("failed");
            setIndexNote("未找到索引状态，请重新上传");
            return;
          }
          setIndexNote({
            queued: "已排队，等待后台处理…",
            parsing: "正在解析 PDF 文本…",
            embedding: "正在向量化文档片段…",
            indexing: "正在写入向量索引…",
          }[data.stage ?? ""] ?? "正在建立索引…");
        }
      } catch {
        // 瞬时网络错误继续轮询，直到超时
      }
      if (Date.now() - startedAt > 180000) {
        stopPolling();
        setIndexState("failed");
        setIndexNote("索引超时（超过 3 分钟），请检查后端日志后重新上传");
      }
    };
    void poll();
    pollTimer.current = window.setInterval(() => void poll(), 1500);
  };

  interface UploadResponse {
    file_id?: string;
    filename?: string;
    status?: string;
    detail?: string;
  }

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0];
    if (selected && selected.type === "application/pdf") {
      setFile(selected);
      setProgress(0);
      setStatus("idle");
      setUploadedFileId(null);
    } else {
      alert("请选择一个 PDF 文件！");
    }
  };

  const handleUpload = async () => {
    if (!file) {
      alert("请先选择文件！");
      return;
    }

    const formData = new FormData();
    formData.append("file", file);

    const xhr = new XMLHttpRequest();
    const apiBase = API_BASE_URL;
    xhr.open("POST", `${apiBase}/upload`, true);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        setProgress(percent);
        setStatus("uploading");
      }
    };

    xhr.onload = () => {
      const bodyText = xhr.responseText;
      let body: UploadResponse | string = "";

      try {
        body = bodyText ? (JSON.parse(bodyText) as UploadResponse) : "";
      } catch {
        body = bodyText;
      }

      if (xhr.status === 200) {
        console.log("上传成功:", body);
        setStatus("success");

        if (typeof body === "object" && body.file_id) {
          setUploadedFileId(body.file_id);
          setUploadedFileName(body.filename || file.name);
          startPolling(body.file_id);
        } else {
          console.error("响应中未包含 file_id:", body);
          alert("上传成功，但未返回文件 ID。");
        }
      } else {
        console.error("上传失败:", xhr.status, body);
        setStatus("error");
        alert("上传失败，请重试。");
      }
    };

    xhr.onerror = () => {
      console.error("上传请求出错");
      setStatus("error");
      alert("网络错误，请检查连接。");
    };

    xhr.send(formData);
  };

  const handleReset = () => {
    stopPolling();
    setFile(null);
    setProgress(0);
    setStatus("idle");
    setUploadedFileId(null);
    setUploadedFileName(null);
    setIndexState("idle");
    setIndexNote("");
  };

  return (
    <main className="max-w-4xl mx-auto mt-10">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <Link
          href="/"
          className="flex items-center text-gray-600 hover:text-gray-900 transition"
        >
          <ArrowLeft className="w-5 h-5 mr-2" />
          返回首页
        </Link>
        <Link
          href="/chat"
          className="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 transition"
        >
          前往聊天 →
        </Link>
      </div>

      <h1 className="text-3xl font-bold text-center mb-8">📤 上传 PDF 文件</h1>

      {/* Upload Zone */}
      <div className="bg-white rounded-2xl shadow-lg p-8 border">
        {status === "success" && uploadedFileId ? (
          // 上传成功
          <div className="text-center">
            <div className={`flex items-center justify-center w-20 h-20 rounded-full mx-auto mb-4 ${
              indexState === "indexed" ? "bg-green-100" : indexState === "failed" ? "bg-red-100" : "bg-blue-100"
            }`}>
              {indexState === "indexed"
                ? <CheckCircle2 className="w-10 h-10 text-green-600" />
                : indexState === "failed"
                  ? <AlertTriangle className="w-10 h-10 text-red-600" />
                  : <Loader2 className="w-10 h-10 text-blue-600 animate-spin" />}
            </div>
            <h2 className="text-2xl font-semibold mb-2">
              {indexState === "indexed" ? "索引完成！" : indexState === "failed" ? "索引失败" : "正在建立索引…"}
            </h2>
            <p className="text-gray-600 mb-2">
              文件：<span className="font-medium">{uploadedFileName}</span>
            </p>
            <p
              aria-live="polite"
              className={`mb-6 text-sm ${indexState === "failed" ? "text-red-600" : indexState === "indexed" ? "text-green-700" : "text-gray-500"}`}
            >
              {indexNote}
            </p>

            <div className="space-y-3">
              {indexState === "indexed" ? (
                <Link
                  href={`/chat?fileId=${uploadedFileId}`}
                  className="block w-full bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition"
                >
                  与此文件开始对话
                </Link>
              ) : (
                <button
                  type="button"
                  disabled
                  className="w-full bg-gray-300 text-white px-6 py-3 rounded-lg cursor-not-allowed"
                >
                  {indexState === "failed" ? "索引失败，请重新上传" : "索引完成后即可开始对话"}
                </button>
              )}
              <button
                onClick={handleReset}
                className="w-full bg-gray-200 text-gray-700 px-6 py-3 rounded-lg hover:bg-gray-300 transition"
              >
                重新上传文件
              </button>
            </div>
          </div>
        ) : (
          // 上传表单
          <>
            <div className="mb-6">
              <label className="w-full cursor-pointer border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-blue-500 transition block">
                <input
                  type="file"
                  accept="application/pdf"
                  className="hidden"
                  onChange={handleFileChange}
                />
                {file ? (
                  <div className="flex items-center justify-center">
                    <FileText className="w-8 h-8 text-blue-600 mr-3" />
                    <p className="text-gray-700 truncate font-medium">{file.name}</p>
                  </div>
                ) : (
                  <div>
                    <FileText className="w-12 h-12 text-gray-400 mx-auto mb-3" />
                    <p className="text-gray-600">点击或拖拽 PDF 文件到此处</p>
                    <p className="text-sm text-gray-400 mt-2">仅支持 .pdf 格式文件</p>
                  </div>
                )}
              </label>
            </div>

            <button
              onClick={handleUpload}
              disabled={!file || status === "uploading"}
              className={`w-full py-3 rounded-lg text-white font-medium transition ${
                status === "uploading"
                  ? "bg-gray-400 cursor-not-allowed"
                  : "bg-blue-600 hover:bg-blue-700"
              } disabled:opacity-50`}
            >
              {status === "uploading" ? `正在上传... ${progress}%` : "开始上传"}
            </button>

            {/* 进度条 */}
            {status !== "idle" && (
              <div className="mt-4">
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-gray-600">
                    {status === "uploading"
                      ? `上传中：${progress}%`
                      : status === "error"
                      ? "上传失败"
                      : ""}
                  </span>
                </div>
                <div className="w-full bg-gray-200 h-2 rounded-full overflow-hidden">
                  <div
                    className={`h-2 transition-all duration-300 ${
                      status === "error" ? "bg-red-500" : "bg-blue-500"
                    }`}
                    style={{ width: `${progress}%` }}
                  ></div>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* 上传说明 */}
      <div className="mt-8 bg-blue-50 rounded-xl p-6">
        <h3 className="font-semibold mb-3">📋 上传说明</h3>
        <ul className="space-y-2 text-sm text-gray-700">
          <li>• 仅支持 PDF 格式文件</li>
          <li>• 推荐文件大小不超过 50MB</li>
          <li>• 上传完成后系统会自动解析并建立索引</li>
          <li>• 需等待索引完成（页面显示“索引完成”）后再开始聊天</li>
        </ul>
      </div>
    </main>
  );
}
