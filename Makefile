.PHONY: install test lint run infra-up infra-down

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
