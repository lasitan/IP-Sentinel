# IP-Sentinel Master — Cloudflare Workers + DO + KV

与 VPS Go Master **同契约**：

| 路径 | 说明 |
|------|------|
| `POST /tg` | Telegram Webhook（`X-Telegram-Bot-Api-Secret-Token`） |
| `GET /ws` | Agent WebSocket（MessagePack 二进制帧）→ Durable Object |
| `GET /v1/ready` | 健康检查 |

状态存 KV（`node:*`、`trend:*`）；Agent 长连接由 Durable Object `AgentHub` 维持。

## 开发与类型检查

```bash
cd deploy/workers
npm install
npm run typecheck
```

## 部署

在支持 wrangler/workerd 的平台（通常为 Linux/macOS x64/arm64）执行：

```bash
cd deploy/workers
npm install
npm run deploy   # 或: npx wrangler@4.14.4 deploy
# 首次需:
# npx wrangler kv namespace create SENTINEL_KV  # 填入 wrangler.toml
# npx wrangler secret put TG_TOKEN
# npx wrangler secret put WEBHOOK_SECRET
```

部署后设置 Telegram webhook：

```text
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<worker>/tg&secret_token=<WEBHOOK_SECRET>
```

Agent `MASTER_BASE_URL` 设为 Worker 公网 HTTPS 根地址（勿在 TG 聊天中预览/重发 WSS URL）。

## 说明

- Workers 面不提供 Nginx 一键菜单（无主机 nginx）。
- 协议实现见 `src/protocol.ts`，需与 Go `internal/protocol` 保持一致。
