.PHONY: help setup clean reset dev test lint format build run gui debug-gui debug-shell

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Resolve all paths to absolute at parse time.
# $(abspath) converts relative paths (e.g. from env overrides) using the Make
# working directory; already-absolute paths are passed through unchanged.
DK_CONFIG_DIR  := $(abspath $(or $(DK_CONFIG_DIR),$(CURDIR)/data/config))
DK_LOG_DIR     := $(abspath $(or $(DK_LOG_DIR),$(CURDIR)/data/logs))
DK_BACKUP_DIR  := $(abspath $(or $(DK_BACKUP_DIR),$(CURDIR)/data/backups))
DK_LOCK_DIR    := $(abspath $(or $(DK_LOCK_DIR),$(CURDIR)/data/var_lock))
DK_RESTORE_DIR := $(abspath $(or $(DK_RESTORE_DIR),$(CURDIR)/data/restore))
DK_APPDATA_DIR := $(abspath $(or $(DK_APPDATA_DIR),$(CURDIR)/data/appdata))
DK_SCRIPTS_DIR := $(abspath $(or $(DK_SCRIPTS_DIR),$(CURDIR)/data/scripts))
DK_ALLOW_INLINE_HOOKS := $(or $(DK_ALLOW_INLINE_HOOKS),true)
# Single source of truth: default RCLONE_CONFIG below DK_CONFIG_DIR, so an
# override of DK_CONFIG_DIR moves the rclone config with it (matches the
# docker-entrypoint default RCLONE_CONFIG=${DK_CONFIG_DIR}/rclone.conf).
RCLONE_CONFIG   := $(abspath $(or $(RCLONE_CONFIG),$(DK_CONFIG_DIR)/rclone.conf))

help:
	@echo "Available commands:"
	@echo "  make setup    - Create venv and install all dependencies"
	@echo "  make clean    - Remove venv and build artifacts"
	@echo "  make reset    - clean + setup"
	@echo "  make dev      - Open shell with dk command available"
	@echo "  make test     - Run tests with coverage"
	@echo "  make lint     - Run black --check, ruff, mypy"
	@echo "  make format   - Auto-format with black and ruff"
	@echo "  make build    - Build Docker image"
	@echo "  make run      - Start container via docker compose"
	@echo "  make gui      - Start GUI locally on port 8080"
	@echo "  make debug-gui - Start GUI under debugpy on port 8080; attach debugger on 5678"
	@echo "  make debug-shell - Open shell with dk and dk-debug commands available"

setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	$(PIP) install -r requirements-dev.txt -q
	$(PIP) install -e . -q
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

clean:
	rm -rf $(VENV) dockkeep.egg-info
	@echo "Removed $(VENV) and build artifacts."

reset: clean setup

dev:
	@echo "Activating venv and opening shell (exit with 'exit')..."
	@bash -c 'source $(VENV)/bin/activate \
	&& export DK_CONFIG_DIR=$(DK_CONFIG_DIR) \
	&& export DK_LOG_DIR=$(DK_LOG_DIR) \
	&& export DK_BACKUP_DIR=$(DK_BACKUP_DIR) \
	&& export DK_LOCK_DIR=$(DK_LOCK_DIR) \
	&& export DK_RESTORE_DIR=$(DK_RESTORE_DIR) \
	&& export DK_APPDATA_DIR=$(DK_APPDATA_DIR) \
	&& export DK_SCRIPTS_DIR=$(DK_SCRIPTS_DIR) \
	&& export DK_ALLOW_INLINE_HOOKS=$(DK_ALLOW_INLINE_HOOKS) \
	&& export RCLONE_CONFIG=$(RCLONE_CONFIG) \
	&& export RESTIC_PASSWORD=$${RESTIC_PASSWORD:-test} \
	&& exec bash'

