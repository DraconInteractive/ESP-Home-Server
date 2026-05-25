#!/usr/bin/env python3
"""Send a short email from a spoken command transcript."""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
from argparse import ArgumentParser
from email.message import EmailMessage


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def clean_transcript(text: str) -> str:
    text = " ".join(text.strip().split())
    text = re.sub(r"^(run action|execute action|action|run script)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(send an email|send email|email)\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def spoken_email_address(text: str) -> str:
    cleaned = text.strip().lower()
    replacements = {
        " at ": "@",
        " dot ": ".",
        " underscore ": "_",
        " dash ": "-",
        " hyphen ": "-",
        " plus ": "+",
    }
    cleaned = f" {cleaned} "
    for phrase, value in replacements.items():
        cleaned = cleaned.replace(phrase, value)
    cleaned = cleaned.strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned


def find_marker(text: str, markers: tuple[str, ...], start: int = 0) -> re.Match[str] | None:
    pattern = "|".join(re.escape(marker) for marker in sorted(markers, key=len, reverse=True))
    return re.search(rf"(?<!\S)({pattern})(?!\S)", text[start:], flags=re.IGNORECASE)


def absolute_match(match: re.Match[str] | None, start: int) -> tuple[int, int] | None:
    if not match:
        return None
    return start + match.start(), start + match.end()


def parse_email_command(transcript: str) -> tuple[str | None, str | None, str]:
    text = clean_transcript(transcript)
    subject_markers = ("subject", "with subject")
    body_markers = ("message", "body", "saying", "that says")

    to_match = absolute_match(find_marker(text, ("to",)), 0)
    subject_match = absolute_match(find_marker(text, subject_markers), 0)
    body_search_start = subject_match[1] if subject_match else 0
    body_match = absolute_match(find_marker(text, body_markers, body_search_start), body_search_start)

    recipient_text = None
    if to_match:
        recipient_start = to_match[1]
        recipient_end_candidates = [
            position[0]
            for position in (subject_match, body_match)
            if position and position[0] > recipient_start
        ]
        recipient_end = min(recipient_end_candidates) if recipient_end_candidates else len(text)
        recipient_text = text[recipient_start:recipient_end].strip()

    subject = None
    if subject_match:
        subject_start = subject_match[1]
        subject_end = body_match[0] if body_match and body_match[0] > subject_start else len(text)
        subject = text[subject_start:subject_end].strip()

    if body_match:
        body = text[body_match[1]:].strip()
    elif subject_match:
        body = text[subject_match[1]:].strip()
    elif to_match:
        body = text[to_match[1]:].strip()
    else:
        body = text

    recipient = None
    if recipient_text:
        match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", recipient_text, flags=re.IGNORECASE)
        recipient = match.group(1) if match else spoken_email_address(recipient_text)

    body_text = (body or text).strip()
    body_text = re.sub(r"^(saying|that says|with message|message|body)\s+", "", body_text, flags=re.IGNORECASE).strip()
    return recipient, subject.strip() if subject else None, body_text


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def main() -> int:
    parser = ArgumentParser(description="Send an email through configured SMTP.")
    parser.add_argument("--to", dest="recipient", default="")
    parser.add_argument("--subject", default="")
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    host = required_env("COMMAND_SERVER_SMTP_HOST")
    port = int(env("COMMAND_SERVER_SMTP_PORT", "587"))
    username = env("COMMAND_SERVER_SMTP_USERNAME")
    password = env("COMMAND_SERVER_SMTP_PASSWORD")
    sender = env("COMMAND_SERVER_EMAIL_FROM", username)
    default_to = env("COMMAND_SERVER_EMAIL_TO")
    default_subject = env("COMMAND_SERVER_EMAIL_SUBJECT", "")
    allow_freeform_to = env("COMMAND_SERVER_EMAIL_ALLOW_FREEFORM_TO") == "1"
    use_ssl = env("COMMAND_SERVER_SMTP_SSL") == "1"
    use_starttls = env("COMMAND_SERVER_SMTP_STARTTLS", "1") != "0"

    if not sender:
        raise RuntimeError("COMMAND_SERVER_EMAIL_FROM or COMMAND_SERVER_SMTP_USERNAME is required")

    transcript = env("SCD_TRANSCRIPT")
    requested_to, requested_subject, message_text = parse_email_command(transcript)
    if args.recipient:
        requested_to = args.recipient
    if args.subject:
        requested_subject = args.subject
    if args.message:
        message_text = args.message
    recipient = requested_to or default_to
    if default_to and requested_to and not allow_freeform_to:
        recipient = default_to
    if requested_to and not default_to:
        recipient = requested_to
    if not recipient:
        raise RuntimeError("email recipient missing; say: send email to name@example.com subject ... message ...")
    if "@" not in recipient or "." not in recipient:
        raise RuntimeError(f"email recipient does not look valid: {recipient}")
    subject = requested_subject or default_subject
    if not subject:
        raise RuntimeError("email subject missing; say: subject followed by the subject")
    if not message_text:
        raise RuntimeError("email message missing; say: message followed by the message body")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(message_text)

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
            if username or password:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if use_starttls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if username or password:
                smtp.login(username, password)
            smtp.send_message(message)

    print(f"Email sent to {recipient}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Email failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
