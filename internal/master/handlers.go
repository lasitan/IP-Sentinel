package master

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/lasitan/IP-Sentinel/internal/protocol"
)

type Handlers struct {
	cfg    *Config
	db     *DB
	tg     *Telegram
	hub    *Hub
	mu     sync.Mutex
	rename map[int]string    // msgID -> node
	nginx  map[string]string // chatID -> awaiting domain|email
	forum  map[string]string // chatID -> awaiting forum_bind
}

func NewHandlers(cfg *Config, db *DB, tg *Telegram, hub *Hub) *Handlers {
	return &Handlers{
		cfg:    cfg,
		db:     db,
		tg:     tg,
		hub:    hub,
		rename: map[int]string{},
		nginx:  map[string]string{},
		forum:  map[string]string{},
	}
}

func (h *Handlers) resolveOwner(chatID string, threadID int, text string) string {
	owner := SanitizeChatID(chatID)
	if h.cfg.ForumMode && h.cfg.ForumChatID != "" && owner == SanitizeChatID(h.cfg.ForumChatID) {
		if fo := SanitizeChatID(h.cfg.ForumOwnerChatID); fo != "" {
			owner = fo
		}
		if threadID > 0 {
			rows, _ := h.db.Query(`SELECT chat_id FROM nodes WHERE message_thread_id=? LIMIT 1`, threadID)
			if len(rows) > 0 {
				if o := SanitizeChatID(AsString(rows[0]["chat_id"])); o != "" {
					return o
				}
			}
		}
		if node := nodeFromCallback(text); node != "" {
			rows, _ := h.db.Query(`SELECT chat_id FROM nodes WHERE node_name=? LIMIT 1`, node)
			if len(rows) > 0 {
				if o := SanitizeChatID(AsString(rows[0]["chat_id"])); o != "" {
					return o
				}
			}
		}
	}
	return owner
}

func nodeFromCallback(text string) string {
	parts := strings.Split(text, ":")
	if len(parts) < 2 {
		return ""
	}
	switch parts[0] {
	case "toggle":
		if len(parts) >= 3 {
			return SanitizeNodeName(parts[2])
		}
	case "manage", "google", "trust", "run", "report", "log", "log_refresh",
		"quality", "trend", "ota_confirm", "ota_execute", "ota_one", "rename", "del":
		return SanitizeNodeName(parts[1])
	}
	return ""
}

// uiDest 决定回复发到哪里：论坛内操作回论坛话题；私聊回私聊（绝不带论坛 thread）。
func (h *Handlers) uiDest(owner, sourceChat string, threadID int) (dest string, uiThread int) {
	src := SanitizeChatID(sourceChat)
	forum := SanitizeChatID(h.cfg.ForumChatID)
	if h.cfg.ForumMode && forum != "" && src == forum {
		return forum, threadID
	}
	return SanitizeChatID(owner), 0
}

