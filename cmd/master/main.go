package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"github.com/lasitan/IP-Sentinel/internal/master"
)

func main() {
	cfg, err := master.RequireConfig("")
	if err != nil {
		log.Fatal(err)
	}
	app, err := master.NewApp(cfg)
	if err != nil {
		log.Fatal(err)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	app.StartTG(ctx)
	if err := app.StartHTTP(ctx); err != nil {
		log.Fatal(err)
	}
}
