package platform

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	pb "example.com/grea/server/api"
	"example.com/grea/server/internal/demo"
	"google.golang.org/grpc"
	"google.golang.org/grpc/backoff"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"time"
)

type Service struct {
	pb.UnimplementedLoginServer
	pb.UnimplementedGameServer
	pb.UnimplementedWorldServer
	pb.UnimplementedChatServer
	pb.UnimplementedBillingServer
	Role                  string
	DB                    *sql.DB
	Config                demo.Config
	Login                 pb.LoginClient
	DevelopmentCredential string
	GameAddress           string
}

func Unavailable() error {
	return status.Error(codes.Unavailable, "durable result unknown; query or retry same command identity")
}
func (s *Service) player(ctx context.Context, q *pb.Request) (string, error) {
	if q.SessionId == "" {
		return "", status.Error(codes.Unauthenticated, "session required")
	}
	r, e := s.Login.Call(ctx, &pb.Request{Action: "validate", SessionId: q.SessionId})
	if e != nil {
		return "", e
	}
	return r.Message, nil
}
func (s *Service) Call(ctx context.Context, q *pb.Request) (*pb.Reply, error) {
	if s.Role == "login" {
		if q.Action == "login" {
			if subtle.ConstantTimeCompare([]byte(q.Credential), []byte(s.DevelopmentCredential)) != 1 {
				return nil, status.Error(codes.Unauthenticated, "invalid development credential")
			}
			b := make([]byte, 32)
			if _, e := rand.Read(b); e != nil {
				return nil, Unavailable()
			}
			token := hex.EncodeToString(b)
			if _, e := s.DB.ExecContext(ctx, `INSERT INTO sessions VALUES(?,?,?)`, Hash(token), "example-player", time.Now().Add(time.Hour).UnixMilli()); e != nil {
				return nil, Unavailable()
			}
			return &pb.Reply{Code: "OK", SessionId: token}, nil
		}
		if q.Action != "validate" {
			return nil, status.Error(codes.Unimplemented, "login method")
		}
		var player string
		if e := s.DB.QueryRowContext(ctx, `SELECT player FROM sessions WHERE token_hash=? AND expires_ms>?`, Hash(q.SessionId), time.Now().UnixMilli()).Scan(&player); e != nil {
			if errors.Is(e, sql.ErrNoRows) {
				return nil, status.Error(codes.Unauthenticated, "session expired")
			}
			return nil, Unavailable()
		}
		return &pb.Reply{Code: "OK", Message: player}, nil
	}
	player, e := s.player(ctx, q)
	if e != nil {
		return nil, e
	}
	switch s.Role {
	case "world":
		if q.Action != "locate" {
			return nil, status.Error(codes.Unimplemented, "world method")
		}
		_, e = s.DB.ExecContext(ctx, `INSERT INTO routes VALUES(?,?) ON DUPLICATE KEY UPDATE endpoint=VALUES(endpoint)`, player, s.GameAddress)
		if e != nil {
			return nil, Unavailable()
		}
		return &pb.Reply{Code: "OK", Address: s.GameAddress}, nil
	case "server":
		if q.Action == "state" {
			r := &pb.Reply{Code: "OK", ConfigVersion: s.Config.Version}
			e = s.DB.QueryRowContext(ctx, `SELECT revision,balance FROM players WHERE player=?`, player).Scan(&r.Revision, &r.Balance)
			if e != nil && !errors.Is(e, sql.ErrNoRows) {
				return nil, Unavailable()
			}
			return r, nil
		}
		if q.ConfigVersion != s.Config.Version {
			return nil, status.Error(codes.FailedPrecondition, "configuration version mismatch")
		}
		if q.Action != "claim_starter" {
			return nil, status.Error(codes.Unimplemented, "game method")
		}
		code, rev, balance, e := Command(ctx, s.DB, player, q.OperationId, q.Action, q.ExpectedRevision, s.Config.StarterReward)
		if e != nil {
			return nil, Unavailable()
		}
		return &pb.Reply{Code: code, Revision: rev, Balance: balance, ConfigVersion: s.Config.Version}, nil
	case "chat":
		if len(q.Text) == 0 || len(q.Text) > 500 || q.OperationId == "" || len(q.OperationId) > 80 {
			return nil, status.Error(codes.InvalidArgument, "bounded chat text/operation ID required")
		}
		// First writer wins; same ID different content is refused, never rewritten.
		_, e = s.DB.ExecContext(ctx, `INSERT INTO messages VALUES(?,?,?,?) ON DUPLICATE KEY UPDATE operation_id=operation_id`, player, q.OperationId, q.Text, time.Now().UnixMilli())
		if e != nil {
			return nil, Unavailable()
		}
		var content string
		if e = s.DB.QueryRowContext(ctx, `SELECT content FROM messages WHERE player=? AND operation_id=?`, player, q.OperationId).Scan(&content); e != nil {
			return nil, Unavailable()
		}
		if content != q.Text {
			return &pb.Reply{Code: "COMMAND_ID_CONFLICT"}, nil
		}
		return &pb.Reply{Code: "OK", Message: content}, nil
	case "billing":
		if q.Action != "create_order" {
			return nil, status.Error(codes.Unavailable, "payment provider and fulfillment integration unavailable")
		}
		if q.OperationId == "" || len(q.OperationId) > 80 || q.Text != "example-sku" {
			return nil, status.Error(codes.InvalidArgument, "example order identity required")
		}
		if _, e = s.DB.ExecContext(ctx, `INSERT INTO orders VALUES(?,?,?,'PENDING_PROVIDER') ON DUPLICATE KEY UPDATE operation_id=operation_id`, player, q.OperationId, q.Text); e != nil {
			return nil, Unavailable()
		}
		return &pb.Reply{Code: "PENDING_PROVIDER"}, nil
	}
	return nil, status.Error(codes.Unimplemented, "role")
}
func Dial(address, key string) (*grpc.ClientConn, error) {
	return grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithConnectParams(grpc.ConnectParams{Backoff: backoff.Config{BaseDelay: 100 * time.Millisecond, Multiplier: 1.6, Jitter: .2, MaxDelay: 2 * time.Second}, MinConnectTimeout: time.Second}), grpc.WithUnaryInterceptor(func(ctx context.Context, m string, q, r interface{}, cc *grpc.ClientConn, f grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		return f(metadata.AppendToOutgoingContext(ctx, "x-grea-internal", key), m, q, r, cc, opts...)
	}))
}
func Internal(key string) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
		values := metadata.ValueFromIncomingContext(ctx, "x-grea-internal")
		if len(values) != 1 || subtle.ConstantTimeCompare([]byte(values[0]), []byte(key)) != 1 {
			return nil, status.Error(codes.Unauthenticated, "internal identity required")
		}
		if _, ok := ctx.Deadline(); !ok {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, 3*time.Second)
			defer cancel()
		}
		return handler(ctx, req)
	}
}
func ValidRole(s string) bool {
	switch s {
	case "gateway", "chat", "server", "login", "world", "billing":
		return true
	}
	return false
}
