# Dependencies

Go toolchain 1.26.2; go.mod/go.sum lock the actual resolved source graph. mysql driver: MPL-2.0; go-redis: BSD-2-Clause; grpc-go: Apache-2.0; protobuf-go: BSD-3-Clause. These libraries are downloaded from their official Go module identities, not copied from a product. Retain their licenses when distributing compiled/vendor artifacts. Generated protobuf files derive from this example's own protocol. No game media, account, credential, executable or toolchain is part of the source template.

Development Docker composition pins image digests in its generated lock; local overrides require recording their identity. MySQL/Redis are optional external dependencies, never embedded in grea's Python runtime. Real payment, production identity/TLS, node migration/owner fencing, outbox/inbox and broadcast chat are not implemented by this finite single-node example.
