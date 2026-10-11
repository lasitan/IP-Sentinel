// Package protocol defines the Master↔Agent binary WebSocket message codec (MessagePack).
package protocol

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
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

// WSURLFromBase derives wss(s)://host/ws from MASTER_BASE_URL (https://host).
func WSURLFromBase(base string) string {
	base = trimSlash(base)
	if base == "" {
		return ""
	}
	switch {
	case hasPrefix(base, "https://"):
		return "wss://" + base[len("https://"):] + "/ws"
	case hasPrefix(base, "http://"):
		return "ws://" + base[len("http://"):] + "/ws"
	case hasPrefix(base, "wss://") || hasPrefix(base, "ws://"):
		if hasSuffix(base, "/ws") {
			return base
		}
		return trimSlash(base) + "/ws"
	default:
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
