package agent

import (
	"crypto/tls"
	"fmt"
	"math/rand"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/lasitan/IP-Sentinel/internal/protocol"
)

type trendItem struct {
	node, score, goog, play, gemini string
}

// WSClient connects to Master /ws using MASTER_BASE_URL (binary MessagePack).
type WSClient struct {
	cfg     *Config
	stop    chan struct{}
	mu      sync.Mutex
	trends  []trendItem
	lastPong time.Time
}

func NewWSClient(cfg *Config) *WSClient {
	return &WSClient{cfg: cfg, stop: make(chan struct{}), lastPong: time.Now()}
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
		err = c.session(url)
		if err != nil {
			cfg.Log("WS", "WARN", fmt.Sprintf("会话结束: %v", err))
		} else {
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
	dialer := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
		TLSClientConfig:  &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // master may use self-signed behind IP
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

	node := cfg.NodeName
	chatID := cfg.ChatID
	ts := time.Now().Unix()
	sign := protocol.Sign(chatID, fmt.Sprintf("auth:%s:%s:%d", chatID, node, ts))
	auth := protocol.Envelope{
		"op": protocol.OpAuth, "chat_id": chatID, "node": node, "ts": ts, "sign": sign,
		"region": cfg.RegionCode, "ip": cfg.PublicIP, "alias": firstNonEmpty(cfg.NodeAlias, node),
		"ota": boolStr(cfg.EnableOTA),
	}
	if err := writeBin(ws, auth); err != nil {
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
	c.lastPong = time.Now()

	ws.SetPongHandler(func(string) error {
		c.lastPong = time.Now()
		_ = ws.SetReadDeadline(time.Now().Add(protocol.WSPingInterval + protocol.WSPingTimeout))
		return nil
	})

	errCh := make(chan error, 1)
	go func() {
		ticker := time.NewTicker(protocol.AppPingEvery)
		defer ticker.Stop()
		for {
			select {
			case <-c.stop:
				errCh <- nil
				return
			case <-ticker.C:
				if time.Since(c.lastPong) > protocol.AppPongWait {
					errCh <- fmt.Errorf("app pong timeout")
					return
				}
				if err := writeBin(ws, protocol.Envelope{"op": protocol.OpPing}); err != nil {
					errCh <- err
					return
				}
				_ = ws.WriteControl(websocket.PingMessage, []byte("p"), time.Now().Add(5*time.Second))
			}
		}
	}()

	for {
		select {
		case err := <-errCh:
			return err
		default:
		}
		c.flushTrend(ws)
		_ = ws.SetReadDeadline(time.Now().Add(time.Second))
		mt, data, err := ws.ReadMessage()
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return err
		}
		if mt != websocket.BinaryMessage && mt != websocket.TextMessage {
			continue
		}
		msg, err := protocol.Decode(data)
		if err != nil {
			continue
		}
		switch msg.Op() {
		case protocol.OpPong:
			c.lastPong = time.Now()
		case protocol.OpCmd:
			reqID := msg.String("id")
			path := msg.String("path")
			params := msg.Map("params")
			status, body := ExecuteCommand(cfg, path, params)
			_ = writeBin(ws, protocol.Envelope{
				"op": protocol.OpCmdResult, "id": reqID, "status": status, "body": body,
			})
		}
	}
}

func (c *WSClient) flushTrend(ws *websocket.Conn) {
	c.mu.Lock()
	batch := c.trends
	c.trends = nil
	c.mu.Unlock()
	for _, t := range batch {
		_ = writeBin(ws, protocol.Envelope{
			"op": protocol.OpTrendSave, "node": t.node, "score": t.score,
			"goog": t.goog, "play": t.play, "gemini": t.gemini,
		})
	}
}

func writeBin(ws *websocket.Conn, msg protocol.Envelope) error {
	raw, err := protocol.Encode(msg)
	if err != nil {
		return err
	}
	_ = ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return ws.WriteMessage(websocket.BinaryMessage, raw)
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
