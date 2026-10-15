package master

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

type DB struct {
	path string
	mu   sync.Mutex
}

func OpenDB(path string) (*DB, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	d := &DB{path: path}
	if err := d.migrate(); err != nil {
		return nil, err
	}
	return d, nil
}

func (d *DB) connect() (*sql.DB, error) {
	db, err := sql.Open("sqlite", d.path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := db.Exec(`PRAGMA synchronous=NORMAL`); err != nil {
		_ = db.Close()
		return nil, err
	}
	return db, nil
}

func (d *DB) migrate() error {
	db, err := d.connect()
	if err != nil {
		return err
	}
	defer db.Close()
	_, err = db.Exec(`
CREATE TABLE IF NOT EXISTS nodes (
  chat_id TEXT,
  node_name TEXT,
  agent_ip TEXT,
  last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  region TEXT DEFAULT 'UNKNOWN',
  node_alias TEXT,
  enable_google TEXT DEFAULT 'true',
  enable_trust TEXT DEFAULT 'true',
  enable_ota TEXT DEFAULT 'false',
  message_thread_id INTEGER,
  topic_ui_message_id INTEGER,
  topic_ui_edit_count INTEGER DEFAULT 0,
  PRIMARY KEY(chat_id, node_name)
);
CREATE TABLE IF NOT EXISTS ip_trend_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_name TEXT,
  check_time DATETIME DEFAULT CURRENT_TIMESTAMP,
  scam_score INTEGER,
  goog_status TEXT,
  nf_status TEXT,
  gpt_status TEXT
);
CREATE TABLE IF NOT EXISTS forum_topics (
  forum_chat_id TEXT,
  thread_id INTEGER,
  owner TEXT,
  node_name TEXT,
  title TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(forum_chat_id, thread_id)
);`)
	return err
}

func (d *DB) Exec(sqlStr string, args ...any) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	db, err := d.connect()
	if err != nil {
		return err
	}
	defer db.Close()
	_, err = db.Exec(sqlStr, args...)
	return err
}

func (d *DB) Query(sqlStr string, args ...any) ([]map[string]any, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	db, err := d.connect()
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query(sqlStr, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	var out []map[string]any
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		row := map[string]any{}
		for i, c := range cols {
			switch t := vals[i].(type) {
			case []byte:
				row[c] = string(t)
			default:
				row[c] = t
			}
		}
		out = append(out, row)
	}
	return out, rows.Err()
}

func (d *DB) Scalar(sqlStr string, args ...any) (any, error) {
	rows, err := d.Query(sqlStr, args...)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return nil, nil
	}
	for _, v := range rows[0] {
		return v, nil
	}
	return nil, fmt.Errorf("empty row")
}

func (d *DB) TouchLastSeen(chatID, node string) error {
	return d.Exec(`UPDATE nodes SET last_seen=CURRENT_TIMESTAMP WHERE chat_id=? AND node_name=?`, chatID, node)
}

// SeenRecently reports whether last_seen is within maxAge (SQLite CURRENT_TIMESTAMP = UTC).
func (d *DB) SeenRecently(chatID, node string, maxAge time.Duration) bool {
	if maxAge <= 0 {
		maxAge = 120 * time.Second
	}
	rows, err := d.Query(
		`SELECT last_seen FROM nodes WHERE chat_id=? AND node_name=? LIMIT 1`,
		chatID, node,
	)
	if err != nil || len(rows) == 0 {
		return false
	}
	raw := AsString(rows[0]["last_seen"])
	if raw == "" {
		return false
	}
	layouts := []string{
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05Z",
	}
	var t time.Time
	var parseErr error
	for _, layout := range layouts {
		t, parseErr = time.ParseInLocation(layout, raw, time.UTC)
		if parseErr == nil {
			return time.Since(t) <= maxAge
		}
	}
	return false
}

func (d *DB) UpsertNode(chatID, node, ip, region, alias, ota string) error {
	return d.Exec(`
INSERT INTO nodes (chat_id, node_name, agent_ip, last_seen, region, node_alias, enable_ota)
VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
ON CONFLICT(chat_id, node_name) DO UPDATE SET
  agent_ip=excluded.agent_ip,
  last_seen=CURRENT_TIMESTAMP,
  region=excluded.region,
  node_alias=excluded.node_alias,
  enable_ota=excluded.enable_ota`, chatID, node, ip, region, alias, ota)
}

func (d *DB) InsertTrend(node string, score int, goog, play, gemini string) error {
	return d.Exec(`
INSERT INTO ip_trend_log (node_name, scam_score, goog_status, nf_status, gpt_status)
VALUES (?, ?, ?, ?, ?)`, node, score, goog, play, gemini)
}

func AsString(v any) string {
	if v == nil {
		return ""
	}
	switch t := v.(type) {
	case string:
		return t
	case []byte:
		return string(t)
	default:
		return fmt.Sprint(t)
	}
}
