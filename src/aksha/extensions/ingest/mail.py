"""Email (``.eml``, RFC 5322) through the standard-library ``email`` parser.

The representation is the headers (From, To, Cc, Date, Subject), the plain body, or the
HTML body through :func:`~aksha.extensions.ingest.text.html_doc` when there is no plain
part, then a list of the attachments. Each attachment is returned as a
:class:`~aksha.extensions.ingest.types.Child`, which intake ingests as its own document
(``origin="attachment"``, ``parent`` the email's doc key) under the nesting-depth and
per-email caps. A nested email (``message/rfc822``) is an attachment like any other, so
its own attachments go one level deeper.
"""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from aksha.extensions.ingest.text import html_doc
from aksha.extensions.ingest.types import Anchor, Child, Converted, Gap, PageRung, Representation

_HEADERS = ("From", "To", "Cc", "Date", "Subject")


def _payload(part: EmailMessage) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        message = inner[0] if isinstance(inner, list) and inner else inner
        if isinstance(message, EmailMessage):
            return bytes(message.as_bytes(policy=policy.default))
    data = part.get_payload(decode=True)
    if isinstance(data, bytes):
        return data
    content = part.get_content()
    return content.encode("utf-8") if isinstance(content, str) else bytes(content)


def convert(data: bytes) -> Converted:
    """The email's representation and its attachments as children (module doc)."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except Exception as e:  # the parser is lenient; this catches only hard failures
        return Converted(gap=Gap("unreadable", f"the email does not parse: {type(e).__name__}",
                                 ("parse_fast",)))  # fmt: skip
    assert isinstance(message, EmailMessage)
    lines = [f"{h}: {message[h]}" for h in _HEADERS if message[h] is not None]
    parts: list[str] = []
    anchors: list[Anchor] = []

    def add(label: str, text: str) -> None:
        start = sum(len(p) for p in parts)
        parts.append(text)
        anchors.append(Anchor(label, start, start + len(text.rstrip("\n"))))

    add("headers", "\n".join(lines) + "\n\n")
    notes: list[str] = []
    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        text = content if isinstance(content, str) else ""
        if body.get_content_type() == "text/html":
            text = html_doc(text).text
            notes.append("HTML body as visible text")
        add("body", text.strip("\n") + "\n\n")
    children: list[Child] = []
    listed: list[str] = []
    for n, part in enumerate(message.iter_attachments(), start=1):
        assert isinstance(part, EmailMessage)
        name = part.get_filename() or f"attachment-{n}"
        payload = _payload(part)
        children.append(Child(name, payload, part.get_content_type()))
        listed.append(f"{n}. {name} ({part.get_content_type()}, {len(payload)} bytes)")
    if listed:
        add("attachments", "attachments:\n" + "\n".join(listed) + "\n")
        notes.append(f"{len(listed)} attachments read as their own documents")
    rep = Representation("".join(parts), tuple(anchors), ("python stdlib email",),
                         (PageRung("message", "parse_fast", "extracted"),),
                         notes=tuple(notes))  # fmt: skip
    return Converted(representation=rep, children=tuple(children))
