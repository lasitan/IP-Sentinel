package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// ExecuteCommand handles Master WS cmd paths (/trigger_*).
func ExecuteCommand(cfg *Config, path string, params map[string]any) (int, string) {
	switch path {
	case "/trigger_run":
		go func() { _ = RunMaintenance(cfg, true) }()
		cfg.Log("WS", "INFO", "收到指令: 立即执行维护")
		return 200, "Action Accepted"
	case "/trigger_google":
		if !cfg.EnableGoogle {
			return 403, "Google module disabled"
		}
		go func() { _ = RunGoogle(cfg) }()
		cfg.Log("WS", "INFO", "收到指令: Google 地理定位维护")
		return 200, "Action Accepted"
	case "/trigger_trust":
		if !cfg.EnableTrust {
			return 403, "Trust module disabled"
		}
		go func() { _ = RunTrust(cfg) }()
		cfg.Log("WS", "INFO", "收到指令: 信任站点访问")
		return 200, "Action Accepted"
	case "/trigger_report":
		go func() { _ = RunReport(cfg, false) }()
		cfg.Log("WS", "INFO", "收到指令: 生成报告")
		return 200, "Action Accepted"
	case "/trigger_report_summary":
		body, err := BuildDailySummary(cfg)
		if err != nil {
			return 500, err.Error()
		}
		return 200, body
	case "/trigger_quality":
		go RunQuality(cfg)
		cfg.Log("WS", "INFO", "收到指令: IP 解锁检测")
		return 200, "Action Accepted"
	case "/trigger_quality_sync":
		cfg.Log("WS", "INFO", "收到指令: IP 解锁检测（同步）")
		b, err := json.Marshal(RunQuality(cfg))
		if err != nil {
			return 500, err.Error()
		}
		return 200, string(b)
	case "/trigger_log":
		go pushLog(cfg)
		return 200, "Action Accepted"
	case "/trigger_set_topic":
		if strParam(params, "clear") == "1" {
			_ = cfg.SaveKeys(map[string]string{
				"TG_DEST_CHAT_ID": "", "MESSAGE_THREAD_ID": "", "TOPIC_BOT_MESSAGE_ID": "",
			})
			return 200, "topic cleared"
		}
		_ = cfg.SaveKeys(map[string]string{
			"TG_DEST_CHAT_ID":      strParam(params, "chat"),
			"MESSAGE_THREAD_ID":    strParam(params, "thread"),
			"TOPIC_BOT_MESSAGE_ID": strParam(params, "msg"),
		})
		return 200, "topic bound"
	case "/trigger_rename":
		b64 := strParam(params, "b64")
		raw, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			return 400, "bad b64"
		}
		alias := strings.TrimSpace(string(raw))
		_ = cfg.SaveKeys(map[string]string{"NODE_ALIAS": alias})
		return 200, "renamed"
	case "/trigger_toggle":
		mod := strParam(params, "mod")
		state := strParam(params, "state")
		key := "ENABLE_GOOGLE"
		if mod == "trust" {
			key = "ENABLE_TRUST"
		}
		_ = cfg.SaveKeys(map[string]string{key: state})
		return 200, "toggled"
	case "/trigger_ota":
		if !cfg.EnableOTA {
			return 403, "remote upgrade disabled"
		}
		go runOTA(cfg)
		return 200, "remote upgrade started"
	case "/trigger_uninstall":
		go runUninstall(cfg)
		return 200, "uninstall started"
	default:
		return 404, "unknown path"
	}
}

func strParam(params map[string]any, key string) string {
	if params == nil {
		return ""
	}
	v, _ := params[key].(string)
	if v == "" {
		v = fmt.Sprint(params[key])
		if v == "<nil>" {
			return ""
		}
	}
	return v
}

func pushLog(cfg *Config) {
	tail := TailFile(cfg.LogFile, 15)
	alias := firstNonEmpty(cfg.NodeAlias, cfg.NodeName)
	text := fmt.Sprintf("📄 <b>[%s] 实时日志 (v%s):</b>\n<pre><code>%s</code></pre>", alias, cfg.AgentVersion, htmlEscape(tail))
	_ = TGPushHTML(cfg, text)
}

func runOTA(cfg *Config) {
	script := filepath.Join(cfg.InstallDir, "core", "install.sh")
	if _, err := os.Stat(script); err != nil {
		// fallback: fetch installer then install deb
		script = "/tmp/ip-sentinel-agent-install.sh"
		_ = exec.Command("curl", "-fsSL",
			"https://raw.githubusercontent.com/lasitan/IP-Sentinel/main/core/install.sh",
			"-o", script).Run()
	}
	cmd := exec.Command("bash", script)
	cmd.Env = append(os.Environ(),
		"SILENT_AGENT_OTA=true",
		"IP_SENTINEL_INSTALL_DIR="+cfg.InstallDir,
		"IP_SENTINEL_CONFIG="+cfg.Path,
	)
	_ = cmd.Start()
}

func runUninstall(cfg *Config) {
	script := filepath.Join(cfg.InstallDir, "core", "uninstall.sh")
	cmd := exec.Command("bash", script)
	_ = cmd.Start()
}

func htmlEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;")
	return r.Replace(s)
}

func TailFile(path string, n int) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return "日志文件不存在或为空"
	}
	lines := strings.Split(string(b), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// BuildDailySummary returns a short JSON/text summary for fleet.
func BuildDailySummary(cfg *Config) (string, error) {
	m := map[string]any{
		"node":    firstNonEmpty(cfg.NodeAlias, cfg.NodeName),
		"version": cfg.AgentVersion,
		"ip":      cfg.PublicIP,
		"region":  cfg.RegionCode,
	}
	b, err := json.Marshal(m)
	return string(b), err
}
