package platform

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"strings"
	"testing"
)

// Own SQL driver simulates a lost Commit acknowledgment; no database engine is substituted in runtime.
type unknownDriver struct{}
type unknownConnection struct{}
type unknownTransaction struct{}
type ownRows struct{ sent bool }

func (unknownDriver) Open(string) (driver.Conn, error)        { return unknownConnection{}, nil }
func (unknownConnection) Prepare(string) (driver.Stmt, error) { return nil, errors.New("unused") }
func (unknownConnection) Close() error                        { return nil }
func (unknownConnection) Begin() (driver.Tx, error)           { return unknownTransaction{}, nil }
func (unknownConnection) ExecContext(context.Context, string, []driver.NamedValue) (driver.Result, error) {
	return driver.RowsAffected(1), nil
}
func (unknownConnection) QueryContext(_ context.Context, q string, _ []driver.NamedValue) (driver.Rows, error) {
	return &ownRows{sent: strings.Contains(q, "FROM receipts")}, nil
}
func (unknownTransaction) Commit() error   { return errors.New("commit acknowledgment lost") }
func (unknownTransaction) Rollback() error { return nil }
func (r *ownRows) Columns() []string       { return []string{"revision", "balance", "claimed"} }
func (r *ownRows) Close() error            { return nil }
func (r *ownRows) Next(values []driver.Value) error {
	if r.sent {
		return io.EOF
	}
	r.sent = true
	values[0] = int64(0)
	values[1] = int64(0)
	values[2] = false
	return nil
}
func init() { sql.Register("grea-owned-unknown-commit", unknownDriver{}) }
func TestCommitUnknownNeverReturnsSuccess(t *testing.T) {
	db, e := sql.Open("grea-owned-unknown-commit", "")
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	_, _, _, e = Command(context.Background(), db, "owned-player", "same-id", "claim_starter", 0, 7)
	if e == nil {
		t.Fatal("commit acknowledgment failure must preserve uncertain outcome")
	}
}
