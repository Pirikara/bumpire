import json
from io import StringIO

from depfix.adapters import create_adapter
from depfix.events import EventLogger
from depfix.models import DependencyIssue


def test_detects_npm_and_collects_local_peer_constraints(tmp_path) -> None:
    peer_dir = tmp_path / "packages" / "peer"
    peer_dir.mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"lodash": "4.17.20"}}),
        encoding="utf-8",
    )
    (peer_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "local-peer",
                "peerDependencies": {"lodash": "4.17.20"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/lodash": {"version": "4.17.20"}},
            }
        ),
        encoding="utf-8",
    )

    adapter = create_adapter(tmp_path, ".", EventLogger(stream=StringIO()))
    state = adapter.inspect(
        DependencyIssue(
            ecosystem="npm",
            package="lodash",
            safe_version="4.17.21",
        )
    )

    assert adapter.ecosystem == "npm"
    assert state.target_locked_version == "4.17.20"
    assert [(item.manifest_path, item.section) for item in state.constraints] == [
        ("package.json", "dependencies"),
        ("packages/peer/package.json", "peerDependencies"),
    ]
