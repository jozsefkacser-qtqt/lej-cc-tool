.PHONY: install dev test lint fmt fixtures run docker

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	PYTHONPATH=src pytest -q

lint:
	ruff check src tests && mypy src

fmt:
	ruff format src tests && ruff check --fix src tests

fixtures:
	python tests/make_fixtures.py

run:
	python -m lej_cc.main

docker:
	docker compose up --build -d
