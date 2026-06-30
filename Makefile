.PHONY: install test lint run infra-up infra-down agent-status agent-claim agent-submit

install:
	python3 -m pip install -e ".[dev,api]"

test:
	python3 -m pytest

lint:
	python3 -m ruff check .
	python3 -m mypy src

run:
	python3 -m uvicorn memory_plane.api.app:create_app --factory --reload

infra-up:
	docker compose up -d

infra-down:
	docker compose down

agent-status:
	./scripts/agent-status.sh

agent-claim:
	@test -n "$(ISSUE)" -a -n "$(SLUG)" || (echo "Use ISSUE=<n> SLUG=<name>" && exit 1)
	./scripts/agent-claim.sh "$(ISSUE)" "$(SLUG)"

agent-submit:
	@test -n "$(ISSUE)" || (echo "Use ISSUE=<n>" && exit 1)
	./scripts/agent-submit.sh "$(ISSUE)"
