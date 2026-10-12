PRETTIER = bunx --no-install prettier --no-error-on-unmatched-pattern
DOCS = "**/*.md" "**/*.json" "**/*.json5" "**/*.yaml" "**/*.yml" "**/*.toml"

.PHONY: setup build run fmt lint test integration

setup:
	uv sync --frozen
	bun install --frozen-lockfile
	bunx --no-install lefthook install

build:
	uv build

run:
	uv run --frozen tuya-mqtt run --config config.json

fmt:
	uv run --frozen ruff check --fix .
	uv run --frozen ruff format .
	$(PRETTIER) --write $(DOCS)

lint:
	uv run --frozen ruff format --check .
	uv run --frozen ruff check .
	uv run --frozen ty check src
	$(PRETTIER) --check $(DOCS)
	uv run --frozen zizmor --offline --persona pedantic .github/workflows

test:
	uv run --frozen pytest --cov --cov-branch --cov-report=term-missing

integration:
	uv run --frozen pytest -m integration
