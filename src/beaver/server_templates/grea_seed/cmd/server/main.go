package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("server"); err != nil {
		log.Fatal("server startup/runtime failed; check local dependency health and configuration")
	}
}