func (h *Handlers) Dispatch(chatID, text, cbID string, msgID, threadID, userMsgID int, replyTo string) {
	owner := h.resolveOwner(chatID, threadID, text)
	if owner == "" {
		return
	}
	dest, uiThread := h.uiDest(owner, chatID, threadID)
	if cbID != "" {
		h.tg.AnswerCallback(cbID, "")
	}

	// Nginx wizard state
	if st := h.nginx[owner]; st != "" {
		h.handleNginxWizard(owner, text, st, dest, uiThread)
		return
	}
	if st := h.forum[owner]; st == "bind" {
		h.handleForumBindReply(owner, text, dest, uiThread)
		return
	}

	if strings.HasPrefix(text, "#REGISTER#") {
		h.handleRegister(owner, text, dest, uiThread)
		return
	}
	// Intentionally ignore #WSS_LOOKUP# and do not preview/resend WSS URLs.
	if strings.HasPrefix(text, "#WSS_LOOKUP#") {
		if userMsgID > 0 {
			h.tg.DeleteMessage(chatID, userMsgID, threadID)
		}
		return
	}
	if strings.HasPrefix(text, "svq|") {
		h.handleSVQ(owner, text, dest, uiThread)
		return
	}

	cmd := normalizeCmd(text)
	switch {
	case cmd == "/start" || cmd == "/menu" || text == "main_menu":
		h.sendMainMenu(dest, msgID, uiThread)
	case strings.HasPrefix(cmd, "/quality"):
		h.cmdQuality(owner, dest, cmd, uiThread)
	case strings.HasPrefix(cmd, "/trend"):
		h.cmdTrend(owner, dest, cmd, uiThread)
	case cmd == "/nginx" || text == "nginx_deploy":
		h.startNginxWizard(owner, dest, uiThread)
	case text == "list_nodes":
		h.sendRegionMenu(owner, dest, msgID, uiThread)
	case strings.HasPrefix(text, "region:"):
		h.sendRegionNodes(owner, dest, strings.TrimPrefix(text, "region:"), msgID, uiThread)
	case strings.HasPrefix(text, "manage:"):
		h.sendNodeConsole(owner, dest, strings.TrimPrefix(text, "manage:"), msgID, uiThread)
	case strings.HasPrefix(text, "google:") || strings.HasPrefix(text, "trust:") ||
		strings.HasPrefix(text, "run:") || strings.HasPrefix(text, "report:") ||
		strings.HasPrefix(text, "log:") || strings.HasPrefix(text, "quality:"):
		parts := strings.SplitN(text, ":", 2)
		if len(parts) == 2 {
			h.agentAction(owner, dest, parts[0], parts[1], msgID, uiThread)
		}
	case strings.HasPrefix(text, "log_refresh:"):
		node := strings.TrimPrefix(text, "log_refresh:")
		_ = h.hub.SendCommand(owner, node, "/trigger_log", map[string]any{"msg_id": msgID}, 0)
	case strings.HasPrefix(text, "trend:"):
		h.showTrend(owner, dest, strings.TrimPrefix(text, "trend:"), msgID, uiThread)
	case strings.HasPrefix(text, "toggle:"):
		h.toggleMod(owner, dest, text, msgID, uiThread)
	case strings.HasPrefix(text, "rename:"):
		h.promptRename(owner, dest, strings.TrimPrefix(text, "rename:"), uiThread)
	case strings.HasPrefix(text, "del:"):
		h.deleteNode(owner, dest, strings.TrimPrefix(text, "del:"), msgID, uiThread)
	case strings.HasPrefix(text, "ota_confirm:"):
		h.otaConfirm(owner, dest, strings.TrimPrefix(text, "ota_confirm:"), msgID, uiThread)
	case strings.HasPrefix(text, "ota_execute:") || strings.HasPrefix(text, "ota_one:"):
		node := strings.TrimPrefix(strings.TrimPrefix(text, "ota_execute:"), "ota_one:")
		resp := h.hub.SendCommand(owner, node, "/trigger_ota", nil, 0)
		_, _ = h.tg.SendMessage(dest, formatAgentResp(resp, "OTA"), true, uiThread)
	case text == "all_ota_confirm":
		h.allOTAConfirm(dest, msgID, uiThread)
	case text == "all_ota_execute":
		h.broadcastOTA(owner, dest, uiThread)
	case text == "master_ota_confirm":
		h.masterOTAConfirm(dest, msgID, uiThread)
	case text == "master_ota_execute":
		h.masterOTAExecute(dest, uiThread)
	case text == "all_run":
		h.broadcast(owner, dest, "/trigger_run", uiThread)
	case text == "all_reports":
		h.broadcast(owner, dest, "/trigger_report", uiThread)
	case text == "fleet_summary":
		h.fleetSummary(owner, dest, uiThread)
	case text == "nginx_menu":
		h.startNginxWizard(owner, dest, uiThread)
	case text == "forum_bind_prompt":
		h.startForumBind(owner, dest, uiThread)
	case strings.HasPrefix(text, "forum_bind:"):
		h.applyForumBind(owner, dest, strings.TrimPrefix(text, "forum_bind:"), uiThread)
	case text == "forum_topics_rebuild":
		h.forumTopicsRebuild(owner, dest, uiThread)
	case text == "ignore":
		return
	default:
		if replyTo != "" && strings.Contains(replyTo, "请回复本消息以重命名节点") {
			h.handleRenameReply(owner, dest, text, replyTo, uiThread)
			return
		}
		if replyTo != "" && strings.Contains(replyTo, "请回复论坛群组 Chat ID") {
			h.handleForumBindReply(owner, text, dest, uiThread)
			return
		}
		if strings.HasPrefix(text, "/") {
			_, _ = h.tg.SendMessage(dest, "未知指令，发送 /start 打开菜单。", false, uiThread)
		}
	}
}

