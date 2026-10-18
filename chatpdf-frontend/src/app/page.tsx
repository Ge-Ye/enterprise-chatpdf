"use client";

import Link from "next/link";
import { BookOpen, MessageSquare } from "lucide-react";

export default function Home() {
  return (
    <main className="max-w-4xl mx-auto mt-20">
      <div className="text-center mb-12">
        <h1 className="text-5xl font-bold text-gray-900 mb-4">
          📚 智能文档问答系统
        </h1>
        <p className="text-xl text-gray-600">
          上传 PDF 文档，与 AI 智能对话，快速获取答案
        </p>
      </div>

      <div className="grid md:grid-cols-2 gap-6 mt-12">
        {/* 上传卡片 */}
        <Link href="/upload">
          <div className="bg-white rounded-2xl shadow-lg p-8 hover:shadow-xl transition-shadow cursor-pointer border-2 border-transparent hover:border-blue-500">
            <div className="flex items-center justify-center w-16 h-16 bg-blue-100 rounded-full mb-4 mx-auto">
              <BookOpen className="w-8 h-8 text-blue-600" />
            </div>
            <h2 className="text-2xl font-semibold text-center mb-3">
              上传文档
            </h2>
            <p className="text-gray-600 text-center">
              上传 PDF 文件到系统，支持多文档管理和智能解析
            </p>
            <div className="mt-6 text-center">
              <span className="inline-block bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 transition">
                开始上传 →
              </span>
            </div>
          </div>
        </Link>

        {/* 聊天卡片 */}
        <Link href="/chat">
          <div className="bg-white rounded-2xl shadow-lg p-8 hover:shadow-xl transition-shadow cursor-pointer border-2 border-transparent hover:border-green-500">
            <div className="flex items-center justify-center w-16 h-16 bg-green-100 rounded-full mb-4 mx-auto">
              <MessageSquare className="w-8 h-8 text-green-600" />
            </div>
            <h2 className="text-2xl font-semibold text-center mb-3">
              智能问答
            </h2>
            <p className="text-gray-600 text-center">
              基于已上传的文档进行智能问答，获取精准答案
            </p>
            <div className="mt-6 text-center">
              <span className="inline-block bg-green-600 text-white px-6 py-2 rounded-lg hover:bg-green-700 transition">
                开始对话 →
              </span>
            </div>
          </div>
        </Link>
      </div>

      {/* 功能说明 */}
      <div className="mt-16 bg-gradient-to-r from-blue-50 to-green-50 rounded-xl p-8">
        <h3 className="text-2xl font-semibold mb-6 text-center text-gray-800">📖 使用指南</h3>
        <div className="grid md:grid-cols-3 gap-6">
          <div className="text-center bg-white rounded-lg p-6 shadow-sm">
            <div className="text-4xl mb-3">📤</div>
            <h4 className="font-semibold text-lg mb-2 text-gray-800">第一步</h4>
            <p className="text-gray-600">上传 PDF 文档到系统，系统将自动解析内容</p>
          </div>
          <div className="text-center bg-white rounded-lg p-6 shadow-sm">
            <div className="text-4xl mb-3">💬</div>
            <h4 className="font-semibold text-lg mb-2 text-gray-800">第二步</h4>
            <p className="text-gray-600">选择文档并输入问题，开始智能对话</p>
          </div>
          <div className="text-center bg-white rounded-lg p-6 shadow-sm">
            <div className="text-4xl mb-3">✨</div>
            <h4 className="font-semibold text-lg mb-2 text-gray-800">第三步</h4>
            <p className="text-gray-600">获取 AI 生成的精准回答和相关引用</p>
          </div>
        </div>
      </div>

      {/* 特色功能 */}
      <div className="mt-12 grid md:grid-cols-2 gap-6">
        <div className="bg-white rounded-xl p-6 shadow-md border border-gray-100">
          <h4 className="font-semibold text-lg mb-3 flex items-center">
            <span className="text-2xl mr-2">🚀</span>
            快速智能
          </h4>
          <p className="text-gray-600 text-sm">
            基于先进的 AI 技术，快速理解文档内容，提供准确的答案
          </p>
        </div>
        <div className="bg-white rounded-xl p-6 shadow-md border border-gray-100">
          <h4 className="font-semibold text-lg mb-3 flex items-center">
            <span className="text-2xl mr-2">🔒</span>
            安全可靠
          </h4>
          <p className="text-gray-600 text-sm">
            文档存储安全，支持多用户协作，保护您的数据隐私
          </p>
        </div>
      </div>
    </main>
  );
}