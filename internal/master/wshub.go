package master

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/lasitan/IP-Sentinel/internal/protocol"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	CheckOrigin:     func(r *http.Request) bool { return true },
}

type pendingResult struct {
	ch chan cmdResult
}

type cmdResult struct {
	status int
	body   string
}

type connKey struct {
	owner string
	node  string
}

type agentConn struct {
	ws       *websocket.Conn
	mu       sync.Mutex // gorilla/websocket allows only one concurrent writer
	key      connKey
	done     chan struct{}
	lastSeen atomic.Int64
}

func (ac *agentConn) send(msg protocol.Envelope) error {
	raw, err := protocol.Encode(msg)
	if err != nil {
		return err
	}
	ac.mu.Lock()
	defer ac.mu.Unlock()
	_ = ac.ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return ac.ws.WriteMessage(websocket.BinaryMessage, raw)
}

func (ac *agentConn) control(mt int, data []byte) error {
	ac.mu.Lock()
	defer ac.mu.Unlock()
	return ac.ws.WriteControl(mt, data, time.Now().Add(5*time.Second))
}

// lastSeenEvery throttles DB last_seen writes per connection.
const lastSeenEvery = 30 * time.Second

// Hub manages Agent WebSocket connections on /ws (binary MessagePack).
type Hub struct {
	db      *DB
	mu      sync.Mutex
	conns   map[connKey]*agentConn
	pending map[string]*pendingResult
	// OnAuthed is invoked after successful auth (e.g. ensure forum topic).
	OnAuthed func(owner, node, ip, region, alias string)
}

func NewHub(db *DB) *Hub {
	return &Hub{
		db:      db,
		conns:   map[connKey]*agentConn{},
		pending: map[string]*pendingResult{},
	}
}

func (h *Hub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	EnableTCPKeepAlive(ws.UnderlyingConn())
	_ = ws.SetReadDeadline(time.Now().Add(protocol.AuthTimeout))
	ws.SetReadLimit(protocol.MaxMessageSize)

	_, data, err := ws.ReadMessage()
	if err != nil {
		_ = ws.Close()
		return
	}
	msg, err := protocol.Decode(data)
	if err != nil || msg.Op() != protocol.OpAuth {
		_ = h.write(ws, protocol.Envelope{"op": protocol.OpAuthErr, "error": "auth required"})
		_ = ws.Close()
		return
	}
	chatID := SanitizeChatID(msg.String("chat_id"))
	node := SanitizeNodeName(msg.String("node"))
	ts := msg.Int64("ts")
	sign := msg.String("sign")
	if chatID == "" || node == "" || !protocol.VerifyAuth(chatID, chatID, node, ts, sign) {
		_ = h.write(ws, protocol.Envelope{"op": protocol.OpAuthErr, "error": "invalid signature"})
		_ = ws.Close()
		return
	}

	key := connKey{owner: chatID, node: node}
	ac := &agentConn{ws: ws, key: key, done: make(chan struct{})}

	h.mu.Lock()
	if old := h.conns[key]; old != nil {
		_ = old.ws.Close()
	}
	h.conns[key] = ac
	h.mu.Unlock()

	region := SanitizeRegion(msg.String("region"))
	ip := SanitizeAgentIP(msg.String("ip"))
	alias := SanitizeAlias(msg.String("alias"), 30)
	if alias == "" {
		alias = node
	}
	ota := msg.String("ota")
	if ota == "" {
		ota = "false"
	}
	_ = h.db.UpsertNode(chatID, node, ip, region, alias, ota)
	if g, t := msg.String("google"), msg.String("trust"); g == "true" || g == "false" {
		if t != "true" && t != "false" {
			t = "true"
		}
		_ = h.db.Exec(`UPDATE nodes SET enable_google=?, enable_trust=? WHERE chat_id=? AND node_name=?`, g, t, chatID, node)
	}
	ac.lastSeen.Store(time.Now().UnixNano())
	_ = ac.send(protocol.Envelope{"op": protocol.OpAuthOK, "node": node})
	log.Printf("[master-ws] Agent connected: %s (%s)", node, chatID)
	if h.OnAuthed != nil {
		go h.OnAuthed(chatID, node, ip, region, alias)
	}

	alive := func() {
		_ = ws.SetReadDeadline(time.Now().Add(protocol.WSDeadAfter))
		h.touch(ac)
	}
	alive()
	ws.SetPongHandler(func(string) error { alive(); return nil })
	ws.SetPingHandler(func(appData string) error {
		alive()
		return ac.control(websocket.PongMessage, []byte(appData))
	})
	go h.pingLoop(ac)

	defer func() {
		close(ac.done)
		h.mu.Lock()
		if h.conns[key] == ac {
			delete(h.conns, key)
		}
		h.mu.Unlock()
		_ = ws.Close()
		log.Printf("[master-ws] Agent disconnected: %s (%s)", node, chatID)
	}()

	for {
		mt, data, err := ws.ReadMessage()
		if err != nil {
			return
		}
		alive()
		if mt != websocket.BinaryMessage && mt != websocket.TextMessage {
			continue
		}
		h.onMessage(ac, data)
	}
}

