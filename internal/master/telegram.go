package master

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type Telegram struct {
	token  string
	client *http.Client
	base   string
}

func NewTelegram(token string) *Telegram {
	return &Telegram{
		token: token,
		base:  "https://api.telegram.org/bot" + token,
		client: &http.Client{
			Timeout: 60 * time.Second,
		},
	}
}

func (t *Telegram) Call(method string, payload map[string]any) (map[string]any, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequest(http.MethodPost, t.base+"/"+method, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := t.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	if ok, _ := out["ok"].(bool); !ok {
		desc := fmt.Sprint(out["description"])
		low := strings.ToLower(desc)
		if payload["parse_mode"] != nil && (strings.Contains(low, "can't parse") || strings.Contains(low, "parse entities")) {
			plain := map[string]any{}
			for k, v := range payload {
				if k != "parse_mode" {
					plain[k] = v
				}
			}
			return t.Call(method, plain)
		}
		if method == "editMessageText" && strings.Contains(low, "message is not modified") {
			return map[string]any{"ok": true}, nil
		}
		// deleteMessage 常见 400：消息已删/过旧/无权限/论坛参数非法 —— 视为软成功
		if method == "deleteMessage" && (strings.Contains(low, "message to delete not found") ||
			strings.Contains(low, "message can't be deleted") ||
			strings.Contains(low, "message_id_invalid") ||
			strings.Contains(low, "bad request")) {
			return map[string]any{"ok": true}, nil
		}
		return out, fmt.Errorf("telegram %s: %s", method, desc)
	}
	return out, nil
}

func withThread(payload map[string]any, chatID string, threadID int) {
	// 仅超级群/论坛可带 message_thread_id；私聊带 thread 会被 Telegram 拒绝
	if threadID > 0 && strings.HasPrefix(strings.TrimSpace(chatID), "-") {
		payload["message_thread_id"] = threadID
	}
}

// WebhookInfo is a subset of Telegram getWebhookInfo result.
type WebhookInfo struct {
	URL                string
	PendingUpdateCount int
	LastErrorDate      int64
	LastErrorMessage   string
}

func (t *Telegram) GetWebhookInfo() (WebhookInfo, error) {
	out, err := t.Call("getWebhookInfo", map[string]any{})
	if err != nil {
		return WebhookInfo{}, err
	}
	res, _ := out["result"].(map[string]any)
	info := WebhookInfo{
		URL:                fmt.Sprint(res["url"]),
		PendingUpdateCount: intFrom(res["pending_update_count"]),
		LastErrorDate:      int64(intFrom(res["last_error_date"])),
		LastErrorMessage:   fmt.Sprint(res["last_error_message"]),
	}
	if info.URL == "<nil>" {
		info.URL = ""
	}
	if info.LastErrorMessage == "<nil>" {
		info.LastErrorMessage = ""
	}
	return info, nil
}

func (t *Telegram) GetUpdates(offset int, timeout int) ([]map[string]any, error) {
	if timeout < 1 {
		timeout = 25
	}
	q := url.Values{}
	q.Set("offset", strconv.Itoa(offset))
	q.Set("timeout", strconv.Itoa(timeout))
	q.Set("allowed_updates", `["message","callback_query","my_chat_member"]`)
	// 长轮询：HTTP 客户端超时需大于 Telegram timeout
	client := &http.Client{Timeout: time.Duration(timeout+15) * time.Second}
	resp, err := client.Get(t.base + "/getUpdates?" + q.Encode())
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out struct {
		OK          bool             `json:"ok"`
		Result      []map[string]any `json:"result"`
		Description string           `json:"description"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, err
	}
	if !out.OK {
		desc := out.Description
		if desc == "" {
			desc = "getUpdates failed"
		}
		return nil, fmt.Errorf("%s", desc)
	}
	return out.Result, nil
}

func (t *Telegram) SetWebhook(hookURL, secret string) error {
	payload := map[string]any{
		"url":                  hookURL,
		"drop_pending_updates": false,
		"allowed_updates":      []string{"message", "callback_query", "my_chat_member"},
	}
	if secret != "" {
		payload["secret_token"] = secret
	}
	_, err := t.Call("setWebhook", payload)
	return err
}

func (t *Telegram) DeleteWebhook() error {
	_, err := t.Call("deleteWebhook", map[string]any{"drop_pending_updates": false})
	return err
}

func (t *Telegram) GetMeUsername() string {
	out, err := t.Call("getMe", map[string]any{})
	if err != nil {
		return ""
	}
	res, _ := out["result"].(map[string]any)
	u, _ := res["username"].(string)
	return u
}

func (t *Telegram) SendMessage(chatID, text string, markdown bool, threadID int) (int, error) {
	payload := map[string]any{"chat_id": chatID, "text": text}
	if markdown {
		payload["parse_mode"] = "Markdown"
	}
	withThread(payload, chatID, threadID)
	out, err := t.Call("sendMessage", payload)
	if err != nil {
		log.Printf("[master] sendMessage chat=%s err=%v", chatID, err)
		return 0, err
	}
	res, _ := out["result"].(map[string]any)
	id, _ := res["message_id"].(float64)
	return int(id), nil
}

func (t *Telegram) SendUI(chatID, text string, keyboard [][]map[string]string, threadID int) (int, error) {
	payload := map[string]any{
		"chat_id":      chatID,
		"text":         text,
		"parse_mode":   "Markdown",
		"reply_markup": map[string]any{"inline_keyboard": keyboard},
	}
	withThread(payload, chatID, threadID)
	out, err := t.Call("sendMessage", payload)
	if err != nil {
		log.Printf("[master] sendUI chat=%s err=%v", chatID, err)
		return 0, err
	}
	res, _ := out["result"].(map[string]any)
	id, _ := res["message_id"].(float64)
	return int(id), nil
}

func (t *Telegram) AnswerCallback(cbID, text string) {
	_, _ = t.Call("answerCallbackQuery", map[string]any{"callback_query_id": cbID, "text": text})
}

func (t *Telegram) DeleteMessage(chatID string, messageID, threadID int) {
	if messageID <= 0 || chatID == "" {
		return
	}
	// Bot API deleteMessage 仅接受 chat_id + message_id；带 message_thread_id 会 400
	_, _ = t.Call("deleteMessage", map[string]any{"chat_id": chatID, "message_id": messageID})
}

func (t *Telegram) ReplaceUI(chatID string, oldMsgID int, text string, keyboard [][]map[string]string, threadID int) (int, error) {
	newID, err := t.SendUI(chatID, text, keyboard, threadID)
	if err != nil {
		return 0, err
	}
	if oldMsgID > 0 {
		t.DeleteMessage(chatID, oldMsgID, threadID)
	}
	return newID, nil
}

func (t *Telegram) CreateForumTopic(chatID, name string) (int, error) {
	out, err := t.Call("createForumTopic", map[string]any{"chat_id": chatID, "name": truncate(name, 128)})
	if err != nil {
		return 0, err
	}
	res, _ := out["result"].(map[string]any)
	id, _ := res["message_thread_id"].(float64)
	return int(id), nil
}

func (t *Telegram) SyncBotCommands() {
	cmds := []map[string]string{
		{"command": "start", "description": "打开主菜单"},
		{"command": "menu", "description": "打开主菜单"},
		{"command": "quality", "description": "IP 质量检测"},
		{"command": "trend", "description": "IP 污染趋势"},
		{"command": "nginx", "description": "一键部署 Nginx 反代"},
	}
	_, _ = t.Call("setMyCommands", map[string]any{"commands": cmds})
	_, _ = t.Call("setMyCommands", map[string]any{"commands": cmds, "scope": map[string]string{"type": "all_group_chats"}})
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// ExtractUpdate parses chat/text/callback fields from a Telegram update.
func ExtractUpdate(update map[string]any) (chatID, text, cbID string, msgID, threadID, userMsgID int, replyTo string) {
	msg, _ := update["message"].(map[string]any)
	cb, _ := update["callback_query"].(map[string]any)
	cbMsg, _ := cb["message"].(map[string]any)
	chat := map[string]any{}
	if msg != nil {
		chat, _ = msg["chat"].(map[string]any)
		text, _ = msg["text"].(string)
		userMsgID = intFrom(msg["message_id"])
		threadID = intFrom(msg["message_thread_id"])
		if rt, ok := msg["reply_to_message"].(map[string]any); ok {
			replyTo, _ = rt["text"].(string)
		}
	}
	if cb != nil {
		cbID, _ = cb["id"].(string)
		if d, ok := cb["data"].(string); ok && d != "" {
			text = d
		}
		if cbMsg != nil {
			chat, _ = cbMsg["chat"].(map[string]any)
			msgID = intFrom(cbMsg["message_id"])
			if threadID == 0 {
				threadID = intFrom(cbMsg["message_thread_id"])
			}
		}
	}
	if chat != nil {
		chatID = fmt.Sprint(chat["id"])
	}
	text = strings.TrimSpace(text)
	return
}

func intFrom(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case int64:
		return int(t)
	case json.Number:
		i, _ := t.Int64()
		return int(i)
	default:
		return 0
	}
}
