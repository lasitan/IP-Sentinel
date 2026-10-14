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

	pollStop   chan struct{}
	polling    atomic.Bool
	tgInbound  atomic.Bool // 已收到来自 Telegram 的 webhook POST
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
			log.Printf("[master] webhook 拒绝：secret_token 不匹配")
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
	// 必须像真实 Telegram Update（含 update_id），避免对本机 /tg 的探测 POST
	// 误触发「已切 webhook + 停止轮询」，导致之后完全收不到消息。
	if _, ok := update["update_id"]; !ok {
		log.Printf("[master] webhook 忽略非 Telegram update（无 update_id）")
		http.Error(w, "bad update", http.StatusBadRequest)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"ok":true}`))

	if a.Cfg.PublicBaseURL != "" && a.tgInbound.CompareAndSwap(false, true) {
		log.Printf("[master] 已收到来自 Telegram 的 HTTPS webhook 入站")
		_ = a.Cfg.SaveKeys(map[string]string{"TG_MODE": "webhook"})
		a.Cfg.TGMode = "webhook"
		a.StopPolling()
	}
	go a.processUpdate(update)
}

func (a *App) processUpdate(update map[string]any) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("[master] panic: %v", rec)
		}
	}()
	chatID, text, cbID, msgID, threadID, userMsgID, replyTo := ExtractUpdate(update)
	chatID = SanitizeChatID(chatID)
	if chatID == "" {
		return
	}
	preview := text
	if len(preview) > 48 {
		preview = preview[:48] + "…"
	}
	log.Printf("[master] tg update chat=%s thread=%d text=%q cb=%v", chatID, threadID, preview, cbID != "")
	a.Handlers.Dispatch(chatID, text, cbID, msgID, threadID, userMsgID, replyTo)
}

func (a *App) StartHTTP(ctx context.Context) error {
	ln, err := net.Listen("tcp", a.Cfg.ListenAddr)
	if err != nil {
		return err
	}
	srv := http.Server{
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

	// 默认 HTTP 长轮询。仅当配置明确为 webhook 且 PUBLIC_BASE_URL 可信时才 setWebhook。
	// 若曾因 webhook 异常回退为 polling，即使仍有 PUBLIC_BASE_URL 也不自动再挂 webhook，
	// 避免重启后再次卡死；用户可通过 /nginx 重新启用。
	wantWebhook := a.Cfg.PublicBaseURL != "" && strings.EqualFold(a.Cfg.TGMode, "webhook")
	if wantWebhook {
		if err := a.Handlers.EnableWebhookFromConfig(); err != nil {
			log.Printf("[master] webhook 不可用，使用 HTTP 轮询: %v", err)
			_ = a.Cfg.SaveKeys(map[string]string{"TG_MODE": "polling"})
			a.Cfg.TGMode = "polling"
			_ = a.TG.DeleteWebhook()
		} else {
			log.Printf("[master] Telegram 已接受 webhook %s/tg（若入站失败将自动回退轮询）", strings.TrimRight(a.Cfg.PublicBaseURL, "/"))
			a.Cfg.TGMode = "webhook"
			a.polling.Store(false)
			go a.watchWebhook(ctx)
			return
		}
	} else {
		_ = a.TG.DeleteWebhook()
		if !strings.EqualFold(a.Cfg.TGMode, "polling") {
			_ = a.Cfg.SaveKeys(map[string]string{"TG_MODE": "polling"})
		}
		a.Cfg.TGMode = "polling"
		if a.Cfg.PublicBaseURL != "" {
			log.Printf("[master] 保持 HTTP 轮询（TG_MODE=polling；公网已配置，可用 /nginx 重新启用 webhook）")
		}
	}

	a.startPolling(ctx)
}

func (a *App) startPolling(ctx context.Context) {
	if a.polling.Load() {
		return
	}
	a.pollStop = make(chan struct{})
	a.polling.Store(true)
	log.Printf("[master] TG HTTP 轮询已启动（请用 /nginx 配置 https://域名；Telegram 连通前不使用 webhook）")
	go a.pollLoop(ctx)
}

func (a *App) fallbackToPolling(ctx context.Context) {
	_ = a.TG.DeleteWebhook()
	_ = a.Cfg.SaveKeys(map[string]string{"TG_MODE": "polling"})
	a.Cfg.TGMode = "polling"
	a.tgInbound.Store(false)
	a.StopPolling()
	// 稍等让旧 pollLoop 退出后再起
	time.Sleep(200 * time.Millisecond)
	a.startPolling(ctx)
}

func (a *App) watchWebhook(ctx context.Context) {
	started := time.Now()
	t := time.NewTicker(30 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if a.Cfg.TGMode != "webhook" {
				return
			}
			if a.tgInbound.Load() {
				// 已证实可达，停止巡检
				return
			}
			info, err := a.TG.GetWebhookInfo()
			if err != nil {
				log.Printf("[master] getWebhookInfo: %v", err)
				continue
			}
			if info.URL == "" {
				log.Printf("[master] webhook 已被清除，回退轮询")
				a.fallbackToPolling(ctx)
				return
			}
			unhealthy := info.LastErrorMessage != "" && info.LastErrorDate > 0
			stuck := info.PendingUpdateCount > 0 && time.Since(started) > 90*time.Second
			if unhealthy || stuck {
				log.Printf("[master] webhook 异常，回退轮询: last_error=%q pending=%d",
					info.LastErrorMessage, info.PendingUpdateCount)
				a.fallbackToPolling(ctx)
				return
			}
		}
	}
}

func (a *App) pollLoop(ctx context.Context) {
	offsetPath := filepath.Join(a.Cfg.MasterDir, ".tg_offset")
	_ = os.MkdirAll(a.Cfg.MasterDir, 0o755)
	backoff := 2 * time.Second
	var lastErrLog time.Time
	for {
		select {
		case <-ctx.Done():
			a.polling.Store(false)
			return
		case <-a.pollStop:
			a.polling.Store(false)
			return
		default:
		}
		if a.Cfg.TGMode == "webhook" && a.tgInbound.Load() {
			log.Printf("[master] 停止轮询，已进入 webhook")
			a.polling.Store(false)
			return
		}
		// 配置刚切到 webhook 但尚未证实入站：继续轮询会 Conflict，应退出并交给 watchWebhook
		if a.Cfg.TGMode == "webhook" {
			log.Printf("[master] 停止轮询，等待 webhook 入站确认")
			a.polling.Store(false)
			go a.watchWebhook(ctx)
			return
		}
		offset := 0
		if b, err := os.ReadFile(offsetPath); err == nil {
			offset, _ = strconv.Atoi(strings.TrimSpace(string(b)))
		}
		updates, err := a.TG.GetUpdates(offset, 25)
		if err != nil {
			errStr := err.Error()
			// 意外残留 webhook：清掉后继续轮询
			if strings.Contains(errStr, "Conflict") || strings.Contains(strings.ToLower(errStr), "webhook") {
				if c2, err2 := LoadConfig(a.Cfg.Path); err2 == nil {
					a.Cfg.TGMode = c2.TGMode
					a.Cfg.PublicBaseURL = c2.PublicBaseURL
					a.Cfg.WebhookSecret = c2.WebhookSecret
				}
				if a.Cfg.TGMode == "webhook" {
					log.Printf("[master] getUpdates conflict（webhook 模式），停止轮询")
					a.polling.Store(false)
					go a.watchWebhook(ctx)
					return
				}
				log.Printf("[master] getUpdates conflict，清除残留 webhook 后继续轮询")
				_ = a.TG.DeleteWebhook()
				backoff = 2 * time.Second
				continue
			}
			transient := strings.Contains(errStr, "timeout") ||
				strings.Contains(errStr, "Timeout") ||
				strings.Contains(errStr, "deadline") ||
				strings.Contains(errStr, "unreachable") ||
				strings.Contains(errStr, "connection reset") ||
				strings.Contains(errStr, "connection refused") ||
				strings.Contains(errStr, "no such host") ||
				strings.Contains(errStr, "TLS") ||
				strings.Contains(errStr, "EOF")
			if time.Since(lastErrLog) > 60*time.Second {
				log.Printf("[master] getUpdates: %v (backoff %s)", err, backoff)
				lastErrLog = time.Now()
			}
			wait := backoff
			if !transient {
				wait = 2 * time.Second
			}
			select {
			case <-ctx.Done():
				a.polling.Store(false)
				return
			case <-a.pollStop:
				a.polling.Store(false)
				return
			case <-time.After(wait):
			}
			if transient {
				backoff *= 2
				if backoff > 60*time.Second {
					backoff = 60 * time.Second
				}
			}
			continue
		}
		backoff = 2 * time.Second
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
	a.polling.Store(false)
}
