"""Read-only, identity-checked summaries for configured web mailboxes."""

from __future__ import annotations

import logging
import re
import time
from datetime import date
from typing import Any

from pywinauto import Desktop

from .mail_backends import (
    BackendStatus,
    EdgeFallbackBackend,
    GraphBackendConfig,
    GraphReadonlyBackend,
    MailBackendResult,
    WindowsCredentialManagerTokenStore,
)
from .mailboxes import (
    MAILBOX_IDENTITIES,
    MailboxIdentity,
    _read_edge_service_domain,
    confirm_mailbox_identity,
    get_or_open_mailbox_window,
    get_runtime_mailbox_context,
)
from .server import mcp
from .uia import _run_bounded


LOGGER = logging.getLogger(__name__)
_MAIL_CONTROL_TYPES = {"DataItem", "Hyperlink", "ListItem", "Option"}
_MAX_EMAILS = 10


def _is_sensitive_browser_control(
    name: str, control_type: str, automation_id: str
) -> bool:
    """Exclude address/search controls so session URLs are never retained."""
    lowered_name = name.casefold()
    lowered_id = automation_id.casefold()
    if control_type.casefold() == "edit":
        return True
    sensitive_tokens = (
        "address and search bar", "地址和搜索栏", "address bar",
        "omnibox", "location bar", "view_1022",
    )
    return any(
        token in lowered_name or token in lowered_id
        for token in sensitive_tokens
    )


def _empty_group(
    identity: MailboxIdentity, status: str, message: str
) -> dict[str, Any]:
    return {
        "mailbox_id": identity.mailbox_id,
        "display_name": identity.display_name,
        "status": status,
        "message": message,
        "today_count": 0,
        "emails": [],
        "read_state_change": "NONE",
    }


def _snapshot_edge_window(hwnd: int) -> dict[str, Any]:
    """Collect bounded UIA metadata and retain only the address hostname."""
    service_domain = _read_edge_service_domain(hwnd)

    def collect() -> dict[str, Any]:
        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        controls = []
        for control in [window, *window.descendants()]:
            try:
                info = control.element_info
                name = (info.name or "").strip()
                control_type = info.control_type or ""
                automation_id = info.automation_id or ""
                if _is_sensitive_browser_control(
                    name, control_type, automation_id
                ):
                    continue
                if name:
                    controls.append({
                        "name": name,
                        "control_type": control_type,
                        "automation_id": automation_id,
                    })
                if len(controls) >= 500:
                    break
            except Exception:
                continue
        return {
            "title": window.window_text(),
            "service_domain": service_domain,
            "controls": controls,
        }

    return _run_bounded(collect, 5.0, "reading Edge mailbox metadata")


