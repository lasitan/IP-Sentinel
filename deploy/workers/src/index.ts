import type { Env } from "./env";
import { AgentHub } from "./hub";
import { extractUpdate, sendMessage, sendUI, tgCall } from "./tg";

export { AgentHub };
export type { Env };

function hubStub(env: Env) {
  // single global hub DO — scale by sharding owner hash if needed
  const id = env.AGENT_HUB.idFromName("global");
  return env.AGENT_HUB.get(id);
}

async function listNodes(env: Env, owner: string) {
  const listed = await env.SENTINEL_KV.list({ prefix: `node:${owner}|` });
  const nodes: Record<string, unknown>[] = [];
  for (const k of listed.keys) {
    const v = await env.SENTINEL_KV.get(k.name, "json");
    if (v) nodes.push(v as Record<string, unknown>);
  }
  return nodes;
}

async function handleUpdate(env: Env, update: Record<string, unknown>) {
  const { chatId, text, cbId } = extractUpdate(update);
  if (!chatId) return;
  if (cbId) await tgCall(env, "answerCallbackQuery", { callback_query_id: cbId });

  // Ignore WSS lookup / never preview or resend WSS URLs
  if (text.startsWith("#WSS_LOOKUP#")) return;

  if (text.startsWith("#REGISTER#")) {
    const parts = text.split("|");
    const node = (parts[1] || "").replace(/[^a-zA-Z0-9._-]/g, "").slice(0, 64);
    const ip = parts[2] || "";
    const region = parts[3] || "UNKNOWN";
    const alias = parts[4] || node;
    const ota = (parts[5] || "false").toLowerCase();
    if (!node) {
      await sendMessage(env, chatId, "节点名无效", false);
      return;
    }
    await env.SENTINEL_KV.put(
      `node:${chatId}|${node}`,
      JSON.stringify({
        chat_id: chatId,
        node_name: node,
        agent_ip: ip,
        region,
        node_alias: alias,
        enable_ota: ota,
        last_seen: new Date().toISOString(),
      }),
    );
    await sendMessage(env, chatId, `✅ *已注册*\n节点 \`${alias}\` 已加入列表。`);
    return;
  }

  const cmd = text.split(/\s+/)[0]?.split("@")[0] || text;
  if (cmd === "/start" || cmd === "/menu" || text === "main_menu") {
    const base = env.PUBLIC_BASE_URL || "";
    const body =
      `*IP-Sentinel 控制端* (Workers)\n` +
      (base ? `公网: \`${base}\`\n` : "") +
      `\n请选择操作：`;
    await sendUI(env, chatId, body, [
      [{ text: "节点列表", callback_data: "list_nodes" }],
      [{ text: "节点状态摘要", callback_data: "fleet_summary" }],
    ]);
    return;
  }

  if (text === "list_nodes") {
    const nodes = await listNodes(env, chatId);
    if (!nodes.length) {
      await sendMessage(env, chatId, "暂无节点。请发送 `#REGISTER#|...` 注册。");
      return;
    }
    const kb: { text: string; callback_data: string }[][] = [];
    for (const n of nodes) {
      const node = String(n.node_name);
      const alias = String(n.node_alias || node);
      const online = await hubStub(env).fetch(
        `https://do/internal/online?owner=${encodeURIComponent(chatId)}&node=${encodeURIComponent(node)}`,
      );
      const oj = (await online.json()) as { online: boolean };
      kb.push([
        {
          text: `${oj.online ? "🟢" : "🔴"} ${alias}`,
          callback_data: `manage:${node}`,
        },
      ]);
    }
    kb.push([{ text: "返回", callback_data: "main_menu" }]);
    await sendUI(env, chatId, "*节点列表*", kb);
    return;
  }

  if (text.startsWith("manage:")) {
    const node = text.slice("manage:".length);
    const raw = await env.SENTINEL_KV.get(`node:${chatId}|${node}`, "json");
    if (!raw) {
      await sendMessage(env, chatId, "节点不存在", false);
      return;
    }
    const n = raw as Record<string, string>;
    const online = await hubStub(env).fetch(
      `https://do/internal/online?owner=${encodeURIComponent(chatId)}&node=${encodeURIComponent(node)}`,
    );
    const oj = (await online.json()) as { online: boolean };
    const body = `*${n.node_alias || node}*\n状态: *${oj.online ? "在线" : "离线"}*\nIP: \`${n.agent_ip || ""}\``;
    await sendUI(env, chatId, body, [
      [
        { text: "运行维护", callback_data: `run:${node}` },
        { text: "IP 质量检测", callback_data: `quality:${node}` },
      ],
      [
        { text: "Google 定位", callback_data: `google:${node}` },
        { text: "信任站点", callback_data: `trust:${node}` },
      ],
      [{ text: "返回", callback_data: "list_nodes" }],
    ]);
    return;
  }

  for (const action of ["run", "quality", "google", "trust", "report", "log"]) {
    if (text.startsWith(action + ":")) {
      const node = text.slice(action.length + 1);
      const res = await hubStub(env).fetch("https://do/internal/cmd", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          owner: chatId,
          node,
          path: `/trigger_${action}`,
          params: {},
        }),
      });
      const j = (await res.json()) as { result: string };
      const labels: Record<string, string> = {
        run: "运行维护",
        quality: "IP 质量检测",
        google: "Google 定位",
        trust: "信任站点",
        report: "生成报告",
        log: "查看日志",
      };
      const label = labels[action] || action;
      const msg =
        j.result === "FAILED" ? "WSS 未连接或指令超时" : `\`${label}\` 已受理\n${j.result}`;
      await sendMessage(env, chatId, msg);
      return;
    }
  }

  if (text === "fleet_summary") {
    const nodes = await listNodes(env, chatId);
    let online = 0;
    const lines: string[] = [];
    for (const n of nodes) {
      const node = String(n.node_name);
      const alias = String(n.node_alias || node);
      const oRes = await hubStub(env).fetch(
        `https://do/internal/online?owner=${encodeURIComponent(chatId)}&node=${encodeURIComponent(node)}`,
      );
      const oj = (await oRes.json()) as { online: boolean };
      if (oj.online) {
        online++;
        lines.push(`• *${alias}*: 在线`);
      } else {
        lines.push(`• *${alias}*: (未连接)`);
      }
    }
    await sendMessage(
      env,
      chatId,
      `节点状态摘要\n共 *${nodes.length}* | 在线 *${online}*\n\n${lines.join("\n")}`,
    );
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/v1/ready") {
      return Response.json({ ok: true, service: "ip-sentinel-master-workers" });
    }

    if (url.pathname === "/tg" && request.method === "POST") {
      if (env.WEBHOOK_SECRET) {
        const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
        if (secret !== env.WEBHOOK_SECRET) {
          return new Response("forbidden", { status: 403 });
        }
      }
      const update = (await request.json()) as Record<string, unknown>;
      await handleUpdate(env, update);
      return Response.json({ ok: true });
    }

    if (url.pathname === "/ws") {
      return hubStub(env).fetch(request);
    }

    if (url.pathname === "/" ) {
      return new Response("IP-Sentinel Master (Workers + DO + KV)");
    }

    return new Response("not found", { status: 404 });
  },
};
