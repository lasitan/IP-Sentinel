export interface Env {
  SENTINEL_KV: KVNamespace;
  AGENT_HUB: DurableObjectNamespace;
  TG_TOKEN: string;
  WEBHOOK_SECRET?: string;
  PUBLIC_BASE_URL?: string;
}

/** Cloudflare hibernation WebSocket extensions. */
export type HibernationWebSocket = WebSocket & {
  serializeAttachment(value: unknown): void;
  deserializeAttachment(): unknown;
};

export function asHibernationWS(ws: WebSocket): HibernationWebSocket {
  return ws as HibernationWebSocket;
}

export function sendBinary(ws: WebSocket, data: Uint8Array): void {
  // Copy into a fresh ArrayBuffer-backed view for Workers typing.
  const copy = new Uint8Array(data.byteLength);
  copy.set(data);
  ws.send(copy);
}
