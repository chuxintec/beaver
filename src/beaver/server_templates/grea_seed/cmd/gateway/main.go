package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("gateway"); err != nil {
		log.Fatal("gateway startup/runtime failed; check local dependency health and configuration")
	}
}
