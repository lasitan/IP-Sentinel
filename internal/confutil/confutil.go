// Package confutil loads KEY=VALUE conf files used by Master and Agent.
package confutil

import (
	"bufio"
	"os"
	"strings"
	"sync"
)

var fileMu sync.Mutex

func Load(path string) (map[string]string, error) {
	out := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return out, nil
		}
		return nil, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		k, v, _ := strings.Cut(line, "=")
		k = strings.TrimSpace(k)
		v = strings.TrimSpace(v)
		v = strings.Trim(v, `"'`)
		out[k] = v
	}
	return out, sc.Err()
}

func SaveKeys(path string, updates map[string]string) error {
	fileMu.Lock()
	defer fileMu.Unlock()

	var lines []string
	if data, err := os.ReadFile(path); err == nil {
		lines = strings.Split(string(data), "\n")
		// preserve trailing structure
		if len(lines) > 0 && lines[len(lines)-1] == "" {
			lines = lines[:len(lines)-1]
		}
	}
	for key, val := range updates {
		prefix := key + "="
		line := key + "=\"" + val + "\""
		found := false
		for i, existing := range lines {
			trim := strings.TrimSpace(existing)
			if strings.HasPrefix(trim, prefix) {
				lines[i] = line
				found = true
				break
			}
		}
		if !found {
			lines = append(lines, line)
		}
	}
	body := strings.Join(lines, "\n")
	if body != "" && !strings.HasSuffix(body, "\n") {
		body += "\n"
	}
	return os.WriteFile(path, []byte(body), 0o644)
}

func Get(cfg map[string]string, key, def string) string {
	if v, ok := cfg[key]; ok && v != "" {
		return v
	}
	return def
}

func Bool(cfg map[string]string, key string) bool {
	return strings.EqualFold(cfg[key], "true") || cfg[key] == "1"
}
