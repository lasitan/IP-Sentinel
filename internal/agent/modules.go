package agent

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

var maintMu sync.Mutex

// RunMaintenance follows the original runner: random 0–179s sleep for scheduled runs,
// then a 70/30 roulette between Google 纠偏 and IP 信用净化; skip if a round is still running.
func RunMaintenance(cfg *Config, manual bool) error {
	cfg.Log("SYSTEM", "INFO", "主控 runner 已唤醒（定时或手动触发）")
	if !maintMu.TryLock() {
		cfg.Log("SYSTEM", "INFO", "上一轮巡逻任务尚未结束，本次触发自动取消。")
		return nil
	}
	defer maintMu.Unlock()

	if manual {
		cfg.Log("SYSTEM", "INFO", "Master 手动触发，跳过静默休眠，立即执行任务！")
	} else {
		jitter := rand.Intn(180)
		cfg.Log("SYSTEM", "INFO", fmt.Sprintf("进入防并发随机休眠状态: %d 秒...", jitter))
		time.Sleep(time.Duration(jitter) * time.Second)
	}

	g, t := cfg.EnableGoogle, cfg.EnableTrust
	switch {
	case g && t:
		if rand.Intn(100) < 70 {
			g, t = true, false
		} else {
			g, t = false, true
		}
	case !g && !t:
		cfg.Log("SYSTEM", "WARN", "未启用任何维护模块，跳过本轮。")
		return nil
	}
	if g {
		cfg.Log("SYSTEM", "INFO", "命中触发条件，加载并执行子模块: Google 区域纠偏")
		_ = RunGoogle(cfg)
	} else {
		cfg.Log("SYSTEM", "INFO", "命中触发条件，加载并执行子模块: IP 信用净化")
		_ = RunTrust(cfg)
	}
	cfg.Log("SYSTEM", "INFO", "本轮模块调度完成。")
	return nil
}

func RunGoogle(cfg *Config) error {
	cfg.Log("GOOGLE", "INFO", "Google 地理定位维护开始")
	lat, lng := regionCoords(cfg)
	url := fmt.Sprintf("https://www.google.com/maps/@%f,%f,12z", lat, lng)
	if err := VisitMapsWithGeo(cfg, lat, lng, url); err != nil {
		cfg.Log("GOOGLE", "WARN", err.Error())
		return err
	}
	// light search hit
	_, _, _ = HTTPGet(cfg, "https://www.google.com/search?q=weather", 15*time.Second)
	cfg.Log("GOOGLE", "INFO", "Google 地理定位维护完成")
	return nil
}

func RunTrust(cfg *Config) error {
	cfg.Log("TRUST", "INFO", "信任站点访问开始")
	urls := loadTrustURLs(cfg)
	if len(urls) == 0 {
		urls = []string{
			"https://www.cloudflare.com/",
			"https://www.microsoft.com/",
			"https://www.apple.com/",
		}
	}
	rand.Shuffle(len(urls), func(i, j int) { urls[i], urls[j] = urls[j], urls[i] })
	n := 5
	if len(urls) < n {
		n = len(urls)
	}
	for _, u := range urls[:n] {
		_, code, err := HTTPGet(cfg, u, 20*time.Second)
		if err != nil {
			cfg.Log("TRUST", "WARN", u+" "+err.Error())
		} else {
			cfg.Log("TRUST", "INFO", fmt.Sprintf("%s -> %d", u, code))
		}
		time.Sleep(time.Duration(500+rand.Intn(1500)) * time.Millisecond)
	}
	cfg.Log("TRUST", "INFO", "信任站点访问完成")
	return nil
}

// RunQuality probes unlock status, pushes the report to this node's topic and saves the trend.
func RunQuality(cfg *Config) UnlockResult {
	cfg.Log("QUALITY", "INFO", "IP 解锁检测开始")
	r := ProbeUnlock(cfg)
	if err := TGPushHTML(cfg, formatUnlockReport(cfg, r)); err != nil {
		cfg.Log("QUALITY", "WARN", "报告推送失败: "+err.Error())
	}
	if r.Error == "" && trendSink != nil {
		trendSink.QueueTrend(cfg.NodeName, "0",
			firstNonEmpty(r.YT.Region, r.YT.Status),
			firstNonEmpty(r.Play.Region, r.Play.Status),
			firstNonEmpty(r.Gemini.Region, r.Gemini.Status))
	}
	cfg.Log("QUALITY", "INFO", fmt.Sprintf("IP 解锁检测完成 ip=%s yt=%s play=%s gemini=%s",
		r.IP, r.YT.Status, r.Play.Status, r.Gemini.Status))
	return r
}

func RunReport(cfg *Config, silent bool) error {
	cfg.Log("REPORT", "INFO", "生成报告")
	sum, _ := BuildDailySummary(cfg)
	if !silent {
		_ = TGPushHTML(cfg, "<b>日维护报告</b>\n<pre>"+htmlEscape(sum)+"</pre>")
	}
	return nil
}

var trendSink *WSClient

func SetTrendSink(c *WSClient) { trendSink = c }

func loadTrustURLs(cfg *Config) []string {
	p := filepath.Join(cfg.InstallDir, "data", "trust_urls.txt")
	b, err := os.ReadFile(p)
	if err != nil {
		return nil
	}
	var out []string
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "#") {
			out = append(out, line)
		}
	}
	return out
}

func regionCoords(cfg *Config) (float64, float64) {
	// try data/regions via map.json-like simple defaults
	p := filepath.Join(cfg.InstallDir, "data", "map.json")
	b, err := os.ReadFile(p)
	if err == nil {
		var m map[string]any
		if json.Unmarshal(b, &m) == nil {
			if reg, ok := m[cfg.RegionCode].(map[string]any); ok {
				lat, _ := reg["lat"].(float64)
				lng, _ := reg["lng"].(float64)
				if lat != 0 || lng != 0 {
					return lat, lng
				}
			}
		}
	}
	return 35.68 + rand.Float64()*0.1, 139.69 + rand.Float64()*0.1
}
