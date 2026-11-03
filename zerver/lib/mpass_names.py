# The starting display name of an account the mPass SSO layer creates
# (zerver/lib/mpass_sso.py): the part of the email before the @, made
# acceptable to check_full_name.  Users change it in settings; nothing
# reads roles or tenancy from it.
import re

from zerver.lib.string_validation import is_character_printable
from zerver.models import UserProfile

TRAILING_MENTION_SUFFIX = re.compile(r"(\|\d+)+$")
FALLBACK_NAME = "mPass user"


def sanitized_name(raw: str) -> str:
    kept = "".join(
        character
        for character in raw
        if is_character_printable(character) and character not in UserProfile.NAME_INVALID_CHARS
    )
    truncated = kept.strip()[: UserProfile.MAX_NAME_LENGTH].strip()
    return TRAILING_MENTION_SUFFIX.sub("", truncated).strip()


def display_name_for_email(email: str) -> str:
    local_part, at, domain = email.rpartition("@")
    if not at:
        local_part, domain = email, ""
    candidates = [local_part, f"{local_part} at {domain}" if domain else ""]
    return next(filter(None, map(sanitized_name, candidates)), FALLBACK_NAME)
