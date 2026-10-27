package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("chat"); err != nil {
		log.Fatal("chat startup/runtime failed; check local dependency health and configuration")
	}
}
