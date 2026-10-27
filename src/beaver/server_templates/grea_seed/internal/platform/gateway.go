package platform

import (
	"context"
	"encoding/json"
	pb "example.com/grea/server/api"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"io"
	"net"
	"net/http"
	"time"
)

type Gateway struct {
	Login   pb.LoginClient
	Game    pb.GameClient
	World   pb.WorldClient
	Chat    pb.ChatClient
	Billing pb.BillingClient
	Redis   *redis.Client
}

var inFlight = make(chan struct{}, 32)

var limiter = redis.NewScript(`local n=redis.call('INCR',KEYS[1]);if n==1 then redis.call('EXPIRE',KEYS[1],60) end;return n`)

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if origin := r.Header.Get("Origin"); origin != "" {
		if origin != env("GREA_BROWSER_ORIGIN", "http://127.0.0.1:7456") {
			http.Error(w, `{"code":"ORIGIN_REFUSED"}`, 403)
			return
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
		w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
		if r.Method == "OPTIONS" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
	}
	if r.URL.Path == "/healthz" {
		if e := g.Redis.Ping(r.Context()).Err(); e != nil {
			http.Error(w, `{"code":"UNAVAILABLE"}`, 503)
			return
		}
		w.Write([]byte(`{"ready":true}`))
		return
	}
	if r.Method != "POST" {
		http.Error(w, `{"code":"METHOD_NOT_ALLOWED"}`, 405)
		return
	}
	select {
	case inFlight <- struct{}{}:
		defer func() { <-inFlight }()
	default:
		http.Error(w, `{"code":"BUSY"}`, 503)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 4*time.Second)
	defer cancel()
	// Redis holds only a reconstructible minute counter, never authoritative assets.
	ip, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		http.Error(w, `{"code":"BAD_PEER"}`, 400)
		return
	}
	n, e := limiter.Run(ctx, g.Redis, []string{"grea:rate:" + Hash(ip)}).Int()
	if e != nil || n > 120 {
		http.Error(w, `{"code":"RATE_LIMIT_OR_CACHE_UNAVAILABLE"}`, 503)
		return
	}
	b, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 16384))
	if e != nil {
		http.Error(w, `{"code":"BAD_INPUT"}`, 400)
		return
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(b, &fields) != nil {
		http.Error(w, `{"code":"BAD_INPUT"}`, 400)
		return
	}
	for _, name := range []string{"expectedRevision", "expected_revision"} {
		if raw, ok := fields[name]; ok {
			var value string
			if json.Unmarshal(raw, &value) != nil {
				http.Error(w, `{"code":"INT64_STRING_REQUIRED"}`, 400)
				return
			}
		}
	}
	q := &pb.Request{}
	if e = protojson.Unmarshal(b, q); e != nil {
		http.Error(w, `{"code":"BAD_INPUT"}`, 400)
		return
	}
	var reply *pb.Reply
	if r.URL.Path == "/v1/login" {
		q.Action = "login"
		reply, e = g.Login.Call(ctx, q)
	} else {
		if q.SessionId != "" {
			http.Error(w, `{"code":"SESSION_IN_BODY_REFUSED"}`, 400)
			return
		}
		h := r.Header.Get("Authorization")
		if len(h) < 8 || h[:7] != "Bearer " {
			http.Error(w, `{"code":"UNAUTHENTICATED"}`, 401)
			return
		}
		q.SessionId = h[7:]
		if _, e = g.Login.Call(ctx, &pb.Request{Action: "validate", SessionId: q.SessionId}); e == nil {
			switch r.URL.Path {
			case "/v1/state", "/v1/command":
				route, err := g.World.Call(ctx, &pb.Request{Action: "locate", SessionId: q.SessionId})
				if err != nil {
					e = err
					break
				}
				// Fixed one-node example: never dial a client-controlled endpoint.
				if route.Address == "" {
					e = status.Error(codes.Unavailable, "route unavailable")
					break
				}
				if r.URL.Path == "/v1/state" {
					q.Action = "state"
				}
				reply, e = g.Game.Call(ctx, q)
			case "/v1/chat":
				reply, e = g.Chat.Call(ctx, q)
			case "/v1/order":
				reply, e = g.Billing.Call(ctx, q)
			default:
				http.Error(w, `{"code":"NOT_FOUND"}`, 404)
				return
			}
		}
	}
	if e != nil {
		code := http.StatusServiceUnavailable
		switch status.Code(e) {
		case codes.InvalidArgument:
			code = 400
		case codes.Unauthenticated:
			code = 401
		case codes.PermissionDenied:
			code = 403
		case codes.FailedPrecondition:
			code = 409
		case codes.Unimplemented:
			code = 501
		}
		// Stable safe error, no raw driver/credential contents.
		http.Error(w, `{"code":"`+status.Code(e).String()+`","retry":"same_identity_if_result_unknown"}`, code)
		return
	}
	data, e := (protojson.MarshalOptions{EmitUnpopulated: true}).Marshal(reply)
	if e != nil {
		http.Error(w, `{"code":"UNAVAILABLE"}`, 503)
		return
	}
	w.Write(data)
}
