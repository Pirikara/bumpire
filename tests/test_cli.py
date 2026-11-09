import pytest

from bumpire.cli import _parser, _request


def _parse(*arguments: str):
    return _parser().parse_args(["preview", "--repo", ".", *arguments])


def test_publish_parser_reuses_update_inputs() -> None:
    args = _parser().parse_args(["publish", "--repo", ".", "--target", "lodash", "4.18.1"])

    assert args.command == "publish"
    assert args.target == [["lodash", "4.18.1"]]
    assert args.remote == "origin"
    assert args.base is None


def test_publish_parser_accepts_a_verified_artifact() -> None:
    args = _parser().parse_args(["publish", "--repo", ".", "--artifact-input", "verified.json"])

    assert args.artifact_input.name == "verified.json"
    assert args.target is None


def test_repeated_targets_form_one_batch() -> None:
    request = _request(
        _parse("--target", "rails", "7.2.3.2", "--target", "activestorage", "7.2.3.2"),
        "bundler",
    )

    assert [target.package for target in request.targets] == ["rails", "activestorage"]
    assert all(target.requirement.version == "7.2.3.2" for target in request.targets)
    assert all(target.requirement.kind == "exact" for target in request.targets)


def test_explicit_minimum_requirement_is_parsed() -> None:
    request = _request(_parse("--target", "lodash", ">=4.18.1"), "npm")

    assert request.targets[0].requirement.kind == "at_least"
    assert request.targets[0].requirement.version == "4.18.1"


def test_single_target_compatibility_options_remain_supported() -> None:
    request = _request(
        _parse(
            "--package",
            "lodash",
            "--safe-version",
            "4.18.1",
        ),
        "npm",
    )

    assert request.targets[0].package == "lodash"
    assert request.targets[0].requirement.kind == "at_least"


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


def test_removal_must_name_a_requested_package() -> None:
    args = _parse("--target", "lodash", "4.18.1", "--allow-removal", "rails")

    with pytest.raises(ValueError, match="not targets"):
        _request(args, "npm")


@pytest.mark.parametrize("requirement", ["^4.18.1", "!=4.18.1", "<5.0.0"])
def test_unsupported_requirement_operator_is_rejected(requirement: str) -> None:
    with pytest.raises(ValueError, match="support only"):
        _request(_parse("--target", "lodash", requirement), "npm")
