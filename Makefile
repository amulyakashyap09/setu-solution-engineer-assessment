.PHONY: install run seed test lint docker-build docker-run clean

install:
	pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --port 8000

seed:
	python -m scripts.seed --file data/sample_events.json --database data/payments.db

reseed:
	python -m scripts.seed --file data/sample_events.json --database data/payments.db --reset

test:
	pytest

test-verbose:
	pytest -v

docker-build:
	docker build -t payment-reconciliation .

docker-run:
	docker run --rm -p 8000:8000 payment-reconciliation

clean:
	rm -f data/*.db data/*.db-wal data/*.db-shm
	find . -type d -name __pycache__ -exec rm -rf {} +
