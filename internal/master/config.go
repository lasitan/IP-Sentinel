package master

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/lasitan/IP-Sentinel/internal/confutil"
)

const (
	DefaultMasterDir = "/var/lib/ip-sentinel-master"
	DefaultListen    = "127.0.0.1:19530"
)

type Config struct {
	Path               string
	MasterDir          string
	DBFile             string
	TGToken            string
	MasterVersion      string
	OfficialGateway    bool
	EnableMasterOTA    bool
	ForumMode          bool
	ForumChatID        string
	ForumOwnerChatID   string
	IPPref             string
	PublicIP           string
	BindIP             string
	PublicBaseURL      string
	WebhookSecret      string
	TGMode             string // polling | webhook
	ListenAddr         string
	Raw                map[string]string
}

func ConfigPath() string {
	if p := os.Getenv("IP_SENTINEL_MASTER_CONFIG"); p != "" {
		return p
	}
	if _, err := os.Stat("/etc/ip-sentinel-master/master.conf"); err == nil {
		return "/etc/ip-sentinel-master/master.conf"
	}
	return filepath.Join(DefaultMasterDir, "master.conf")
}

func LoadConfig(path string) (*Config, error) {
	if path == "" {
		path = ConfigPath()
	}
	raw, err := confutil.Load(path)
	if err != nil {
		return nil, err
	}
	dir := confutil.Get(raw, "MASTER_DIR", DefaultMasterDir)
	c := &Config{
		Path:             path,
		MasterDir:        dir,
		DBFile:           confutil.Get(raw, "DB_FILE", filepath.Join(dir, "sentinel.db")),
		TGToken:          raw["TG_TOKEN"],
		MasterVersion:    confutil.Get(raw, "MASTER_VERSION", "5.0.0"),
		OfficialGateway:  confutil.Bool(raw, "IS_OFFICIAL_GATEWAY"),
		EnableMasterOTA:  confutil.Bool(raw, "ENABLE_MASTER_OTA"),
		ForumMode:        confutil.Bool(raw, "FORUM_MODE"),
		ForumChatID:      raw["FORUM_CHAT_ID"],
		ForumOwnerChatID: raw["FORUM_OWNER_CHAT_ID"],
		IPPref:           confutil.Get(raw, "IP_PREF", "4"),
		PublicIP:         raw["PUBLIC_IP"],
		BindIP:           raw["BIND_IP"],
		PublicBaseURL:    raw["PUBLIC_BASE_URL"],
		WebhookSecret:    raw["WEBHOOK_SECRET"],
		TGMode:           confutil.Get(raw, "TG_MODE", "polling"),
		ListenAddr:       confutil.Get(raw, "LISTEN_ADDR", DefaultListen),
		Raw:              raw,
	}
	return c, nil
}

func RequireConfig(path string) (*Config, error) {
	c, err := LoadConfig(path)
	if err != nil {
		return nil, err
	}
	if c.TGToken == "" {
		return nil, fmt.Errorf("master.conf 缺失或无效 (TG_TOKEN)")
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
		case "PUBLIC_BASE_URL":
			c.PublicBaseURL = v
		case "WEBHOOK_SECRET":
			c.WebhookSecret = v
		case "TG_MODE":
			c.TGMode = v
		case "LISTEN_ADDR":
			c.ListenAddr = v
		case "FORUM_MODE":
			c.ForumMode = confutil.Bool(map[string]string{"FORUM_MODE": v}, "FORUM_MODE")
		case "FORUM_CHAT_ID":
			c.ForumChatID = v
		case "FORUM_OWNER_CHAT_ID":
			c.ForumOwnerChatID = v
		}
	}
	return nil
}
