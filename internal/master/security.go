package master

import (
	"encoding/base64"
	"net"
	"regexp"
	"strings"
	"unicode"
)

var nodeRe = regexp.MustCompile(`[^a-zA-Z0-9._-]`)

func SanitizeChatID(s string) string {
	s = strings.TrimSpace(s)
	out := strings.Builder{}
	for _, r := range s {
		if unicode.IsDigit(r) || r == '-' {
			out.WriteRune(r)
		}
	}
	return out.String()
}

func SanitizeNodeName(s string) string {
	s = strings.TrimSpace(s)
	s = nodeRe.ReplaceAllString(s, "")
	if len(s) > 64 {
		s = s[:64]
	}
	return s
}

func SanitizeAlias(s string, max int) string {
	s = strings.TrimSpace(s)
	s = strings.ReplaceAll(s, "_", "-")
	if max <= 0 {
		max = 30
	}
	runes := []rune(s)
	if len(runes) > max {
		s = string(runes[:max])
	}
	return s
}

func SanitizeRegion(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "UNKNOWN"
	}
	if len(s) > 16 {
		s = s[:16]
	}
	return s
}

func SanitizeAgentIP(s string) string {
	s = strings.TrimSpace(s)
	if len(s) > 64 {
		s = s[:64]
	}
	return s
}

func SanitizeScore(s string) string {
	s = strings.TrimSpace(s)
	out := strings.Builder{}
	for _, r := range s {
		if unicode.IsDigit(r) {
			out.WriteRune(r)
		}
	}
	return out.String()
}

func SanitizeStatusField(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "Unknown"
	}
	if len(s) > 64 {
		s = s[:64]
	}
	return s
}

func AliasToB64(alias string) string {
	return base64.StdEncoding.EncodeToString([]byte(alias))
}

func IsSSRFIP(ipStr string) bool {
	ip := net.ParseIP(strings.Trim(ipStr, "[]"))
	if ip == nil {
		return true
	}
	if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsUnspecified() {
		return true
	}
	return false
}
