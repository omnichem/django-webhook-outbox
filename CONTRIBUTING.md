# Contributing

Issues and pull requests are welcome at
<https://github.com/omnichem/django-webhook-outbox>.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/).

```console
uv sync --group dev                 # Python 3.12 by default (.python-version)
uv run pytest                       # SQLite, a few seconds
uv run ruff check . && uv run black --check .
uv run python -m django makemigrations --check --dry-run --settings=tests.settings --pythonpath=.
```

To run the tests against PostgreSQL, as CI does:

```console
uv sync --group dev --group postgres
DB_ENGINE=postgres POSTGRES_HOST=localhost POSTGRES_USER=postgres \
  POSTGRES_PASSWORD=postgres POSTGRES_DB=webhook_outbox uv run pytest
```

To try another Django version, run `uv pip install "django~=4.2.0"` and then
`uv run --no-sync pytest`.

## Ground rules

- **Behaviour is pinned.** 1.x keeps the semantics in
  [docs/semantics.md](docs/semantics.md). A change to detection, delivery or
  the envelope needs a discussion first, an entry in `CHANGELOG.md`, and an
  update to the `test_limitation_*` / golden tests that pin it.
- Model changes need a migration (`makemigrations webhook_outbox`). CI fails
  on missing migrations.
- New user-facing strings must be translatable. Update
  `src/webhook_outbox/locale/ru/LC_MESSAGES/django.po` and compile it
  (`django-admin compilemessages`, run from `src/webhook_outbox`).
- Do not put real URLs, hostnames, credentials or production data in code,
  tests or docs. Use `example.com`.
- Do not change the version anywhere. It comes from the git tag.

## Releasing (maintainers)

Push a tag `X.Y.Z` (numeric, no `v` prefix) on `master`. The `release`
workflow runs the tests, builds the sdist and wheel, and publishes to PyPI
through Trusted Publishing (environment `pypi`).