// touch refreshes DB last_seen at most once per lastSeenEvery.
func (h *Hub) touch(ac *agentConn) {
	now := time.Now().UnixNano()
	prev := ac.lastSeen.Load()
	if now-prev < int64(lastSeenEvery) || !ac.lastSeen.CompareAndSwap(prev, now) {
		return
	}
	_ = h.db.TouchLastSeen(ac.key.owner, ac.key.node)
}

func (h *Hub) pingLoop(ac *agentConn) {
	ticker := time.NewTicker(protocol.WSPingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ac.done:
			return
		case <-ticker.C:
			if err := ac.control(websocket.PingMessage, []byte("ping")); err != nil {
				_ = ac.ws.Close()
				return
			}
		}
	}
}

func (h *Hub) onMessage(ac *agentConn, raw []byte) {
	msg, err := protocol.Decode(raw)
	if err != nil {
		return
	}
	switch msg.Op() {
	case protocol.OpPing:
		_ = ac.send(protocol.Envelope{"op": protocol.OpPong})
	case protocol.OpCmdResult:
		id := msg.String("id")
		h.mu.Lock()
		p := h.pending[id]
		if p != nil {
			delete(h.pending, id)
		}
		h.mu.Unlock()
		if p != nil {
			select {
			case p.ch <- cmdResult{status: msg.Int("status"), body: msg.String("body")}:
			default:
			}
		}
	case protocol.OpTrendSave:
		node := SanitizeNodeName(msg.String("node"))
		if node == "" {
			node = ac.key.node
		}
		score := SanitizeScore(msg.String("score"))
		if node == "" || score == "" {
			return
		}
		var n int
		_, _ = fmt.Sscanf(score, "%d", &n)
		_ = h.db.InsertTrend(node, n,
			SanitizeStatusField(msg.String("goog")),
			SanitizeStatusField(msg.String("play")),
			SanitizeStatusField(msg.String("gemini")),
		)
	}
}

func (h *Hub) write(ws *websocket.Conn, msg protocol.Envelope) error {
	raw, err := protocol.Encode(msg)
	if err != nil {
		return err
	}
	_ = ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return ws.WriteMessage(websocket.BinaryMessage, raw)
}

// onlineGrace is how long last_seen counts as online during brief WS reconnect gaps.
const onlineGrace = 120 * time.Second

// IsConnected reports whether a live WS connection exists right now.
func (h *Hub) IsConnected(owner, node string) bool {
	key := connKey{owner: SanitizeChatID(owner), node: SanitizeNodeName(node)}
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.conns[key] != nil
}

func (h *Hub) IsOnline(owner, node string) bool {
	owner = SanitizeChatID(owner)
	node = SanitizeNodeName(node)
	if h.IsConnected(owner, node) {
		return true
	}
	// Brief disconnect / reconnect window: treat recent last_seen as online for UI.
	return h.db != nil && h.db.SeenRecently(owner, node, onlineGrace)
}

func (h *Hub) SendCommand(owner, node, path string, params map[string]any, timeout time.Duration) string {
	if timeout <= 0 {
		timeout = protocol.CmdTimeout
	}
	key := connKey{owner: SanitizeChatID(owner), node: SanitizeNodeName(node)}
	h.mu.Lock()
	ac := h.conns[key]
	h.mu.Unlock()
	if ac == nil {
		return "FAILED"
	}
	reqID := uuid.NewString()
	ch := make(chan cmdResult, 1)
	h.mu.Lock()
	h.pending[reqID] = &pendingResult{ch: ch}
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.pending, reqID)
		h.mu.Unlock()
	}()

	if params == nil {
		params = map[string]any{}
	}
	err := ac.send(protocol.Envelope{
		"op": protocol.OpCmd, "id": reqID, "path": path, "params": params,
	})
	if err != nil {
		return "FAILED"
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	select {
	case <-ctx.Done():
		return "FAILED"
	case res := <-ch:
		if res.status >= 400 {
			if res.body == "" {
				return fmt.Sprintf("ERROR %d", res.status)
			}
			return res.body
		}
		return res.body
	}
}

// EnableTCPKeepAlive sets TCP keepalive on the underlying connection when possible.
func EnableTCPKeepAlive(c net.Conn) {
	if tc, ok := c.(*net.TCPConn); ok {
		_ = tc.SetKeepAlive(true)
		_ = tc.SetKeepAlivePeriod(30 * time.Second)
	}
}
