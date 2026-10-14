package master

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestFormatChatIDFromFloat(t *testing.T) {
	// Reproduce json.Unmarshal float64 + fmt.Sprint scientific notation bug.
	raw := []byte(`{"message":{"chat":{"id":7617414857},"text":"/start","message_id":1}}`)
	var update map[string]any
	if err := json.Unmarshal(raw, &update); err != nil {
		t.Fatal(err)
	}
	chatID, text, _, _, _, _, _ := ExtractUpdate(update)
	if chatID != "7617414857" {
		t.Fatalf("chatID=%q want 7617414857 (scientific-notation glue bug)", chatID)
	}
	if text != "/start" {
		t.Fatalf("text=%q", text)
	}
}

func TestFormatChatIDSupergroup(t *testing.T) {
	raw := []byte(`{"message":{"chat":{"id":-1003978381617},"text":"/nginx","message_id":2}}`)
	var update map[string]any
	if err := json.Unmarshal(raw, &update); err != nil {
		t.Fatal(err)
	}
	chatID, _, _, _, _, _, _ := ExtractUpdate(update)
	if chatID != "-1003978381617" {
		t.Fatalf("chatID=%q want -1003978381617", chatID)
	}
}

func TestFormatChatIDJSONNumber(t *testing.T) {
	raw := []byte(`{"message":{"chat":{"id":7617414857},"text":"/start","message_id":1}}`)
	var update map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&update); err != nil {
		t.Fatal(err)
	}
	chatID, _, _, _, _, _, _ := ExtractUpdate(update)
	if chatID != "7617414857" {
		t.Fatalf("chatID=%q", chatID)
	}
}
