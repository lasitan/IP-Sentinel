package master

import (
	"fmt"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/lasitan/IP-Sentinel/internal/protocol"
)

func TestAuthRoundTripCodec(t *testing.T) {
	chat, node := "42", "n1"
	ts := time.Now().Unix()
	sign := protocol.Sign(chat, "auth:"+chat+":"+node+":"+strconv.FormatInt(ts, 10))
	msg := protocol.Envelope{
		"op": protocol.OpAuth, "chat_id": chat, "node": node, "ts": ts, "sign": sign,
	}
	raw, err := protocol.Encode(msg)
	if err != nil {
		t.Fatal(err)
	}
	out, err := protocol.Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if !protocol.VerifyAuth(chat, out.String("chat_id"), out.String("node"), out.Int64("ts"), out.String("sign")) {
		t.Fatal("verify failed")
	}
}

func TestHubConcurrentSendCommand(t *testing.T) {
	db, err := OpenDB(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	hub := NewHub(db)
	srv := httptest.NewServer(hub)
	defer srv.Close()

	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ws.Close()

	chat, node := "42", "n1"
	ts := time.Now().Unix()
	var wmu sync.Mutex
	send := func(m protocol.Envelope) {
		raw, _ := protocol.Encode(m)
		wmu.Lock()
		defer wmu.Unlock()
		_ = ws.WriteMessage(websocket.BinaryMessage, raw)
	}
	send(protocol.Envelope{
		"op": protocol.OpAuth, "chat_id": chat, "node": node, "ts": ts,
		"sign": protocol.Sign(chat, fmt.Sprintf("auth:%s:%s:%d", chat, node, ts)),
	})
	_, data, err := ws.ReadMessage()
	if err != nil {
		t.Fatal(err)
	}
	if m, _ := protocol.Decode(data); m.Op() != protocol.OpAuthOK {
		t.Fatalf("auth failed: %v", m)
	}

	// Agent side: answer cmds, and keep sending app pings so master writes pongs concurrently.
	go func() {
		for {
			_, data, err := ws.ReadMessage()
			if err != nil {
				return
			}
			m, _ := protocol.Decode(data)
			if m.Op() == protocol.OpCmd {
				send(protocol.Envelope{"op": protocol.OpCmdResult, "id": m.String("id"), "status": 200, "body": "ok:" + m.String("path")})
			}
		}
	}()
	stopPing := make(chan struct{})
	go func() {
		for {
			select {
			case <-stopPing:
				return
			default:
				send(protocol.Envelope{"op": protocol.OpPing})
				time.Sleep(time.Millisecond)
			}
		}
	}()
	defer close(stopPing)

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			path := fmt.Sprintf("/p%d", i)
			if got := hub.SendCommand(chat, node, path, nil, 5*time.Second); got != "ok:"+path {
				t.Errorf("cmd %s: got %q", path, got)
			}
		}(i)
	}
	wg.Wait()
	if !hub.IsConnected(chat, node) {
		t.Fatal("connection dropped during concurrent writes")
	}
}
