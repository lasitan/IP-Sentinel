import { DurableObject } from "cloudflare:workers";
import { asHibernationWS, sendBinary, type Env, type HibernationWebSocket } from "./env";
import {
  Op,
  asNumber,
  asString,
  decodeMsg,
  encodeMsg,
  verifyAuth,
  type Envelope,
} from "./protocol";

type Pending = {
  resolve: (v: { status: number; body: string }) => void;
};

/** Durable Object: maintains Agent WebSocket sessions (binary MessagePack). */
export class AgentHub extends DurableObject<Env> {
  sessions = new Map<string, HibernationWebSocket>();
  pending = new Map<string, Pending>();

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/internal/cmd" && request.method === "POST") {
      const body = (await request.json()) as {
        owner: string;
        node: string;
        path: string;
        params?: Record<string, unknown>;
        timeoutMs?: number;
      };
      const result = await this.sendCommand(
        body.owner,
        body.node,
        body.path,
        body.params || {},
        body.timeoutMs || 15000,
      );
      return Response.json({ result });
    }
    if (url.pathname === "/internal/online" && request.method === "GET") {
      const owner = url.searchParams.get("owner") || "";
      const node = url.searchParams.get("node") || "";
      return Response.json({ online: this.sessions.has(`${owner}|${node}`) });
    }

    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket", { status: 426 });
    }
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    this.ctx.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, message: ArrayBuffer | string): Promise<void> {
    const socket = asHibernationWS(ws);
    const raw =
      typeof message === "string"
        ? new TextEncoder().encode(message)
        : new Uint8Array(message);
    let msg: Envelope;
    try {
      msg = decodeMsg(raw);
    } catch {
      return;
    }
    const op = asString(msg.op);
    const key = this.keyFor(socket);

    if (!key) {
      if (op !== Op.Auth) {
        sendBinary(socket, encodeMsg({ op: Op.AuthErr, error: "auth required" }));
        socket.close(1008, "auth");
        return;
      }
      const chatId = asString(msg.chat_id);
      const node = asString(msg.node).replace(/[^a-zA-Z0-9._-]/g, "").slice(0, 64);
      const ts = asNumber(msg.ts);
      const sign = asString(msg.sign);
      if (!chatId || !node || !(await verifyAuth(chatId, chatId, node, ts, sign))) {
        sendBinary(socket, encodeMsg({ op: Op.AuthErr, error: "invalid signature" }));
        socket.close(1008, "auth");
        return;
      }
      const k = `${chatId}|${node}`;
      const old = this.sessions.get(k);
      if (old && old !== socket) {
        try {
          old.close(1000, "replaced");
        } catch {
          /* ignore */
        }
      }
      this.sessions.set(k, socket);
      try {
        socket.serializeAttachment({ key: k });
      } catch {
        (socket as HibernationWebSocket & { __key?: string }).__key = k;
      }
      await this.env.SENTINEL_KV.put(
        `node:${k}`,
        JSON.stringify({
          chat_id: chatId,
          node_name: node,
          agent_ip: asString(msg.ip),
          region: asString(msg.region) || "UNKNOWN",
          node_alias: asString(msg.alias) || node,
          enable_ota: asString(msg.ota) || "false",
          last_seen: new Date().toISOString(),
        }),
      );
      sendBinary(socket, encodeMsg({ op: Op.AuthOK, node }));
      return;
    }

    if (op === Op.Ping) {
      const rawNode = await this.env.SENTINEL_KV.get(`node:${key}`);
      if (rawNode) {
        const obj = JSON.parse(rawNode) as Record<string, unknown>;
        obj.last_seen = new Date().toISOString();
        await this.env.SENTINEL_KV.put(`node:${key}`, JSON.stringify(obj));
      }
      sendBinary(socket, encodeMsg({ op: Op.Pong }));
      return;
    }
    if (op === Op.CmdResult) {
      const id = asString(msg.id);
      const p = this.pending.get(id);
      if (p) {
        this.pending.delete(id);
        p.resolve({ status: asNumber(msg.status), body: asString(msg.body) });
      }
      return;
    }
    if (op === Op.TrendSave) {
      const node = asString(msg.node) || key.split("|")[1];
      const score = asNumber(msg.score);
      const entry = {
        node_name: node,
        scam_score: score,
        goog_status: asString(msg.goog) || "Unknown",
        nf_status: asString(msg.play) || "Unknown",
        gpt_status: asString(msg.gemini) || "Unknown",
        check_time: new Date().toISOString(),
      };
      const listKey = `trend:${node}`;
      const prev = (await this.env.SENTINEL_KV.get(listKey, "json")) as unknown[] | null;
      const list = Array.isArray(prev) ? prev : [];
      list.unshift(entry);
      await this.env.SENTINEL_KV.put(listKey, JSON.stringify(list.slice(0, 200)));
    }
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    const key = this.keyFor(asHibernationWS(ws));
    if (key && this.sessions.get(key) === ws) this.sessions.delete(key);
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    await this.webSocketClose(ws);
  }

  private keyFor(ws: HibernationWebSocket): string | undefined {
    try {
      const att = ws.deserializeAttachment() as { key?: string } | null;
      if (att?.key) return att.key;
    } catch {
      /* ignore */
    }
    return (ws as HibernationWebSocket & { __key?: string }).__key;
  }

  async sendCommand(
    owner: string,
    node: string,
    path: string,
    params: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<string> {
    const k = `${owner}|${node}`;
    const ws = this.sessions.get(k);
    if (!ws) return "FAILED";
    const id = crypto.randomUUID().replace(/-/g, "");
    const result = new Promise<{ status: number; body: string }>((resolve) => {
      this.pending.set(id, { resolve });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          resolve({ status: 504, body: "FAILED" });
        }
      }, timeoutMs);
    });
    sendBinary(ws, encodeMsg({ op: Op.Cmd, id, path, params }));
    const res = await result;
    if (res.body === "FAILED" || res.status >= 400) {
      return res.body || "FAILED";
    }
    return res.body;
  }
}

export type { Env };
