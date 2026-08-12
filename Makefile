.PHONY: install verify test
install:
	python -m pip install -e '.[dev]'
verify:
	python scripts/verify_repo.py
test:
	python -m pytest -q
