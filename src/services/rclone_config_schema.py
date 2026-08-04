"""Curated schemas for editing simple rclone remotes."""

from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["bool", "number", "password", "select", "text"]


@dataclass(frozen=True)
class RcloneField:
    """Describe one schema-supported rclone config field."""

    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    choices: tuple[str, ...] = ()
    placeholder: str = ""
    help_text: str = ""
    rclone_password: bool = False
    secret: bool = False


@dataclass(frozen=True)
class RcloneBackendSchema:
    """Describe one supported rclone backend."""

    key: str
    label: str
    description: str
    fields: tuple[RcloneField, ...]


def field(
    key: str,
    label: str,
    kind: FieldKind = "text",
    *,
    required: bool = False,
    choices: tuple[str, ...] = (),
    placeholder: str = "",
    help_text: str = "",
    rclone_password: bool = False,
    secret: bool = False,
) -> RcloneField:
    return RcloneField(
        key=key,
        label=label,
        kind=kind,
        required=required,
        choices=choices,
        placeholder=placeholder,
        help_text=help_text,
        rclone_password=rclone_password,
        secret=secret,
    )


SUPPORTED_BACKENDS: tuple[RcloneBackendSchema, ...] = (
    RcloneBackendSchema(
        key="sftp",
        label="SFTP",
        description="SSH/SFTP server or NAS.",
        fields=(
            field("host", "Host", required=True, placeholder="example.com"),
            field("user", "User", placeholder="backup"),
            field("port", "Port", "number", placeholder="22"),
            field("pass", "Password", "password", rclone_password=True),
            field("key_file", "Private key file", placeholder="/config/id_ed25519"),
            field("key_file_pass", "Key password", "password", rclone_password=True),
        ),
    ),
    RcloneBackendSchema(
        key="ftp",
        label="FTP",
        description="FTP server, optionally with TLS.",
        fields=(
            field("host", "Host", required=True, placeholder="ftp.example.com"),
            field("user", "User", placeholder="backup"),
            field("port", "Port", "number", placeholder="21"),
            field("pass", "Password", "password", rclone_password=True),
            field("tls", "Use TLS", "bool"),
            field("explicit_tls", "Explicit TLS", "bool"),
        ),
    ),
    RcloneBackendSchema(
        key="smb",
        label="SMB",
        description="Windows/NAS share.",
        fields=(
            field("host", "Host", required=True, placeholder="nas.local"),
            field("user", "User", placeholder="backup"),
            field("port", "Port", "number", placeholder="445"),
            field("pass", "Password", "password", rclone_password=True),
            field("domain", "Domain", placeholder="WORKGROUP"),
        ),
    ),
    RcloneBackendSchema(
        key="webdav",
        label="WebDAV",
        description="Nextcloud, Owncloud, SharePoint, or generic WebDAV.",
        fields=(
            field(
                "url",
                "URL",
                required=True,
                placeholder="https://cloud.example.com/remote.php/dav/files/user",
            ),
            field(
                "vendor",
                "Provider",
                "select",
                choices=("nextcloud", "owncloud", "sharepoint", "other"),
            ),
            field("user", "User", placeholder="backup"),
            field("pass", "Password", "password", rclone_password=True),
            field("bearer_token", "Bearer Token", "password", secret=True),
        ),
    ),
    RcloneBackendSchema(
        key="s3",
        label="S3",
        description="S3-compatible storage such as AWS, MinIO, Wasabi, or Cloudflare R2.",
        fields=(
            field(
                "provider",
                "Provider",
                "select",
                choices=("AWS", "Minio", "Wasabi", "Cloudflare", "Other"),
                required=True,
            ),
            field("env_auth", "Credentials from environment", "bool"),
            field("access_key_id", "Access Key ID", placeholder="AKIA..."),
            field("secret_access_key", "Secret Access Key", "password", secret=True),
            field("region", "Region", placeholder="eu-central-1"),
            field("endpoint", "Endpoint", placeholder="https://s3.example.com"),
        ),
    ),
    RcloneBackendSchema(
        key="alias",
        label="Alias",
        description="Alias for an existing remote or local path.",
        fields=(field("remote", "Target", required=True, placeholder="remote:path"),),
    ),
    RcloneBackendSchema(
        key="crypt",
        label="Crypt",
        description="Encryption layer over an existing remote.",
        fields=(
            field("remote", "Target", required=True, placeholder="remote:path"),
            field(
                "filename_encryption",
                "Filename encryption",
                "select",
                choices=("standard", "obfuscate", "off"),
            ),
            field("directory_name_encryption", "Encrypt directory names", "bool"),
            field("password", "Password", "password", required=True, rclone_password=True),
            field("password2", "Salt/second password", "password", rclone_password=True),
        ),
    ),
)

BACKEND_SCHEMAS: dict[str, RcloneBackendSchema] = {
    schema.key: schema for schema in SUPPORTED_BACKENDS
}


def supported_backend_types() -> tuple[str, ...]:
    return tuple(schema.key for schema in SUPPORTED_BACKENDS)


def get_backend_schema(remote_type: str) -> RcloneBackendSchema | None:
    return BACKEND_SCHEMAS.get(remote_type)


def backend_choices() -> list[dict[str, str]]:
    return [
        {"value": schema.key, "label": schema.label, "description": schema.description}
        for schema in SUPPORTED_BACKENDS
    ]
