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

type pendingReg struct {
	owner  string
	node   string
	ip     string
	region string
	alias  string
	ota    string
}

type Handlers struct {
	cfg    *Config
	db     *DB
	tg     *Telegram
	hub    *Hub
	mu     sync.Mutex
	rename map[int]string    // msgID -> node
	nginx  map[string]string // chatID -> awaiting domain|email
	forum  map[string]string // chatID -> awaiting forum_bind
	reg    map[string]pendingReg
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
		reg:    map[string]pendingReg{},
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
		if userMsgID > 0 {
			h.tg.DeleteMessage(chatID, userMsgID, threadID)
		}
		h.handleRegisterPrompt(owner, text, dest, uiThread)
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
	case strings.HasPrefix(text, "reg_ok:"):
		h.confirmRegister(owner, dest, strings.TrimPrefix(text, "reg_ok:"), msgID, uiThread)
	case strings.HasPrefix(text, "reg_no:"):
		h.rejectRegister(owner, dest, strings.TrimPrefix(text, "reg_no:"), msgID, uiThread)
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
	case text == "all_quality":
		h.startUnlockSweep(owner, dest, uiThread)
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
		{{"text": "全部节点解锁检测", "callback_data": "all_quality"}},
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

func parseRegisterPayload(text string) (node, ip, region, alias, ota string, err error) {
	// #REGISTER#|node|ip|region|alias|ota
	parts := strings.Split(text, "|")
	if len(parts) < 3 {
		return "", "", "", "", "", fmt.Errorf("注册格式无效")
	}
	node = SanitizeNodeName(parts[1])
	ip = SanitizeAgentIP(parts[2])
	region = "UNKNOWN"
	alias = node
	ota = "false"
	if len(parts) > 3 {
		region = SanitizeRegion(parts[3])
	}
	if len(parts) > 4 {
		alias = SanitizeAlias(parts[4], 30)
	}
	if len(parts) > 5 {
		ota = strings.ToLower(parts[5])
		if ota != "true" && ota != "false" {
			ota = "false"
		}
	}
	if node == "" {
		return "", "", "", "", "", fmt.Errorf("节点名无效")
	}
	if ip != "" && IsSSRFIP(ip) {
		return "", "", "", "", "", fmt.Errorf("拒绝注册：IP 不可用（私网/环回）")
	}
	return node, ip, region, alias, ota, nil
}

// handleRegisterPrompt 收到注册请求后发确认按钮，不立刻入库。
func (h *Handlers) handleRegisterPrompt(owner, text, dest string, threadID int) {
	node, ip, region, alias, ota, err := parseRegisterPayload(text)
	if err != nil {
		_, _ = h.tg.SendMessage(dest, "❌ "+err.Error(), false, threadID)
		return
	}
	if err := h.promptRegister(owner, dest, node, ip, region, alias, ota, threadID); err != nil {
		log.Printf("[master] register prompt: %v", err)
		_, _ = h.tg.SendMessage(dest, "❌ 发送注册确认失败: "+err.Error(), false, threadID)
	}
}

// PromptRegisterFromAPI 供 POST /v1/register 调用：向 chat_id 发送确认按钮。
func (h *Handlers) PromptRegisterFromAPI(chatID, node, ip, region, alias, ota string) error {
	owner := SanitizeChatID(chatID)
	if owner == "" {
		return fmt.Errorf("chat_id 无效")
	}
	node = SanitizeNodeName(node)
	ip = SanitizeAgentIP(ip)
	if region == "" {
		region = "UNKNOWN"
	} else {
		region = SanitizeRegion(region)
	}
	if alias == "" {
		alias = node
	} else {
		alias = SanitizeAlias(alias, 30)
	}
	ota = strings.ToLower(ota)
	if ota != "true" && ota != "false" {
		ota = "false"
	}
	if node == "" {
		return fmt.Errorf("节点名无效")
	}
	if ip != "" && IsSSRFIP(ip) {
		return fmt.Errorf("拒绝注册：IP 不可用（私网/环回）")
	}
	return h.promptRegister(owner, owner, node, ip, region, alias, ota, 0)
}

func (h *Handlers) promptRegister(owner, dest, node, ip, region, alias, ota string, threadID int) error {
	id := randomHex(8)
	h.mu.Lock()
	h.reg[id] = pendingReg{owner: owner, node: node, ip: ip, region: region, alias: alias, ota: ota}
	h.mu.Unlock()
	body := fmt.Sprintf(
		"🆕 *节点注册请求*\n节点: `%s`\n别名: `%s`\nIP: `%s`\n地区: `%s`\nOTA: `%s`\n\n请确认是否注册到控制端：",
		node, alias, ip, region, ota,
	)
	kb := [][]map[string]string{
		{
			{"text": "✅ 确认注册", "callback_data": "reg_ok:" + id},
			{"text": "❌ 拒绝", "callback_data": "reg_no:" + id},
		},
	}
	_, err := h.tg.SendUI(dest, body, kb, threadID)
	if err != nil {
		h.mu.Lock()
		delete(h.reg, id)
		h.mu.Unlock()
		return err
	}
	log.Printf("[master] register prompt sent owner=%s node=%s", owner, node)
	return nil
}

func (h *Handlers) confirmRegister(owner, dest, id string, msgID, threadID int) {
	h.mu.Lock()
	p, ok := h.reg[id]
	if ok {
		delete(h.reg, id)
	}
	h.mu.Unlock()
	if !ok || p.owner != owner {
		_, _ = h.tg.SendMessage(dest, "注册请求已失效或不属于你，请让节点重新发送注册。", false, threadID)
		return
	}
	_ = h.db.UpsertNode(p.owner, p.node, p.ip, p.region, p.alias, p.ota)
	body := fmt.Sprintf("✅ *已注册* (v%s)\n节点 `%s` 已加入列表。", h.cfg.MasterVersion, p.alias)
	if tid, err := h.ensureNodeForumTopic(p.owner, p.node, p.alias, p.region); err != nil {
		log.Printf("[master] forum topic after register: %v", err)
		body += "\n⚠️ 论坛话题创建失败，可稍后「重建论坛话题」。"
	} else if tid > 0 {
		body += fmt.Sprintf("\n📌 已创建论坛话题 (thread `%d`)", tid)
	}
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, [][]map[string]string{
			{{"text": "节点列表", "callback_data": "list_nodes"}},
		}, threadID)
	} else {
		_, _ = h.tg.SendMessage(dest, body, true, threadID)
	}
	h.sendRegionMenu(owner, dest, 0, threadID)
}

