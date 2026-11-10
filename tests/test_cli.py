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
    assert all(target.version == "7.2.3.2" for target in request.targets)


def test_explicit_exact_prefix_is_accepted() -> None:
    request = _request(_parse("--target", "lodash", "==4.18.1"), "npm")

    assert request.targets[0].version == "4.18.1"


def test_target_is_required() -> None:
    with pytest.raises(ValueError, match="at least one"):
        _request(_parse(), "npm")


def test_duplicate_batch_package_is_rejected() -> None:
    args = _parse("--target", "lodash", "4.18.0", "--target", "lodash", "4.18.1")

    with pytest.raises(ValueError, match="each package may appear only once"):
        _request(args, "npm")


@pytest.mark.parametrize("version", [">=4.18.1", "^4.18.1", "!=4.18.1", "<5.0.0"])
def test_version_policy_operators_are_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="only an exact version"):
        _request(_parse("--target", "lodash", version), "npm")
