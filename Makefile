.PHONY: install lint format typecheck test up down migrate revision run

install:
	pip install -e ".[dev]"

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy app

test:
	pytest -m "not integration"

test-all:
	pytest

up:
	docker compose up -d db

down:
	docker compose down

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"

run:
	uvicorn app.main:app --reload
