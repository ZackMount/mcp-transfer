from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import atomic_write


def private_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def make_csr(home: Path, name: str) -> str:
    path = home / "device.key"
    if not path.exists():
        atomic_write(path, private_pem(rsa.generate_private_key(public_exponent=65537, key_size=3072)))
    key = serialization.load_pem_private_key(path.read_bytes(), None)
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .sign(key, hashes.SHA256())
    )
    return request.public_bytes(serialization.Encoding.PEM).decode()


class PKI:
    def __init__(self, home: Path):
        self.home = home / "pki"
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_path = self.home / "ca.key"
        cert_path = self.home / "ca.crt"
        if not key_path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCP Transfer private CA")])
            now = dt.datetime.now(dt.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(
                    x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True
                )
                .sign(key, hashes.SHA256())
            )
            atomic_write(key_path, private_pem(key))
            atomic_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
        self.key = serialization.load_pem_private_key(key_path.read_bytes(), None)
        self.cert = x509.load_pem_x509_certificate(cert_path.read_bytes())

    @property
    def ca(self):
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign_csr(self, csr: str, identity: str, names: list[str], days: int = 30) -> str:
        request = x509.load_pem_x509_csr(csr.encode())
        if not request.is_signature_valid:
            raise ValueError("Invalid CSR signature")
        key = request.public_key()
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("CSR requires an RSA key of at least 2048 bits")
        sans = []
        for name in names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                sans.append(x509.DNSName(name))
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, identity)]))
            .issuer_name(self.cert.subject)
            .public_key(key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .sign(self.key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()

    def local_certificate(self, stem: str, names: list[str]):
        cert_path = self.home / f"{stem}.crt"
        if cert_path.exists():
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
            existing_names = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names_match = {str(item.value) for item in existing_names} == set(names)
            if names_match and existing.not_valid_after_utc > dt.datetime.now(dt.UTC) + dt.timedelta(days=7):
                return
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, stem)]))
            .sign(key, hashes.SHA256())
        )
        certificate = self.sign_csr(
            csr.public_bytes(serialization.Encoding.PEM).decode(), stem, names, days=365
        )
        atomic_write(self.home / f"{stem}.key", private_pem(key))
        atomic_write(cert_path, certificate)
