.PHONY: check install

# Run the full test suite (quiet output, summary at the end).
check:
	python -m pytest -q

# Install all dev dependencies (use before running check in a fresh clone).
install:
	python -m pip install -r requirements-dev.txt
