# depfix-agent

依存関係の脆弱性修正を、隔離したclone上で計画・実行・検証するエージェントループの実験実装です。

LLMは「どの直接依存や制約を変更するか」を計画します。manifest編集、パッケージマネージャーの実行、lockfile確認、終了判定は決定論的なコードが担当します。

現在のMVPはnpmとBundlerを自動検出します。デフォルトはAPI不要のヒューリスティックplannerで、npmのローカルpeer dependencyや、Railsのようにコンポーネントを同一バージョンへ固定する構成を処理できます。`--planner ai`を指定するとOpenAI Responses APIの構造化出力をplannerとして使用します。

## Architecture

```text
PreviewOrchestrator
├── isolated shallow git clone (hooks disabled)
├── ecosystem adapter
│   ├── npm: package.json / package-lock.json
│   └── Bundler: Gemfile / Gemfile.lock
├── planner
│   ├── heuristic
│   └── AI structured planner
└── verification gate
    ├── dependency graph resolves
    ├── target version is safe
    ├── only allowlisted files changed
    └── staged diff is reviewable
```

各試行は新しいcloneで実行されます。元のリポジトリは変更しません。成功時はstaged diffをpreviewとして返します。

コアのループはpackage manager非依存で、adapter protocolが検出、manifest編集、resolver実行、lockfile読取を担当します。対応ecosystemはadapter単位で増やします。現時点の実装済みadapterはnpmとBundlerで、pnpm、Yarn、Bunは未実装です。

## Workflowとagent loopの境界

実行ログに現れる大部分は、意図的に固定したworkflowです。

1. リポジトリを隔離cloneする
2. package managerとmanifestを検出する
3. plannerが次の変更候補を1つ返す
4. 制約付きeditorがmanifestを変更する
5. package managerでlockfileを再解決する
6. 安全版、変更ファイル、差分を検証する
7. 失敗ならresolverの結果をplannerへ戻し、別の候補を試す

「agentic」なのはステップ自体を自由に選ぶことではなく、3と7の閉ループです。clone、コマンド実行、成功判定、終了条件はplannerに委ねません。また、Aikidoの画面に`agentic ... update`と表示されることだけでは、内側でLLMが使われているかは判定できません。この実装ではplannerを交換可能にし、同じworkflowをヒューリスティックでもLLMでも動かせます。

## npm peer dependency example

```sh
uv run depfix preview \
  --repo ../aikido-peer-dependency-repro \
  --package lodash \
  --safe-version 4.18.1 \
  --planner heuristic \
  --output npm-peer.patch
```

このfixtureではrootの`lodash`だけを更新する最初の試行が`ERESOLVE`になります。2回目はローカルpackageの`peerDependencies`も同じ安全版へ揃えます。生成差分はAikido PR #2と一致する3ファイルです。

## Rails / Active Storage example

```sh
uv sync
uv run depfix preview \
  --repo ../aikido-peer-dependency-repro \
  --workspace rails-activestorage \
  --package activestorage \
  --safe-version 7.2.3.2 \
  --planner heuristic \
  --output activestorage.patch
```

この例では、最初にActive Storage単体のpinを試します。Bundlerが`rails (= 7.2.3.1)`との競合を返すと、次の試行で直接依存のRailsも`7.2.3.2`へ更新します。

確認済みの実行結果:

```text
attempt 1: activestorage >= 7.2.3.2
           -> rails 7.2.3.1との競合で失敗
attempt 2: rails 7.2.3.2 + activestorage >= 7.2.3.2
           -> activestorage 7.2.3.2をlockし、検証成功
```

AI plannerを使う場合:

```sh
export OPENAI_API_KEY=...
uv run depfix preview \
  --repo ../aikido-peer-dependency-repro \
  --workspace rails-activestorage \
  --package activestorage \
  --safe-version 7.2.3.2 \
  --planner ai \
  --model gpt-5.1
```

## Safety model

- 元の作業ツリーではなく、一時cloneだけを編集します。
- clone、add、diffではGit hooksを無効化します。
- Bundlerでは`bundle lock`、npmでは`npm install --package-lock-only --ignore-scripts`だけを実行し、アプリケーションコードやtestは自動実行しません。
- 変更可能なファイルはplannerが指定した依存manifestと、対応するlockfileだけです。
- plannerが成功を宣言しても終了できません。lockfileが解決し、安全版が実際に選択された場合だけ成功します。

現段階の隔離は一時cloneによるファイル分離です。コマンド実行をOSレベルで隔離するDocker backend、private registry、修正後test、残存CVEスキャン、PR作成は次の実装対象です。
