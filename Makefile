.PHONY: install dev google test lint fmt fixtures run doctor docker \
        shortcut start stop restart update status logs

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

# The Google client is optional: only needed for the central sheet.
google:
	pip install -e ".[google]"

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

# --- running it on this machine ---------------------------------------
# `make shortcut` installs the `awb` command; after that these are just
# awb start / awb restart / awb status from anywhere.

shortcut:
	./deploy/awbctl install

start:
	./deploy/awbctl start

stop:
	./deploy/awbctl stop

restart:
	./deploy/awbctl restart

update:
	./deploy/awbctl update

status:
	./deploy/awbctl status

logs:
	./deploy/awbctl logs
