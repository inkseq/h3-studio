"""Real pinned offline list, canonical domain matching and unknown eligibility."""

import sys
import unittest
from unittest.mock import patch

import disposable_email_domains

from studio_platform.welcome_eligibility import eligible_email


class WelcomeEligibilityTests(unittest.TestCase):
    def test_pinned_list_excludes_disposable_domains_and_their_subdomains(self):
        for email in (
            "person@mailinator.com",
            "person@MAILINATOR.COM",
            "person@nested.mailinator.com",
            "person@mailinator.com.",
            "person@ＭＡＩＬＩＮＡＴＯＲ．ＣＯＭ。",
        ):
            with self.subTest(email=email):
                self.assertFalse(eligible_email(email))

    def test_unlisted_domains_do_not_apply_local_part_rules(self):
        for email in (
            "person@gmail.com",
            "per.son+alias@gmail.com",
            "person@example.test",
            "person@mailinator.com.permanent.test",
        ):
            with self.subTest(email=email):
                self.assertTrue(eligible_email(email))

    def test_suffix_matching_uses_domain_labels_without_substring_matches(self):
        with patch.object(disposable_email_domains, "blocklist", {"mailinator.com"}):
            self.assertFalse(eligible_email("person@nested.mailinator.com"))
            self.assertTrue(eligible_email("person@notmailinator.com"))
            self.assertTrue(eligible_email("person@mailinator.com.permanent.test"))

    def test_idna_and_unicode_domains_share_one_blocklist_classification(self):
        with patch.object(disposable_email_domains, "blocklist", {"xn--bcher-kva.de"}):
            for domain in ("bücher.de", "xn--bcher-kva.de", "sub.BÜCHER.de"):
                with self.subTest(domain=domain):
                    self.assertFalse(eligible_email("person@" + domain))
            self.assertTrue(eligible_email("person@other-bücher.de"))

    def test_invalid_email_or_domain_is_not_eligible(self):
        for email in (
            None,
            123,
            "",
            "person",
            "@example.test",
            "person@@example.test",
            "person@localhost",
            "person@example..test",
            "person@-example.test",
            "person@example.test..",
            "person@exa mple.test",
            "person@\x00example.test",
            "person@\ud800.test",
            " person@example.test",
        ):
            with self.subTest(email=email):
                self.assertFalse(eligible_email(email))

    def test_missing_empty_or_malformed_list_skips_grant_eligibility(self):
        with patch.dict(sys.modules, {"disposable_email_domains": None}):
            self.assertFalse(eligible_email("person@example.test"))
        for blocklist in (None, [], set(), {None}, {""}):
            with (
                self.subTest(blocklist=blocklist),
                patch.object(disposable_email_domains, "blocklist", blocklist),
            ):
                self.assertFalse(eligible_email("person@example.test"))
