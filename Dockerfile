# Copyright © 2026 Michael Shields
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM debian:trixie-slim@sha256:89c1ea91e897f0fe0a3731595330bc45b8bdef4047464a07116f891a94054b31 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH
WORKDIR /build
RUN uv python install 3.15.2
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# uv source cache metadata embeds timestamps and has no runtime consumer.
RUN /opt/app/bin/python - <<'PY'
import csv
from pathlib import Path
from sysconfig import get_path

for cache in Path(get_path("purelib")).glob("*.dist-info/uv_cache.json"):
    record = cache.with_name("RECORD")
    relative = cache.relative_to(cache.parent.parent).as_posix()
    rows = list(csv.reader(record.read_text().splitlines()))
    with record.open("w", newline="") as stream:
        csv.writer(stream).writerows(row for row in rows if row[0] != relative)
    cache.unlink()
PY

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:3ee00291cf386ec52a552e6a858d555debd74776eb4bfa53fa3df2cd6677515e
COPY --from=build /opt/python /opt/python
COPY --from=build /opt/app /opt/app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG REVISION
ARG VERSION
ENV TUYA_MQTT_REVISION=$REVISION TUYA_MQTT_VERSION=$VERSION
LABEL org.opencontainers.image.source="https://github.com/shields/tucson" \
      org.opencontainers.image.revision=$REVISION \
      org.opencontainers.image.version=$VERSION
USER 65532:65532
WORKDIR /app
EXPOSE 8080
ENTRYPOINT ["/opt/app/bin/python", "-m", "tuya_mqtt"]
CMD ["run", "--config", "/config/config.json"]
