package agent

import (
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"time"
)

func HTTPClient(cfg *Config, timeout time.Duration) *http.Client {
	if timeout <= 0 {
		timeout = 20 * time.Second
	}
	dialer := &net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}
	if cfg.BindIP != "" {
		local := &net.TCPAddr{IP: net.ParseIP(cfg.BindIP)}
		dialer.LocalAddr = local
	}
	return &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				if cfg.IPPref == "6" {
					network = "tcp6"
				} else if cfg.IPPref == "4" {
					network = "tcp4"
				}
				return dialer.DialContext(ctx, network, addr)
			},
		},
	}
}

func ResolvePublicIP(cfg *Config) string {
	if cfg.PublicIP != "" {
		return cfg.PublicIP
	}
	client := HTTPClient(cfg, 8*time.Second)
	for _, u := range []string{
		"https://api.ipify.org",
		"https://ifconfig.me/ip",
		"https://icanhazip.com",
	} {
		resp, err := client.Get(u)
		if err != nil {
			continue
		}
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 64))
		_ = resp.Body.Close()
		ip := strings.TrimSpace(string(b))
		if ip != "" {
			return ip
		}
	}
	return ""
}

func HTTPGet(cfg *Config, url string, timeout time.Duration) (string, int, error) {
	client := HTTPClient(cfg, timeout)
	resp, err := client.Get(url)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return string(b), resp.StatusCode, err
}
