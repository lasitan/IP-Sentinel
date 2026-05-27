# IP-Sentinel

![Agent Installs](https://img.shields.io/endpoint?url=https://ip-sentinel-count.samanthaestime296.workers.dev/stats/agent)
![Master Commands](https://img.shields.io/endpoint?url=https://ip-sentinel-count.samanthaestime296.workers.dev/stats/master)
![License](https://img.shields.io/github/license/lasitan/IP-Sentinel)

轻量级 **Master–Agent** 分布式工具：在 VPS 上定时执行 Google 地理纠偏、站点访问与 IP 质量检测，并通过 Telegram 集中管理多节点。

Telegram 频道：[IP-Sentinel Matrix](https://t.me/IP_Sentinel_Matrix)

## 功能概览

- **IP 质量检测**：集成 Scamalytics、AbuseIPDB 等数据源，输出风险分、流媒体解锁与 Google 地理判定结果。
- **高并发 Master**：SQLite WAL + 请求排队，降低 `database is locked` 与 Telegram 429 概率。
- **极简依赖**：业务代码主要使用 Python 标准库；生产环境由安装脚本安装 [uv](https://docs.astral.sh/uv/) 管理运行时。
- **Telegram 控制台**：Inline 键盘管理节点、开关模块、查看趋势与日志。
- **OTA 升级**：私有 Master 可向 Agent 下发远程升级；Master 自身也支持 OTA（可选）。
- **配置热升级**：安装脚本从远端读取版本号，已安装节点可保留配置直接升级。
- **数据流水线**：GitHub Actions 定期更新 UA 库、区域关键词与信任站点列表。
- **UTC 调度**：默认每 20 分钟执行维护，按部署时间错峰，减轻 API 压力。
- **网络容错**：出站接口绑定、快速连通性检测与多级回退，适配 NAT / 双栈环境。
- **HMAC 签名**：Agent Webhook 指令带时间戳与 HMAC-SHA256（60 秒有效）。
- **公共 Master**：[@OmniBeacon_bot](https://t.me/OmniBeacon_bot) 免自建即可试用；亦支持私有化部署。

## 目录结构

```text
IP-Sentinel/
├── .github/workflows/   # CI：UA 生成、关键词抓取等
├── master/              # Master 安装/卸载脚本 (Bash)
├── py/master/           # Master 逻辑 (Telegram、SQLite、HMAC)
├── core/                # Agent 安装/卸载脚本；运行时 cert、探针等
├── py/                  # Agent 逻辑
├── scripts/             # 数据维护脚本
├── data/                # 区域配置、关键词、UA 库
├── version.txt          # Agent / Master 版本号
└── telemetry/           # 匿名安装计数 (Cloudflare Workers)
```

## 本地开发

生产环境由 `install*.sh` 安装 uv，在 `/opt/ip_sentinel` 或 `/opt/ip_sentinel_master` 执行 `uv sync --no-dev` 后以 `uv run python py/...` 运行。

```bash
uv sync
uv run python py/mod_google.py
make lint
```

- Python 版本：`.python-version`（默认 3.12）
- 锁文件：`uv.lock`

## 快速部署

支持 Debian / Ubuntu / CentOS / RHEL / Alpine / Arch。

### 模式 A：私有 Master（推荐）

适合需要数据自控与 OTA 的场景。

1. **安装 Master**（一台 VPS 即可）  
   [部署说明](https://blog.iot-architect.com/engineering-practice/ip-sentinel-master-deployment-guide/)

```bash
curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/master/install_master.sh -o /tmp/ins_master.sh && sudo bash /tmp/ins_master.sh
```

2. **安装 Agent**（各维护节点）  
   选择私有 Master，填写自建 Bot [Token](https://blog.iot-architect.com/engineering-practice/create-private-telegram-bot-via-botfather) 与 [Chat ID](https://blog.iot-architect.com/engineering-practice/get-telegram-personal-id-via-userinfobot)。

```bash
curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/core/install.sh -o /tmp/ins_agent.sh && sudo bash /tmp/ins_agent.sh
```

3. 将安装后收到的 `#REGISTER#...` 消息发给您的 Bot 完成注册。

### 模式 B：官方公共网关

1. 在 Telegram 打开 [@OmniBeacon_bot](https://t.me/OmniBeacon_bot) 并发送 `/start`。
2. 在 VPS 上运行 Agent 安装脚本，选择官方网关并填写 Chat ID。  
   [说明](https://blog.iot-architect.com/engineering-practice/deploy-ip-sentinel-official-gateway/)

```bash
curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/core/install.sh -o /tmp/ins_agent.sh && sudo bash /tmp/ins_agent.sh
```

3. 将注册消息转发给官方 Bot。

## 升级

### OTA（私有 Master）

1. Master：菜单中「升级 Master 至 vX.X.X」。
2. 全部 Agent：「全节点 OTA 升级」。
3. 单节点：区域列表 → 节点面板 →「OTA 升级」。

### SSH 重装

在节点上再次执行 `core/install.sh`；若检测到已有配置，可选择保留配置仅更新程序。

## 卸载

```bash
bash /opt/ip_sentinel/core/uninstall.sh
```

或重新运行 `install.sh` 并选择卸载。

## Legacy（Debian 9）

```bash
bash <(curl -sL https://raw.githubusercontent.com/lasitan/IP-Sentinel/legacy/core/install.sh)
```

旧系统分支仅做基础维护，建议使用较新发行版。

## 社区

- Telegram：[@IP_Sentinel_Matrix](https://t.me/IP_Sentinel_Matrix)

## 贡献

新增区域请在 `data/regions/`、`data/keywords/` 与 `data/map.json` 中补充配置后提交 PR。

IP 质量检测脚本感谢 [xykt/IPQuality](https://github.com/xykt/IPQuality)。

## 免责声明

仅供学习与个人 VPS 维护。请遵守当地法律及服务商 ToS，勿用于恶意请求；使用风险自负。

## 链接

[![Blog](https://img.shields.io/badge/Blog-个人博客-blue)](https://blog.iot-architect.com)

[![Stargazers over time](https://starchart.cc/lasitan/IP-Sentinel.svg?variant=adaptive)](https://starchart.cc/lasitan/IP-Sentinel)
