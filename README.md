# Bumpire

依存関係をbumpし、その判定を取り仕切るagentic dependency updaterです。脆弱性修正を、隔離したclone上で計画・実行・検証します。

LLMは、許可されたdependency manifestを読み、「どのファイルをどう変更するか」を計画します。package managerの検出条件、許可manifest、resolver環境、lockfile確認方法は宣言的なTOML profileで与えます。コマンド実行、変更範囲、安全版、終了判定は共通の決定論的コードが担当します。

このツールの責務は、安全版へ解決できる最小の依存関係差分を生成し、レビュー可能なPRとして提出するところまでです。アプリケーションのtest、lint、build、互換性判定は導入先リポジトリの既存CIへ委ねます。

現在のMVPはnpmとBundlerをTOML profileで自動検出します。package manager共通のmanifest replacement toolをagentが呼び、モデルproviderはLiteLLM経由で選択します。package manager固有のPython adapterは持ちません。

## Architecture

```text
PreviewOrchestrator
├── isolated shallow git clone (hooks disabled)
├── Docker resolver sandbox (default)
├── declarative package-manager profile
│   ├── detection + allowed manifests
│   ├── runtime image + resolver procedure
│   └── resolved-version probe
├── planner
│   └── Agents SDK + LiteLLM
└── verification gate
    ├── dependency graph resolves
    ├── target version is safe
    ├── only allowlisted files changed
    └── staged diff is reviewable
```

各試行は新しいcloneと一時コンテナで実行されます。元のリポジトリは変更しません。成功時はstaged diffをpreviewとして返します。resolverは常にDocker内で実行します。

agent loopのコアはpackage manager非依存です。agentにはprofileが許可した既存manifestの内容だけを渡し、agentは変更するmanifestの全文をtoolへ返します。共通executorはそれを新しいcloneへ書き、profile内の信頼済みresolver手順をDockerで実行し、安全版と変更ファイルを検証します。agentはimage、shell command、lockfile、成功条件を変更できません。

新しいpackage managerは原則として`bumpire/profiles/*.toml`を追加します。profileに必要なのは次の情報です。

- workspaceの検出条件とmanifest glob
- manifest内で変更可能なdependency領域
- 変更を許可するlockfile
- runtime versionの取得方法とDocker image
- lockfileを再解決する手順
- 解決済みtarget versionを取り出す手順
- agent向けのpackage manager固有ヒント

この範囲で表現できるpackage managerなら、Python adapterは不要です。一方で、特殊な認証、複数lockfileの選択、独自sandbox、lockfileの複雑な意味解析など新しい安全性primitiveが必要なら共通executor側へ実装を足します。つまり「差分の考案」はagentへ寄せますが、「何を実行・変更・成功と認めるか」はprofileと共通コードに残します。現在のbuiltin profileはnpmとBundlerで、pnpm、Yarn、Bunはまだ追加していません。

## Workflowとagent loopの境界

実行ログに現れる大部分は、意図的に固定したworkflowです。

1. リポジトリを隔離cloneする
2. package managerとmanifestを検出する
3. plannerが次の変更候補を1つ返す
4. 制約付きeditorがmanifestを変更する
5. package managerでlockfileを再解決する
6. 安全版、変更ファイル、差分を検証する
7. 失敗ならresolverの結果をplannerへ戻し、別の候補を試す

「agentic」なのはステップ自体を自由に選ぶことではなく、3と7の閉ループです。clone、コマンド実行、成功判定、終了条件はplannerに委ねません。また、Aikidoの画面に`agentic ... update`と表示されることだけでは、内側でLLMが使われているかは判定できません。この実装ではplannerを交換可能にし、同じworkflowをヒューリスティックでもLLMでも動かせます。汎用Agent plannerが呼べる書込み系toolは`try_profile_fix`だけで、各呼出しは新しい隔離clone上でresolverとverification gateを実行します。

## npm peer dependency example

```sh
uv run bumpire preview \
  --repo ../aikido-peer-dependency-repro \
  --package lodash \
  --safe-version 4.18.1 \
  --model openrouter/openrouter/free \
  --output npm-peer.patch
```

このfixtureではagentがrootの`lodash`とローカルpackageの`peerDependencies`を同じ安全版へ揃えます。生成差分はAikido PR #2と一致する3ファイルです。

## Rails / Active Storage example

```sh
uv sync
uv run bumpire preview \
  --repo ../aikido-peer-dependency-repro \
  --workspace rails-activestorage \
  --package activestorage \
  --safe-version 7.2.3.2 \
  --model openrouter/openrouter/free \
  --output activestorage.patch
```

この例では、agentがGemfileとprofile guidanceから、Active Storage単体ではなく直接依存のRailsを`7.2.3.2`へ更新します。

確認済みの実行結果:

```text
attempt 1: rails 7.2.3.2
           -> activestorage 7.2.3.2をlockし、検証成功
```

## Agent planner and model providers

OpenRouterの無料モデルルーターを使う場合:

```sh
export OPENROUTER_API_KEY=...
uv run bumpire preview \
  --repo ../aikido-peer-dependency-repro \
  --package lodash \
  --safe-version 4.18.1 \
  --model openrouter/openrouter/free
```

モデル名はLiteLLM形式です。例えば`gemini/...`、`groq/...`、`ollama_chat/...`などへ同じagent loopを接続できます。provider標準のAPI key環境変数を利用するか、`--api-key-env MY_PROVIDER_KEY`で明示します。OpenAI Agents SDKはtool実行とturn管理に使用しますが、モデルproviderをOpenAIへ固定しません。

agentはmanifest本文を生成できるぶん柔軟ですが、実行可能なresolver commandは信頼済みprofileに固定されています。Dockerは安全境界の一部なので必須です。

## Safety model

- 元の作業ツリーではなく、一時cloneだけを編集します。
- clone、add、diffではGit hooksを無効化します。
- resolverはデフォルトで一時Dockerコンテナ内に実行します。root filesystemはread-only、Linux capabilitiesは全削除、`no-new-privileges`、PID・memory・CPU上限を設定します。
- 一時cloneだけをread-write mountし、`.git` metadataは別のread-only mountで保護します。
- コンテナが作ったstaged、unstaged、untrackedファイルをすべて検査し、allowlist外の変更があれば失敗させます。
- Bundlerでは`bundle lock`、npmでは`npm install --package-lock-only --ignore-scripts`だけを実行し、アプリケーションコードやtestは自動実行しません。
- agentが置換できるのはprofileのglobに一致する既存dependency manifestだけです。対応するlockfileはagentではなくresolverだけが更新します。
- plannerが成功を宣言しても終了できません。lockfileが解決し、安全版が実際に選択された場合だけ成功します。
- resolver成功は依存グラフと安全版の選択だけを証明します。API・挙動・runtime互換性は主張せず、その判定はPR上で動く導入先リポジトリのCIに委ねます。

コンテナのnetworkは現在registry取得に必要な通常のbridge接続です。registry allowlist、private registry認証、残存CVEスキャン、branch作成、push、PR作成、CI statusのPR表示が次の実装対象です。

## Scope boundary

このツールが成功として扱う範囲:

- manifestとlockfileが安全版へ解決できる
- 変更ファイルがallowlist内である
- レビュー可能な差分とPRを作成できる

導入先リポジトリのCIが判定する範囲:

- unit／integration test
- lint、typecheck、build
- アプリケーション固有のAPI・挙動・runtime互換性
- deploy可能性
