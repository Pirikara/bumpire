import json

from depfix.json_manifest import apply_dependency_edits
from depfix.models import DependencyEdit


def test_edits_root_and_local_peer_manifests(tmp_path) -> None:
    root = tmp_path / "package.json"
    peer = tmp_path / "packages" / "peer" / "package.json"
    peer.parent.mkdir(parents=True)
    root.write_text('{"dependencies":{"lodash":"4.17.20"}}\n', encoding="utf-8")
    peer.write_text('{"peerDependencies":{"lodash":"4.17.20"}}\n', encoding="utf-8")

    changed = apply_dependency_edits(
        tmp_path,
        [
            DependencyEdit(
                package="lodash",
                requirement="4.17.21",
                manifest_path="package.json",
                section="dependencies",
            ),
            DependencyEdit(
                package="lodash",
                requirement="4.17.21",
                manifest_path="packages/peer/package.json",
                section="peerDependencies",
            ),
        ],
    )

    assert changed == ["package.json", "packages/peer/package.json"]
    assert json.loads(root.read_text())["dependencies"]["lodash"] == "4.17.21"
    assert json.loads(peer.read_text())["peerDependencies"]["lodash"] == "4.17.21"
