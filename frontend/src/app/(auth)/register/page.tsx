"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { register } from "@/lib/api";

export default function RegisterPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    setLoading(true);
    try {
      await register(username, password);
      router.push("/login?registered=1");
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败");
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
        <h1 className="text-2xl font-bold mb-1">创建账号</h1>
        <p className="text-sm text-neutral-400 mb-6">用户名 3-32 位字母/数字/下划线</p>

        <label className="block text-sm mb-1 text-neutral-400">用户名</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
          pattern="[a-zA-Z0-9_]{3,32}"
          className="w-full mb-4 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-sky-500"
        />

        <label className="block text-sm mb-1 text-neutral-400">密码（至少 8 位）</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          className="w-full mb-4 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-sky-500"
        />

        <label className="block text-sm mb-1 text-neutral-400">确认密码</label>
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          className="w-full mb-6 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 outline-none focus:border-sky-500"
        />

        {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-sky-600 py-2 font-medium hover:bg-sky-500 disabled:opacity-50"
        >
          {loading ? "注册中…" : "注册"}
        </button>
        <p className="mt-4 text-center text-sm text-neutral-500">
          已有账号？
          <button type="button" onClick={() => router.push("/login")} className="text-sky-400 hover:underline ml-1">
            去登录
          </button>
        </p>
      </form>
    </main>
  );
}