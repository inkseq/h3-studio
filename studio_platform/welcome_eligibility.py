"""Offline welcome-grant eligibility; this does not restrict sign-in."""

import idna


def eligible_email(email):
    """Exclude known disposable domains and skip grants when classification fails."""
    if (
        type(email) is not str
        or not 3 <= len(email) <= 320
        or email.count("@") != 1
        or any(character.isspace() or ord(character) < 32 for character in email)
    ):
        return False
    local, domain = email.split("@")
    if not local:
        return False
    try:
        from disposable_email_domains import blocklist

        domain = (
            idna.encode(domain, uts46=True, std3_rules=True)
            .decode("ascii")
            .removesuffix(".")
        )
    except (ImportError, UnicodeError):
        return False
    if (
        not isinstance(blocklist, (set, frozenset))
        or not blocklist
        or any(type(value) is not str or not value for value in blocklist)
        or "." not in domain
    ):
        return False
    # A listed domain also covers its subdomains, with an exact label boundary.
    while domain:
        if domain in blocklist:
            return False
        domain = domain.partition(".")[2]
    return True
