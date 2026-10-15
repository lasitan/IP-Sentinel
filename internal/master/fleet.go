package master

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"
)

const (
	unlockSweepTimeout     = 120 * time.Second
	unlockSweepConcurrency = 8
)

// StartFleetScheduler runs the daily unlock sweep at UTC 16:00 (Beijing 00:00) for all owners.
func StartFleetScheduler(ctx context.Context, h *Handlers) {
	go func() {
		lastDay := -1
		t := time.NewTicker(30 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-t.C:
				utc := now.UTC()
				if utc.Hour() == 16 && utc.Minute() == 0 && utc.YearDay() != lastDay {
					lastDay = utc.YearDay()
					h.runFleetDaily()
				}
			}
		}
	}()
}

func (h *Handlers) runFleetDaily() {
	rows, err := h.db.Query(`SELECT DISTINCT chat_id FROM nodes`)
	if err != nil {
		log.Printf("[fleet] query owners: %v", err)
		return
	}
	for _, r := range rows {
		owner := SanitizeChatID(AsString(r["chat_id"]))
		if owner == "" {
			continue
		}
		h.runUnlockSweep(owner, "每日定时")
	}
}

// generalDest is the forum group General topic (thread 0) when bound for this owner, else the owner DM.
func (h *Handlers) generalDest(owner string) string {
	if h.cfg.ForumMode && h.cfg.ForumChatID != "" && SanitizeChatID(h.cfg.ForumOwnerChatID) == owner {
		return h.cfg.ForumChatID
	}
	return owner
}

type unlockMedia struct {
	Status string `json:"status"`
	Region string `json:"region"`
}

type unlockResult struct {
	Node    string      `json:"node"`
	Alias   string      `json:"alias"`
	IP      string      `json:"ip"`
	Country string      `json:"country"`
	YT      unlockMedia `json:"yt"`
	Play    unlockMedia `json:"play"`
	Gemini  unlockMedia `json:"gemini"`
	Port25  string      `json:"port25"`
	Error   string      `json:"error,omitempty"`
}

func unlockIcon(m unlockMedia) string {
	reg := m.Region
	if reg == "" {
		reg = "--"
	}
	switch m.Status {
	case "解锁":
		return "🟢" + reg
	case "送中":
		return "🟡CN"
	case "失败":
		return "🔴" + reg
	case "N/A":
		return "⚪N/A"
	default:
		return "⚪" + reg
	}
}

var (
	sweepMu      sync.Mutex
	sweepRunning = map[string]bool{}
)

// runUnlockSweep asks every node of owner to run the unlock check (each node posts to its own topic),
// then posts a summary to the General topic. Returns false if a sweep is already running.
func (h *Handlers) runUnlockSweep(owner, reason string) bool {
	sweepMu.Lock()
	if sweepRunning[owner] {
		sweepMu.Unlock()
		return false
	}
	sweepRunning[owner] = true
	sweepMu.Unlock()
	defer func() {
		sweepMu.Lock()
		delete(sweepRunning, owner)
		sweepMu.Unlock()
	}()

	rows, _ := h.db.Query(`SELECT node_name, node_alias FROM nodes WHERE chat_id=? ORDER BY region, node_name`, owner)
	lines := make([]string, len(rows))
	var okN, failN, offN int
	var cntMu sync.Mutex
	sem := make(chan struct{}, unlockSweepConcurrency)
	var wg sync.WaitGroup
	for i, r := range rows {
		node := AsString(r["node_name"])
		alias := AsString(r["node_alias"])
		if alias == "" {
			alias = node
		}
		wg.Add(1)
		go func(i int, node, alias string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			line, status := h.unlockOne(owner, node, alias)
			lines[i] = line
			cntMu.Lock()
			switch status {
			case "ok":
				okN++
			case "offline":
				offN++
			default:
				failN++
			}
			cntMu.Unlock()
		}(i, node, alias)
	}
	wg.Wait()

	now := time.Now().In(time.FixedZone("CST", 8*3600)).Format("2006-01-02 15:04")
	body := fmt.Sprintf("🎯 IP 解锁检测汇总（%s）\n⏱️ %s (UTC+8)\n共 %d | 成功 %d | 失败 %d | 离线 %d\n图例: YT / Play / Gemini\n\n%s",
		reason, now, len(rows), okN, failN, offN, strings.Join(lines, "\n"))
	if len(rows) == 0 {
		body = fmt.Sprintf("🎯 IP 解锁检测汇总（%s）\n暂无节点。", reason)
	}
	if _, err := h.tg.SendMessage(h.generalDest(owner), truncate(body, 4000), false, 0); err != nil {
		log.Printf("[fleet] unlock summary owner=%s: %v", owner, err)
	}
	return true
}

func (h *Handlers) unlockOne(owner, node, alias string) (line, status string) {
	if !h.hub.IsConnected(owner, node) {
		return fmt.Sprintf("🔴 %s：未连接", alias), "offline"
	}
	resp := h.hub.SendCommand(owner, node, "/trigger_quality_sync", nil, unlockSweepTimeout)
	var r unlockResult
	if resp == "FAILED" || json.Unmarshal([]byte(resp), &r) != nil {
		return fmt.Sprintf("⚠️ %s：检测超时或失败", alias), "fail"
	}
	if r.Error != "" {
		return fmt.Sprintf("⚠️ %s：%s", alias, r.Error), "fail"
	}
	return fmt.Sprintf("• %s｜%s｜%s / %s / %s", alias, r.IP, unlockIcon(r.YT), unlockIcon(r.Play), unlockIcon(r.Gemini)), "ok"
}
