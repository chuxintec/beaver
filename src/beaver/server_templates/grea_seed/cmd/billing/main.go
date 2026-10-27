package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("billing"); err != nil {
		log.Fatal("billing startup/runtime failed; check local dependency health and configuration")
	}
}
