"""
Подписать собранный agent.exe (запускается в GitHub Actions).

    SIGNING_KEY=<закрытый ключ base64> python tools/sign_release.py dist/agent.exe 0.3.0 dist/

Создаёт dist/manifest.json и dist/manifest.sig.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.update import verify_manifest  # noqa: E402

exe, version, outdir = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ["SIGNING_KEY"].strip()))
data = exe.read_bytes()
manifest = json.dumps({
    "version": version,
    "sha256": hashlib.sha256(data).hexdigest(),
    "size": len(data),
    "file": "agent.exe",
}, ensure_ascii=False).encode()
sig = base64.b64encode(key.sign(manifest)).decode()
(outdir / "manifest.json").write_bytes(manifest)
(outdir / "manifest.sig").write_text(sig)
# Самопроверка: подпись должна сходиться с открытым ключом в коде агента
verify_manifest(manifest, sig)
print(f"Подписано: версия {version}, {len(data)} байт")
