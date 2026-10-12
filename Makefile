PRETTIER = bunx --no-install prettier --no-error-on-unmatched-pattern
DOCS = "**/*.md" "**/*.json" "**/*.json5" "**/*.yaml" "**/*.yml" "**/*.toml" "**/*.html"
TEST_BROKER_REVISION = 99fa50f30e325609394c324c8ff71cfbbe95d8ab
TEST_BROKER_SOURCE = .build/mosquitto-source
TEST_BROKER_BUILD = .build/mosquitto-build
TEST_PATH = $(abspath $(TEST_BROKER_BUILD)/src):$(PATH)
IMAGE ?= tuya-mqtt:test
IMAGE_PLATFORM ?= linux/$(shell uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')
REVISION ?= $(shell git rev-parse HEAD)
SOURCE_DATE_EPOCH ?= $(shell git show -s --format=%ct HEAD)
CRANE_VERSION = 0.22.1
CRANE_SHA256_arm64 = 898c0cff975f898a33e8c4580bdafb0e7c02c7faa33374e946762f97c4ab7110
CRANE_SHA256_x86_64 = 0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0
CRANE_ARCH = $(shell uname -m | sed 's/aarch64/arm64/')
export REVISION

.PHONY: setup build run fmt lint test integration test-broker test-broker-deps image test-image image-export release-tools release

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

image:
	docker buildx build --network host --platform "$(IMAGE_PLATFORM)" --provenance=false --build-arg REVISION="$(REVISION)" --build-arg SOURCE_DATE_EPOCH="$(SOURCE_DATE_EPOCH)" --output type=docker,rewrite-timestamp=true --tag "$(IMAGE)" .

test-image:
	PATH="$(TEST_PATH)" TUCSON_IMAGE="$(IMAGE)" TUCSON_REVISION="$(REVISION)" TUCSON_PLATFORM="$(IMAGE_PLATFORM)" uv run --frozen python -m pytest -p tests.broker -p tests.tcp image

image-export:
	mkdir -p .build/images
	docker image save --output ".build/images/$(subst linux/,,$(IMAGE_PLATFORM)).tar" "$(IMAGE)"

release-tools:
	mkdir -p .build/tools
	curl --fail --silent --show-error --location "https://github.com/google/go-containerregistry/releases/download/v$(CRANE_VERSION)/go-containerregistry_Linux_$(CRANE_ARCH).tar.gz" --output .build/tools/crane.tar.gz
	printf '%s  %s\n' '$(CRANE_SHA256_$(CRANE_ARCH))' '.build/tools/crane.tar.gz' | sha256sum --check
	tar -xzf .build/tools/crane.tar.gz -C .build/tools crane

release:
	@set -eu; \
	printf '%s\n' "$$RELEASE_TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$$'; \
	printf '%s\n' "$$REVISION" | grep -Eq '^[0-9a-f]{40}$$'; \
	test -n "$$IMAGE_REPOSITORY"; test -n "$$RUN_SUFFIX"; \
	for arch in amd64 arm64; do test -f ".build/images/$$arch.tar"; done; \
	mkdir -p .build/release; \
	staging="$$IMAGE_REPOSITORY:build-$$RUN_SUFFIX"; \
	for arch in amd64 arm64; do \
		crane push ".build/images/$$arch.tar" "$$staging-$$arch"; \
		crane config "$$staging-$$arch" > ".build/release/$$arch.json"; \
		jq --exit-status --arg revision "$$REVISION" --arg arch "$$arch" '.architecture == $$arch and .os == "linux" and .config.Labels["org.opencontainers.image.revision"] == $$revision' ".build/release/$$arch.json"; \
	done; \
	crane index append --manifest "$$staging-amd64" --manifest "$$staging-arm64" --tag "$$staging"; \
	digest=$$(crane digest "$$staging"); \
	crane ls "$$IMAGE_REPOSITORY" > .build/release/tags; \
	for tag in "sha-$$REVISION" "$$RELEASE_TAG"; do \
		if grep -Fxq -- "$$tag" .build/release/tags; then \
			existing=$$(crane digest "$$IMAGE_REPOSITORY:$$tag"); \
			test "$$existing" = "$$digest" || { printf 'Refusing to replace immutable tag %s\n' "$$tag" >&2; exit 1; }; \
		fi; \
	done; \
	for tag in "sha-$$REVISION" "$$RELEASE_TAG"; do crane tag "$$IMAGE_REPOSITORY@$$digest" "$$tag"; done

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