test: $(VENV)
	DK_CONFIG_DIR=$(DK_CONFIG_DIR) \
	DK_LOG_DIR=$(DK_LOG_DIR) \
	DK_BACKUP_DIR=$(DK_BACKUP_DIR) \
	DK_LOCK_DIR=$(DK_LOCK_DIR) \
	DK_RESTORE_DIR=$(DK_RESTORE_DIR) \
	DK_APPDATA_DIR=$(DK_APPDATA_DIR) \
	DK_SCRIPTS_DIR=$(DK_SCRIPTS_DIR) \
	DK_ALLOW_INLINE_HOOKS=$(DK_ALLOW_INLINE_HOOKS) \
	RCLONE_CONFIG=$(RCLONE_CONFIG) \
	timeout 30 $(PYTHON) -m pytest tests/ -v

lint: $(VENV)
	$(PYTHON) -m black --check src/ tests/
	$(PYTHON) -m ruff check src/ tests/
	$(PYTHON) -m mypy src/

format: $(VENV)
	$(PYTHON) -m black src/ tests/
	$(PYTHON) -m ruff check --fix src/ tests/

build:
	docker build -t dockkeep .

run:
	docker compose up -d

gui: $(VENV)
	DK_CONFIG_DIR=$(DK_CONFIG_DIR) \
	DK_LOG_DIR=$(DK_LOG_DIR) \
	DK_BACKUP_DIR=$(DK_BACKUP_DIR) \
	DK_LOCK_DIR=$(DK_LOCK_DIR) \
	DK_RESTORE_DIR=$(DK_RESTORE_DIR) \
	DK_APPDATA_DIR=$(DK_APPDATA_DIR) \
	DK_SCRIPTS_DIR=$(DK_SCRIPTS_DIR) \
	DK_ALLOW_INLINE_HOOKS=$(DK_ALLOW_INLINE_HOOKS) \
	RCLONE_CONFIG=$(RCLONE_CONFIG) \
	RESTIC_PASSWORD=$${RESTIC_PASSWORD:-test} \
	$(PYTHON) -m src.runtime gui --port 8080

debug-gui: $(VENV)
	DK_CONFIG_DIR=$(DK_CONFIG_DIR) \
	DK_LOG_DIR=$(DK_LOG_DIR) \
	DK_BACKUP_DIR=$(DK_BACKUP_DIR) \
	DK_LOCK_DIR=$(DK_LOCK_DIR) \
	DK_RESTORE_DIR=$(DK_RESTORE_DIR) \
	DK_APPDATA_DIR=$(DK_APPDATA_DIR) \
	DK_SCRIPTS_DIR=$(DK_SCRIPTS_DIR) \
	DK_ALLOW_INLINE_HOOKS=$(DK_ALLOW_INLINE_HOOKS) \
	RCLONE_CONFIG=$(RCLONE_CONFIG) \
	RESTIC_PASSWORD=$${RESTIC_PASSWORD:-test} \
	$(PYTHON) -Xfrozen_modules=off -m debugpy --listen 5678 --wait-for-client -m src.runtime gui --host 127.0.0.1 --port 8080

debug-shell: $(VENV)
	DK_CONFIG_DIR=$(DK_CONFIG_DIR) \
	DK_LOG_DIR=$(DK_LOG_DIR) \
	DK_BACKUP_DIR=$(DK_BACKUP_DIR) \
	DK_LOCK_DIR=$(DK_LOCK_DIR) \
	DK_RESTORE_DIR=$(DK_RESTORE_DIR) \
	DK_APPDATA_DIR=$(DK_APPDATA_DIR) \
	DK_SCRIPTS_DIR=$(DK_SCRIPTS_DIR) \
	DK_ALLOW_INLINE_HOOKS=$(DK_ALLOW_INLINE_HOOKS) \
	DK_MODE=cli \
	RCLONE_CONFIG=$(RCLONE_CONFIG) \
	RESTIC_PASSWORD=$${RESTIC_PASSWORD:-test} \
	DK_DEBUG_PYTHON=$(abspath $(PYTHON)) \
	bash tools/debug-shell.sh

$(VENV):
	@echo "Run 'make setup' first."
	@exit 1
