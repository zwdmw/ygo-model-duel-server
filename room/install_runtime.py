"""Install only into a new private room directory; never alter training packages."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def main():
    root = Path(__file__).resolve().parent
    app = root / "app"
    if app.exists():
        raise RuntimeError("Runtime directory already exists; refusing to overwrite")
    app.mkdir(mode=0o700)
    with tarfile.open(root / "runtime.tar.gz") as archive:
        for member in archive.getmembers():
            path = (app / member.name).resolve()
            if not path.is_relative_to(app) or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe runtime archive member")
        archive.extractall(app)
    for name in ("server.py", "worker.py", "relay.py", "deck_config.py", "policy_ipc.py",
                 "native-build.json"):
        shutil.copy2(root / name, app / name)
    shutil.copy2(root / "portable_ygoenv.so",
                 app / "project/ygoenv/ygopro/ygopro_ygoenv.cpython-311-x86_64-linux-gnu.so")
    (app / "bin").mkdir()
    shutil.copy2(root / "mono", app / "bin/mono")
    (app / "bin/mono").chmod(0o755)
    (root / "data").mkdir(mode=0o700, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(root / "venv")], check=True)
    python = root / "venv/bin/python"
    subprocess.run([str(python), "-m", "pip", "install", "--index-url",
                    "https://pypi.tuna.tsinghua.edu.cn/simple", "--timeout", "25",
                    "--retries", "2", "-r", str(root / "requirements-model.txt"),
                    "-r", str(root / "requirements-gateway.txt")], check=True)
    print(json.dumps({"installed": str(root), "python": str(python)}), flush=True)


if __name__ == "__main__":
    main()
