"""Apex's own little certificate authority, and the mail server's TLS certificate signed by it.

Real TLS, no "verify=False" anywhere: Roundcube, the responders and the tests all trust
data/generated/certs/ca.crt. Idempotent: existing files are kept (delete the folder to rotate).
ponytail: a lab CA on disk with no passphrase; a real shop would use an internal PKI or ACME.

Also issues `ingress.crt` (localhost) for the agent stack's Envoy ingress (S1).

Usage: uv run python -m legacy.mail.certs
"""

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
CERTS = ROOT / "data" / "generated" / "certs"
NAMES = ["mail.apex.local", "mail", "localhost"]


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_key(k: rsa.RSAPrivateKey) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption())  # fmt: skip


def _ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Load the lab CA, or create it the first time."""
    if (CERTS / "ca.crt").exists() and (CERTS / "ca.key").exists():
        key = serialization.load_pem_private_key((CERTS / "ca.key").read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        return key, x509.load_pem_x509_certificate((CERTS / "ca.crt").read_bytes())
    now = dt.datetime.now(dt.UTC)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Apex Precision Components"),
                         x509.NameAttribute(NameOID.COMMON_NAME, "Apex Lab Root CA")])  # fmt: skip
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))  # fmt: skip
    (CERTS / "ca.key").write_bytes(_pem_key(ca_key))
    (CERTS / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return ca_key, ca


def issue(name: str, dns_names: list[str]) -> bool:
    """`<name>.crt/.key` for these DNS names + 127.0.0.1, signed by the lab CA. False if it already exists."""
    if (CERTS / f"{name}.crt").exists():
        return False
    CERTS.mkdir(parents=True, exist_ok=True)
    ca_key, ca = _ca()
    now = dt.datetime.now(dt.UTC)
    key = _key()
    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in dns_names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))  # fmt: skip
    (CERTS / f"{name}.key").write_bytes(_pem_key(key))
    (CERTS / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return True


def main() -> None:
    made = [n for n, names in (("mail", NAMES), ("ingress", ["localhost"])) if issue(n, names)]
    print(f"certs: {'new ' + ', '.join(made) if made else 'present'} in {CERTS.relative_to(ROOT)} (lab CA ca.crt)")


if __name__ == "__main__":
    main()
