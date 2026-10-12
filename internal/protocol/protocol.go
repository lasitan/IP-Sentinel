// Package protocol defines the Master↔Agent binary WebSocket message codec (MessagePack).
package protocol

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"net/url"
	"strings"
	"time"

	"github.com/vmihailenco/msgpack/v5"
)

const (
	OpAuth       = "auth"
	OpAuthOK     = "auth_ok"
	OpAuthErr    = "auth_err"
	OpPing       = "ping"
	OpPong       = "pong"
	OpCmd        = "cmd"
	OpCmdResult  = "cmd_result"
	OpTrendSave  = "trend_save"

	AuthSkewSec = 120

	// Keepalive (plan defaults)
	WSPingInterval = 10 * time.Second
	WSPingTimeout  = 10 * time.Second
	AppPingEvery   = 10 * time.Second
	AppPongWait    = 15 * time.Second
	AuthTimeout    = 15 * time.Second
	CmdTimeout     = 15 * time.Second
	MaxMessageSize = 1 << 20
)

// Envelope is a MessagePack object with string keys (compatible with Workers TS).
type Envelope map[string]any

func Encode(msg Envelope) ([]byte, error) {
	return msgpack.Marshal(msg)
}

func Decode(raw []byte) (Envelope, error) {
	var msg Envelope
	if err := msgpack.Unmarshal(raw, &msg); err != nil {
		return nil, err
	}
	if msg == nil {
		return nil, fmt.Errorf("message must be object")
	}
	return msg, nil
}

func (e Envelope) Op() string {
	return AsString(e["op"])
}

func (e Envelope) String(key string) string {
	return AsString(e[key])
}

func (e Envelope) Int64(key string) int64 {
	return AsInt64(e[key])
}

func (e Envelope) Int(key string) int {
	return int(AsInt64(e[key]))
}

func (e Envelope) Map(key string) map[string]any {
	v, _ := e[key].(map[string]any)
	if v == nil {
		return map[string]any{}
	}
	return v
}

func AsString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case []byte:
		return string(t)
	case nil:
		return ""
	default:
		return fmt.Sprint(t)
	}
}

func AsInt64(v any) int64 {
	switch t := v.(type) {
	case int64:
		return t
	case int:
		return int64(t)
	case int32:
		return int64(t)
	case uint64:
		return int64(t)
	case float64:
		return int64(t)
	case float32:
		return int64(t)
	default:
		return 0
	}
}

func Sign(authKey, message string) string {
	mac := hmac.New(sha256.New, []byte(authKey))
	_, _ = mac.Write([]byte(message))
	return hex.EncodeToString(mac.Sum(nil))
}

func VerifyAuth(authKey, chatID, node string, ts int64, sign string) bool {
	now := time.Now().Unix()
	if abs64(now-ts) > AuthSkewSec {
		return false
	}
	expected := Sign(authKey, fmt.Sprintf("auth:%s:%s:%d", chatID, node, ts))
	return hmac.Equal([]byte(expected), []byte(sign))
}

func abs64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}

// IsIPHost reports whether host is a literal IPv4/IPv6 address (brackets stripped).
func IsIPHost(host string) bool {
	host = strings.TrimSpace(host)
	host = strings.TrimPrefix(host, "[")
	host = strings.TrimSuffix(host, "]")
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	return net.ParseIP(host) != nil
}

// ValidateSecureHTTPSURL requires https:// + DNS hostname (rejects IP / http / empty path hosts).
func ValidateSecureHTTPSURL(raw string) error {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return fmt.Errorf("URL 为空")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("URL 无效: %w", err)
	}
	if !strings.EqualFold(u.Scheme, "https") {
		return fmt.Errorf("仅允许 https://（当前: %s）", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("缺少主机名")
	}
	if IsIPHost(host) {
		return fmt.Errorf("不信任 IP 地址，请使用域名: %s", host)
	}
	return nil
}

// ValidateSecureWSSURL requires wss:// + DNS hostname.
func ValidateSecureWSSURL(raw string) error {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return fmt.Errorf("URL 为空")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("URL 无效: %w", err)
	}
	if !strings.EqualFold(u.Scheme, "wss") {
		return fmt.Errorf("仅允许 wss://（当前: %s）", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("缺少主机名")
	}
	if IsIPHost(host) {
		return fmt.Errorf("不信任 IP 地址，请使用域名: %s", host)
	}
	return nil
}

// WSURLFromBase derives wss://host/ws from MASTER_BASE_URL.
// Only https://domain or wss://domain are accepted; http/ws/IP are rejected (returns "").
func WSURLFromBase(base string) string {
	base = trimSlash(strings.TrimSpace(base))
	if base == "" {
		return ""
	}
	switch {
	case hasPrefix(base, "https://"):
		if err := ValidateSecureHTTPSURL(base); err != nil {
			return ""
		}
		return "wss://" + base[len("https://"):] + "/ws"
	case hasPrefix(base, "wss://"):
		out := base
		if !hasSuffix(out, "/ws") {
			out = trimSlash(out) + "/ws"
		}
		if err := ValidateSecureWSSURL(out); err != nil {
			return ""
		}
		return out
	default:
		// bare hostname → https then wss
		if strings.Contains(base, "://") {
			return ""
		}
		httpsURL := "https://" + base
		if err := ValidateSecureHTTPSURL(httpsURL); err != nil {
			return ""
		}
		return "wss://" + base + "/ws"
	}
}

func trimSlash(s string) string {
	for len(s) > 0 && s[len(s)-1] == '/' {
		s = s[:len(s)-1]
	}
	return s
}

func hasPrefix(s, p string) bool {
	return len(s) >= len(p) && s[:len(p)] == p
}

func hasSuffix(s, p string) bool {
	return len(s) >= len(p) && s[len(s)-len(p):] == p
}
