"""The browser session the events and blog fetchers render pages with (lib/browser.py)."""
import unittest

import helpers  # noqa: F401

from lib import browser


class Session(unittest.TestCase):
    def test_the_user_agent_drops_the_headless_marker(self):
        raw = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "HeadlessChrome/141.0.0.0 Safari/537.36")
        self.assertEqual(browser.user_agent(raw), raw.replace("HeadlessChrome/", "Chrome/"))

    def test_only_hashicorp_com_is_the_own_site(self):
        self.assertTrue(browser.own_site("https://www.hashicorp.com/en/events"))
        self.assertTrue(browser.own_site("https://hashicorp.com/en/blog/products/vault"))
        self.assertFalse(browser.own_site("https://events.hashicorp.com/VaultQuickstartDec"))
        self.assertFalse(browser.own_site("https://ibm.webcasts.com/starthere.jsp?ei=1"))

    def reported(self, **kwargs):
        """(navigator.webdriver, user agent) of a session — offline: a local page is enough."""
        try:
            with browser.session(**kwargs) as ctx:
                page = ctx.new_page()
                page.set_content("<title>offline</title>")
                return page.evaluate("[navigator.webdriver, navigator.userAgent]")
        except ImportError:
            self.skipTest("Playwright is not installed")
        except Exception as exc:  # noqa: BLE001
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Playwright's Chromium is not installed")
            raise

    def test_the_session_does_not_report_automation_to_hashicorp_com(self):
        webdriver, agent = self.reported()
        self.assertFalse(webdriver)
        self.assertNotIn("Headless", agent)

    def test_for_any_other_site_the_browser_says_it_is_automated(self):
        webdriver, agent = self.reported(report_automation=True)
        self.assertTrue(webdriver)
        self.assertIn("HeadlessChrome", agent)
