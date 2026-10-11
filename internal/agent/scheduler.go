package agent

import (
	"sync"
	"time"
)

type Scheduler struct {
	cfg  *Config
	stop chan struct{}
	wg   sync.WaitGroup
}

func StartScheduler(cfg *Config) *Scheduler {
	s := &Scheduler{cfg: cfg, stop: make(chan struct{})}
	s.wg.Add(1)
	go s.loop()
	return s
}

func (s *Scheduler) Stop() {
	select {
	case <-s.stop:
	default:
		close(s.stop)
	}
	s.wg.Wait()
}

func (s *Scheduler) loop() {
	defer s.wg.Done()
	// stagger by install hash-ish: use minute offset from node name length
	offset := time.Duration(len(s.cfg.NodeName)%20) * time.Minute
	ticker := time.NewTicker(20 * time.Minute)
	defer ticker.Stop()

	daily := time.NewTicker(time.Hour)
	defer daily.Stop()
	lastDailyDay := -1

	select {
	case <-time.After(offset):
	case <-s.stop:
		return
	}

	_ = RunMaintenance(s.cfg)

	for {
		select {
		case <-s.stop:
			return
		case <-ticker.C:
			cfg, err := LoadConfig(s.cfg.Path)
			if err == nil {
				s.cfg = cfg
			}
			_ = RunMaintenance(s.cfg)
		case <-daily.C:
			now := time.Now().UTC()
			if now.Hour() == 16 && now.Day() != lastDailyDay {
				lastDailyDay = now.Day()
				_ = RunReport(s.cfg, false)
				_ = RunQuality(s.cfg)
			}
		}
	}
}
