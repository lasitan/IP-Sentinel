import { decode, encode } from "@msgpack/msgpack";

export type Envelope = Record<string, unknown>;

export const Op = {
  Auth: "auth",
  AuthOK: "auth_ok",
  AuthErr: "auth_err",
  Ping: "ping",
  Pong: "pong",
  Cmd: "cmd",
  CmdResult: "cmd_result",
  TrendSave: "trend_save",
} as const;

export function encodeMsg(msg: Envelope): Uint8Array {
  return encode(msg);
}

export function decodeMsg(raw: ArrayBuffer | Uint8Array): Envelope {
  const data = decode(raw) as Envelope;
  if (!data || typeof data !== "object") throw new Error("bad message");
  return data;
}

export async function hmacSign(key: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    enc.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function verifyAuth(
  authKey: string,
  chatId: string,
  node: string,
  ts: number,
  sign: string,
): Promise<boolean> {
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > 120) return false;
  const expected = await hmacSign(authKey, `auth:${chatId}:${node}:${ts}`);
  return expected === sign;
}

export function asString(v: unknown): string {
  if (typeof v === "string") return v;
  if (v == null) return "";
  return String(v);
}

export function asNumber(v: unknown): number {
  if (typeof v === "number") return v;
  if (typeof v === "string") return parseInt(v, 10) || 0;
  return 0;
}
