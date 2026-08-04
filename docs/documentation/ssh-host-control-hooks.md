# SSH Hook Control Setup

This guide sets up one restricted SSH key that lets Dockkeep run approved
commands on the Docker host. Dockkeep only receives the private key. Docker,
Compose, systemd, database tools, and service scripts stay on the host.

The examples use Paperless start/stop commands. Replace the service name,
Compose project, and paths with your own host commands.

## 1. Prepare A Restricted Host User

Run these commands on the Docker host:

```sh
sudo useradd -m -s /bin/sh dockkeep-control
sudo passwd -l dockkeep-control
```

Use a real shell such as `/bin/sh`. SSH forced commands are executed through the
account shell, so `/usr/sbin/nologin` is not suitable here.

## 2. Create The Host Command Script

Create a root-owned script that performs the real host operation:

```sh
sudo nano /usr/local/bin/dockkeep-paperless
```

Use this content:

```sh
#!/bin/sh
set -eu

case "${1:-}" in
  start|stop)
    exec docker compose -p paperless \
      -f /mnt/cache/appdata/Paperless-ngx-Stack/docker-compose.yml \
      "$1"
    ;;
  *)
    echo "usage: dockkeep-paperless {start|stop}" >&2
    exit 64
    ;;
esac
```

Lock the file down:

```sh
sudo chown root:root /usr/local/bin/dockkeep-paperless
sudo chmod 755 /usr/local/bin/dockkeep-paperless
```

Test the script on the host:

```sh
sudo /usr/local/bin/dockkeep-paperless stop
sudo /usr/local/bin/dockkeep-paperless start
```

## 3. Create The SSH Allowlist Wrapper

Create the forced-command wrapper:

```sh
sudo nano /usr/local/bin/dockkeep-control
```

Use this content:

```sh
#!/bin/sh
set -eu

case "${SSH_ORIGINAL_COMMAND:-}" in
  "paperless stop")
    exec sudo /usr/local/bin/dockkeep-paperless stop
    ;;
  "paperless start")
    exec sudo /usr/local/bin/dockkeep-paperless start
    ;;
  *)
    echo "command not allowed" >&2
    exit 126
    ;;
esac
```

Lock the wrapper down:

```sh
sudo chown root:root /usr/local/bin/dockkeep-control
sudo chmod 755 /usr/local/bin/dockkeep-control
```

The wrapper must match complete commands from `SSH_ORIGINAL_COMMAND`. Do not
forward arbitrary SSH arguments to `sudo`.

## 4. Allow Only The Needed Sudo Commands

Create a sudoers file:

```sh
sudo visudo -f /etc/sudoers.d/dockkeep-control
```

Add this line:

```sudoers
dockkeep-control ALL=(root) NOPASSWD: /usr/local/bin/dockkeep-paperless stop, /usr/local/bin/dockkeep-paperless start
```

This lets `dockkeep-control` run only the listed script and argument
combinations as root without a password. Keep every listed script owned by
`root` and not writable by the `dockkeep-control` user.

## 5. Generate The Dockkeep SSH Key

Run this next to your `docker-compose.yml`:

```sh
mkdir -p secrets
ssh-keygen -t ed25519 -f secrets/dockkeep_control_key -N ""
chmod 600 secrets/dockkeep_control_key
touch secrets/known_hosts
```

Show the public key. You need it in the next step:

```sh
cat secrets/dockkeep_control_key.pub
```

## 6. Install The Public Key On The Host

Prepare `authorized_keys` for the restricted host user:

```sh
sudo mkdir -p /home/dockkeep-control/.ssh
sudo nano /home/dockkeep-control/.ssh/authorized_keys
```

Add the public key on one line with this forced-command prefix:

```text
command="/usr/local/bin/dockkeep-control",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```

Then set SSH permissions:

```sh
sudo chown -R dockkeep-control:dockkeep-control /home/dockkeep-control/.ssh
sudo chmod 700 /home/dockkeep-control/.ssh
sudo chmod 600 /home/dockkeep-control/.ssh/authorized_keys
```

## 7. Mount The Key Into Dockkeep

Add the private key and `known_hosts` file to the Dockkeep service:

```yaml
services:
  app:
    volumes:
      - ./secrets/dockkeep_control_key:/config/ssh/dockkeep_control_key:ro
      - ./secrets/known_hosts:/config/ssh/known_hosts
```

The private key must be readable by the Dockkeep container user, and
`known_hosts` must be writable so `StrictHostKeyChecking=accept-new` can store
the host key. If Dockkeep runs with `PUID`/`PGID`, grant that UID/GID access to
both files.

On Linux, add this so Dockkeep can reach the host as `host.docker.internal`:

```yaml
services:
  app:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Recreate the container after changing Compose:

```sh
docker compose up -d
```

## 8. Test SSH From Inside Dockkeep

Open a shell in the Dockkeep container:

```sh
docker exec -it dockkeep sh
```

Run an allowed command:

```sh
ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal paperless stop
```

Start the service again:

```sh
ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal paperless start
```

Verify that unapproved commands are rejected:

```sh
ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal whoami
```

Expected output:

```text
command not allowed
```

## 9. Create The Dockkeep Hook Script

Create the hook script in the mounted `/scripts` directory:

```sh
mkdir -p scripts
nano scripts/paperless-ssh.sh
chmod +x scripts/paperless-ssh.sh
```

Use this content:

```sh
#!/bin/sh
set -eu

exec ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal "paperless $1"
```

Test the script from inside Dockkeep:

```sh
docker exec -it dockkeep sh
/scripts/paperless-ssh.sh stop
/scripts/paperless-ssh.sh start
```

## 10. Configure Dockkeep Hooks

Add the script to the relevant job, backup, workflow, or rclone task:

```toml
pre_hooks      = ["/scripts/paperless-ssh.sh stop"]
post_hooks     = ["/scripts/paperless-ssh.sh start"]
on_error_hooks = ["/scripts/paperless-ssh.sh start"]
```

`on_error_hooks` starts Paperless again if the backup or another step fails
after the stop hook has already run.

## Add More Commands

To add another allowed command, for example `paperless db-dump`:

1. Add a strict argument branch to `/usr/local/bin/dockkeep-paperless`, or
   create another root-owned service script.
2. Add an exact `case` entry to `/usr/local/bin/dockkeep-control`.
3. Add the exact script and argument combination to
   `/etc/sudoers.d/dockkeep-control`.
4. Test the command from inside Dockkeep:

```sh
ssh -i /config/ssh/dockkeep_control_key \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/config/ssh/known_hosts \
  dockkeep-control@host.docker.internal paperless db-dump
```

This keeps one SSH key while still enforcing a strict host-side command
allowlist.

## Troubleshooting

If SSH rejects the key, check that `secrets/dockkeep_control_key` is not
group- or world-readable and is readable by the Dockkeep container user.

If `accept-new` cannot store the host key, check that
`/config/ssh/known_hosts` is mounted and writable inside the container.

If every command returns `command not allowed`, compare the exact SSH command
with the `case` entries in `/usr/local/bin/dockkeep-control`.
