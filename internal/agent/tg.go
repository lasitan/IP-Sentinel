package agent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

func TGPushHTML(cfg *Config, text string) error {
	token := cfg.TGToken
	chat := cfg.ChatID
	if dest := cfg.Raw["TG_DEST_CHAT_ID"]; dest != "" {
		chat = dest
	}
	if token == "" || chat == "" {
		return fmt.Errorf("tg not configured")
	}
	payload := map[string]any{
		"chat_id":    chat,
		"text":       text,
		"parse_mode": "HTML",
	}
	if th := cfg.Raw["MESSAGE_THREAD_ID"]; th != "" {
		payload["message_thread_id"] = th
	}
	body, _ := json.Marshal(payload)
	url := fmt.Sprintf("https://api.telegram.org/bot%s/sendMessage", token)
	if api := cfg.Raw["TG_API_URL"]; api != "" {
		url = api
	}
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	if ok, _ := out["ok"].(bool); !ok {
		return fmt.Errorf("tg push failed: %s", string(raw))
	}
	return nil
}
