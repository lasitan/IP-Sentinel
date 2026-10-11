# IP-Sentinel

![License](https://img.shields.io/github/license/lasitan/IP-Sentinel)

轻量级 **Master–Agent** 分布式维护工具（Go）：在 VPS 上定时执行 Google 地理定位维护、信任站点访问与 IP 质量检测，并通过 Telegram 集中管理多节点。

Telegram 频道：[IP-Sentinel Matrix](https://t.me/IP_Sentinel_Matrix)

## 功能概览

- **Go 双端**：控制端 / 节点均为单二进制；WebSocket 使用 **MessagePack 二进制帧** + HMAC 鉴权。
- **同端口控制面**：控制端单端口提供 `POST /tg`（Telegram Webhook）与 `GET /ws`（节点连接）。
- **引导轮询 → Webhook**：仅有 Token 时用 `getUpdates` 引导配置；`/nginx` 部署反代后自动 `setWebhook`。
- **加强保活**：传输层 Ping 10s + 应用层 ping/pong 10s/15s，并刷新 `last_seen`。
- **节点发现**：安装时写入 `MASTER_BASE_URL`（不在聊天中预览 WSS 地址）。
- **deb 发包**：Release 提供 `.deb`；安装脚本检测 Debian/Ubuntu 环境后 `dpkg` 安装。
- **Workers 部署面**：可选 Cloudflare Workers + Durable Objects + KV（见 [`deploy/workers`](deploy/workers)）。

## 目录结构

```text
IP-Sentinel/
├── cmd/master|agent     # 入口
├── internal/protocol    # 二进制协议
├── internal/master      # TG / WS / Nginx / DB
├── internal/agent       # 调度 / 业务模块
├── packaging/           # nfpm deb 打包
├── deploy/workers       # Workers + DO + KV
├── master/              # 控制端安装脚本
├── core/                # 节点安装脚本
├── data/                # 区域与关键词
└── scripts/             # 数据维护与 build-deb
```

## 本地开发

需要 Go 1.22+。

```bash
make tidy
make test
make build
# 构建 deb（需 nfpm）
make deb
```

## 快速部署

### 模式 A：私有控制端（VPS，Debian/Ubuntu）

```bash
curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/master/install_master.sh -o /tmp/ins_master.sh && sudo bash /tmp/ins_master.sh
```

在 TG 对 Bot 发送 `/nginx` 配置域名；节点安装时填写同一 `MASTER_BASE_URL`。

```bash
curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/core/install.sh -o /tmp/ins_agent.sh && sudo bash /tmp/ins_agent.sh
```

将 `#REGISTER#|...` 发给 Bot 完成注册。

### 模式 B：Workers + KV

见 [deploy/workers/README.md](deploy/workers/README.md)。

## 卸载

```bash
# 节点
sudo bash /opt/ip_sentinel/core/uninstall.sh
# 或: sudo apt-get remove ip-sentinel-agent

# 控制端
sudo bash /opt/ip_sentinel_master/uninstall.sh
# 或: sudo apt-get remove ip-sentinel-master
```

## 免责声明

仅供学习与个人 VPS 维护。请遵守当地法律及服务商 ToS；使用风险自负。
