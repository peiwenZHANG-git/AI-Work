"""Self-signed LAN transport identity with explicit SPKI pinning."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..mail_backends import WindowsCredentialManagerSecretStore


TLS_CREDENTIAL_SERVICE = 'AI-Work/windows-gui/remote'
CERTIFICATE_LIFETIME_DAYS = 90
RENEWAL_WINDOW_DAYS = 14
KEY_LIFETIME_DAYS = 365
SERVER_IDENTITY_URN_PREFIX = 'urn:ai-work:remote:'


@dataclass(frozen=True)
class TlsMaterial:
    private_key_pem: bytes
    certificate_pem: bytes
    spki_sha256: str
    certificate_expires_at: datetime.datetime
    private_key_created_at: datetime.datetime


def _default_store(username: str) -> Any:
    return WindowsCredentialManagerSecretStore(TLS_CREDENTIAL_SERVICE, username)


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timestamp has no timezone')
    return parsed.astimezone(datetime.timezone.utc)


def spki_sha256(certificate: x509.Certificate) -> str:
    der = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def _certificate_matches_identity(
    certificate: x509.Certificate, *, server_id: str, bind_ip: str,
) -> bool:
    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName,
        ).value
        return (
            ipaddress.ip_address(bind_ip) in names.get_values_for_type(x509.IPAddress)
            and SERVER_IDENTITY_URN_PREFIX + server_id
            in names.get_values_for_type(x509.UniformResourceIdentifier)
        )
    except (ValueError, x509.ExtensionNotFound):
        return False


def certificate_from_pem(value: bytes | str) -> x509.Certificate:
    if isinstance(value, str):
        value = value.encode('utf-8')
    return x509.load_pem_x509_certificate(value)


def private_key_from_pem(value: bytes | str) -> ec.EllipticCurvePrivateKey:
    if isinstance(value, str):
        value = value.encode('utf-8')
    key = serialization.load_pem_private_key(value, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError('unsupported remote TLS private key')
    return key


def issue_certificate(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    server_id: str,
    bind_ip: str,
    not_before: datetime.datetime | None = None,
    lifetime_days: int = CERTIFICATE_LIFETIME_DAYS,
) -> tuple[bytes, datetime.datetime]:
    now = not_before or _utc_now()
    not_after = now + datetime.timedelta(days=lifetime_days)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'AI-Work Remote Server'),
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(bind_ip)),
                x509.UniformResourceIdentifier(SERVER_IDENTITY_URN_PREFIX + server_id),
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=False, content_commitment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )
    certificate = builder.sign(private_key, hashes.SHA256())
    return certificate.public_bytes(serialization.Encoding.PEM), not_after


def generate_material(
    *, server_id: str, bind_ip: str,
    now: datetime.datetime | None = None,
) -> tuple[TlsMaterial, ec.EllipticCurvePrivateKey]:
    created_at = now or _utc_now()
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem, expires = issue_certificate(
        private_key, server_id=server_id, bind_ip=bind_ip,
        not_before=created_at,
    )
    certificate = certificate_from_pem(certificate_pem)
    material = TlsMaterial(
        private_key_pem=key_pem,
        certificate_pem=certificate_pem,
        spki_sha256=spki_sha256(certificate),
        certificate_expires_at=expires,
        private_key_created_at=created_at,
    )
    return material, private_key


def load_material(
    private_key_pem: bytes, certificate_pem: bytes,
) -> tuple[TlsMaterial, ec.EllipticCurvePrivateKey]:
    private_key = private_key_from_pem(private_key_pem)
    certificate = certificate_from_pem(certificate_pem)
    if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
        raise ValueError('remote TLS key and certificate do not match')
    expires = certificate.not_valid_after_utc
    return TlsMaterial(
        private_key_pem=private_key_pem,
        certificate_pem=certificate_pem,
        spki_sha256=spki_sha256(certificate),
        certificate_expires_at=expires,
        private_key_created_at=certificate.not_valid_before_utc,
    ), private_key


class TlsManager:
    """Persist only key/certificate material through injectable secret stores."""

    def __init__(
        self,
        *,
        store_factory: Callable[[str], Any] = _default_store,
        now_factory: Callable[[], datetime.datetime] = _utc_now,
    ) -> None:
        self._store_factory = store_factory
        self._now = now_factory

    def _store(self, username: str) -> Any:
        return self._store_factory(username)

    def _read(self, username: str) -> bytes | None:
        value = self._store(username).get_secret()
        return value.encode('utf-8') if value else None

    def _write(self, username: str, value: bytes) -> None:
        self._store(username).set_secret(value.decode('utf-8'))

    def _delete(self, username: str) -> bool:
        store = self._store(username)
        return bool(store.delete_secret()) if hasattr(store, 'delete_secret') else False

    def load_or_create(
        self, *, server_id: str, bind_ip: str,
    ) -> TlsMaterial:
        key_pem = self._read('tls_private_key')
        certificate_pem = self._read('tls_certificate')
        stored_fingerprint = self._read('tls_spki_sha256')
        stored_created_at = self._read('tls_private_key_created_at')
        if key_pem and certificate_pem:
            material, private_key = load_material(key_pem, certificate_pem)
            certificate = certificate_from_pem(certificate_pem)
            if (
                stored_fingerprint
                and material.spki_sha256 != stored_fingerprint.decode('ascii')
            ):
                raise ValueError('remote TLS fingerprint metadata mismatch')
            if not stored_created_at:
                raise ValueError('remote TLS key age metadata missing')
            key_age = self._now() - _parse_utc(stored_created_at.decode('ascii'))
            if key_age > datetime.timedelta(days=KEY_LIFETIME_DAYS):
                raise ValueError('remote TLS private key rotation required')
            if (
                self._now() >= material.certificate_expires_at - datetime.timedelta(
                    days=RENEWAL_WINDOW_DAYS,
                )
                or not _certificate_matches_identity(
                    certificate, server_id=server_id, bind_ip=bind_ip,
                )
            ):
                certificate_pem, expires = issue_certificate(
                    private_key, server_id=server_id, bind_ip=bind_ip,
                    not_before=self._now(),
                )
                material = TlsMaterial(
                    private_key_pem=key_pem,
                    certificate_pem=certificate_pem,
                    spki_sha256=material.spki_sha256,
                    certificate_expires_at=expires,
                    private_key_created_at=_parse_utc(
                        stored_created_at.decode('ascii'),
                    ),
                )
                self._write('tls_certificate', certificate_pem)
                self._write(
                    'tls_spki_sha256', material.spki_sha256.encode('ascii'),
                )
                self._write(
                    'tls_private_key_created_at', stored_created_at,
                )
            return material

        if key_pem or certificate_pem or stored_fingerprint or stored_created_at:
            raise ValueError('remote TLS material is incomplete')
        material, _ = generate_material(
            server_id=server_id, bind_ip=bind_ip, now=self._now(),
        )
        self._write('tls_private_key', material.private_key_pem)
        self._write('tls_certificate', material.certificate_pem)
        self._write('tls_spki_sha256', material.spki_sha256.encode('ascii'))
        self._write(
            'tls_private_key_created_at',
            material.private_key_created_at.isoformat().encode('ascii'),
        )
        return material

    def rotate_key(self, *, server_id: str, bind_ip: str) -> TlsMaterial:
        material, _ = generate_material(
            server_id=server_id, bind_ip=bind_ip, now=self._now(),
        )
        self._write('tls_private_key', material.private_key_pem)
        self._write('tls_certificate', material.certificate_pem)
        self._write('tls_spki_sha256', material.spki_sha256.encode('ascii'))
        self._write(
            'tls_private_key_created_at',
            material.private_key_created_at.isoformat().encode('ascii'),
        )
        return material

    def delete_material(self) -> None:
        for username in (
            'tls_private_key', 'tls_certificate', 'tls_spki_sha256',
            'tls_private_key_created_at',
        ):
            self._delete(username)


def server_ssl_context(material: TlsMaterial) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20')
    combined = material.certificate_pem + material.private_key_pem
    descriptor, path = tempfile.mkstemp(prefix='aiwork-remote-tls-', suffix='.pem')
    try:
        os.write(descriptor, combined)
        os.close(descriptor)
        context.load_cert_chain(str(Path(path)))
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return context


def client_pinned_spki(
    der_certificate: bytes, expected_sha256: str,
    *, now: datetime.datetime | None = None,
) -> None:
    certificate = x509.load_der_x509_certificate(der_certificate)
    current = now or _utc_now()
    if (
        current < certificate.not_valid_before_utc
        or current > certificate.not_valid_after_utc
    ):
        raise ssl.SSLCertVerificationError('remote server certificate is not valid now')
    actual = spki_sha256(certificate)
    if not hmac.compare_digest(actual, expected_sha256.casefold()):
        raise ssl.SSLCertVerificationError('remote server SPKI pin mismatch')
