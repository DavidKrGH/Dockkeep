# Security Policy

## Supported versions

Fixes go into the latest release. There are no backports to earlier tags.

| Version | Supported |
|---|---|
| 1.0.x   | Yes       |
| < 1.0   | No        |

## Reporting a vulnerability

Report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/DavidKrGH/Dockkeep/security/advisories/new)
— the *Security* tab of this repository, *Report a vulnerability*. Please do not
open a public issue for a suspected vulnerability.

Useful in a report: the affected version or image tag, the configuration
involved with credentials removed, the steps to reproduce, and what an attacker
gains. Dockkeep is maintained by one person in their spare time — expect a first
response within about two weeks. There is no bounty program.

## Scope

Dockkeep is an administrative tool for a trusted local network: a home server, a
homelab, a NAS. It is not designed to be exposed to the internet, and the
following are documented design decisions rather than vulnerabilities:

- **The web UI has no authentication.** Anyone who can reach the port has full
  control. Putting it behind a VPN or an authenticating reverse proxy is a
  deployment decision, not something Dockkeep does for you.
- **Repository passwords and Rclone credentials are readable** to anyone who can
  reach the UI or read the mounted config and log directories. The config file
  holds them by design.
- **Hooks run commands from the configuration.** Anyone who can edit
  `config.toml` can already run code in the container; inline hooks additionally
  require `DK_ALLOW_INLINE_HOOKS=true`.
- **The run-control Unix socket** (`$DK_APPDATA_DIR/run-control.sock`, mode
  `0600`) is local IPC between Dockkeep's own processes, not a security
  boundary.

What is in scope is anything that breaks out of the boundaries Dockkeep does
promise to enforce, for example:

- A restore that writes outside the restore base (`$DK_RESTORE_DIR`).
- Path traversal in the log viewer or the repository browser.
- A hook path that escapes `$DK_SCRIPTS_DIR` despite validation.
- Credentials leaking off the machine — into notifications, into the published
  image, or into anything sent to a third party.
- A vulnerability introduced by the published image itself, beyond what the
  upstream base image and dependencies carry.

If you are unsure whether something is in scope, report it privately anyway.
