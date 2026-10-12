PRETTIER = bunx --no-install prettier --no-error-on-unmatched-pattern
DOCS = "**/*.md" "**/*.json" "**/*.json5" "**/*.yaml" "**/*.yml" "**/*.toml" "**/*.html"
TEST_BROKER_REVISION = 99fa50f30e325609394c324c8ff71cfbbe95d8ab
TEST_BROKER_SOURCE = .build/mosquitto-source
TEST_BROKER_BUILD = .build/mosquitto-build
TEST_PATH = $(abspath $(TEST_BROKER_BUILD)/src):$(PATH)

.PHONY: setup build run fmt lint test integration test-broker test-broker-deps

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
	PATH="$(TEST_PATH)" uv run --frozen pytest --cov --cov-branch --cov-report=term-missing

integration:
	PATH="$(TEST_PATH)" uv run --frozen pytest -m integration

test-broker-deps:
	sudo apt-get update
	sudo apt-get install --yes --no-install-recommends build-essential cmake libssl-dev libcjson-dev

test-broker:
	mkdir -p $(TEST_BROKER_SOURCE)
	git -C $(TEST_BROKER_SOURCE) init
	git -C $(TEST_BROKER_SOURCE) fetch --depth 1 https://github.com/eclipse-mosquitto/mosquitto.git $(TEST_BROKER_REVISION)
	git -C $(TEST_BROKER_SOURCE) checkout --detach FETCH_HEAD
	test "$$(git -C $(TEST_BROKER_SOURCE) rev-parse HEAD)" = "$(TEST_BROKER_REVISION)"
	git -C $(TEST_BROKER_SOURCE) diff --exit-code HEAD
	cmake -S $(TEST_BROKER_SOURCE) -B $(TEST_BROKER_BUILD) -DWITH_TESTS=OFF -DWITH_DOCS=OFF -DWITH_APPS=OFF -DWITH_PLUGINS=OFF -DWITH_CLIENTS=OFF -DWITH_CTRL_SHELL=OFF
	cmake --build $(TEST_BROKER_BUILD) --target mosquitto --parallel
