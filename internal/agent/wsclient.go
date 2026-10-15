package agent

import (
	"crypto/tls"
	"fmt"
	"math/rand"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/lasitan/IP-Sentinel/internal/protocol"
)

type trendItem struct {
	node, score, goog, play, gemini string
}

// WSClient connects to Master /ws using MASTER_BASE_URL (binary MessagePack).
type WSClient struct {
	cfg    *Config
	stop   chan struct{}
	mu     sync.Mutex
	trends []trendItem
}

func NewWSClient(cfg *Config) *WSClient {
	return &WSClient{cfg: cfg, stop: make(chan struct{})}
}

func (c *WSClient) Stop() {
	select {
	case <-c.stop:
	default:
		close(c.stop)
	}
}

func (c *WSClient) QueueTrend(node, score, goog, play, gemini string) {
	c.mu.Lock()
	c.trends = append(c.trends, trendItem{node, score, goog, play, gemini})
	c.mu.Unlock()
}

func (c *WSClient) Run() {
	backoff := time.Second
	for {
		select {
		case <-c.stop:
			return
		default:
		}
		cfg, err := LoadConfig(c.cfg.Path)
		if err != nil {
			time.Sleep(5 * time.Second)
			continue
		}
		c.cfg = cfg
		if cfg.ChatID == "" || cfg.MasterBaseURL == "" {
			cfg.Log("WS", "WARN", "缺少 CHAT_ID 或 MASTER_BASE_URL")
			time.Sleep(30 * time.Second)
			continue
		}
		url := protocol.WSURLFromBase(cfg.MasterBaseURL)
		if url == "" {
			cfg.Log("WS", "WARN", "MASTER_BASE_URL 无效：需 https://域名（禁止 IP / http / 自签场景）")
			time.Sleep(30 * time.Second)
			continue
		}
		started := time.Now()
		err = c.session(url)
		if err != nil {
			cfg.Log("WS", "WARN", fmt.Sprintf("会话结束: %v", err))
		}
		if err == nil || time.Since(started) > time.Minute {
			backoff = time.Second
		}
		jitter := time.Duration(rand.Int63n(int64(backoff/2) + 1))
		wait := backoff + jitter
		if wait > 30*time.Second {
			wait = 30 * time.Second
		}
		select {
		case <-c.stop:
			return
		case <-time.After(wait):
		}
		if err != nil {
			backoff *= 2
			if backoff > 30*time.Second {
				backoff = 30 * time.Second
			}
		}
	}
}

func (c *WSClient) session(url string) error {
	cfg := c.cfg
	if err := protocol.ValidateSecureWSSURL(url); err != nil {
		return fmt.Errorf("拒绝非可信 WSS: %w", err)
	}
	dialer := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
		// 系统 CA 校验；禁止自签 / InsecureSkipVerify
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12},
		NetDial: func(network, addr string) (net.Conn, error) {
			d := net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}
			return d.Dial(network, addr)
		},
	}
	header := http.Header{}
	ws, _, err := dialer.Dial(url, header)
	if err != nil {
		return err
	}
	defer ws.Close()
	ws.SetReadLimit(protocol.MaxMessageSize)
	w := &sessionWriter{ws: ws}

	node := cfg.NodeName
	chatID := cfg.ChatID
	ts := time.Now().Unix()
	sign := protocol.Sign(chatID, fmt.Sprintf("auth:%s:%s:%d", chatID, node, ts))
	auth := protocol.Envelope{
		"op": protocol.OpAuth, "chat_id": chatID, "node": node, "ts": ts, "sign": sign,
		"region": cfg.RegionCode, "ip": cfg.PublicIP, "alias": firstNonEmpty(cfg.NodeAlias, node),
		"ota": boolStr(cfg.EnableOTA), "google": boolStr(cfg.EnableGoogle), "trust": boolStr(cfg.EnableTrust),
	}
	if err := w.write(auth); err != nil {
		return err
	}
	_ = ws.SetReadDeadline(time.Now().Add(protocol.AuthTimeout))
	_, data, err := ws.ReadMessage()
	if err != nil {
		return err
	}
	msg, err := protocol.Decode(data)
	if err != nil || msg.Op() != protocol.OpAuthOK {
		return fmt.Errorf("auth failed: %v", msg.String("error"))
	}
	cfg.Log("WS", "INFO", "已连接 Master "+url)

	var lastPong atomic.Int64
	touch := func() {
		lastPong.Store(time.Now().UnixNano())
		_ = ws.SetReadDeadline(time.Now().Add(protocol.WSDeadAfter))
	}
	touch()
	// Pong/Ping handlers run inside ReadMessage (reader goroutine).
	ws.SetPongHandler(func(string) error { touch(); return nil })
	ws.SetPingHandler(func(appData string) error {
		touch()
		return w.control(websocket.PongMessage, []byte(appData))
	})

	// gorilla: a read error (incl. timeout) is permanent, so one reader with a long deadline.
	msgCh := make(chan protocol.Envelope, 16)
	readErr := make(chan error, 1)
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			mt, data, err := ws.ReadMessage()
			if err != nil {
				readErr <- err
				return
			}
			touch()
			if mt != websocket.BinaryMessage && mt != websocket.TextMessage {
				continue
			}
			m, err := protocol.Decode(data)
			if err != nil {
				continue
			}
			select {
			case msgCh <- m:
			case <-done:
				return
			}
		}
	}()

	pingT := time.NewTicker(protocol.AppPingEvery)
	defer pingT.Stop()
	trendT := time.NewTicker(time.Second)
	defer trendT.Stop()

	for {
		select {
		case <-c.stop:
			_ = w.control(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""))
			return nil
		case err := <-readErr:
			return err
		case <-pingT.C:
			if time.Since(time.Unix(0, lastPong.Load())) > protocol.AppPongWait {
				return fmt.Errorf("pong timeout")
			}
			if err := w.write(protocol.Envelope{"op": protocol.OpPing}); err != nil {
				return err
			}
			if err := w.control(websocket.PingMessage, []byte("p")); err != nil {
				return err
			}
		case <-trendT.C:
			c.flushTrend(w)
		case m := <-msgCh:
			if m.Op() != protocol.OpCmd {
				continue
			}
			reqID := m.String("id")
			path := m.String("path")
			params := m.Map("params")
			go func() {
				status, body := ExecuteCommand(cfg, path, params)
				_ = w.write(protocol.Envelope{
					"op": protocol.OpCmdResult, "id": reqID, "status": status, "body": body,
				})
			}()
		}
	}
}

// sessionWriter serializes writes; gorilla/websocket allows only one concurrent writer.
type sessionWriter struct {
	mu sync.Mutex
	ws *websocket.Conn
}

func (w *sessionWriter) write(msg protocol.Envelope) error {
	raw, err := protocol.Encode(msg)
	if err != nil {
		return err
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	_ = w.ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return w.ws.WriteMessage(websocket.BinaryMessage, raw)
}

func (w *sessionWriter) control(mt int, data []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.ws.WriteControl(mt, data, time.Now().Add(5*time.Second))
}

func (c *WSClient) flushTrend(w *sessionWriter) {
	c.mu.Lock()
	batch := c.trends
	c.trends = nil
	c.mu.Unlock()
	for _, t := range batch {
		_ = w.write(protocol.Envelope{
			"op": protocol.OpTrendSave, "node": t.node, "score": t.score,
			"goog": t.goog, "play": t.play, "gemini": t.gemini,
		})
	}
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func boolStr(v bool) string {
	if v {
		return "true"
	}
	return "false"
}