func normalizeCmd(text string) string {
	t := strings.TrimSpace(text)
	if !strings.HasPrefix(t, "/") {
		return t
	}
	parts := strings.Fields(t)
	if len(parts) == 0 {
		return t
	}
	cmd := parts[0]
	if i := strings.Index(cmd, "@"); i > 0 {
		cmd = cmd[:i]
	}
	if len(parts) > 1 {
		return cmd + " " + strings.Join(parts[1:], " ")
	}
	return cmd
}

func (h *Handlers) sendMainMenu(dest string, msgID, threadID int) {
	mode := h.cfg.TGMode
	base := h.cfg.PublicBaseURL
	body := fmt.Sprintf("*IP-Sentinel 控制端* v%s\n模式: `%s`\n", h.cfg.MasterVersion, mode)
	if base != "" {
		body += fmt.Sprintf("公网: `%s`\n", base)
	} else {
		body += "公网: _未配置_（可用 /nginx 部署反代后自动启用 webhook）\n"
	}
	body += "\n请选择操作："
	kb := [][]map[string]string{
		{{"text": "节点列表", "callback_data": "list_nodes"}},
		{{"text": "全部节点维护", "callback_data": "all_run"}, {"text": "全部节点报告", "callback_data": "all_reports"}},
		{{"text": "节点状态摘要", "callback_data": "fleet_summary"}, {"text": "批量升级节点", "callback_data": "all_ota_confirm"}},
		{{"text": "部署 Nginx 反代", "callback_data": "nginx_menu"}},
	}
	if !h.cfg.OfficialGateway && h.cfg.EnableMasterOTA {
		kb = append(kb, []map[string]string{{"text": "升级控制端", "callback_data": "master_ota_confirm"}})
	}
	if !h.cfg.ForumMode {
		kb = append(kb, []map[string]string{{"text": "绑定论坛群组", "callback_data": "forum_bind_prompt"}})
	} else {
		kb = append(kb, []map[string]string{{"text": "重建论坛话题", "callback_data": "forum_topics_rebuild"}})
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) sendRegionMenu(owner, dest string, msgID, threadID int) {
	rows, _ := h.db.Query(`SELECT DISTINCT region FROM nodes WHERE chat_id=? ORDER BY region`, owner)
	if len(rows) == 0 {
		_, _ = h.tg.SendMessage(dest, "暂无节点。请在 Agent 安装后发送 `#REGISTER#|...` 注册。", true, threadID)
		return
	}
	var kb [][]map[string]string
	for _, r := range rows {
		reg := AsString(r["region"])
		kb = append(kb, []map[string]string{{"text": reg, "callback_data": "region:" + reg}})
	}
	kb = append(kb, []map[string]string{{"text": "返回", "callback_data": "main_menu"}})
	body := "*按区域查看节点*\n请选择区域："
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) sendRegionNodes(owner, dest, region string, msgID, threadID int) {
	rows, _ := h.db.Query(`SELECT node_name, node_alias, last_seen FROM nodes WHERE chat_id=? AND region=? ORDER BY node_name`, owner, region)
	var kb [][]map[string]string
	for _, r := range rows {
		node := AsString(r["node_name"])
		alias := AsString(r["node_alias"])
		if alias == "" {
			alias = node
		}
		online := "🔴"
		if h.hub.IsOnline(owner, node) {
			online = "🟢"
		}
		kb = append(kb, []map[string]string{{"text": online + " " + alias, "callback_data": "manage:" + node}})
	}
	kb = append(kb, []map[string]string{{"text": "区域列表", "callback_data": "list_nodes"}})
	body := fmt.Sprintf("区域 `%s`", region)
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) sendNodeConsole(owner, dest, node string, msgID, threadID int) {
	node = SanitizeNodeName(node)
	rows, _ := h.db.Query(`SELECT * FROM nodes WHERE chat_id=? AND node_name=?`, owner, node)
	if len(rows) == 0 {
		_, _ = h.tg.SendMessage(dest, "节点不存在", false, threadID)
		return
	}
	r := rows[0]
	alias := AsString(r["node_alias"])
	if alias == "" {
		alias = node
	}
	status := "离线"
	if h.hub.IsOnline(owner, node) {
		status = "在线"
	}
	body := fmt.Sprintf("*%s*\n节点: `%s`\nIP: `%s`\n状态: *%s*\n上次: `%s`",
		alias, node, AsString(r["agent_ip"]), status, AsString(r["last_seen"]))
	gOn := AsString(r["enable_google"]) != "false"
	tOn := AsString(r["enable_trust"]) != "false"
	gLabel, tLabel := "Google 模块：关", "信任站点：关"
	if gOn {
		gLabel = "Google 模块：开"
	}
	if tOn {
		tLabel = "信任站点：开"
	}
	kb := [][]map[string]string{
		{{"text": "运行维护", "callback_data": "run:" + node}, {"text": "IP 质量检测", "callback_data": "quality:" + node}},
		{{"text": "Google 定位", "callback_data": "google:" + node}, {"text": "信任站点", "callback_data": "trust:" + node}},
		{{"text": "生成报告", "callback_data": "report:" + node}, {"text": "质量趋势", "callback_data": "trend:" + node}},
		{{"text": "查看日志", "callback_data": "log:" + node}, {"text": "刷新日志", "callback_data": "log_refresh:" + node}},
		{
			{"text": gLabel, "callback_data": fmt.Sprintf("toggle:google:%s:%v", node, !gOn)},
			{"text": tLabel, "callback_data": fmt.Sprintf("toggle:trust:%s:%v", node, !tOn)},
		},
		{{"text": "重命名", "callback_data": "rename:" + node}, {"text": "远程升级", "callback_data": "ota_confirm:" + node}},
		{{"text": "删除注册", "callback_data": "del:" + node}},
		{{"text": "返回", "callback_data": "list_nodes"}},
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) agentAction(owner, dest, action, node string, msgID, threadID int) {
	node = SanitizeNodeName(node)
	path := "/trigger_" + action
	resp := h.hub.SendCommand(owner, node, path, nil, 0)
	_, _ = h.tg.SendMessage(dest, formatAgentResp(resp, action), true, threadID)
	h.sendNodeConsole(owner, dest, node, msgID, threadID)
}

func actionLabel(action string) string {
	switch action {
	case "run":
		return "运行维护"
	case "google":
		return "Google 定位"
	case "trust":
		return "信任站点"
	case "report":
		return "生成报告"
	case "log":
		return "查看日志"
	case "quality":
		return "IP 质量检测"
	case "OTA", "ota":
		return "远程升级"
	default:
		return action
	}
}

func formatAgentResp(resp, action string) string {
	if resp == "FAILED" {
		return "WSS 未连接或指令超时"
	}
	return fmt.Sprintf("`%s` 已受理\n%s", actionLabel(action), resp)
}

func (h *Handlers) handleRegister(owner, text, dest string, threadID int) {
	// #REGISTER#|node|ip|region|alias|ota
	parts := strings.Split(text, "|")
	if len(parts) < 3 {
		_, _ = h.tg.SendMessage(dest, "注册格式无效", false, threadID)
		return
	}
	node := SanitizeNodeName(parts[1])
	ip := SanitizeAgentIP(parts[2])
	region := "UNKNOWN"
	alias := node
	ota := "false"
	if len(parts) > 3 {
		region = SanitizeRegion(parts[3])
	}
	if len(parts) > 4 {
		alias = SanitizeAlias(parts[4], 30)
	}
	if len(parts) > 5 {
		ota = strings.ToLower(parts[5])
	}
	if node == "" {
		_, _ = h.tg.SendMessage(dest, "节点名无效", false, threadID)
		return
	}
	if ip != "" && IsSSRFIP(ip) {
		_, _ = h.tg.SendMessage(dest, "拒绝注册：IP 不可用（私网/环回）", false, threadID)
		return
	}
	_ = h.db.UpsertNode(owner, node, ip, region, alias, ota)
	// No WSS URL preview / private resend — agents use MASTER_BASE_URL from install.
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("✅ *已注册* (v%s)\n节点 `%s` 已加入列表。", h.cfg.MasterVersion, alias), true, threadID)
	h.sendRegionMenu(owner, dest, 0, threadID)
}

func (h *Handlers) handleSVQ(owner, text, dest string, threadID int) {
	// svq|node|score|goog|nf|gpt
	parts := strings.Split(text, "|")
	if len(parts) < 3 {
		return
	}
	node := SanitizeNodeName(parts[1])
	score := SanitizeScore(parts[2])
	goog, nf, gpt := "Unknown", "Unknown", "Unknown"
	if len(parts) > 3 {
		goog = SanitizeStatusField(parts[3])
	}
	if len(parts) > 4 {
		nf = SanitizeStatusField(parts[4])
	}
	if len(parts) > 5 {
		gpt = SanitizeStatusField(parts[5])
	}
	n, _ := strconv.Atoi(score)
	_ = h.db.InsertTrend(node, n, goog, nf, gpt)
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("📈 趋势已记录 `%s` score=%s", node, score), true, threadID)
}

