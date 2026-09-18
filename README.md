# AI Development Harness — Maintainer Tools

Служебный control plane для сопровождения **самого AI Development Harness**. Он отделён от [`ai-development-harness-template`](https://github.com/ai-development-harness/ai-development-harness-template), поэтому maintainer-only workflow не копируются в пользовательские проекты.

## Release flow

Обычный merge PR в `main` **не выпускает релиз**. Релиз состоит из двух ручных workflow:

```text
обычные PR → main
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

1. проверяет, что текущие `manifest / lock / update graph` согласованы;
2. проверяет отсутствие target tag, Release и release branch;
3. через `scripts/release.py` обновляет:
   - `.project/manifest.yaml → harness.release`;
   - `.project/harness.lock.json → release/source.ref`;
   - `.project/harness-update-graph.json → latest`;
   - transition `current → target`;
4. запускает Harness validator;
5. создаёт `release/vX.Y.Z`;
6. открывает PR `chore: подготовить release vX.Y.Z`.

Tag и GitHub Release здесь **не создаются**.

### Publish Harness Release

`.github/workflows/publish-release.yml` запускается после merge release PR.

Он находит именно merged PR `release/vX.Y.Z`, берёт его merge commit, повторно проверяет metadata/validator и только затем создаёт lightweight tag и GitHub Release.

Tag привязывается **не к текущему `main HEAD`**, а к merge commit release PR. Поэтому PR, случайно влитый после подготовки релиза, не попадёт в уже подготовленный release.

Publish идемпотентен для частичного сбоя: существующий tag допустим только если уже указывает на ожидаемый commit; существующий Release допустим только с ожидаемым названием.

## Конфигурация

Target repository и пути metadata находятся в:

```text
config/release.json
```

Deterministic helper:

```bash
python scripts/release.py --config config/release.json current --repo-dir <repo>
python scripts/release.py --config config/release.json prepare --repo-dir <repo> --version vX.Y.Z
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
version: v0.2.7
```

Появится release PR в `ai-development-harness-template`. Проверь diff и CI.

**2. Merge**

Сделай обычный **Squash and merge** release PR. Tag/Release вручную не создавай.

**3. Publish**

Открой:

- [Publish Harness Release](https://github.com/ai-development-harness/maintainer-tools/actions/workflows/publish-release.yml)

и нажми **Run workflow**:

```text
version: v0.2.7
title:   v0.2.7 — Release title
```

Workflow найдёт merge commit release PR и только после повторной проверки создаст tag + Release.

## Failure policy

- Нет merged release PR → publish блокируется.
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
