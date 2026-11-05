import unittest

from youtube_dagbanli import channel_slug, normalise_url


class ChannelUrlTests(unittest.TestCase):
    def test_handle_url_gets_videos_tab(self):
        self.assertEqual(
            normalise_url("https://www.youtube.com/@learndagbani9379"),
            "https://www.youtube.com/@learndagbani9379/videos",
        )

    def test_existing_videos_url_is_preserved(self):
        self.assertEqual(
            normalise_url("https://www.youtube.com/@learndagbani9379/videos"),
            "https://www.youtube.com/@learndagbani9379/videos",
        )

    def test_bare_handle_is_accepted(self):
        self.assertEqual(
            normalise_url("@learndagbani9379"),
            "https://www.youtube.com/@learndagbani9379/videos",
        )

    def test_channel_slug_is_safe(self):
        self.assertEqual(channel_slug("https://www.youtube.com/@learn.dagbani/videos"), "learn.dagbani")

    def test_non_youtube_url_is_rejected(self):
        with self.assertRaises(ValueError):
            normalise_url("https://example.com/channel")


if __name__ == "__main__":
    unittest.main()