func (h *Handlers) cmdQuality(owner, dest, cmd string, threadID int) {
	fields := strings.Fields(cmd)
	if len(fields) < 2 {
		_, _ = h.tg.SendMessage(dest, "用法: /quality 节点名", false, threadID)
		return
	}
	node := SanitizeNodeName(fields[1])
	resp := h.hub.SendCommand(owner, node, "/trigger_quality", nil, 0)
	_, _ = h.tg.SendMessage(dest, formatAgentResp(resp, "quality"), true, threadID)
}

func (h *Handlers) cmdTrend(owner, dest, cmd string, threadID int) {
	fields := strings.Fields(cmd)
	if len(fields) < 2 {
		_, _ = h.tg.SendMessage(dest, "用法: /trend 节点名", false, threadID)
		return
	}
	h.showTrend(owner, dest, fields[1], 0, threadID)
}

func (h *Handlers) showTrend(owner, dest, node string, msgID, threadID int) {
	node = SanitizeNodeName(node)
	rows, _ := h.db.Query(`SELECT check_time, scam_score, goog_status, nf_status, gpt_status FROM ip_trend_log WHERE node_name=? ORDER BY id DESC LIMIT 10`, node)
	if len(rows) == 0 {
		_, _ = h.tg.SendMessage(dest, "暂无趋势数据", false, threadID)
		return
	}
	var b strings.Builder
	b.WriteString(fmt.Sprintf("📈 *%s* 趋势\n", node))
	for _, r := range rows {
		b.WriteString(fmt.Sprintf("`%s` score=%v goog=%s\n", AsString(r["check_time"]), r["scam_score"], AsString(r["goog_status"])))
	}
	_, _ = h.tg.SendMessage(dest, b.String(), true, threadID)
}

