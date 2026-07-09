# 本地开发命令（需已安装 uv: https://docs.astral.sh/uv/）
UV ?= uv

.PHONY: sync lock lint fmt fmt-check
.PHONY: runner google trust quality report updater daemon
.PHONY: fetch-trends fetch-trust-urls ua-factory master

sync:
	$(UV) sync

lock:
	$(UV) lock

lint:
	$(UV) run ruff check py

fmt:
	$(UV) run ruff format py

fmt-check:
	$(UV) run ruff format --check py

runner:
	$(UV) run python py/runner.py

google:
	$(UV) run python py/mod_google.py

trust:
	$(UV) run python py/mod_trust.py

quality:
	$(UV) run python py/mod_quality.py

report:
	$(UV) run python py/report.py

updater:
	$(UV) run python py/updater.py

daemon:
	$(UV) run python py/agent_daemon.py

fetch-trends:
	$(UV) run python scripts/fetch_trends.py

fetch-trust-urls:
	$(UV) run python scripts/fetch_trust_urls.py

ua-factory:
	$(UV) run python scripts/ua_generator.py

master:
	$(UV) run python py/run_master.py
