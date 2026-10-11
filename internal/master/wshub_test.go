package master

import (
	"strconv"
	"testing"
	"time"

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
