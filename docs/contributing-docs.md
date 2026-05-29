# Documentation Conventions

## Docstring standard
- Use **Google-style** docstrings.
- Start with a one-line summary, followed by a blank line and sections such as
  `Args:`, `Returns:`, `Raises:`, `Side Effects:`, and `Async:`.
- Every top-level module should include a module docstring describing purpose,
  key flows, and external dependencies.
- Public or complex call paths should document parameters, return values, side
  effects, and async behavior.

## Documentation generation
The recommended generator is **Sphinx** with **autodoc** and **napoleon** so
Google-style docstrings render correctly.

Suggested steps (one-time setup, if not already present):
1. `pip install sphinx`
2. `sphinx-quickstart docs/_sphinx`
3. Enable extensions in `docs/_sphinx/conf.py`:
   - `sphinx.ext.autodoc`
   - `sphinx.ext.napoleon`
4. Generate API stubs:
   - `sphinx-apidoc -o docs/_sphinx/api .`
5. Build HTML docs:
   - `sphinx-build -b html docs/_sphinx docs/_build/html`
