from depfix.gemfile import apply_edit
from depfix.models import DependencyEdit


def test_updates_existing_gem_requirement() -> None:
    source = 'source "https://rubygems.org"\n\ngem "rails", "7.2.3.1"\n'

    updated = apply_edit(source, DependencyEdit(package="rails", requirement="7.2.3.2"))

    assert updated == 'source "https://rubygems.org"\n\ngem "rails", "7.2.3.2"\n'


def test_adds_missing_direct_dependency() -> None:
    source = 'source "https://rubygems.org"\n'

    updated = apply_edit(
        source,
        DependencyEdit(package="activestorage", requirement=">= 7.2.3.2", operation="add"),
    )

    assert updated.endswith('\ngem "activestorage", ">= 7.2.3.2"\n')
