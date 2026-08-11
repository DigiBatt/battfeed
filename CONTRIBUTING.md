# Contributing

## Development setup

1. Create and activate a virtual environment (on Windows, `py -3 -m venv .venv`).
2. Install development dependencies:

```bash
pip install -e ".[dev,parquet]"
```

The core has no third-party dependencies; extras are only needed for the sources and sinks you work on.

## Common commands

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
```

CI runs these on Linux and Windows across Python 3.10–3.13, including a no-extras leg that catches accidental hard imports of optional dependencies.

## Documentation

The docs site is built with Sphinx (MyST Markdown, pydata theme), structured along [Diátaxis](https://diataxis.fr/) lines, and deployed to GitHub Pages on every push to `main`. `CHANGELOG.md`, `ROADMAP.md`, and this file are rendered into the site from the repository root, so edit them here, not under `docs/`. To build locally:

```bash
pip install -e ".[docs]"
sphinx-build -W -b html docs site
```

## Contributing a source

New sources should pass `battfeed.testing.check_source`, follow the raise-on-trouble rule (no retry loops inside sources), and be developed replay-first against a recorded tape rather than live hardware — see the [custom-source tutorial](https://digibatt.github.io/battfeed/tutorials/custom-source.html) and `docs/project/development.md` for the design rules.

## Pull request checklist

- Add or update tests for behavior changes; hardware paths need a tape.
- Keep the core dependency-free (optional imports stay inside the source/sink that needs them).
- Keep public APIs backwards compatible unless explicitly planned.
- Update `CHANGELOG.md` for user-visible changes.