func (h *Handlers) toggleMod(owner, dest, text string, msgID, threadID int) {
	// toggle:google|trust:node[:true|false]
	parts := strings.Split(text, ":")
	if len(parts) < 3 {
		return
	}
	mod, node := parts[1], SanitizeNodeName(parts[2])
	col := "enable_google"
	if mod == "trust" {
		col = "enable_trust"
	}
	state := ""
	if len(parts) >= 4 && (parts[3] == "true" || parts[3] == "false") {
		state = parts[3]
	} else {
		rows, _ := h.db.Query(`SELECT `+col+` FROM nodes WHERE chat_id=? AND node_name=?`, owner, node)
		state = "true"
		if len(rows) > 0 && AsString(rows[0][col]) == "true" {
			state = "false"
		}
	}
	_ = h.db.Exec(`UPDATE nodes SET `+col+`=? WHERE chat_id=? AND node_name=?`, state, owner, node)
	_ = h.hub.SendCommand(owner, node, "/trigger_toggle", map[string]any{"mod": mod, "state": state}, 0)
	h.sendNodeConsole(owner, dest, node, msgID, threadID)
}

func (h *Handlers) promptRename(owner, dest, node string, threadID int) {
	node = SanitizeNodeName(node)
	_, _ = h.tg.SendMessage(dest, "请回复本消息以重命名节点: `"+node+"`", true, threadID)
}

