from depfix.bundler_lock import BundlerLock

LOCKFILE = """\
GEM
  remote: https://rubygems.org/
  specs:
    actionmailbox (7.2.3.1)
      activestorage (= 7.2.3.1)
    activestorage (7.2.3.1)
    rails (7.2.3.1)
      actionmailbox (= 7.2.3.1)
      activestorage (= 7.2.3.1)

PLATFORMS
  ruby

DEPENDENCIES
  bundler (= 4.0.10)
  rails (= 7.2.3.1)

BUNDLED WITH
   4.0.10
"""


def test_parses_graph_and_direct_dependencies() -> None:
    lock = BundlerLock.parse(LOCKFILE)

    assert lock.specs["activestorage"].version == "7.2.3.1"
    assert lock.direct_dependencies == {"bundler": "= 4.0.10", "rails": "= 7.2.3.1"}
    assert lock.bundler_version == "4.0.10"
    assert lock.paths_to("activestorage") == [
        ["rails", "activestorage"],
        ["rails", "actionmailbox", "activestorage"],
    ]
