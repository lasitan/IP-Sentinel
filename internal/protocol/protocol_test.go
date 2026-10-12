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
	ok := map[string]string{
		"https://m.example.com":  "wss://m.example.com/ws",
		"https://m.example.com/": "wss://m.example.com/ws",
		"wss://m.example.com/ws": "wss://m.example.com/ws",
		"m.example.com":          "wss://m.example.com/ws",
	}
	for in, want := range ok {
		if got := WSURLFromBase(in); got != want {
			t.Fatalf("%q: got %q want %q", in, got, want)
		}
	}
	reject := []string{
		"http://m.example.com",
		"ws://m.example.com/ws",
		"https://1.2.3.4",
		"https://45.207.210.149",
		"wss://1.2.3.4/ws",
		"1.2.3.4",
	}
	for _, in := range reject {
		if got := WSURLFromBase(in); got != "" {
			t.Fatalf("%q: expected empty, got %q", in, got)
		}
	}
}

func TestValidateSecureHTTPSURL(t *testing.T) {
	if err := ValidateSecureHTTPSURL("https://master.example.com"); err != nil {
		t.Fatal(err)
	}
	if err := ValidateSecureHTTPSURL("https://1.2.3.4"); err == nil {
		t.Fatal("expected IP reject")
	}
	if err := ValidateSecureHTTPSURL("http://master.example.com"); err == nil {
		t.Fatal("expected http reject")
	}
}