func (h *Handlers) otaConfirm(owner, dest, node string, msgID, threadID int) {
	node = SanitizeNodeName(node)
	body := fmt.Sprintf("确认对该节点执行远程升级？\n节点: `%s`", node)
	kb := [][]map[string]string{
		{{"text": "确认升级", "callback_data": "ota_execute:" + node}, {"text": "取消", "callback_data": "manage:" + node}},
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) allOTAConfirm(dest string, msgID, threadID int) {
	body := "确认对所有 *已启用远程升级* 的节点执行批量升级？"
	kb := [][]map[string]string{
		{{"text": "确认批量升级", "callback_data": "all_ota_execute"}, {"text": "取消", "callback_data": "main_menu"}},
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) broadcastOTA(owner, dest string, threadID int) {
	rows, _ := h.db.Query(`SELECT node_name FROM nodes WHERE chat_id=? AND enable_ota='true'`, owner)
	ok, fail := 0, 0
	for _, r := range rows {
		node := AsString(r["node_name"])
		if h.hub.SendCommand(owner, node, "/trigger_ota", nil, 0) == "FAILED" {
			fail++
		} else {
			ok++
		}
	}
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("批量升级完成：成功 %d / 失败 %d（仅已启用远程升级的节点）", ok, fail), false, threadID)
}

func (h *Handlers) masterOTAConfirm(dest string, msgID, threadID int) {
	if h.cfg.OfficialGateway || !h.cfg.EnableMasterOTA {
		_, _ = h.tg.SendMessage(dest, "当前控制端未开启自升级", false, threadID)
		return
	}
	body := fmt.Sprintf("确认升级本机控制端至 v%s？服务将短暂重启。", h.cfg.MasterVersion)
	kb := [][]map[string]string{
		{{"text": "确认升级控制端", "callback_data": "master_ota_execute"}, {"text": "取消", "callback_data": "main_menu"}},
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, kb, threadID)
	} else {
		_, _ = h.tg.SendUI(dest, body, kb, threadID)
	}
}

func (h *Handlers) masterOTAExecute(dest string, threadID int) {
	if h.cfg.OfficialGateway || !h.cfg.EnableMasterOTA {
		_, _ = h.tg.SendMessage(dest, "当前控制端未开启自升级", false, threadID)
		return
	}
	_, _ = h.tg.SendMessage(dest, "正在通过安装脚本拉取并安装控制端 deb 包…", false, threadID)
	go func() {
		cmd := `curl -fsSL https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/master/install_master.sh -o /tmp/ins_master.sh && SILENT_MASTER_OTA=true bash /tmp/ins_master.sh`
		_ = runShell(cmd)
	}()
}

func (h *Handlers) startForumBind(owner, dest string, threadID int) {
	h.mu.Lock()
	h.forum[owner] = "bind"
	h.mu.Unlock()
	_, _ = h.tg.SendMessage(dest, "📌 请回复论坛群组 Chat ID（例 `-100xxxxxxxxxx`）\n发送 `cancel` 取消。\n请回复论坛群组 Chat ID", false, threadID)
}

func (h *Handlers) handleForumBindReply(owner, text, dest string, threadID int) {
	text = strings.TrimSpace(text)
	if strings.EqualFold(text, "cancel") {
		h.mu.Lock()
		delete(h.forum, owner)
		h.mu.Unlock()
		_, _ = h.tg.SendMessage(dest, "已取消", false, threadID)
		return
	}
	h.applyForumBind(owner, dest, text, threadID)
}

func (h *Handlers) applyForumBind(owner, dest, rawID string, threadID int) {
	h.mu.Lock()
	delete(h.forum, owner)
	h.mu.Unlock()
	gid := SanitizeChatID(rawID)
	if gid == "" {
		_, _ = h.tg.SendMessage(dest, "Chat ID 无效", false, threadID)
		return
	}
	_ = h.cfg.SaveKeys(map[string]string{
		"FORUM_MODE":          "true",
		"FORUM_CHAT_ID":       gid,
		"FORUM_OWNER_CHAT_ID": owner,
	})
	h.cfg.ForumMode = true
	h.cfg.ForumChatID = gid
	h.cfg.ForumOwnerChatID = owner
	_, _ = h.tg.SendMessage(dest, "✅ 已绑定论坛群组 `"+gid+"`，开始重建话题…", true, threadID)
	h.forumTopicsRebuild(owner, dest, threadID)
}

