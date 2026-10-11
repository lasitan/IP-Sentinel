package master

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

type App struct {
	Cfg      *Config
	DB       *DB
	TG       *Telegram
	Hub      *Hub
	Handlers *Handlers

	pollStop chan struct{}
	polling  atomic.Bool
}

func NewApp(cfg *Config) (*App, error) {
	db, err := OpenDB(cfg.DBFile)
	if err != nil {
		return nil, err
	}
	tg := NewTelegram(cfg.TGToken)
	hub := NewHub(db)
	h := NewHandlers(cfg, db, tg, hub)
	return &App{Cfg: cfg, DB: db, TG: tg, Hub: hub, Handlers: h, pollStop: make(chan struct{})}, nil
}

func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/ready", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"service":"ip-sentinel-master"}`))
	})
	mux.HandleFunc("/tg", a.handleWebhook)
	mux.Handle("/ws", a.Hub)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = w.Write([]byte("IP-Sentinel Master"))
	})
	return mux
}

func (a *App) handleWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if secret := a.Cfg.WebhookSecret; secret != "" {
		if r.Header.Get("X-Telegram-Bot-Api-Secret-Token") != secret {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	var update map[string]any
	if err := json.Unmarshal(raw, &update); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"ok":true}`))
	go a.processUpdate(update)
}

func (a *App) processUpdate(update map[string]any) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("[master] panic: %v", rec)
		}
	}()
	chatID, text, cbID, msgID, threadID, userMsgID, replyTo := ExtractUpdate(update)
	if chatID == "" {
		return
	}
	a.Handlers.Dispatch(chatID, text, cbID, msgID, threadID, userMsgID, replyTo)
}

func (a *App) StartHTTP(ctx context.Context) error {
	ln, err := net.Listen("tcp", a.Cfg.ListenAddr)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Handler:           a.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
		ConnState: func(c net.Conn, s http.ConnState) {
			if s == http.StateNew {
				EnableTCPKeepAlive(c)
			}
		},
	}
	go func() {
		<-ctx.Done()
		_ = srv.Shutdown(context.Background())
	}()
	log.Printf("[master] HTTP listen %s ( /tg + /ws )", a.Cfg.ListenAddr)
	err = srv.Serve(ln)
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func (a *App) StartTG(ctx context.Context) {
	a.TG.SyncBotCommands()
	StartFleetScheduler(ctx, a.Handlers)

	if a.Cfg.TGMode == "webhook" && a.Cfg.PublicBaseURL != "" {
		if err := a.Handlers.EnableWebhookFromConfig(); err != nil {
			log.Printf("[master] webhook 启用失败，回退轮询: %v", err)
			_ = a.Cfg.SaveKeys(map[string]string{"TG_MODE": "polling"})
		} else {
			log.Printf("[master] Webhook 模式: %s/tg", strings.TrimRight(a.Cfg.PublicBaseURL, "/"))
			return
		}
	}

	// Bootstrap / fallback: long polling until webhook configured via /nginx
	_ = a.TG.DeleteWebhook()
	a.polling.Store(true)
	log.Printf("[master] TG 引导轮询已启动（配置 PUBLIC_BASE_URL 或 /nginx 后切换 webhook）")
	go a.pollLoop(ctx)
}

func (a *App) pollLoop(ctx context.Context) {
	offsetPath := filepath.Join(a.Cfg.MasterDir, ".tg_offset")
	_ = os.MkdirAll(a.Cfg.MasterDir, 0o755)
	for {
		select {
		case <-ctx.Done():
			return
		case <-a.pollStop:
			return
		default:
		}
		if a.Cfg.TGMode == "webhook" {
			log.Printf("[master] 停止轮询，已进入 webhook")
			a.polling.Store(false)
			return
		}
		offset := 0
		if b, err := os.ReadFile(offsetPath); err == nil {
			offset, _ = strconv.Atoi(strings.TrimSpace(string(b)))
		}
		updates, err := a.TG.GetUpdates(offset, 30)
		if err != nil {
			log.Printf("[master] getUpdates: %v", err)
			time.Sleep(2 * time.Second)
			continue
		}
		for _, u := range updates {
			uid := intFrom(u["update_id"])
			next := uid + 1
			a.processUpdate(u)
			_ = os.WriteFile(offsetPath, []byte(strconv.Itoa(next)), 0o644)
		}
		// reload mode from disk in case /nginx switched it
		if c2, err := LoadConfig(a.Cfg.Path); err == nil {
			a.Cfg.TGMode = c2.TGMode
			a.Cfg.PublicBaseURL = c2.PublicBaseURL
			a.Cfg.WebhookSecret = c2.WebhookSecret
		}
	}
}

func (a *App) StopPolling() {
	select {
	case <-a.pollStop:
	default:
		close(a.pollStop)
	}
}
