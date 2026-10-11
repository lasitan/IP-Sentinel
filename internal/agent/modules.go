package agent

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func RunMaintenance(cfg *Config) error {
	cfg.Log("RUNNER", "INFO", "开始运行维护")
	_ = RunGoogle(cfg)
	_ = RunTrust(cfg)
	cfg.Log("RUNNER", "INFO", "运行维护完成")
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

func RunQuality(cfg *Config) error {
	cfg.Log("QUALITY", "INFO", "IP 质量检测开始")
	ip := ResolvePublicIP(cfg)
	score := 0
	goog, play, gemini := "Unknown", "Unknown", "Unknown"
	// lightweight probes
	body, code, err := HTTPGet(cfg, "https://scamalytics.com/ip/"+ip, 20*time.Second)
	if err == nil && code == 200 {
		if strings.Contains(strings.ToLower(body), "fraud score") {
			score = 10
		}
	}
	if _, c, e := HTTPGet(cfg, "https://www.google.com/", 10*time.Second); e == nil && c < 400 {
		goog = "OK"
	}
	alias := firstNonEmpty(cfg.NodeAlias, cfg.NodeName)
	text := fmt.Sprintf("<b>[%s] IP 质量检测</b>\nIP: <code>%s</code>\nScore≈%d\nGoogle: %s", alias, ip, score, goog)
	_ = TGPushHTML(cfg, text)
	// trend via global client if set
	if trendSink != nil {
		trendSink.QueueTrend(cfg.NodeName, fmt.Sprintf("%d", score), goog, play, gemini)
	}
	cfg.Log("QUALITY", "INFO", "IP 质量检测完成")
	return nil
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
