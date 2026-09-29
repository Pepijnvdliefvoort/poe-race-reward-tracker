from __future__ import annotations

import unittest

from server.sales_discord_notify import account_profile_url, build_account_banned_embed


class AccountBannedEmbedTests(unittest.TestCase):
    def test_profile_url_replaces_hash_with_dash(self) -> None:
        self.assertEqual(
            account_profile_url("Dethklok#2196"),
            "https://www.pathofexile.com/account/view-profile/Dethklok-2196",
        )

    def test_profile_url_encodes_unusual_names(self) -> None:
        self.assertEqual(
            account_profile_url("Ärger Bär#0001"),
            "https://www.pathofexile.com/account/view-profile/%C3%84rger%20B%C3%A4r-0001",
        )
        # Names with dashes or underscores keep them.
        self.assertTrue(account_profile_url("some_name-x#12").endswith("/some_name-x-12"))

    def test_profile_url_without_discriminator_or_name(self) -> None:
        self.assertTrue(account_profile_url("OldAccount").endswith("/OldAccount"))
        self.assertIsNone(account_profile_url(""))

    def test_ban_embed_links_the_profile(self) -> None:
        embed = build_account_banned_embed(account_name="Dethklok#2196")
        url = "https://www.pathofexile.com/account/view-profile/Dethklok-2196"
        self.assertEqual(embed["url"], url)
        self.assertIn(f"[View on pathofexile.com]({url})", embed["description"])
        self.assertIn("**Account:** `Dethklok#2196`", embed["description"])


if __name__ == "__main__":
    unittest.main()
