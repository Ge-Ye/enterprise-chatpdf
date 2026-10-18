"use client";
import React, { useState } from "react";

interface Props {
  onUpload: (file: File) => void;
}

export default function PDFUploader({ onUpload }: Props) {
  const [fileName, setFileName] = useState("");

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setFileName(file.name);
      onUpload(file);
    }
  };

  return (
    <div className="border-2 border-dashed border-gray-300 rounded-xl p-6 text-center">
      <label className="cursor-pointer">
        <input type="file" accept="application/pdf" className="hidden" onChange={handleFileChange} />
        <span className="text-blue-600 hover:underline">点击选择 PDF 文件</span>
      </label>
      {fileName && <p className="mt-2 text-sm text-gray-500">已选择：{fileName}</p>}
    </div>
  );
}
