package platform

import (
	"context"
	pb "example.com/grea/server/api"
	"example.com/grea/server/internal/demo"
	"fmt"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func secret(name string) string {
	path := os.Getenv(name + "_FILE")
	b, e := os.ReadFile(path)
	if e != nil || len(strings.TrimSpace(string(b))) < 16 {
		panic("required secret file unavailable: " + name)
	}
	return strings.TrimSpace(string(b))
}
func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
func Run(role string) error {
	if !ValidRole(role) || os.Getenv("GREA_MODE") != "development" {
		return fmt.Errorf("explicit development mode and role required; production TLS/identity adapters are not implemented")
	}
	cfg, e := demo.LoadConfig(env("GREA_GAME_CONFIG", "config/game.json"))
	if e != nil {
		return e
	}
	key := secret("GREA_INTERNAL")
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	login, e := Dial(env("GREA_LOGIN", "login:50051"), key)
	if e != nil {
		return e
	}
	defer login.Close()
	if role == "gateway" {
		game, e := Dial(env("GREA_SERVER", "server:50051"), key)
		if e != nil {
			return e
		}
		defer game.Close()
		world, e := Dial(env("GREA_WORLD", "world:50051"), key)
		if e != nil {
			return e
		}
		defer world.Close()
		chat, e := Dial(env("GREA_CHAT", "chat:50051"), key)
		if e != nil {
			return e
		}
		defer chat.Close()
		billing, e := Dial(env("GREA_BILLING", "billing:50051"), key)
		if e != nil {
			return e
		}
		defer billing.Close()
		cache := redis.NewClient(&redis.Options{Addr: env("GREA_REDIS", "redis:6379"), Password: secret("GREA_REDIS_PASSWORD"), PoolSize: 4, MaxRetries: 0})
		defer cache.Close()
		srv := &http.Server{Addr: env("GREA_LISTEN", ":8080"), Handler: &Gateway{pb.NewLoginClient(login), pb.NewGameClient(game), pb.NewWorldClient(world), pb.NewChatClient(chat), pb.NewBillingClient(billing), cache}, ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 6 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
		done := make(chan error, 1)
		go func() { done <- srv.ListenAndServe() }()
		select {
		case <-ctx.Done():
		case err := <-done:
			if err != http.ErrServerClosed {
				return err
			}
		}
		end, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(end)
	}
	start, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	db, e := Open(start, role, secret("GREA_MYSQL_DSN"))
	if e != nil {
		return e
	}
	defer db.Close()
	s := &Service{Role: role, DB: db, Config: cfg, Login: pb.NewLoginClient(login), GameAddress: env("GREA_SERVER", "server:50051")}
	if role == "login" {
		s.DevelopmentCredential = secret("GREA_DEVELOPMENT_CREDENTIAL")
	}
	server := grpc.NewServer(grpc.UnaryInterceptor(Internal(key)), grpc.MaxRecvMsgSize(16384), grpc.MaxConcurrentStreams(32))
	switch role {
	case "login":
		pb.RegisterLoginServer(server, s)
	case "server":
		pb.RegisterGameServer(server, s)
	case "world":
		pb.RegisterWorldServer(server, s)
	case "chat":
		pb.RegisterChatServer(server, s)
	case "billing":
		pb.RegisterBillingServer(server, s)
	}
	h := health.NewServer()
	healthpb.RegisterHealthServer(server, h)
	h.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	listener, e := net.Listen("tcp", env("GREA_LISTEN", ":50051"))
	if e != nil {
		return e
	}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
	case e := <-done:
		return e
	}
	h.Shutdown()
	stopped := make(chan struct{})
	go func() { server.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		server.Stop()
	}
	return nil
}
