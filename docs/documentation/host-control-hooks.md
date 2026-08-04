# Hook Tooling Setup

Dockkeep runs hooks inside the Dockkeep container. A hook script can only use
tools that are installed in the image, mounted into the container, or reached
through an external control channel such as SSH.

Use this guide to choose and set up the right path for host-control hooks.

## 1. Check The Included Tools

The default image includes Restic, Rclone, and these hook-friendly clients:

```text
bash-completion
ca-certificates
curl
default-mysql-client
docker-ce-cli
docker-compose-plugin
gosu
jq
openssh-client
postgresql-client
redis-tools
sqlite3
```

Open a shell in the running container and check the tool you want to use:

```sh
docker exec -it dockkeep sh
ssh -V
docker --version
docker compose version
pg_dump --version
mysqldump --version
redis-cli --version
sqlite3 --version
jq --version
```

If a command is missing, use a custom image. If the command exists but needs to
control the host, continue with Docker socket control or SSH control.

## 2. Choose A Host-Control Method

Use Docker socket control when the hook only needs to run Docker or Docker
Compose commands on the host and the Dockkeep container is allowed to control
the host Docker daemon.

Use SSH control when you want a narrower host-side allowlist, or when the hook
needs host tools that should stay outside the Dockkeep image.

Use a custom image when the hook needs extra client binaries inside the
Dockkeep container.

## 3. Set Up Docker Socket Control

Mount the hook scripts directory and the Docker socket:

```yaml
services:
  app:
    volumes:
      - ./scripts:/scripts
      - /var/run/docker.sock:/var/run/docker.sock
```

If the hook refers to host paths, mount those paths into Dockkeep too. The path
used by the script must be the container path:

```yaml
services:
  app:
    volumes:
      - ./scripts:/scripts
      - /var/run/docker.sock:/var/run/docker.sock
      - /mnt/cache/appdata:/mnt/cache/appdata:ro
```

Create an executable hook script:

```sh
mkdir -p scripts
nano scripts/paperless-compose.sh
chmod +x scripts/paperless-compose.sh
```

Example content:

```sh
#!/bin/sh
set -eu

exec docker compose -p paperless \
  -f /mnt/cache/appdata/Paperless-ngx-Stack/docker-compose.yml \
  "$1"
```

Test it from inside the container:

```sh
docker exec -it dockkeep sh
/scripts/paperless-compose.sh stop
/scripts/paperless-compose.sh start
```

Then configure the hooks:

```toml
pre_hooks      = ["/scripts/paperless-compose.sh stop"]
post_hooks     = ["/scripts/paperless-compose.sh start"]
on_error_hooks = ["/scripts/paperless-compose.sh start"]
```

If Dockkeep runs with `PUID`/`PGID`, that UID/GID must be able to access the
mounted Docker socket. Check from inside the container:

```sh
docker ps
```

The Docker socket gives Dockkeep broad control over the host Docker daemon. Use
this only where that trust boundary is acceptable.

## 4. Set Up SSH Control

Use SSH when you want the host to decide exactly which commands Dockkeep may
run. The Dockkeep image already includes `openssh-client`; Docker, Podman,
systemd, database tools, or service scripts can stay on the host.

The detailed setup is in
[`ssh-host-control-hooks.md`](ssh-host-control-hooks.md). Follow that guide to:

1. Create a restricted host user.
2. Add a forced-command allowlist.
3. Mount one private key and `known_hosts` into Dockkeep.
4. Call the approved host commands from hook scripts.

After setup, a hook script usually looks like this:

```sh
#!/bin/sh
set -eu

exec ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal "paperless $1"
```

## 5. Build A Custom Image For Extra Tools

If a hook needs a client that is not included in the default image, build a
small image on top of Dockkeep:

```dockerfile
FROM ghcr.io/davidkrgh/dockkeep:latest

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       borgbackup \
       resticprofile \
    && rm -rf /var/lib/apt/lists/*
```

Use the custom image in Compose:

```yaml
services:
  app:
    build: .
```

Recreate the container and verify the new tool from inside Dockkeep:

```sh
docker compose up -d --build
docker exec -it dockkeep sh
borg --version
```

## Troubleshooting

If a hook logs `not found`, the script started but the command inside it is not
available in the container or on the selected control channel.

If `docker ps` fails inside Dockkeep, the Docker CLI is present but the mounted
socket is missing or not accessible to the container user.

If a script refers to a host path and the command says the path is missing,
mount that host path into the container and use the container-side path in the
script.
