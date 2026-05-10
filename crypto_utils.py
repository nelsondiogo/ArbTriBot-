"""
Utilitários de criptografia para proteger API keys armazenadas no banco.
Usa Fernet (AES-128-CBC + HMAC-SHA256) com chave derivada do FLASK_SECRET.
"""
import base64
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Salt fixo é aceitável aqui porque a chave mestra (FLASK_SECRET) já é secreta
_SALT = b"nelsondiogo-bot-v6-salt"


def _build_fernet(secret_key: str) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=390_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode()))
    return Fernet(key)


def encrypt(value: str, secret_key: str) -> str:
    """Criptografa uma string e retorna texto base64."""
    return _build_fernet(secret_key).encrypt(value.encode()).decode()


def decrypt(value: str, secret_key: str) -> str:
    """Descriptografa e retorna a string original. Lança InvalidToken se falhar."""
    try:
        return _build_fernet(secret_key).decrypt(value.encode()).decode()
    except InvalidToken:
        raise ValueError("Falha ao descriptografar: FLASK_SECRET pode ter mudado.")


def mask(value: str) -> str:
    """Retorna '••••••••…XXXX' para exibição segura no Dashboard."""
    if not value or len(value) < 8:
        return "••••••••"
    return "••••••••" + value[-4:]
