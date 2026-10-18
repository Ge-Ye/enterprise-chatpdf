"use client";

import React, { useState } from "react";
import { useChat } from "../context/ChatContext";

export default function UploadZone() {
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number>(0);
  const [status, setStatus] = useState<"idle" | "uploading" | "success" | "error">("idle");
  const { setCurrentFileId, clearMessages } = useChat(); // ✅ 获取方法

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
    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/api";
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
        console.log("✅ 上传成功:", body);
        setStatus("success");
        
        // ✅✅✅ 关键修复：保存 file_id 到全局状态
        if (typeof body === "object" && body.file_id) {
          console.log("💾 保存 file_id:", body.file_id); // ← 添加日志确认
          setCurrentFileId(body.file_id);
          clearMessages();
          alert(`✅ ${body.filename || "文件"} 上传成功！可以开始提问了`);
        } else {
          console.error("❌ 响应中缺少 file_id:", body);
          alert("上传成功但未返回 file_id，请检查后端");
        }
      } else {
        console.error("❌ 上传失败:", xhr.status, body);
        setStatus("error");
        alert("上传失败，请重试");
      }
    };

    xhr.onerror = () => {
      console.error("❌ 上传请求出错");
      setStatus("error");
      alert("网络错误，请检查后端服务");
    };

    xhr.send(formData);
  };

  return (
    <div className="flex flex-col items-center justify-center w-full max-w-md mx-auto bg-white p-6 rounded-2xl shadow-md border">
      <h2 className="text-xl font-semibold mb-4 text-gray-800">上传你的 PDF 文件</h2>

      <label className="w-full cursor-pointer border-2 border-dashed border-gray-300 rounded-lg p-6 text-center hover:border-blue-500 transition">
        <input type="file" accept="application/pdf" className="hidden" onChange={handleFileChange} />
        {file ? (
          <p className="text-gray-700 truncate">{file.name}</p>
        ) : (
          <p className="text-gray-400">点击或拖拽文件到此处</p>
        )}
      </label>

      <button
        onClick={handleUpload}
        disabled={!file || status === "uploading"}
        className={`mt-4 px-6 py-2 rounded-xl text-white font-medium transition ${
          status === "uploading" ? "bg-gray-400" : "bg-blue-600 hover:bg-blue-700"
        } disabled:opacity-50`}
      >
        {status === "uploading" ? "上传中..." : "开始上传"}
      </button>

      {status !== "idle" && (
        <div className="w-full mt-4">
          <div className="flex justify-between text-sm mb-1">
            <span className="text-gray-600">
              {status === "uploading"
                ? `正在上传：${progress}%`
                : status === "success"
                ? "✅ 上传成功"
                : status === "error"
                ? "❌ 上传失败"
                : ""}
            </span>
          </div>
          <div className="w-full bg-gray-200 h-2 rounded-full">
            <div
              className={`h-2 rounded-full transition-all duration-300 ${
                status === "success"
                  ? "bg-green-500"
                  : status === "error"
                  ? "bg-red-500"
                  : "bg-blue-500"
              }`}
              style={{ width: `${progress}%` }}
            ></div>
          </div>
        </div>
      )}
    </div>
  );
}