import shutil
import ssl
import subprocess

import pytest
import requests

from jarvis_app import certs

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="нужен openssl")


@pytest.fixture
def pem_and_der(tmp_path):
    key = tmp_path / "key.pem"
    pem = tmp_path / "root.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(pem),
         "-days", "1", "-subj", "/CN=Test Root CA"],
        check=True, capture_output=True,
    )
    der = tmp_path / "root.cer"
    der.write_bytes(ssl.PEM_cert_to_DER_cert(pem.read_text()))
    return pem, der


def test_load_pem_accepts_der_and_pem(pem_and_der):
    pem, der = pem_and_der
    assert certs.load_pem(der).strip() == pem.read_text().strip()
    assert "BEGIN CERTIFICATE" in certs.load_pem(pem)


def test_load_pem_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.cer"
    bad.write_bytes(b"hello")
    with pytest.raises(ValueError):
        certs.load_pem(bad)


def test_bundle_from_certs_dir(pem_and_der):
    _, der = pem_and_der
    shutil.copy(der, certs.certs_dir() / "russian_trusted_root_ca.cer")
    session = requests.Session()
    verify = certs.configure_session(session, "")
    assert isinstance(verify, str) and verify.endswith("ca-bundle-zakupki.pem")
    text = open(verify, encoding="ascii").read()
    assert "russian_trusted_root_ca.cer" in text
    # стандартные корневые сертификаты тоже на месте, и файл читается OpenSSL
    assert text.count("BEGIN CERTIFICATE") > 50
    ssl.create_default_context(cafile=verify)


def test_setting_path_to_folder(pem_and_der, tmp_path):
    pem, _ = pem_and_der
    folder = tmp_path / "mycerts"
    folder.mkdir()
    shutil.copy(pem, folder / "sub.crt")
    assert [p.name for p in certs.find_custom_certs(str(folder))] == ["sub.crt"]


def test_without_certs_uses_windows_store_adapter():
    session = requests.Session()
    assert certs.configure_session(session, "") is True
    assert isinstance(session.get_adapter("https://zakupki.gov.ru"), certs._TruststoreAdapter)
