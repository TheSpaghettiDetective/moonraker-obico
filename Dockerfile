# Build the venv
FROM python:3-bookworm as build

WORKDIR /opt

COPY requirements.txt .
RUN python -m venv venv \
 && venv/bin/pip install -r requirements.txt

# Runtime Image
FROM python:3-slim-bookworm

RUN apt update \
 && apt install -y janus ffmpeg \
 && apt clean

WORKDIR /opt
RUN groupadd obico --gid 1000 \
 && useradd obico --uid 1000 --gid obico \
 && usermod obico --append --groups video
RUN mkdir -p printer_data/config printer_data/logs \
 && chown -R obico:obico /opt/*

COPY --chown=obico:obico --from=build /opt/venv venv
COPY --chown=obico:obico . moonraker-obico

USER obico
ENV PYTHONPATH=/opt/moonraker-obico
VOLUME ["/opt/printer_data/config", "/opt/printer_data/logs"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "moonraker_obico.app"]
CMD ["-c", "/opt/printer_data/config/moonraker-obico.cfg"]