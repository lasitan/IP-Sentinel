package protocol

import (
	"strconv"
	"testing"
	"time"
)

func TestEncodeDecodeRoundTrip(t *testing.T) {
	msg := Envelope{"op": OpPing, "node": "n1"}
	raw, err := Encode(msg)
	if err != nil {
		t.Fatal(err)
	}
	out, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	if out.Op() != OpPing || out.String("node") != "n1" {
		t.Fatalf("got %#v", out)
	}
}

func TestVerifyAuth(t *testing.T) {
	chat, node := "123", "node-a"
	ts := time.Now().Unix()
	sign := Sign(chat, "auth:"+chat+":"+node+":"+strconv.FormatInt(ts, 10))
	if !VerifyAuth(chat, chat, node, ts, sign) {
		t.Fatal("expected valid")
	}
	if VerifyAuth(chat, chat, node, ts-300, sign) {
		t.Fatal("expected skew reject")
	}
}

func TestWSURLFromBase(t *testing.T) {
	cases := map[string]string{
		"https://m.example.com":  "wss://m.example.com/ws",
		"http://m.example.com/":  "ws://m.example.com/ws",
		"wss://m.example.com/ws": "wss://m.example.com/ws",
		"m.example.com":          "wss://m.example.com/ws",
	}
	for in, want := range cases {
		if got := WSURLFromBase(in); got != want {
			t.Fatalf("%q: got %q want %q", in, got, want)
		}
	}
}
