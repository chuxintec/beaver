package platform

import (
	"net/http/httptest"
	"testing"
)

func TestBrowserPreflightExactOrigin(t *testing.T) {
	t.Setenv("GREA_BROWSER_ORIGIN", "http://127.0.0.1:7456")
	for _, test := range []struct {
		origin string
		want   int
	}{{"http://127.0.0.1:7456", 204}, {"https://outside.example", 403}} {
		q := httptest.NewRequest("OPTIONS", "/v1/command", nil)
		q.Header.Set("Origin", test.origin)
		r := httptest.NewRecorder()
		(&Gateway{}).ServeHTTP(r, q)
		if r.Code != test.want {
			t.Fatalf("origin check got %d", r.Code)
		}
	}
}
