"""Сертификаты для HTTPS-запросов к ЕИС.

zakupki.gov.ru использует сертификат Минцифры («Russian Trusted Root CA»), которого нет
в стандартном наборе Python. Два способа доверять ему, только для запросов Джарвиса к ЕИС:

0. Официальные сертификаты Минцифры (с gosuslugi.ru/crt) уже лежат в assets/certs и подключаются сами.
1. Положить файлы сертификатов (.cer, .crt или .pem) в папку %APPDATA%\\Jarvis\\certs
   или указать путь к файлу или папке в настройках. Джарвис объединит их со стандартным
   набором certifi в отдельный файл и будет использовать его только для ЕИС.
2. Если своих сертификатов нет, проверять сайт по хранилищу сертификатов Windows
   (через truststore). Это сработает, если Минцифры уже установлен в Windows.
"""

from __future__ import annotations

import re
import ssl
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from .config import app_data_dir

CERT_SUFFIXES = {".cer", ".crt", ".pem"}
_PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def certs_dir() -> Path:
    path = app_data_dir() / "certs"
    path.mkdir(parents=True, exist_ok=True)
    return path


BUNDLED_DIR = Path(__file__).resolve().parents[1] / "assets" / "certs"


def find_custom_certs(extra: str = "") -> list[Path]:
    """Сертификаты Минцифры, вложенные в приложение, папка certs пользователя и путь из настроек."""
    found: list[Path] = []
    candidates: list[Path] = [BUNDLED_DIR, certs_dir()]
    if extra and extra.strip():
        candidates.append(Path(extra.strip().strip('"')))
    for item in candidates:
        if item.is_file() and item.suffix.lower() in CERT_SUFFIXES:
            found.append(item)
        elif item.is_dir():
            found.extend(sorted(p for p in item.iterdir() if p.is_file() and p.suffix.lower() in CERT_SUFFIXES))
    unique: list[Path] = []
    for path in found:
        if path.resolve() not in {p.resolve() for p in unique}:
            unique.append(path)
    return unique


def load_pem(path: Path) -> str:
    """Читает сертификат в формате PEM или DER и возвращает PEM."""
    data = path.read_bytes()
    blocks = _PEM_RE.findall(data)
    if blocks:
        return "\n".join(b.decode("ascii", errors="ignore").replace("\r\n", "\n") for b in blocks) + "\n"
    # DER начинается с ASN.1 SEQUENCE (0x30); иначе это не сертификат
    if not data.startswith(b"\x30"):
        raise ValueError(f"{path.name} не похож на сертификат")
    return ssl.DER_cert_to_PEM_cert(data)


def build_bundle(paths: list[Path]) -> Path | None:
    """Объединяет certifi и найденные сертификаты в один файл. None — если ни один не прочитался."""
    import certifi

    extra: list[str] = []
    for path in paths:
        try:
            # имя файла в комментарии — только ASCII: «Минцифры.cer» иначе ломал запись набора (UnicodeEncodeError)
            label = path.name.encode("ascii", errors="replace").decode("ascii")
            extra.append(f"# {label}\n" + load_pem(path))
        except (OSError, ValueError, UnicodeError):
            continue
    if not extra:
        return None
    bundle = app_data_dir() / "ca-bundle-zakupki.pem"
    try:
        base = Path(certifi.where()).read_text(encoding="ascii", errors="ignore")
        tmp = bundle.with_name(bundle.name + ".tmp")
        tmp.write_text(base.rstrip() + "\n\n" + "\n".join(extra), encoding="ascii", errors="replace")
        tmp.replace(bundle)
    except OSError:
        return bundle if bundle.exists() else None
    return bundle


class _TruststoreAdapter(HTTPAdapter):
    """HTTPS через хранилище сертификатов Windows."""

    def __init__(self, ctx: ssl.SSLContext, **kwargs) -> None:
        self._ctx = ctx
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx
        return super().proxy_manager_for(*args, **kwargs)


def configure_session(session: requests.Session, ca_setting: str = "") -> bool | str:
    """Настраивает сессию и возвращает значение для параметра verify."""
    bundle = build_bundle(find_custom_certs(ca_setting))
    if bundle is not None:
        return str(bundle)
    try:
        import truststore

        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        session.mount("https://", _TruststoreAdapter(ctx))
    except Exception:  # truststore недоступен — остаётся стандартная проверка certifi
        pass
    return True
