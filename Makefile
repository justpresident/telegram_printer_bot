# Makefile for the Telegram printer bot.
#
# `make install` renders printerbot.service.in for THIS checkout (its path,
# your user, and the active python3) and installs it as a per-user systemd
# service — no hardcoded home directory, no root required.

# Absolute path of the directory containing this Makefile (the repo root).
ROOT     := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
RUN_USER := $(shell id -un)
PYTHON   ?= $(shell command -v python3)

SERVICE       := printerbot.service
TEMPLATE      := $(ROOT)/printerbot.service.in
USER_UNIT_DIR := $(HOME)/.config/systemd/user
SYSTEM_UNIT   := /etc/systemd/system/$(SERVICE)

# Render the unit from the template. $(1)=User= line, $(2)=WantedBy target.
render = sed -e 's|@WORKDIR@|$(ROOT)|g' \
             -e 's|@PYTHON@|$(PYTHON)|g' \
             -e 's|@USERLINE@|$(1)|g' \
             -e 's|@WANTEDBY@|$(2)|g' \
             "$(TEMPLATE)"

.PHONY: help test run install uninstall install-system uninstall-system print-unit

help: ## Show this help
	@sed -n 's/^\([a-z][a-z-]*\):.*## /  \1\t/p' $(MAKEFILE_LIST) | expand -t22

test: ## Run the test suite
	$(PYTHON) -m pytest tests.py -v

run: ## Run the bot from this directory
	cd "$(ROOT)" && $(PYTHON) -m printerbot

print-unit: ## Print the rendered per-user unit (for inspection)
	@$(call render,,default.target)

install: ## Install & start a per-user service (runs as you, no root)
	@command -v systemctl >/dev/null 2>&1 || { echo "ERROR: systemctl not found"; exit 1; }
	@mkdir -p "$(USER_UNIT_DIR)"
	@$(call render,,default.target) > "$(USER_UNIT_DIR)/$(SERVICE)"
	systemctl --user daemon-reload
	systemctl --user enable --now $(SERVICE)
	@loginctl enable-linger "$(RUN_USER)" 2>/dev/null \
	  || echo ">> Note: could not enable linger; run 'sudo loginctl enable-linger $(RUN_USER)' so the bot runs without an active login."
	@echo ">> Installed per-user service from $(ROOT)"
	@echo ">> Logs: journalctl --user -u $(SERVICE) -f"

uninstall: ## Stop & remove the per-user service
	-systemctl --user disable --now $(SERVICE)
	-rm -f "$(USER_UNIT_DIR)/$(SERVICE)"
	-systemctl --user daemon-reload
	@echo ">> Removed per-user service."

install-system: ## Install a system-wide service running as you (uses sudo)
	@command -v systemctl >/dev/null 2>&1 || { echo "ERROR: systemctl not found"; exit 1; }
	@$(call render,User=$(RUN_USER),multi-user.target) | sudo tee "$(SYSTEM_UNIT)" >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(SERVICE)
	@echo ">> Installed system service from $(ROOT) (User=$(RUN_USER))"
	@echo ">> Logs: sudo journalctl -u $(SERVICE) -f"

uninstall-system: ## Stop & remove the system-wide service (uses sudo)
	-sudo systemctl disable --now $(SERVICE)
	-sudo rm -f "$(SYSTEM_UNIT)"
	-sudo systemctl daemon-reload
	@echo ">> Removed system service."
