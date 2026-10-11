package main

import (
	"crypto/md5"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"syscall"

	"github.com/lasitan/IP-Sentinel/internal/agent"
)

func main() {
	cfg, err := agent.LoadConfig("")
	if err != nil {
		log.Fatal(err)
	}
	if cfg.TGToken == "" || cfg.ChatID == "" {
		os.Exit(0)
	}
	_ = os.MkdirAll(filepath.Join(cfg.InstallDir, "core"), 0o755)
	_ = os.MkdirAll(filepath.Join(cfg.InstallDir, "logs"), 0o755)

	if cfg.NodeName == "" {
		raw := cfg.PublicIP
		if raw == "" {
			raw = "127.0.0.1"
		}
		sum := md5.Sum([]byte(raw))
		host, _ := os.Hostname()
		re := regexp.MustCompile(`[^a-zA-Z0-9]`)
		host = re.ReplaceAllString(host, "")
		if len(host) > 10 {
			host = host[:10]
		}
		cfg.NodeName = fmt.Sprintf("%s-%s", host, hex.EncodeToString(sum[:])[:4])
		_ = cfg.SaveKeys(map[string]string{"NODE_NAME": cfg.NodeName})
	}

	ip := agent.ResolvePublicIP(cfg)
	if ip != "" {
		cfg.PublicIP = ip
		_ = cfg.SaveKeys(map[string]string{"PUBLIC_IP": ip})
		cache := filepath.Join(cfg.InstallDir, "core", ".last_ip")
		_ = os.WriteFile(cache, []byte(ip), 0o644)
		log.Printf("[Agent] IP %s", ip)
	}

	if cfg.MasterBaseURL == "" {
		log.Println("[Agent] 警告: 未配置 MASTER_BASE_URL，WSS 无法连接")
	}

	sched := agent.StartScheduler(cfg)
	ws := agent.NewWSClient(cfg)
	agent.SetTrendSink(ws)
	go ws.Run()
	log.Println("[Agent] 调度器与 WSS 客户端已启动")

	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	<-ch
	sched.Stop()
	ws.Stop()
}
