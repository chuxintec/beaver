package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"github.com/go-sql-driver/mysql"
	"strings"
	"time"
)

func Hash(s string) string { b := sha256.Sum256([]byte(s)); return hex.EncodeToString(b[:]) }
func Open(ctx context.Context, role, dsn string) (*sql.DB, error) {
	c, e := mysql.ParseDSN(dsn)
	if e != nil {
		return nil, e
	}
	if c.DBName != "grea_example_"+role {
		return nil, errors.New("isolated role database required")
	}
	c.ParseTime = true
	c.Timeout = 3 * time.Second
	c.ReadTimeout = 5 * time.Second
	c.WriteTimeout = 5 * time.Second
	db, e := sql.Open("mysql", c.FormatDSN())
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(6)
	db.SetMaxIdleConns(2)
	db.SetConnMaxLifetime(5 * time.Minute)
	if e = db.PingContext(ctx); e != nil {
		db.Close()
		return nil, e
	}
	definitions := map[string][]string{
		"login":   {`CREATE TABLE IF NOT EXISTS sessions(token_hash CHAR(64) PRIMARY KEY, player VARCHAR(80) NOT NULL, expires_ms BIGINT NOT NULL)`},
		"server":  {`CREATE TABLE IF NOT EXISTS players(player VARCHAR(80) PRIMARY KEY, revision BIGINT NOT NULL, balance BIGINT NOT NULL, starter_claimed BOOLEAN NOT NULL)`, `CREATE TABLE IF NOT EXISTS receipts(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, payload_hash CHAR(64) NOT NULL, code VARCHAR(40) NOT NULL, revision BIGINT NOT NULL, balance BIGINT NOT NULL, PRIMARY KEY(player,operation_id))`},
		"world":   {`CREATE TABLE IF NOT EXISTS routes(player VARCHAR(80) PRIMARY KEY, endpoint VARCHAR(180) NOT NULL)`},
		"chat":    {`CREATE TABLE IF NOT EXISTS messages(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, content VARCHAR(500) NOT NULL, created_ms BIGINT NOT NULL, PRIMARY KEY(player,operation_id))`},
		"billing": {`CREATE TABLE IF NOT EXISTS orders(player VARCHAR(80) NOT NULL, operation_id VARCHAR(80) NOT NULL, sku VARCHAR(80) NOT NULL, status VARCHAR(40) NOT NULL, PRIMARY KEY(player,operation_id))`},
	}
	for _, q := range definitions[role] {
		if _, e = db.ExecContext(ctx, q); e != nil {
			db.Close()
			return nil, e
		}
	}
	return db, nil
}

// Command executes the fictional once-only starter claim. Persistence precedes success.
// On Commit error the caller must query/retry the exact same operation ID and content.
func Command(ctx context.Context, db *sql.DB, player, id, action string, expected, reward int64) (code string, revision, balance int64, err error) {
	if id == "" || len(id) > 80 || strings.TrimSpace(id) != id {
		return "", 0, 0, errors.New("invalid operation ID")
	}
	payload := Hash(action + ":" + fmtInt(expected))
	tx, e := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if e != nil {
		return "", 0, 0, e
	}
	defer tx.Rollback()
	if _, e = tx.ExecContext(ctx, `INSERT INTO players VALUES(?,0,0,FALSE) ON DUPLICATE KEY UPDATE player=player`, player); e != nil {
		return "", 0, 0, e
	}
	var claimed bool
	if e = tx.QueryRowContext(ctx, `SELECT revision,balance,starter_claimed FROM players WHERE player=? FOR UPDATE`, player).Scan(&revision, &balance, &claimed); e != nil {
		return "", 0, 0, e
	}
	var priorHash string
	e = tx.QueryRowContext(ctx, `SELECT payload_hash,code,revision,balance FROM receipts WHERE player=? AND operation_id=?`, player, id).Scan(&priorHash, &code, &revision, &balance)
	if e == nil {
		if priorHash != payload {
			return "COMMAND_ID_CONFLICT", revision, balance, nil
		}
		return code, revision, balance, nil
	}
	if !errors.Is(e, sql.ErrNoRows) {
		return "", 0, 0, e
	}
	code = "REJECTED"
	if expected != revision {
		code = "REVISION_CONFLICT"
	} else if action == "claim_starter" && !claimed {
		if balance > 9223372036854775807-reward {
			return "", 0, 0, errors.New("balance overflow")
		}
		balance += reward
		revision++
		code = "APPLIED"
		if _, e = tx.ExecContext(ctx, `UPDATE players SET revision=?,balance=?,starter_claimed=TRUE WHERE player=?`, revision, balance, player); e != nil {
			return "", 0, 0, e
		}
	}
	if _, e = tx.ExecContext(ctx, `INSERT INTO receipts VALUES(?,?,?,?,?,?)`, player, id, payload, code, revision, balance); e != nil {
		return "", 0, 0, e
	}
	if e = tx.Commit(); e != nil {
		return "", 0, 0, e
	}
	return code, revision, balance, nil
}
