"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      router.push("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center bg-neutral-950 text-neutral-100">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-2xl border border-neutral-800 bg-neutral-900 p-8 shadow-xl"
      >
        <h1 className="text-2xl font-bold mb-1">Insight AI</h1>
        <p className="text-sm text-neutral-400 mb-6">多智能体情报分析平台 · 登录</p>

        <label className="block text-sm mb-1 text-neutral-400">用户名</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
          autoFocus
          className="w-full mb-4 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-sky-500"
        />

        <label className="block text-sm mb-1 text-neutral-400">密码</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          className="w-full mb-6 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-sky-500"
        />

        {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-sky-600 py-2 font-medium hover:bg-sky-500 disabled:opacity-50"
        >
          {loading ? "登录中…" : "登录"}
        </button>
        <p className="mt-4 text-center text-sm text-neutral-500">
          还没有账号？
          <button type="button" onClick={() => router.push("/register")} className="text-sky-400 hover:underline ml-1">
            立即注册
          </button>
        </p>
      </form>
    </main>
  );
}