func (h *Handlers) forumTopicsRebuild(owner, dest string, threadID int) {
	if !h.cfg.ForumMode || h.cfg.ForumChatID == "" {
		h.startForumBind(owner, dest, threadID)
		return
	}
	rows, _ := h.db.Query(`SELECT node_name, node_alias, region FROM nodes WHERE chat_id=?`, owner)
	ok, fail := 0, 0
	for _, r := range rows {
		node := AsString(r["node_name"])
		alias := AsString(r["node_alias"])
		if alias == "" {
			alias = node
		}
		region := AsString(r["region"])
		title := alias + " · " + region
		tid, err := h.tg.CreateForumTopic(h.cfg.ForumChatID, title)
		if err != nil || tid == 0 {
			fail++
			continue
		}
		_ = h.db.Exec(`UPDATE nodes SET message_thread_id=? WHERE chat_id=? AND node_name=?`, tid, owner, node)
		_ = h.hub.SendCommand(owner, node, "/trigger_set_topic", map[string]any{
			"chat": fmt.Sprint(h.cfg.ForumChatID), "thread": fmt.Sprint(tid),
		}, 0)
		ok++
		time.Sleep(350 * time.Millisecond)
	}
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("📌 话题重建完成：成功 %d / 失败 %d", ok, fail), false, threadID)
}

func (h *Handlers) deleteNode(owner, dest, node string, msgID, threadID int) {
	node = SanitizeNodeName(node)
	resp := h.hub.SendCommand(owner, node, "/trigger_uninstall", nil, 5*time.Second)
	_ = h.db.Exec(`DELETE FROM nodes WHERE chat_id=? AND node_name=?`, owner, node)
	note := "✅ 已从主控移除注册"
	if resp == "FAILED" {
		note += "（远端未能下发卸载）"
	}
	_, _ = h.tg.SendMessage(dest, note, false, threadID)
	h.sendRegionMenu(owner, dest, msgID, threadID)
}

func (h *Handlers) handleRenameReply(owner, dest, text, replyTo string, threadID int) {
	replyClean := strings.ReplaceAll(replyTo, "✏️ 请回复本消息以重命名节点:", "")
	replyClean = strings.ReplaceAll(replyClean, "请回复本消息以重命名节点:", "")
	target := SanitizeNodeName(strings.TrimSpace(strings.Trim(replyClean, "` ")))
	alias := SanitizeAlias(text, 30)
	if target == "" || alias == "" {
		return
	}
	_ = h.db.Exec(`UPDATE nodes SET node_alias=? WHERE chat_id=? AND node_name=?`, alias, owner, target)
	_ = h.hub.SendCommand(owner, target, "/trigger_rename", map[string]any{"b64": AliasToB64(alias)}, 0)
	_, _ = h.tg.SendMessage(dest, "✅ 已重命名为 `"+alias+"`", true, threadID)
}

func (h *Handlers) broadcast(owner, dest, path string, threadID int) {
	rows, _ := h.db.Query(`SELECT node_name FROM nodes WHERE chat_id=?`, owner)
	ok, fail := 0, 0
	for _, r := range rows {
		node := AsString(r["node_name"])
		if h.hub.SendCommand(owner, node, path, nil, 0) == "FAILED" {
			fail++
		} else {
			ok++
		}
	}
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("广播 `%s`：成功 %d / 失败 %d", path, ok, fail), true, threadID)
}

func (h *Handlers) fleetSummary(owner, dest string, threadID int) {
	h.fleetSummaryTo(owner, dest, threadID)
}

func (h *Handlers) fleetSummaryTo(owner, dest string, threadID int) {
	rows, _ := h.db.Query(`SELECT node_name, node_alias FROM nodes WHERE chat_id=?`, owner)
	total := len(rows)
	online := 0
	var lines []string
	for _, r := range rows {
		node := AsString(r["node_name"])
		alias := AsString(r["node_alias"])
		if alias == "" {
			alias = node
		}
		if h.hub.IsOnline(owner, node) {
			online++
			resp := h.hub.SendCommand(owner, node, "/trigger_report_summary", nil, 20*time.Second)
			lines = append(lines, fmt.Sprintf("• *%s*: %s", alias, truncate(resp, 120)))
		} else {
			lines = append(lines, fmt.Sprintf("• *%s*: (未连接)", alias))
		}
	}
	body := fmt.Sprintf("节点状态摘要\n共 *%d* | 在线 *%d*\n\n%s", total, online, strings.Join(lines, "\n"))
	_, _ = h.tg.SendMessage(dest, body, true, threadID)
}

func (h *Handlers) startNginxWizard(owner, dest string, threadID int) {
	h.mu.Lock()
	h.nginx[owner] = "domain"
	h.mu.Unlock()
	_, _ = h.tg.SendMessage(dest, "🌐 *一键 Nginx 反代*\n请回复域名（例: `master.example.com`）\n发送 `cancel` 取消。", true, threadID)
}