def _service_domain_matches(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    return actual.casefold().rstrip(".") == expected.casefold().rstrip(".")


def _bachelor_page_state(snapshot: dict[str, Any]) -> str:
    """Verify generic mailbox UI without reading messages or credentials."""
    ready_signals = set()
    page_texts = [str(snapshot.get("title", ""))]
    for control in snapshot.get("controls", []):
        name = " ".join(str(control.get("name", "")).split())
        control_type = str(control.get("control_type", ""))
        automation_id = str(control.get("automation_id", ""))
        if control_type in {
            "Document", "Hyperlink", "ListItem", "TabItem", "Text"
        }:
            page_texts.append(name)
        if control_type == "Text" and name == "邮箱选项卡":
            ready_signals.add("mail_tabs")
        elif (
            control_type == "TabItem"
            and automation_id.startswith("_mail_tabitem_")
        ):
            ready_signals.add("mail_tabs")
        elif (
            control_type == "TreeItem"
            and automation_id.startswith("_mail_tree_")
        ):
            ready_signals.add("mail_navigation")
        elif (
            control_type == "Button"
            and automation_id.startswith("_mail_component_")
            and name in {"收 信", "写 信"}
        ):
            ready_signals.add("mail_actions")
    combined_text = "\n".join(page_texts).casefold()
    auth_markers = (
        "账号登录", "密码登录", "扫码登录", "登录邮箱", "请登录",
        "输入密码", "验证身份", "sign in", "log in",
        "会话已过期", "请重新登录",
    )
    if any(marker in combined_text for marker in auth_markers):
        return "AUTH_REQUIRED"
    if len(ready_signals) >= 2:
        return "READY"
    return "PAGE_NOT_READY"


def _find_verified_snapshot(
    identity: MailboxIdentity,
) -> tuple[dict[str, Any] | None, str]:
    confirm_mailbox_identity(identity.mailbox_id, identity.profile_directory)
    context = get_runtime_mailbox_context(identity.mailbox_id)
    if context is None or context.hwnd is None:
        return None, "UNKNOWN_WINDOW"
    if context.profile_directory != identity.profile_directory:
        return None, "IDENTITY_MISMATCH"
    try:
        snapshot = _snapshot_edge_window(context.hwnd)
    except Exception:
        return None, "UNKNOWN_WINDOW"
    actual_domain = snapshot.get("service_domain")
    if actual_domain is None:
        return snapshot, "PAGE_NOT_READY"
    accepted_domains = (
        identity.service_domain, *identity.service_domain_aliases
    )
    if not any(
        _service_domain_matches(expected, actual_domain)
        for expected in accepted_domains
    ):
        return snapshot, "IDENTITY_MISMATCH"
    if identity.mailbox_id == "bachelor_mail":
        return snapshot, _bachelor_page_state(snapshot)
    return snapshot, "READY"


def _ensure_mailbox_page(
    identity: MailboxIdentity, timeout: float = 15.0
) -> tuple[dict[str, Any] | None, str]:
    snapshot, state = _find_verified_snapshot(identity)
    if state in {"READY", "IDENTITY_MISMATCH", "AUTH_REQUIRED"}:
        return snapshot, state
    if identity.stable_url is None and state == "PAGE_NOT_READY":
        return snapshot, state

    LOGGER.info(
        "%s: opening stable URL with %s",
        identity.mailbox_id,
        identity.profile_directory,
    )
    get_or_open_mailbox_window(identity.mailbox_id)
    deadline = time.monotonic() + timeout
    last_state = state
    while time.monotonic() < deadline:
        snapshot, last_state = _find_verified_snapshot(identity)
        if last_state in {"READY", "IDENTITY_MISMATCH", "AUTH_REQUIRED"}:
            return snapshot, last_state
        time.sleep(0.5)
    if last_state == "PAGE_NOT_READY":
        return snapshot, "LOAD_TIMEOUT"
    return snapshot, last_state


def _looks_like_today(time_text: str, today: date) -> bool:
    value = time_text.strip()
    if re.search(r"\b\d{1,2}[:：]\d{2}\b", value) and not re.search(
        r"\b20\d{2}\b", value
    ):
        return True
    normalized = re.sub(r"[年./-]", "-", value).replace("月", "-").replace("日", "")
    return any(
        token in normalized
        for token in (
            today.isoformat(),
            f"{today.year}-{today.month:02d}-{today.day:02d}",
            f"{today.year}-{today.month}-{today.day}",
        )
    )


def _parse_mail_accessible_name(name: str) -> dict[str, str] | None:
    compact = " ".join(name.split())
    chinese = re.match(
        r"^(?P<subject>.+?)\s*发件人\s*[：:]\s*(?P<sender>.+?)\s*"
        r"时间\s*[：:]\s*(?P<time>.+)$",
        compact,
        re.IGNORECASE,
    )
    if chinese:
        return {key: value.strip() for key, value in chinese.groupdict().items()}

    labelled = re.search(
        r"(?:from|sender)\s*[：:]\s*(?P<sender>.+?)\s+"
        r"subject\s*[：:]\s*(?P<subject>.+?)\s+"
        r"(?:time|received)\s*[：:]\s*(?P<time>.+)$",
        compact,
        re.IGNORECASE,
    )
    if labelled:
        return {key: value.strip() for key, value in labelled.groupdict().items()}

    pieces = [piece.strip() for piece in re.split(r"\s*[|｜]\s*", compact)]
    if len(pieces) == 3 and all(pieces):
        return {"sender": pieces[0], "subject": pieces[1], "time": pieces[2]}
    return None


def _extract_today_emails(
    snapshot: dict[str, Any], today: date | None = None
) -> list[dict[str, Any]]:
    current_day = today or date.today()
    results = []
    seen = set()
    for control in snapshot.get("controls", []):
        if control.get("control_type") not in _MAIL_CONTROL_TYPES:
            continue
        parsed = _parse_mail_accessible_name(str(control.get("name", "")))
        if parsed is None or not _looks_like_today(parsed["time"], current_day):
            continue
        key = (parsed["sender"], parsed["subject"], parsed["time"])
        if key in seen:
            continue
        seen.add(key)
        parsed.update({
            "summary": (
                f"来自{parsed['sender']}的邮件，主题为《{parsed['subject']}》。"
                "未打开正文，摘要仅基于当前邮件列表元数据。"
            ),
            "summary_source": "LIST_METADATA",
            "read_state_changed": False,
        })
        results.append(parsed)
        if len(results) >= _MAX_EMAILS:
            break
    return results


def _summarize_with_edge(identity: MailboxIdentity) -> dict[str, Any]:
    LOGGER.info(
        "%s: confirming identity %s with %s",
        identity.mailbox_id,
        identity.display_name,
        identity.profile_directory,
    )
    try:
        snapshot, state = _ensure_mailbox_page(identity)
        if state != "READY" or snapshot is None:
            if state == "IDENTITY_MISMATCH":
                message = "Edge Profile 与当前页面邮箱服务不一致，已停止处理"
            elif identity.mailbox_id == "bachelor_mail" and state == "AUTH_REQUIRED":
                message = "本科邮箱会话已失效，需要人工登录"
            elif identity.mailbox_id == "bachelor_mail" and state == "LOAD_TIMEOUT":
                message = "本科邮箱页面加载超时，需要人工检查"
            elif identity.mailbox_id == "bachelor_mail":
                message = "本科邮箱页面未就绪，需要人工打开邮箱页面"
            elif state == "UNKNOWN_WINDOW":
                message = "无法确认本次 Profile 启动所对应的 Edge 窗口"
            else:
                message = "目标邮箱页面未就绪或无法验证"
            result_status = (
                state if state in {"IDENTITY_MISMATCH", "AUTH_REQUIRED", "LOAD_TIMEOUT"}
                else "NOT_READY"
            )
            LOGGER.warning(
                "%s: %s - %s", identity.mailbox_id, result_status, message
            )
            return _empty_group(identity, result_status, message)

        emails = _extract_today_emails(snapshot)
        LOGGER.info("%s: READY - %d messages", identity.mailbox_id, len(emails))
        result = _empty_group(
            identity,
            "READY",
            "只读检查完成；未打开正文，未改变已读状态",
        )
        result["today_count"] = len(emails)
        result["emails"] = emails
        return result
    except Exception as error:
        LOGGER.exception("%s: ERROR", identity.mailbox_id)
        return _empty_group(identity, "ERROR", f"{type(error).__name__}: {error}")


_BACKEND_STATUS_MAP = {
    BackendStatus.READY: 'READY',
    BackendStatus.NOT_AUTHENTICATED: 'NOT_READY',
    BackendStatus.TOKEN_EXPIRED: 'NOT_READY',
    BackendStatus.REQUEST_FAILED: 'ERROR',
    BackendStatus.FALLBACK_REQUIRED: 'NOT_READY',
}

_GRAPH_FALLBACK_STATUSES = {
    BackendStatus.NOT_AUTHENTICATED,
    BackendStatus.TOKEN_EXPIRED,
    BackendStatus.REQUEST_FAILED,
}


def _backend_for_identity(identity: MailboxIdentity) -> Any:
    if identity.mailbox_id == 'master_mail':
        config = GraphBackendConfig.from_environment()
        return GraphReadonlyBackend(
            config=config,
            token_store=WindowsCredentialManagerTokenStore(
                config.token_service, config.token_username
            ),
        )
    return EdgeFallbackBackend(
        summarize=lambda: _summarize_with_edge(identity)
    )


def _summarize_mailbox(identity: MailboxIdentity) -> dict[str, Any]:
    try:
        result = _backend_for_identity(identity).summarize_today(_MAX_EMAILS)
        if result.legacy_result is not None:
            return result.legacy_result
        if (
            identity.mailbox_id == 'master_mail'
            and result.status in _GRAPH_FALLBACK_STATUSES
        ):
            LOGGER.warning(
                '%s: Graph unavailable (%s); using read-only Edge fallback',
                identity.mailbox_id,
                result.status.value,
            )
            fallback = EdgeFallbackBackend(
                summarize=lambda: _summarize_with_edge(identity)
            ).summarize_today(_MAX_EMAILS)
            if fallback.legacy_result is None:
                raise RuntimeError('Edge fallback returned no compatible result')
            return fallback.legacy_result

        group = _empty_group(
            identity,
            _BACKEND_STATUS_MAP[result.status],
            result.message,
        )
        if result.status is BackendStatus.READY:
            group['today_count'] = len(result.emails)
            group['emails'] = [email.as_result() for email in result.emails]
        return group
    except Exception as error:
        LOGGER.exception('%s: ERROR', identity.mailbox_id)
        return _empty_group(identity, 'ERROR', f'{type(error).__name__}: {error}')

def _classify_important(mailboxes: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    categories = {
        "需要回复": ("回复", "reply", "respond"),
        "截止日期/DDL": ("截止", "deadline", "ddl", "due"),
        "课程/学校通知": ("课程", "学校", "选课", "考试", "course", "university"),
        "账号/安全": ("安全", "登录", "密码", "验证", "security", "account"),
        "财务/账单": ("账单", "付款", "发票", "费用", "invoice", "payment"),
    }
    important = {name: [] for name in (*categories, "其他重要事项")}
    general_tokens = ("重要", "紧急", "urgent", "important", "action required")
    for mailbox in mailboxes:
        for email in mailbox["emails"]:
            text = f"{email['sender']} {email['subject']}".casefold()
            matched = False
            for category, tokens in categories.items():
                if any(token in text for token in tokens):
                    important[category].append({
                        "mailbox": mailbox["display_name"],
                        "sender": email["sender"],
                        "subject": email["subject"],
                        "time": email["time"],
                    })
                    matched = True
                    break
            if not matched and any(token in text for token in general_tokens):
                important["其他重要事项"].append({
                    "mailbox": mailbox["display_name"],
                    "sender": email["sender"],
                    "subject": email["subject"],
                    "time": email["time"],
                })
    return important


def inspect_all_mailbox_pages() -> list[dict[str, str]]:
    """Smoke-test helper: verify identity and page only, without parsing mail."""
    results = []
    for identity in MAILBOX_IDENTITIES.values():
        try:
            snapshot, state = _find_verified_snapshot(identity)
            results.append({
                "mailbox_id": identity.mailbox_id,
                "profile_directory": identity.profile_directory,
                "state": state,
                "expected_domain": " | ".join((
                    identity.service_domain, *identity.service_domain_aliases
                )),
                "observed_domain": (
                    snapshot.get("service_domain") if snapshot else None
                ),
            })
        except Exception as error:
            results.append({
                "mailbox_id": identity.mailbox_id,
                "profile_directory": identity.profile_directory,
                "state": "ERROR",
                "error": f"{type(error).__name__}: {error}",
            })
    return results


@mcp.tool()
def summarize_all_mailboxes_today() -> dict[str, Any]:
    """Return a read-only summary of up to ten visible messages per mailbox."""
    mailboxes = [
        _summarize_mailbox(identity) for identity in MAILBOX_IDENTITIES.values()
    ]
    return {
        "mailboxes": mailboxes,
        "important_items": _classify_important(mailboxes),
    }


__all__ = ["inspect_all_mailbox_pages", "summarize_all_mailboxes_today"]
