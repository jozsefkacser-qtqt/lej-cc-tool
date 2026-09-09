.PHONY: install dev test lint fmt fixtures run doctor docker

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	PYTHONPATH=src python -m pytest -q

lint:
	python -m ruff check src tests && python -m mypy src

fmt:
	ruff format src tests && ruff check --fix src tests

fixtures:
	python tests/make_fixtures.py

run:
	python -m lej_cc.main

doctor:
	python -m lej_cc.doctor

docker:
	docker compose up --build -d