// ensureNodeForumTopic creates a forum topic when ForumMode is on and none is bound yet.
func (h *Handlers) ensureNodeForumTopic(owner, node, alias, region string) (int, error) {
	if !h.cfg.ForumMode || h.cfg.ForumChatID == "" {
		return 0, nil
	}
	owner = SanitizeChatID(owner)
	node = SanitizeNodeName(node)
	if owner == "" || node == "" {
		return 0, fmt.Errorf("owner/node 无效")
	}
	rows, _ := h.db.Query(
		`SELECT message_thread_id FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1`,
		owner, node,
	)
	if len(rows) > 0 {
		if tid := intFrom(rows[0]["message_thread_id"]); tid > 0 {
			return tid, nil
		}
	}
	if alias == "" {
		alias = node
	}
	if region == "" {
		region = "UNKNOWN"
	}
	title := alias + " · " + region
	tid, err := h.tg.CreateForumTopic(h.cfg.ForumChatID, title)
	if err != nil || tid == 0 {
		if err == nil {
			err = fmt.Errorf("createForumTopic 返回空 thread")
		}
		return 0, err
	}
	_ = h.db.Exec(`UPDATE nodes SET message_thread_id=? WHERE chat_id=? AND node_name=?`, tid, owner, node)
	_ = h.db.Exec(`INSERT OR REPLACE INTO forum_topics(forum_chat_id, thread_id, owner, node_name, title) VALUES(?,?,?,?,?)`,
		h.cfg.ForumChatID, tid, owner, node, title)
	_ = h.hub.SendCommand(owner, node, "/trigger_set_topic", map[string]any{
		"chat": fmt.Sprint(h.cfg.ForumChatID), "thread": fmt.Sprint(tid),
	}, 0)
	log.Printf("[master] forum topic created node=%s thread=%d", node, tid)
	return tid, nil
}

