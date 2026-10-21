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

FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH
WORKDIR /build
RUN uv python install 3.15.0
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

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:b7e81cb1af38d9345680e68bcf564d388f96304e25222030fc8c051faf8565b3
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