func (h *Handlers) handleNginxWizard(owner, text, st, dest string, threadID int) {
	text = strings.TrimSpace(text)
	if strings.EqualFold(text, "cancel") {
		h.mu.Lock()
		delete(h.nginx, owner)
		h.mu.Unlock()
		_, _ = h.tg.SendMessage(dest, "已取消", false, threadID)
		return
	}
	switch st {
	case "domain":
		domain := strings.ToLower(strings.TrimSpace(text))
		domain = strings.TrimPrefix(domain, "https://")
		domain = strings.TrimPrefix(domain, "http://")
		domain = strings.TrimSuffix(domain, "/")
		if domain == "" || protocol.IsIPHost(domain) || strings.Contains(domain, "/") {
			_, _ = h.tg.SendMessage(dest, "❌ 请提供域名（禁止 IP）。例: `master.example.com`", true, threadID)
			return
		}
		h.mu.Lock()
		h.nginx[owner] = "domain:" + domain
		h.mu.Unlock()
		_, _ = h.tg.SendMessage(dest, "可选：回复邮箱用于 Let's Encrypt（或发送 `-` 跳过）", false, threadID)
	default:
		domain := strings.TrimPrefix(st, "domain:")
		email := text
		if email == "-" {
			email = ""
		}
		h.mu.Lock()
		delete(h.nginx, owner)
		h.mu.Unlock()

		if protocol.IsIPHost(domain) {
			_, _ = h.tg.SendMessage(dest, "❌ 不支持 IP，请使用域名 + Let's Encrypt", false, threadID)
			return
		}

		upstream := "http://" + h.cfg.ListenAddr
		if !strings.Contains(h.cfg.ListenAddr, "://") {
			upstream = "http://" + h.cfg.ListenAddr
		}
		msg, err := DeployNginx(domain, upstream, email, true)
		if err != nil {
			_, _ = h.tg.SendMessage(dest, "❌ "+err.Error(), false, threadID)
			return
		}
		base := "https://" + domain
		if err := protocol.ValidateSecureHTTPSURL(base); err != nil {
			_, _ = h.tg.SendMessage(dest, "❌ "+err.Error(), false, threadID)
			return
		}
		secret := h.cfg.WebhookSecret
		if secret == "" {
			secret = randomHex(16)
		}
		hook := strings.TrimRight(base, "/") + "/tg"
		if err := h.tg.SetWebhook(hook, secret); err != nil {
			_, _ = h.tg.SendMessage(dest, msg+"\n⚠️ setWebhook 失败（仍保持 HTTP 轮询）: "+err.Error(), false, threadID)
			return
		}
		_ = h.cfg.SaveKeys(map[string]string{
			"PUBLIC_BASE_URL": base,
			"WEBHOOK_SECRET":  secret,
			"TG_MODE":         "webhook",
		})
		h.cfg.PublicBaseURL = base
		h.cfg.WebhookSecret = secret
		h.cfg.TGMode = "webhook"
		_, _ = h.tg.SendMessage(dest, msg+"\n🔗 Telegram 已连通 Webhook\nAgent 请配置 `MASTER_BASE_URL="+base+"`（仅 https://域名）", true, threadID)
		log.Printf("[master] webhook enabled: %s", hook)
	}
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func runShell(cmd string) error {
	c := exec.Command("bash", "-lc", cmd)
	return c.Start()
}

// EnableWebhookFromConfig activates webhook when PUBLIC_BASE_URL is a trusted https://domain.
func (h *Handlers) EnableWebhookFromConfig() error {
	base := strings.TrimRight(h.cfg.PublicBaseURL, "/")
	if base == "" {
		return fmt.Errorf("PUBLIC_BASE_URL empty")
	}
	if err := protocol.ValidateSecureHTTPSURL(base); err != nil {
		return fmt.Errorf("PUBLIC_BASE_URL 不可信: %w", err)
	}
	secret := h.cfg.WebhookSecret
	if secret == "" {
		secret = randomHex(16)
		_ = h.cfg.SaveKeys(map[string]string{"WEBHOOK_SECRET": secret})
		h.cfg.WebhookSecret = secret
	}
	hook := base + "/tg"
	if err := h.tg.SetWebhook(hook, secret); err != nil {
		return err
	}
	return h.cfg.SaveKeys(map[string]string{"TG_MODE": "webhook"})
}
