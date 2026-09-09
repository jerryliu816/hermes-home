# hermes-home operations.
#
# Every command you need for normal running is here. `make help` lists them.

COMPOSE := docker compose
SERVICE := hermes-home
DB      := data/hermes-home.db
#: Path inside the container. All live-database access uses this, never the
#: host path -- see the "Database access" section below.
CONTAINER_DB := /data/hermes-home.db

.PHONY: help start stop restart status logs follow ready health upgrade rollback \
        backup restore build test lint shell prune \
        db-shell db-shell-rw db-query db-check db-snapshot

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# --- Normal operation -------------------------------------------------------

start:  ## Start the service (and keep it started across reboots)
	$(COMPOSE) up -d
	@echo "waiting for health..."
	@for i in $$(seq 1 30); do \
		if curl -fsS http://127.0.0.1:8099/health >/dev/null 2>&1; then echo "healthy"; exit 0; fi; \
		sleep 2; done; \
		echo "did not become healthy; check: make logs"; exit 1

stop:  ## Stop the service (stays stopped across reboots)
	$(COMPOSE) stop

restart:  ## Restart the service
	$(COMPOSE) restart
	@sleep 3 && $(MAKE) --no-print-directory status

status:  ## Show container status and readiness
	@$(COMPOSE) ps
	@echo
	@curl -fsS http://127.0.0.1:8099/ready 2>/dev/null | python3 -m json.tool || echo "not responding"

health:  ## Liveness check
	@curl -fsS http://127.0.0.1:8099/health | python3 -m json.tool

ready:  ## Readiness check (non-zero exit if not ready)
	@curl -fsS http://127.0.0.1:8099/ready | python3 -m json.tool

logs:  ## Show recent logs
	$(COMPOSE) logs --tail 200

follow:  ## Follow logs live
	$(COMPOSE) logs -f

# --- Deploy -----------------------------------------------------------------

upgrade:  ## Back up, rebuild the image, and restart on the new version
	./scripts/backup.sh
	$(COMPOSE) build
	$(COMPOSE) up -d
	@sleep 5 && $(MAKE) --no-print-directory status

rollback:  ## Roll back to the previously built image (see docs/operations.md)
	@echo "Rolling back means running a previous image tag."
	@echo "Available hermes-home images:"
	@docker images hermes-home --format '  {{.Repository}}:{{.Tag}}  built {{.CreatedSince}}'
	@echo
	@echo "Then: IMAGE_TAG=<tag> docker compose up -d"
	@echo "If the rollback also needs an older schema, restore a backup first:"
	@echo "  make stop && ./scripts/restore.sh <backup-dir> && make start"

# --- Data -------------------------------------------------------------------

backup:  ## Snapshot the database and config to ~/hermes-home-backups
	./scripts/backup.sh

restore:  ## Restore from a backup: make restore FROM=<dir>
	@test -n "$(FROM)" || { echo "usage: make restore FROM=~/hermes-home-backups/<stamp>"; exit 1; }
	./scripts/restore.sh "$(FROM)"

prune:  ## Drop raw webhook bodies past the retention horizon
	$(COMPOSE) exec $(SERVICE) hermes-home db prune

# --- Database access --------------------------------------------------------
#
# Every one of these runs INSIDE the container, and that is not a style choice.
# Opening the live database from macOS while the container holds it open has
# been measured destroying committed transactions silently: 60 commits, 44
# survivors, no error raised. See docs/operations.md.

db-shell:  ## Open a read-only sqlite3 shell on the live database (in-container)
	$(COMPOSE) exec $(SERVICE) sqlite3 -readonly $(CONTAINER_DB)

db-shell-rw:  ## Writable sqlite3 shell on the live database (in-container). Careful.
	$(COMPOSE) exec $(SERVICE) sqlite3 $(CONTAINER_DB)

db-query:  ## Run one SQL statement: make db-query SQL="select count(*) from events"
	@test -n "$(SQL)" || { echo 'usage: make db-query SQL="select count(*) from events"'; exit 1; }
	@$(COMPOSE) exec -T $(SERVICE) sqlite3 -readonly -header -column $(CONTAINER_DB) "$(SQL)"

db-check:  ## Integrity-check the live database (in-container, read-only, no repair)
	@echo "quick_check:"
	@$(COMPOSE) exec -T $(SERVICE) sqlite3 -readonly $(CONTAINER_DB) "PRAGMA quick_check;" | sed 's/^/  /'
	@echo "integrity_check:"
	@$(COMPOSE) exec -T $(SERVICE) sqlite3 -readonly $(CONTAINER_DB) "PRAGMA integrity_check;" | sed 's/^/  /'
	@echo "settings:"
	@$(COMPOSE) exec -T $(SERVICE) sqlite3 -readonly $(CONTAINER_DB) \
	    "PRAGMA journal_mode; PRAGMA synchronous;" | sed 's/^/  /'

db-snapshot:  ## Consistent snapshot to ./data/snapshots for safe host-side analysis
	@./scripts/snapshot.sh

shell:  ## Shell inside the running container
	$(COMPOSE) exec $(SERVICE) sh

# --- Development ------------------------------------------------------------

build:  ## Rebuild the image without starting it
	$(COMPOSE) build

test:  ## Run the test suite locally
	.venv/bin/python -m pytest tests/ -q

lint:  ## Lint and format-check
	.venv/bin/ruff check src/ tests/
	.venv/bin/ruff format --check src/ tests/
