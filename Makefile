.PHONY: check install

# Pick a Python that actually has the dev deps installed, in order:
#   1. an activated virtualenv ($VIRTUAL_ENV)
#   2. a local .venv/ in the repo
#   3. the system python3 (this is the CI case — deps are installed into it)
# This keeps `make check` working whether or not a venv is activated, so the
# pre-push and Claude Stop hooks don't false-fail just because no venv is active.
PYTHON := $(shell \
	if [ -n "$$VIRTUAL_ENV" ] && [ -x "$$VIRTUAL_ENV/bin/python" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
	elif [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	else echo python3; fi)

# Run the full test suite (quiet output, summary at the end).
check:
	$(PYTHON) -m pytest -q

# Install all dev dependencies (use before running check in a fresh clone).
install:
	$(PYTHON) -m pip install -r requirements-dev.txt
