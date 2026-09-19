"""Root-only renewal hook with fixed source/destination and one allowed reload."""
import os
from pathlib import Path
import pwd
import shutil
import ssl
import subprocess
import tempfile


def main():
    if os.geteuid() != 0:
        raise PermissionError("Root required")
    source = Path("/etc/letsencrypt/live/ygo-admin-ip")
    target = Path("/etc/ygo-admin/tls")
    group = pwd.getpwnam("ygo-admin").pw_gid
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(source / "fullchain.pem", source / "privkey.pem")
    target.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(target, 0, group)
    target.chmod(0o750)
    for name in ("fullchain.pem", "privkey.pem"):
        with tempfile.NamedTemporaryFile(dir=target, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((source / name).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, group)
        temporary.chmod(0o640)
        os.replace(temporary, target / name)
    if subprocess.run(["systemctl", "is-active", "--quiet", "ygo-admin-web.service"]).returncode == 0:
        subprocess.run(["systemctl", "reload", "ygo-admin-web.service"], check=True, timeout=15)
    print("CERTIFICATE_DEPLOYED")


if __name__ == "__main__":
    main()
