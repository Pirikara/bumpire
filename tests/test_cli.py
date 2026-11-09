import pytest

from bumpire.cli import _parser, _request


def _parse(*arguments: str):
    return _parser().parse_args(["preview", "--repo", ".", *arguments])


def test_repeated_targets_form_one_batch() -> None:
    request = _request(
        _parse("--target", "rails", "7.2.3.2", "--target", "activestorage", "7.2.3.2"),
        "bundler",
    )

    assert [target.package for target in request.targets] == ["rails", "activestorage"]
    assert all(target.minimum_version == "7.2.3.2" for target in request.targets)


def test_single_target_compatibility_options_remain_supported() -> None:
    request = _request(
        _parse(
            "--package",
            "lodash",
            "--safe-version",
            "4.18.1",
            "--advisory-id",
            "GHSA-example",
        ),
        "npm",
    )

    assert request.targets[0].package == "lodash"
    assert request.targets[0].advisory_ids == ["GHSA-example"]


def test_batch_and_compatibility_options_cannot_be_mixed() -> None:
    args = _parse(
        "--target",
        "lodash",
        "4.18.1",
        "--package",
        "lodash",
        "--safe-version",
        "4.18.1",
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        _request(args, "npm")


def test_duplicate_batch_package_is_rejected() -> None:
    args = _parse("--target", "lodash", "4.18.0", "--target", "lodash", "4.18.1")

    with pytest.raises(ValueError, match="each package may appear only once"):
        _request(args, "npm")
