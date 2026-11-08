"""
Создать пару ключей для подписи обновлений агента (делается ОДИН раз).

Закрытый ключ → секрет GitHub AGENT_SIGNING_KEY (и резервная копия у учителя).
Открытый ключ → shared/update.py (PUBLIC_KEY_B64), попадает в agent.exe.
"""
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("agent-signing-key.txt")
if out.exists():
    sys.exit(f"{out} уже существует — не перезаписываю")
key = Ed25519PrivateKey.generate()
priv = base64.b64encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                          serialization.NoEncryption())).decode()
pub = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,
                                                     serialization.PublicFormat.Raw)).decode()
out.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(priv + "\n")
print("PUBLIC", pub)