func (h *Handlers) rejectRegister(owner, dest, id string, msgID, threadID int) {
	h.mu.Lock()
	p, ok := h.reg[id]
	if ok {
		delete(h.reg, id)
	}
	h.mu.Unlock()
	if !ok || p.owner != owner {
		_, _ = h.tg.SendMessage(dest, "注册请求已失效。", false, threadID)
		return
	}
	body := fmt.Sprintf("已拒绝注册节点 `%s`。", p.alias)
	if msgID > 0 {
		_, _ = h.tg.ReplaceUI(dest, msgID, body, [][]map[string]string{}, threadID)
	} else {
		_, _ = h.tg.SendMessage(dest, body, true, threadID)
	}
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
	forum := h.cfg.ForumChatID
	_, _ = h.tg.SendMessage(dest, "📌 正在重建论坛话题：保留能对上节点的话题，删除其余旧话题后补建…", false, threadID)

	// Bot API cannot list topics, so only topics recorded in forum_topics (plus current node bindings) are known.
	_ = h.db.Exec(`INSERT OR IGNORE INTO forum_topics(forum_chat_id, thread_id, owner, node_name, title)
		SELECT ?, message_thread_id, chat_id, node_name, COALESCE(node_alias, node_name) FROM nodes
		WHERE chat_id=? AND message_thread_id > 0`, forum, owner)

	type nodeInfo struct{ alias, region string }
	nodes := map[string]nodeInfo{}
	bound := map[string]int{}
	nrows, _ := h.db.Query(`SELECT node_name, node_alias, region, message_thread_id FROM nodes WHERE chat_id=?`, owner)
	for _, r := range nrows {
		node := AsString(r["node_name"])
		alias := AsString(r["node_alias"])
		if alias == "" {
			alias = node
		}
		region := AsString(r["region"])
		if region == "" {
			region = "UNKNOWN"
		}
		nodes[node] = nodeInfo{alias, region}
		if tid := intFrom(r["message_thread_id"]); tid > 0 {
			bound[node] = tid
		}
	}

	kept, deleted, created, fail := 0, 0, 0, 0
	keptNode := map[string]bool{}
	trows, _ := h.db.Query(`SELECT thread_id, node_name FROM forum_topics WHERE forum_chat_id=? AND owner=?`, forum, owner)
	for _, r := range trows {
		tid := intFrom(r["thread_id"])
		node := AsString(r["node_name"])
		if tid <= 0 {
			continue
		}
		if info, ok := nodes[node]; ok && bound[node] == tid && !keptNode[node] {
			exists, err := h.tg.EditForumTopic(forum, tid, info.alias+" · "+info.region)
			if err != nil {
				// Unknown error (e.g. missing rights): keep binding rather than destroy history.
				fail++
				keptNode[node] = true
				log.Printf("[master] forum rebuild check node=%s thread=%d: %v", node, tid, err)
				continue
			}
			if exists {
				kept++
				keptNode[node] = true
				_ = h.db.Exec(`UPDATE forum_topics SET title=? WHERE forum_chat_id=? AND thread_id=?`, info.alias+" · "+info.region, forum, tid)
				_ = h.hub.SendCommand(owner, node, "/trigger_set_topic", map[string]any{
					"chat": fmt.Sprint(forum), "thread": fmt.Sprint(tid),
				}, 0)
				time.Sleep(350 * time.Millisecond)
				continue
			}
		}
		if err := h.tg.DeleteForumTopic(forum, tid); err != nil {
			fail++
			log.Printf("[master] forum rebuild delete thread=%d: %v", tid, err)
		} else {
			deleted++
		}
		_ = h.db.Exec(`DELETE FROM forum_topics WHERE forum_chat_id=? AND thread_id=?`, forum, tid)
		time.Sleep(350 * time.Millisecond)
	}

	for node, info := range nodes {
		if keptNode[node] {
			continue
		}
		_ = h.db.Exec(`UPDATE nodes SET message_thread_id=NULL WHERE chat_id=? AND node_name=?`, owner, node)
		if _, err := h.ensureNodeForumTopic(owner, node, info.alias, info.region); err != nil {
			fail++
			log.Printf("[master] forum rebuild create node=%s: %v", node, err)
		} else {
			created++
		}
		time.Sleep(350 * time.Millisecond)
	}
	_, _ = h.tg.SendMessage(dest, fmt.Sprintf("📌 话题重建完成：保留 %d / 删除 %d / 新建 %d / 失败 %d", kept, deleted, created, fail), false, threadID)
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

func (h *Handlers) startUnlockSweep(owner, dest string, threadID int) {
	where := "私聊"
	if h.generalDest(owner) != owner {
		where = "论坛 General 话题"
	}
	_, _ = h.tg.SendMessage(dest, "🎯 已向全部节点下发解锁检测：各节点结果发到各自话题，汇总稍后发到"+where+"。", false, threadID)
	go func() {
		if !h.runUnlockSweep(owner, "手动触发") {
			_, _ = h.tg.SendMessage(dest, "上一轮解锁检测仍在进行中，请稍后再试。", false, threadID)
		}
	}()
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
		_, _ = h.tg.SendMessage(dest,
			"下一步：\n"+
				"• 发送 `-` → *仅 80 + WebSocket*（不申请证书，适合 Cloudflare 回源）\n"+
				"• 或回复邮箱 → 本机申请 Let's Encrypt 并启用 443\n"+
				"发送 `cancel` 取消。",
			true, threadID)
	default:
		domain := strings.TrimPrefix(st, "domain:")
		enableTLS := !strings.EqualFold(text, "-")
		email := ""
		if enableTLS {
			email = text
		}
		h.mu.Lock()
		delete(h.nginx, owner)
		h.mu.Unlock()

		if protocol.IsIPHost(domain) {
			_, _ = h.tg.SendMessage(dest, "❌ 不支持 IP，请使用域名", false, threadID)
			return
		}

		upstream := "http://" + h.cfg.ListenAddr
		if !strings.Contains(h.cfg.ListenAddr, "://") {
			upstream = "http://" + h.cfg.ListenAddr
		}
		msg, err := DeployNginx(domain, upstream, email, enableTLS)
		if err != nil {
			_, _ = h.tg.SendMessage(dest, "❌ "+err.Error(), false, threadID)
			return
		}

		// 对外仍用 https://（Telegram webhook / Agent WSS 要求）；本机可仅 80 由 CDN 终结 TLS
		base := "https://" + domain
		if err := protocol.ValidateSecureHTTPSURL(base); err != nil {
			_, _ = h.tg.SendMessage(dest, msg+"\n⚠️ "+err.Error(), false, threadID)
			return
		}
		_ = h.cfg.SaveKeys(map[string]string{"PUBLIC_BASE_URL": base})
		h.cfg.PublicBaseURL = base

		secret := h.cfg.WebhookSecret
		if secret == "" {
			secret = randomHex(16)
		}
		hook := strings.TrimRight(base, "/") + "/tg"
		if err := h.tg.SetWebhook(hook, secret); err != nil {
			_ = h.cfg.SaveKeys(map[string]string{"TG_MODE": "polling", "WEBHOOK_SECRET": secret})
			h.cfg.TGMode = "polling"
			h.cfg.WebhookSecret = secret
			_, _ = h.tg.SendMessage(dest, msg+
				"\n⚠️ setWebhook 失败（保持 HTTP 轮询）: "+err.Error()+
				"\nAgent 可配置 `MASTER_BASE_URL="+base+"`（需公网 HTTPS/WSS 可达）", true, threadID)
			return
		}
		_ = h.cfg.SaveKeys(map[string]string{
			"WEBHOOK_SECRET": secret,
			"TG_MODE":        "webhook",
		})
		h.cfg.WebhookSecret = secret
		h.cfg.TGMode = "webhook"
		_, _ = h.tg.SendMessage(dest, msg+
			"\n🔗 Telegram Webhook: `"+hook+"`"+
			"\nAgent 请配置 `MASTER_BASE_URL="+base+"`", true, threadID)
		log.Printf("[master] webhook enabled: %s (local_tls=%v)", hook, enableTLS)
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
