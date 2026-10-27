package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("login"); err != nil {
		log.Fatal("login startup/runtime failed; check local dependency health and configuration")
	}
}
