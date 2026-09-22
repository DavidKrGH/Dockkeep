# Base-Images auf Digest gepinnt, damit ein Rebuild desselben Commits dasselbe
# Artefakt ergibt. Der Tag bleibt zur Lesbarkeit stehen, bindend ist der Digest.
# Dependabot haelt die Digests aktuell.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic-bin
FROM rclone/rclone:v1.74-stable@sha256:2446d4214067d944640e0646a733b36c8d205542e2ed7d75c4729a7ec7443dd2 AS rclone-bin
FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv-bin

FROM python:3.12-bookworm@sha256:3cd9086bdb30f7c9bc08a3fa621d9842e0d3f6f9291aeb4677e0547817c10b12

COPY --from=restic-bin /usr/bin/restic /usr/local/bin/restic
COPY --from=rclone-bin /usr/local/bin/rclone /usr/local/libexec/rclone

# rclone laeuft ueber einen Wrapper, damit jeder Aufruf im Container dieselbe
# rclone.conf sieht wie Dockkeep. Noetig, weil `docker exec` das
# Entrypoint-Skript nicht durchlaeuft und RCLONE_CONFIG dort deshalb fehlt;
# ohne Wrapper wuerde `docker exec ... rclone config` in rclones eigene
# Default-Config unter ~/.config/rclone/rclone.conf schreiben. Ein explizit
# gesetztes RCLONE_CONFIG bleibt unangetastet.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'if [ -z "${RCLONE_CONFIG}" ]; then' \
    '    RCLONE_CONFIG="${DK_CONFIG_DIR:-/config}/rclone.conf"' \
    '    export RCLONE_CONFIG' \
    'fi' \
    'exec /usr/local/libexec/rclone "$@"' \
    > /usr/local/bin/rclone \
    && chmod 0755 /usr/local/bin/rclone

# gosu für optionalen non-root Modus und Basis-Tools für Hooks
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    bash-completion \
    openssh-client \
    postgresql-client \
    default-mysql-client \
    redis-tools \
    sqlite3 \
    curl \
    jq \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI und Compose v2 Plugin für optionale Docker-Socket-Hooks
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Shell-History und Readline-Komfort aktivieren
RUN echo 'HISTSIZE=1000' >> /root/.bashrc \
    && echo 'HISTFILESIZE=2000' >> /root/.bashrc \
    && echo 'HISTCONTROL=ignoredups:erasedups' >> /root/.bashrc \
    && echo 'shopt -s histappend' >> /root/.bashrc \
    && echo '[ -f /usr/share/bash-completion/bash_completion ] && . /usr/share/bash-completion/bash_completion' >> /root/.bashrc

# Laufzeit-Dependencies installieren
# uv.lock ist die Quelle der Wahrheit fuer die Laufzeit-Dependencies. uv rendert
# daraus eine exakt gepinnte Liste mit Hashes, pip installiert sie hash-geprueft.
# uv wird nur fuer diesen Schritt eingeblendet und landet in keiner Image-Layer.
# `--locked` bricht ab, wenn uv.lock nicht zu pyproject.toml passt; sonst wuerde
# eine neue Dependency wegen `--no-deps` still fehlen und erst zur Laufzeit
# auffallen.
COPY pyproject.toml uv.lock /app/
WORKDIR /app
RUN --mount=type=bind,from=uv-bin,source=/uv,target=/usr/local/bin/uv \
    uv export --locked --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.lock \
    && pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# Projekt selbst installieren. Bewusst nach den Dependencies, damit
# Code-Aenderungen den Dependency-Layer nicht invalidieren.
COPY LICENSE THIRD-PARTY-NOTICES.md /app/
COPY src/ /app/src/
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8080

# Entrypoint
COPY docker-entrypoint.sh /
RUN chmod +x /docker-entrypoint.sh

# Läuft standardmäßig als root.
# Non-root Betrieb: PUID und PGID setzen (siehe docker-entrypoint.sh).

ENTRYPOINT ["/docker-entrypoint.sh"]
