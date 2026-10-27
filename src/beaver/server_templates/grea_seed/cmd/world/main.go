package main

import (
	"example.com/grea/server/internal/platform"
	"log"
)

func main() {
	if err := platform.Run("world"); err != nil {
		log.Fatal("world startup/runtime failed; check local dependency health and configuration")
	}
}
