package agent

import (
	"sync"
	"time"
)

// runnerInterval matches the original scheduler (20 minutes, first run after one full interval).
const runnerInterval = 20 * time.Minute

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

func (s *Scheduler) reload() {
	if cfg, err := LoadConfig(s.cfg.Path); err == nil {
		s.cfg = cfg
	}
}

func (s *Scheduler) loop() {
	defer s.wg.Done()
	ticker := time.NewTicker(runnerInterval)
	defer ticker.Stop()

	daily := time.NewTicker(30 * time.Second)
	defer daily.Stop()
	lastReportDay := -1

	for {
		select {
		case <-s.stop:
			return
		case <-ticker.C:
			s.reload()
			s.cfg.Log("Sched", "INFO", "调度触发: 定时巡逻")
			go func(cfg *Config) { _ = RunMaintenance(cfg, false) }(s.cfg)
		case <-daily.C:
			// Daily unlock check is driven by Master (UTC 16:00); agent only keeps the daily report.
			now := time.Now().UTC()
			if now.Hour() == 16 && now.Minute() == 0 && now.YearDay() != lastReportDay {
				lastReportDay = now.YearDay()
				s.reload()
				_ = RunReport(s.cfg, false)
			}
		}
	}
}
