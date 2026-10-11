package master

import (
	"context"
	"log"
	"time"
)

// StartFleetScheduler runs UTC 16:30 daily fleet summary for all owners.
func StartFleetScheduler(ctx context.Context, h *Handlers) {
	go func() {
		var lastDay int
		t := time.NewTicker(time.Minute)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-t.C:
				utc := now.UTC()
				if utc.Hour() == 16 && utc.Minute() == 30 && utc.YearDay() != lastDay {
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
		// Collect under owner; deliver to forum group when bound for that owner.
		dest := owner
		if h.cfg.ForumMode && h.cfg.ForumChatID != "" && SanitizeChatID(h.cfg.ForumOwnerChatID) == owner {
			dest = h.cfg.ForumChatID
		}
		h.fleetSummaryTo(owner, dest, 0)
	}
}
