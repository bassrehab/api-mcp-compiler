.PHONY: install verify test golden docs docs-serve

install:
	python -m pip install -e '.[dev]'

verify:
	python scripts/verify_repo.py

test:
	python -m pytest -q

# Regenerates the golden artifacts and the notebook's stored outputs together, so one run
# produces the whole diff to review. Read that diff: an unexplained change was not intended.
golden:
	python scripts/regen_golden.py

docs:
	python -m pip install '.[docs]'
	mkdocs build --strict

docs-serve:
	mkdocs serve
