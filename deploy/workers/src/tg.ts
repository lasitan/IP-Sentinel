import type { Env } from "./env";

export async function tgCall(env: Env, method: string, payload: Record<string, unknown>) {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json() as Promise<{ ok: boolean; result?: unknown; description?: string }>;
}

export async function sendMessage(env: Env, chatId: string, text: string, markdown = true) {
  const payload: Record<string, unknown> = { chat_id: chatId, text };
  if (markdown) payload.parse_mode = "Markdown";
  return tgCall(env, "sendMessage", payload);
}

export async function sendUI(
  env: Env,
  chatId: string,
  text: string,
  keyboard: { text: string; callback_data: string }[][],
) {
  return tgCall(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "Markdown",
    reply_markup: { inline_keyboard: keyboard },
  });
}

export function extractUpdate(update: Record<string, unknown>) {
  const msg = (update.message || {}) as Record<string, unknown>;
  const cb = (update.callback_query || {}) as Record<string, unknown>;
  const cbMsg = (cb.message || {}) as Record<string, unknown>;
  let chatId = "";
  let text = "";
  let cbId = "";
  if (msg.chat) {
    chatId = String((msg.chat as { id: unknown }).id);
    text = String(msg.text || "");
  }
  if (cb.id) {
    cbId = String(cb.id);
    text = String(cb.data || text);
    if (cbMsg.chat) chatId = String((cbMsg.chat as { id: unknown }).id);
  }
  return { chatId, text: text.trim(), cbId };
}
