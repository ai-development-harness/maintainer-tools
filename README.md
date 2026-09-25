# AI Development Harness — Maintainer Tools

Служебный control plane для сопровождения **самого AI Development Harness**. Он отделён от [`ai-development-harness-template`](https://github.com/ai-development-harness/ai-development-harness-template), поэтому maintainer-only workflow не копируются в пользовательские проекты.

## Release flow

Обычный merge PR в `main` **не выпускает релиз**. Релиз состоит из двух ручных workflow:

```text
изменения → выбранная ветка Harness
             (main / maintenance / release line)
              │
              ▼
      Prepare Harness Release
              │
              ▼
       release/vX.Y.Z + PR
              │
        review / squash merge
              │
              ▼
      Publish Harness Release
              │
              ▼
       tag vX.Y.Z + GitHub Release
```

### Prepare Harness Release

`.github/workflows/prepare-release.yml`:

1. требует явно указать source branch Harness; default branch намеренно отсутствует;
2. проверяет, что выбранная ветка существует, checkout совпадает с её remote HEAD и текущие `manifest / lock / update graph` согласованы;
3. проверяет отсутствие target tag, Release и release branch;
4. через `scripts/release.py` детерминированно определяет единственный полный release layout и обновляет его:
   - новый layout: `.harness/manifest.yaml`, `.harness/harness.lock.json`, `.harness/harness-update-graph.json`;
   - legacy layout: `.project/manifest.yaml`, `.project/harness.lock.json`, `.project/harness-update-graph.json`;
   - manifest → `harness.release`;
   - lock → `release/source.ref`, при этом `source.commit` удаляется из release snapshot;
   - update graph → `latest` и transition `current → target` с явно заданным `reloadRequired`;
5. запускает validator из того же resolved layout;
6. повторно проверяет, что source branch не сдвинулась во время подготовки;
7. создаёт `release/vX.Y.Z`;
8. открывает PR `chore: подготовить release vX.Y.Z` **в выбранную source branch**.

Tag и GitHub Release здесь **не создаются**.

Важно: release snapshot намеренно **не содержит** `lock.source.commit`. SHA самого release commit невозможно знать до создания этого commit, поэтому self-pin в snapshot был бы либо устаревшим, либо вымышленным. После установки/обновления проекта deterministic updater уже разрешает immutable tag и записывает его точный OID в project lock.

### Publish Harness Release

`.github/workflows/publish-release.yml` запускается после merge release PR.

Он требует снова явно указать Harness branch, находит именно merged PR `release/vX.Y.Z` с этой base branch, проверяет, что merge commit всё ещё достижим из выбранной ветки, повторно проверяет metadata/validator и только затем создаёт lightweight tag и GitHub Release.

Tag привязывается **не к текущему `main HEAD`**, а к merge commit release PR. Поэтому PR, случайно влитый после подготовки релиза, не попадёт в уже подготовленный release.

Publish идемпотентен для частичного сбоя: существующий tag допустим только если уже указывает на ожидаемый commit; существующий Release допустим только с ожидаемым названием.

## Конфигурация

Target repository и допустимые release layouts находятся в:

```text
config/release.json
```

Source branch **не хранится в config вообще** и не имеет default. Её нужно явно вводить при каждом Prepare и Publish. Это сделано намеренно: release tooling не должен молча выпускать из `main`.

Helper не выбирает layout по версии Harness и не использует fallback «наиболее похожего» каталога. Релиз разрешён только если в target repository найден **ровно один полный layout** (manifest + lock + update graph + validator). Если одновременно присутствуют оба layout или ни один не полон, операция блокируется.

Deterministic helper:

```bash
python scripts/release.py --config config/release.json layout --repo-dir <repo>
python scripts/release.py --config config/release.json current --repo-dir <repo>
python scripts/release.py --config config/release.json prepare --repo-dir <repo> --version vX.Y.Z
python scripts/release.py --config config/release.json prepare --repo-dir <repo> --version vX.Y.Z --reload-required
python scripts/release.py --config config/release.json prepare --repo-dir <repo> --version vX.Y.Z --reload-required --transition-kind bridge --transition-reason "<reason>"
python scripts/release.py --config config/release.json verify --repo-dir <repo> --version vX.Y.Z
```

Helper использует только Python standard library.

Важно: `prepare` **не исправляет автоматически уже повреждённое release-state**. Если manifest, lock и graph расходятся, workflow блокируется; для такого случая нужен отдельный recovery PR.

## Первичная настройка

### 1. GitHub App

Открой страницу создания App в организации:

- [Create a new GitHub App для `ai-development-harness`](https://github.com/organizations/ai-development-harness/settings/apps/new)
- [Список GitHub Apps организации](https://github.com/organizations/ai-development-harness/settings/apps)
- [Документация GitHub: Creating GitHub Apps](https://docs.github.com/en/apps/creating-github-apps)

Создай GitHub App, например:

```text
AI Harness Release Bot
```

Repository permissions:

| Permission | Access |
|---|---|
| Contents | Read & write |
| Pull requests | Read & write |
| Metadata | Read-only |

Webhook не требуется.

После создания установи App в организации `ai-development-harness` **только** на:

```text
ai-development-harness-template
```

Инструкция GitHub:

- [Installing your own GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- [Choosing permissions for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)

### 2. Private key

На странице настроек созданного GitHub App в секции **Private keys** нажми **Generate a private key**.

Документация:

- [Managing private keys for GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)

Затем открой страницу repository secrets:

- [`maintainer-tools → Actions secrets`](https://github.com/ai-development-harness/maintainer-tools/settings/secrets/actions)
- [Документация GitHub: Using secrets in GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

Создай repository secret:

```text
HARNESS_RELEASE_APP_PRIVATE_KEY
```

В значение вставь **полное содержимое скачанного PEM-файла**, включая строки `BEGIN ... PRIVATE KEY` и `END ... PRIVATE KEY`.

### 3. Client ID

Client ID находится на странице настроек созданного GitHub App. Это **Client ID**, не legacy App ID.

Открой страницу repository variables:

- [`maintainer-tools → Actions variables`](https://github.com/ai-development-harness/maintainer-tools/settings/variables/actions)
- [Документация GitHub: Store information in variables](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-variables)

Создай repository variable:

```text
HARNESS_RELEASE_APP_CLIENT_ID
```

Workflow получает короткоживущий installation token через `actions/create-github-app-token@v3` и дополнительно scope-ит его на target repository.

Подробности официального сценария GitHub App + Actions:

- [Making authenticated API requests with a GitHub App in a GitHub Actions workflow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)

### 4. Immutable releases

Для `ai-development-harness-template` рекомендуется оставить включённой release immutability.

Документация:

- [Preventing changes to releases](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes)

## Использование

Допустим, текущий release `v0.2.6`, нужен `v0.2.7`.

**1. Prepare**

Открой:

- [Prepare Harness Release](https://github.com/ai-development-harness/maintainer-tools/actions/workflows/prepare-release.yml)

и нажми **Run workflow**:

```text
version:           v0.2.7
harness_branch:    main
reload_required:   false
transition_kind:   standard
transition_reason:
```

Появится release PR в `ai-development-harness-template`. Проверь diff и CI.

`reload_required` задаёт свойство **нового transition**, а не поведение самого workflow. Release helper намеренно не пытается угадать необходимость reload по diff.

Обычно:

```text
reload_required: false
```

Если после применения release старый updater/runtime нельзя безопасно продолжать в той же session, укажи:

```text
reload_required: true
```

Например для breaking protocol release, меняющего сам command interface.

Если переход является bootstrap/compatibility boundary, выбери:

```text
transition_kind:   bridge
reload_required:   true
transition_reason: <почему старый runtime не может безопасно продолжить>
```

Для первого release после `v0.5.3` это обязательный contract: deterministic updater
появляется на границе `v0.5.3 → next`, поэтому переход должен быть `bridge` и
требовать reload.

Если в `config/release.json` указаны `updateGraphMirrors`, helper проверяет их
равенство canonical update graph до prepare и синхронизирует их тем же content.
Это используется для legacy compatibility routing endpoint.

**2. Merge**

Сделай обычный **Squash and merge** release PR. Tag/Release вручную не создавай.

**3. Publish**

Открой:

- [Publish Harness Release](https://github.com/ai-development-harness/maintainer-tools/actions/workflows/publish-release.yml)

и нажми **Run workflow**:

```text
version:        v0.2.7
harness_branch: main
title:          v0.2.7 — Release title
```

Workflow найдёт merge commit release PR именно в указанной `harness_branch` и только после повторной проверки создаст tag + Release. Для Publish указывай **ту же ветку**, которая использовалась при Prepare.

## Failure policy

- Harness branch не указана, невалидна или не существует → workflow блокируется; fallback на `main` отсутствует.
- Source branch сдвинулась во время Prepare → workflow блокируется до push release branch.
- Нет merged release PR в выбранной Harness branch → publish блокируется.
- Merge commit release PR больше не достижим из выбранной Harness branch → publish блокируется.
- Metadata не соответствует requested version → publish блокируется до создания tag.
- Tag существует на другом commit → tag не перемещается.
- Release branch/tag/release уже существует при Prepare → overwrite не выполняется.
- Текущий release-state неконсистентен → Prepare блокируется и требует recovery PR.

## Проверки maintainer-tools

```bash
python -m unittest discover -s tests -v
python -m py_compile scripts/release.py
```

То же выполняет `.github/workflows/ci.yml`.

## Структура

```text
.github/workflows/ci.yml
.github/workflows/prepare-release.yml
.github/workflows/publish-release.yml
config/release.json
scripts/release.py
tests/test_release.py
```

## Главное правило

Релиз Harness больше не начинается с **Draft a new release**.

Сначала:

```text
Prepare Harness Release
```

после review/merge:

```text
Publish Harness Release
```
