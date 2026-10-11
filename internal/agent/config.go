package agent

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/lasitan/IP-Sentinel/internal/confutil"
)

const DefaultInstallDir = "/var/lib/ip-sentinel"

func InstallDir() string {
	if d := os.Getenv("IP_SENTINEL_INSTALL_DIR"); d != "" {
		return d
	}
	return DefaultInstallDir
}

func ConfigPath() string {
	if p := os.Getenv("IP_SENTINEL_CONFIG"); p != "" {
		return p
	}
	if _, err := os.Stat("/etc/ip-sentinel/config.conf"); err == nil {
		return "/etc/ip-sentinel/config.conf"
	}
	return filepath.Join(InstallDir(), "config.conf")
}

type Config struct {
	Path           string
	InstallDir     string
	TGToken        string
	ChatID         string
	MasterBaseURL  string
	NodeName       string
	NodeAlias      string
	RegionCode     string
	EnableOTA      bool
	EnableGoogle   bool
	EnableTrust    bool
	EnableMapsGeo  bool
	PublicIP       string
	BindIP         string
	IPPref         string
	AgentVersion   string
	LogFile        string
	Raw            map[string]string
}

func LoadConfig(path string) (*Config, error) {
	if path == "" {
		path = ConfigPath()
	}
	raw, err := confutil.Load(path)
	if err != nil {
		return nil, err
	}
	if len(raw) == 0 {
		return nil, fmt.Errorf("配置文件丢失")
	}
	dir := confutil.Get(raw, "INSTALL_DIR", InstallDir())
	c := &Config{
		Path:          path,
		InstallDir:    dir,
		TGToken:       raw["TG_TOKEN"],
		ChatID:        raw["CHAT_ID"],
		MasterBaseURL: raw["MASTER_BASE_URL"],
		NodeName:      raw["NODE_NAME"],
		NodeAlias:     raw["NODE_ALIAS"],
		RegionCode:    confutil.Get(raw, "REGION_CODE", "UNKNOWN"),
		EnableOTA:     confutil.Bool(raw, "ENABLE_OTA"),
		EnableGoogle:  confutil.Get(raw, "ENABLE_GOOGLE", "true") != "false",
		EnableTrust:   confutil.Get(raw, "ENABLE_TRUST", "true") != "false",
		EnableMapsGeo: confutil.Get(raw, "ENABLE_MAPS_GEO", "true") != "false",
		PublicIP:      raw["PUBLIC_IP"],
		BindIP:        raw["BIND_IP"],
		IPPref:        confutil.Get(raw, "IP_PREF", "4"),
		AgentVersion:  confutil.Get(raw, "AGENT_VERSION", "5.0.0"),
		LogFile:       confutil.Get(raw, "LOG_FILE", "/var/log/ip-sentinel/sentinel.log"),
		Raw:           raw,
	}
	return c, nil
}

func (c *Config) SaveKeys(updates map[string]string) error {
	if err := confutil.SaveKeys(c.Path, updates); err != nil {
		return err
	}
	for k, v := range updates {
		c.Raw[k] = v
		switch k {
		case "MASTER_BASE_URL":
			c.MasterBaseURL = v
		case "NODE_ALIAS":
			c.NodeAlias = v
		case "NODE_NAME":
			c.NodeName = v
		}
	}
	return nil
}

func (c *Config) Log(module, level, msg string) {
	_ = os.MkdirAll(filepath.Dir(c.LogFile), 0o755)
	line := fmt.Sprintf("[%s] [%s] %s\n", module, level, msg)
	f, err := os.OpenFile(c.LogFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(line)
}
