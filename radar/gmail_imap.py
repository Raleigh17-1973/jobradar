"""IMAP access to the personal Gmail inbox that receives job alerts.

The app password is read from the macOS Keychain at run time and never written to disk.
Reads use BODY.PEEK so nothing is marked read just by being fetched; collect marks
processed alerts read only when settings.yaml turns that on.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import re
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Dict, Iterator, List, Optional

FETCH_BATCH = 25

_MSGID_RE = re.compile(rb"X-GM-MSGID (\d+)")
_UID_RE = re.compile(rb"UID (\d+)")


class CredentialError(RuntimeError):
    pass


def keychain_password(service: str, account: str) -> str:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError:
        raise CredentialError(
            f"No Keychain item '{service}' for {account}. Create it with:\n"
            f'  security add-generic-password -a "{account}" -s "{service}" -w'
        ) from None
    # Google shows app passwords in groups of four; IMAP wants them without spaces.
    return out.stdout.strip().replace(" ", "")


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class RawMessage:
    uid: str
    gm_msgid: str  # Gmail's permanent message ID; stable across folders and sessions
    raw: bytes

    def parsed(self) -> EmailMessage:
        return email.message_from_bytes(self.raw, policy=email.policy.default)


class GmailIMAP:
    def __init__(self, address: str, keychain_service: str, host: str = "imap.gmail.com"):
        self.address = address
        self.keychain_service = keychain_service
        self.host = host
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.readonly = True

    def __enter__(self) -> "GmailIMAP":
        password = keychain_password(self.keychain_service, self.address)
        self.conn = imaplib.IMAP4_SSL(self.host)
        try:
            self.conn.login(self.address, password)
        except imaplib.IMAP4.error as e:
            raise CredentialError(f"Gmail rejected the login for {self.address}: {e}") from None
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass

    def select_all_mail(self, readonly: bool = True) -> None:
        """Select All Mail so archived alerts are found too. Its name is localized, so find it by flag."""
        _, folders = self.conn.list()
        for line in folders:
            if b"\\All" in line:
                folder = line.decode().rsplit(' "/" ', 1)[1]
                break
        else:
            raise RuntimeError("Could not find Gmail's All Mail folder over IMAP")
        typ, _ = self.conn.select(folder, readonly=readonly)
        if typ != "OK":
            raise RuntimeError(f"Could not open {folder}")
        self.readonly = readonly

    def search(self, gmail_query: str) -> List[str]:
        """UIDs matching a Gmail web-style search (X-GM-RAW), oldest first."""
        _, data = self.conn.uid("SEARCH", "X-GM-RAW", _quote(gmail_query))
        return data[0].decode().split() if data and data[0] else []

    def gm_msgids(self, uids: List[str]) -> Dict[str, str]:
        """uid -> Gmail message ID, cheaply, so already-processed emails are skipped before download."""
        out: Dict[str, str] = {}
        for i in range(0, len(uids), 200):
            _, data = self.conn.uid("FETCH", ",".join(uids[i:i + 200]), "(X-GM-MSGID)")
            for item in data:
                line = item[0] if isinstance(item, tuple) else item
                uid, msgid = _UID_RE.search(line or b""), _MSGID_RE.search(line or b"")
                if uid and msgid:
                    out[uid.group(1).decode()] = msgid.group(1).decode()
        return out

    def _fetch(self, uids: List[str], part: str) -> Iterator[RawMessage]:
        for i in range(0, len(uids), FETCH_BATCH):
            batch = ",".join(uids[i:i + FETCH_BATCH])
            _, data = self.conn.uid("FETCH", batch, f"(X-GM-MSGID {part})")
            for item in data:
                if not isinstance(item, tuple):
                    continue
                meta, raw = item
                yield RawMessage(
                    uid=_UID_RE.search(meta).group(1).decode(),
                    gm_msgid=_MSGID_RE.search(meta).group(1).decode(),
                    raw=raw,
                )

    def fetch(self, uids: List[str]) -> Iterator[RawMessage]:
        return self._fetch(uids, "BODY.PEEK[]")

    def fetch_headers(self, uids: List[str]) -> Iterator[RawMessage]:
        return self._fetch(uids, "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)]")

    def _require_writable(self) -> None:
        if self.readonly:
            raise RuntimeError("Mailbox was opened read-only; call select_all_mail(readonly=False)")

    def add_label(self, uid: str, label: str) -> None:
        """Gmail creates the label on first use."""
        self._require_writable()
        self.conn.uid("STORE", uid, "+X-GM-LABELS", f"({_quote(label)})")

    def archive(self, uid: str) -> None:
        self._require_writable()
        self.conn.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")

    def mark_read(self, uids: List[str]) -> None:
        self._require_writable()
        for i in range(0, len(uids), 200):
            self.conn.uid("STORE", ",".join(uids[i:i + 200]), "+FLAGS", "(\\Seen)")
