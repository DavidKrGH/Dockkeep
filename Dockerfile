FROM restic/restic:0.19.1 AS restic-bin
FROM rclone/rclone:v1.74-stable AS rclone-bin

FROM python:3.14-bookworm

COPY --from=restic-bin /usr/bin/restic /usr/local/bin/restic
COPY --from=rclone-bin /usr/local/bin/rclone /usr/local/bin/rclone

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

# Python Dependencies & Paket installieren
COPY requirements.txt pyproject.toml LICENSE THIRD-PARTY-NOTICES.md /app/
COPY src/ /app/src/
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

EXPOSE 8080

# Entrypoint
COPY docker-entrypoint.sh /
RUN chmod +x /docker-entrypoint.sh

# Läuft standardmäßig als root.
# Non-root Betrieb: PUID und PGID setzen (siehe docker-entrypoint.sh).

ENTRYPOINT ["/docker-entrypoint.sh"]
