"""Interactively configure whitelisted mail credentials without echoing them."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_gui.mail_assistant import (
    ASSISTANT_CREDENTIAL_SERVICE,
    ASSISTANT_DRAFT_CREDENTIAL_USERNAMES,
    BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
)
from windows_gui.mail_backends import WindowsCredentialManagerSecretStore
from windows_gui.mail_digest import CREDENTIAL_SERVICE, SUMMARY_API_KEY_USERNAME
from windows_gui.imap_mail import (
    BACHELOR_IMAP_CREDENTIAL_SERVICE,
    BACHELOR_IMAP_CREDENTIAL_USERNAME,
    QQ_IMAP_CREDENTIAL_SERVICE,
    QQ_IMAP_CREDENTIAL_USERNAME,
)


ASSISTANT_KEYS = (
    'qq_assistant_draft',
    'bachelor_assistant_draft',
    'bachelor_assistant_smtp',
)
CREDENTIAL_TARGETS: dict[str, tuple[str, str, str]] = {
    'qq_imap_summary': (
        QQ_IMAP_CREDENTIAL_SERVICE,
        QQ_IMAP_CREDENTIAL_USERNAME,
        'QQ 只读 IMAP 授权码',
    ),
    'bachelor_imap_summary': (
        BACHELOR_IMAP_CREDENTIAL_SERVICE,
        BACHELOR_IMAP_CREDENTIAL_USERNAME,
        '本科只读 IMAP 授权码',
    ),
    'qq_assistant_draft': (
        ASSISTANT_CREDENTIAL_SERVICE,
        ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['qq_mail'],
        'QQ 助手草稿授权码',
    ),
    'bachelor_assistant_draft': (
        ASSISTANT_CREDENTIAL_SERVICE,
        ASSISTANT_DRAFT_CREDENTIAL_USERNAMES['bachelor_mail'],
        '本科助手草稿授权码',
    ),
    'bachelor_assistant_smtp': (
        ASSISTANT_CREDENTIAL_SERVICE,
        BACHELOR_ASSISTANT_SMTP_CREDENTIAL_USERNAME,
        '本科助手 SMTP 授权码',
    ),
    'glm_api_key': (
        CREDENTIAL_SERVICE,
        SUMMARY_API_KEY_USERNAME,
        'Zhipu GLM API key',
    ),
}


class CredentialConfigurationError(Exception):
    """Raised when an interactive credential cannot be safely configured."""


def store_secret(
    key: str,
    secret: str,
    *,
    store_factory: Callable[[str, str], WindowsCredentialManagerSecretStore] | None = None,
) -> None:
    store_factory = store_factory or WindowsCredentialManagerSecretStore
    try:
        service, username, _label = CREDENTIAL_TARGETS[key]
    except KeyError as error:
        raise CredentialConfigurationError('unknown credential target') from error
    try:
        store_factory(service, username).set_secret(secret)
    except (OSError, ValueError) as error:
        raise CredentialConfigurationError(
            f'credential write failed: {type(error).__name__}'
        ) from error


def is_configured(
    key: str,
    *,
    store_factory: Callable[[str, str], WindowsCredentialManagerSecretStore] | None = None,
) -> bool:
    store_factory = store_factory or WindowsCredentialManagerSecretStore
    try:
        service, username, _label = CREDENTIAL_TARGETS[key]
    except KeyError as error:
        raise CredentialConfigurationError('unknown credential target') from error
    configured = bool(store_factory(service, username).get_secret())
    return configured


def prompt_and_store(
    key: str,
    *,
    force: bool = False,
    prompt: Callable[[str], str] | None = None,
    output: Callable[[str], None] = print,
    store_factory: Callable[[str, str], WindowsCredentialManagerSecretStore] | None = None,
) -> bool:
    """Prompt twice for one secret, write it, and report only the target key."""
    prompt = prompt or getpass.getpass
    store_factory = store_factory or WindowsCredentialManagerSecretStore
    try:
        _service, _username, label = CREDENTIAL_TARGETS[key]
    except KeyError as error:
        raise CredentialConfigurationError('unknown credential target') from error
    if is_configured(key, store_factory=store_factory) and not force:
        output(f'[SKIP] {key}: already configured')
        return False
    output(
        f'Enter the secret for {key}. Input is hidden and stored only in '
        'Windows Credential Manager.'
    )
    secret = prompt(f'{key}: ')
    repeated = prompt(f'{key} (again): ')
    if not secret or not secrets_match(secret, repeated):
        raise CredentialConfigurationError(f'{key}: empty or mismatched input')
    store_secret(key, secret, store_factory=store_factory)
    if not is_configured(key, store_factory=store_factory):
        raise CredentialConfigurationError(
            f'{key}: credential unavailable after write'
        )
    output(f'[OK] {key}: saved')
    return True


def secrets_match(first: str, second: str) -> bool:
    import secrets

    return bool(first) and secrets.compare_digest(first, second)


def main(
    argv: list[str] | None = None,
    *,
    prompt: Callable[[str], str] | None = None,
) -> int:
    prompt = prompt or getpass.getpass
    parser = argparse.ArgumentParser(description=__doc__)
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        '--missing-assistant',
        action='store_true',
        help='configure only the three assistant authorization-code targets',
    )
    target_group.add_argument(
        '--all-configurable',
        action='store_true',
        help='interactively configure every whitelisted non-OAuth credential',
    )
    target_group.add_argument(
        '--key',
        choices=sorted(CREDENTIAL_TARGETS),
        help='configure one whitelisted credential target',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='replace an existing whitelisted credential after hidden confirmation',
    )
    args = parser.parse_args(argv)
    keys: tuple[str, ...]
    if args.missing_assistant:
        keys = ASSISTANT_KEYS
    elif args.all_configurable:
        keys = tuple(CREDENTIAL_TARGETS)
    else:
        keys = (args.key,)

    updated = 0
    try:
        for key in keys:
            if prompt_and_store(key, force=args.force, prompt=prompt):
                updated += 1
    except CredentialConfigurationError as error:
        print(f'configuration failed: {error}', file=sys.stderr)
        return 1
    print(f'done: updated {updated} item(s).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
