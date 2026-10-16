# AI Development Harness — Maintainer Tools

Служебный control plane для сопровождения **самого AI Development Harness**. Он отделён от [`ai-development-harness-template`](https://github.com/ai-development-harness/ai-development-harness-template), поэтому maintainer-only workflow не копируются в пользовательские проекты.

## Release flow

Обычный merge PR в любую Harness-ветку **не выпускает релиз**. Релиз использует два ручных workflow и отдельную автоматическую Release Qualification между подготовкой candidate и публикацией:

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
              ▼
 Harness Release Qualification
       exact candidate SHA
              │
       review / squash merge
              │
              ▼
 Harness Release Qualification
        exact merge SHA
              │
              ▼
      Publish Harness Release
              │
              ▼
       tag vX.Y.Z + GitHub Release
              │
              ▼
      promotion/vX.Y.Z + canary PR
              │
      независимый CI / review / merge
              │
              ▼
 Verify Harness Release Completion
  canary/main: version + exact source SHA
              │
              ▼
       выпуск завершён (COMPLETE)
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
8. открывает PR `chore: подготовить release vX.Y.Z` **в выбранную source branch**;
9. экспортирует exact candidate SHA и автоматически вызывает reusable `Harness Release Qualification` для этого commit.

Tag и GitHub Release здесь **не создаются**. Qualification также не создаёт tag/release и не изменяет release metadata.

До получения write token и изменения metadata Prepare обязательно запускает `scripts/release_completion.py`: последний опубликованный stable уже должен быть принят в `release-canary/main` через merged promotion PR. Неактуальная база, открытый/закрытый без merge PR, ошибка API или несовпадение source SHA блокируют Prepare до создания нового candidate.

Важно: release snapshot намеренно **не содержит** `lock.source.commit`. SHA самого release commit невозможно знать до создания этого commit, поэтому self-pin в snapshot был бы либо устаревшим, либо вымышленным. После установки/обновления проекта deterministic updater уже разрешает immutable tag и записывает его точный OID в project lock.

### Harness Release Qualification

`.github/workflows/release-qualification.yml` — reusable + manually dispatchable workflow для release-level проверки **exact Harness SHA**.

Он:

1. валидирует `target_repository / target_ref / target_sha` и доказывает, что exact SHA достижим из выбранной Harness branch;
2. создаёт в Harness repository отдельный GitHub Check с устойчивым именем `Harness Release Qualification`, привязанный именно к `target_sha`;
3. создаёт scoped App token только для target Harness repository;
4. читает public `release-canary` без App credentials, разрешает exact SHA accepted canary `main` и использует его как immutable baseline конкретного run;
5. запускает canonical core lanes:
   - current Linux / Python 3.13;
   - minimum Linux / Python 3.11;
   - Windows / Python 3.13;
6. запускает canonical initialized downstream upgrade runner на disposable canary baseline;
7. сохраняет machine-readable evidence каждого lane как Actions artifact;
8. через `always()` finalizer завершает exact-SHA Check как success только если **все** обязательные jobs завершились success.

Workflow не копирует stress logic: bounded stress является частью canonical `release-qualification.py --lane current` в Harness core.

Qualification использует **двухуровневую timeout-защиту**:

- canonical core runner ограничивает отдельный gate и пишет machine-readable `TIMEOUT / timeoutSeconds / durationMs` evidence;
- GitHub Actions job имеет независимый outer `timeout-minutes` на случай зависания самого runner или platform tooling.

Outer budgets намеренно шире обычного inner gate budget: preflight/finalize — 5 минут, minimum — 15 минут, current/windows/downstream — 20 минут. Job-level timeout является last resort и не заменяет inner evidence.

Если public canary недоступен, target SHA/ref не совпадают, lane падает, timeout срабатывает, evidence incomplete или finalizer не может обновить Check — qualification не считается успешной.

Prepare автоматически вызывает этот workflow для prepared candidate SHA. Повторная qualification exact merge SHA перед Publish подключается отдельным publish hard gate и не подменяется candidate PASS.

### Publish Harness Release

`.github/workflows/publish-release.yml` запускается после merge release PR.

Он требует снова явно указать Harness branch, находит **единственный merged** PR `release/vX.Y.Z` с этой base branch и разрешает его exact merge SHA. Исторические closed/unmerged candidates того же version/head/title игнорируются: это позволяет безопасно пересобирать release candidate после fail-closed qualification, не создавая ambiguity при Publish. Затем Publish **сам запускает Release Qualification для exact merge SHA** и не переиспользует candidate PASS. После успешной qualification отдельный hard gate читает Check Runs этого же SHA и требует последние успешные:
- `Harness Release Qualification` от configured Release Bot App;
- `Validate Harness` от GitHub Actions;
- `Validate Python 3.11 compatibility` от GitHub Actions;
- `Validate Windows boundaries` от GitHub Actions.

После проверки metadata/local validator hard gate опрашивает Check Runs каждые 15 секунд и ждёт до 15 минут: отсутствующие или ещё выполняющиеся обязательные проверки (`queued / in_progress / waiting / pending / requested`) удерживают публикацию в ожидании. Каждый опрос заново выбирает последний Check Run на exact merge SHA, поэтому старый PASS не перекрывает новый запуск. Неуспешно завершённая проверка, неизвестное состояние или другой App немедленно блокируют tag/Release. Ошибка чтения GitHub API также останавливает публикацию; Check Run другого SHA не засчитывается. При исчерпании времени выводится `RELEASE GATES: TIMEOUT`, tag/Release не создаются. Внешний `timeout-minutes: 16` ограничивает сам шаг ожидания.

Tag привязывается **не к текущему HEAD выбранной Harness-ветки**, а к merge commit release PR. Поэтому изменения, случайно влитые после подготовки релиза, не попадут в уже подготовленный release.

Publish идемпотентен для частичного сбоя: существующий tag допустим только если уже указывает на ожидаемый commit; существующий Release допустим только с ожидаемым названием.


### Automatic release-canary promotion

`.github/workflows/promote-canary.yml` создаёт **отдельный PR** с обновлением принятого canary baseline **после успешного Publish Harness Release**. Публикация и продвижение независимы: ошибка promotion не отменяет уже опубликованный tag или GitHub Release.

Триггеры: `workflow_run` (только успешный `Publish Harness Release` из `main`), `schedule` каждый час в минуту 17 UTC (резерв для ранее опубликованных/пропущенных релизов) и ручной `workflow_dispatch` без обязательных параметров. Schedule выполняет повторную сверку; если canary уже соответствует последнему стабильному релизу, изменений нет.

**Алгоритм:**

1. Прочитать exact `release-canary/main` и version/source commit из canary lock; найти **ближайший следующий** опубликованный стабильный Harness Release по SemVer. Draft и prerelease не подходят. При нескольких пропущенных версиях нельзя перепрыгивать через промежуточную: сначала review/merge PR первого релиза.
2. Разрешить immutable target tag/SHA, удостовериться, что commit достижим из Harness `main` и граф содержит **прямой** переход из установленной stable; отклонить другие ветки/неполную identity. При существующей `promotion/vX.Y.Z` не перезаписывать её и не создавать дубликат PR.
3. Создать отдельный checkout точного baseline, зафиксировать Git blob SHA/mode project-owned файлов. Вызвать **настоящий** `harness-update.py apply --to vX.Y.Z` и при reload boundary продолжить в новом Python-процессе. Запрещено переписывать manifest/lock вручную ради успешного теста.
4. Проверить project schema (`PROJECT RECONCILE` при необходимости), `HARNESS STATUS`, `HARNESS DOCTOR`, validator, весь `run-self-tests.py`, сохранность исходных project-owned файлов и запрет неожиданных project additions. Затем повторить APPLY и доказать `NO_UPDATE` и отсутствие canonical mutation.
5. Записать `docs/baseline-promotions/vX.Y.Z.md` в canary, создать `promotion/vX.Y.Z` и PR в `release-canary/main` под отдельным GitHub App installation token, ограниченным **только canary**. Токен для push не доступен командам обновления и self-tests.
6. **Не сливать PR автоматически.** Независимый `Harness Integrity` по PR проверяет `Validate Harness`, `Validate Python 3.11 compatibility`, `Validate Windows boundaries`. После проверки diff и успешного CI maintainer вручную сливает PR; только затем новый baseline считается принятым.

Workflow не изменяет `release-canary/main` напрямую. Если target/branch/review/validator/update конфликтуют, он останавливается, не публикует неподтверждённую версию и оставляет диагностику Actions.

**Одноразовая настройка GitHub App** — обязательна для автономного создания PR:

- Открой GitHub App **AI Harness Release Bot** организации и добавь `release-canary` в её установку (раньше она была установлена только на `ai-development-harness-template`).
- В `Permissions & events` добавь repository permission **Workflows: Read & write**, оставь **Contents: Read & write** и **Pull requests: Read & write**. Подтверди изменение permissions/installation. Workflows write необходим, потому что штатный Harness updater может менять `.github/workflows/harness-integrity.yml`; обычный `GITHUB_TOKEN` не годится для этой операции.
- Переменная `HARNESS_RELEASE_APP_CLIENT_ID` и секрет `HARNESS_RELEASE_APP_PRIVATE_KEY` уже используются в `maintainer-tools`; дополнительных секретов автоматизация не требует. Из workflow запрашивается токен с разрешениями `contents: write`, `pull-requests: write`, `workflows: write` **только** для `release-canary`.
- Настрой в `release-canary` branch protection/ruleset для `main` с required checks **Validate Harness**, **Validate Python 3.11 compatibility**, **Validate Windows boundaries** и review перед merge. Само создание PR не делает CI обязательным правилом ветки.

PR, созданный через GitHub App token, автоматически запускает `pull_request` CI (в отличие от обычного `GITHUB_TOKEN`, для которого могут требоваться отдельные approvals). Автоматизация **никогда не использует auto-merge**.

**Операционная поддержка:** workflow — [Promote Published Harness Canary](https://github.com/ai-development-harness/maintainer-tools/actions/workflows/promote-canary.yml). Для разового повтора запусти `Run workflow` в `maintainer-tools/main`: он выбирает ближайший непринятый stable. Если PR уже открыт, дождись CI и заверши review/merge; новый запуск ничего не перезапишет. После успешного merge будет доступно продвижение следующего опубликованного релиза (по расписанию или вручную).

### Обязательное завершение выпуска

Успешный Publish означает, что релиз опубликован. **Выпуск завершён только после принятия этой версии в canary.** Maintainer или агент, выполняющий запрос на выпуск, обязан в рамках того же запроса дождаться promotion PR, проверить diff и три обязательных CI jobs, выполнить review/merge и проверить удалённую базу. Отдельное напоминание пользователя для этого этапа не требуется.

```bash
python3 scripts/release_completion.py --version vX.Y.Z
```

Gate выполняет только read-only GitHub API запросы и возвращает `COMPLETE` лишь при одновременном выполнении условий:

- версия опубликована как stable GitHub Release, tag разрешён в точный commit;
- manifest и lock из одного exact SHA `release-canary/main` содержат эту версию, правильный source repository/ref и commit опубликованного tag;
- существует единственный merged PR `promotion/vX.Y.Z → main` из самого canary; его merge commit входит в историю проверенного `main`;
- `main` не изменился во время проверки.

Результат включает release, source SHA, canary SHA, promotion PR URL и merge SHA. `BLOCKED` с ненулевым exit code означает незавершённый выпуск. Созданный PR, зелёный promotion workflow или совпадение локальной версии не заменяют этот gate. Если продолжить нельзя, итоговый отчёт обязан назвать blocker и незавершённый promotion; уже опубликованный tag/Release не откатывается.

`.github/workflows/release-completion.yml` (**Verify Harness Release Completion**) запускает gate после каждого завершения promotion workflow, включая неуспешное, и доступен вручную. Пока PR открыт, ожидается `BLOCKED`; после merge выполните ручной запуск для немедленного подтверждения. Ежечасный promotion повторно запускает проверку автоматически. Evidence сохраняется в Actions summary. Без `--version` проверяется последний stable по SemVer; появление нового stable во время проверки требует повторного запуска. Этот же режим является обязательным preflight следующего Prepare, без обхода для выбранной source branch.

**Regression:** `python -m unittest discover -s tests -v` включает select/no-op, несколько пропущенных releases, запрет prerelease и mismatched refs, reload/idempotence, preservation project-owned и direct transition. Доступ к GitHub App/срабатывание удалённого workflow проверяется отдельно уже после установки App; локальные unit-тесты не подтверждают права GitHub.


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
| Checks | Read & write |
| Metadata | Read-only |

Webhook не требуется.

После создания установи App в организации `ai-development-harness` **только** на:

```text
ai-development-harness-template
```

`release-canary` публичный и qualification читает его без App credentials. Добавлять canary в installation GitHub App не требуется.

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

Workflow получает короткоживущие installation tokens через `actions/create-github-app-token@v3`. Prepare/Publish scope-ят token под нужные mutating permissions target repository; Release Qualification создаёт target-only token для exact-SHA Check и Harness checkout. Public canary читается без App token.

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

Появится release PR в `ai-development-harness-template`. После push candidate автоматически стартует `Harness Release Qualification` для exact candidate SHA. Перед merge проверь diff, обычный Harness Integrity и отдельный qualification Check.

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

**4. Принять canary и завершить выпуск**

Дождись автоматического promotion PR в `release-canary`, проверь diff и три Harness Integrity checks, выполни review/merge. Запусти `python3 scripts/release_completion.py --version v0.2.7` и сохрани `COMPLETE` с PR URL и exact SHA в итоговом отчёте. Для подтверждения в Actions запусти **Verify Harness Release Completion**. На этом этапе заканчивается исходный запрос на выпуск релиза.

## Failure policy

- Harness branch не указана, невалидна или не существует → workflow блокируется; fallback на `main` отсутствует.
- Source branch сдвинулась во время Prepare → workflow блокируется до push release branch.
- Exact candidate SHA не достижим из выбранной release/source branch → Release Qualification блокируется.
- Public `release-canary` нельзя прочитать или разрешить его exact branch SHA → qualification Check завершается failure.
- Current/minimum/Windows/downstream gate failed/skipped/incomplete или превысил timeout → exact-SHA qualification не может стать success.
- Зависание canonical runner/platform tooling дольше outer job timeout → Actions принудительно завершает job, finalizer фиксирует qualification failure.
- Нет merged release PR в выбранной Harness branch → publish блокируется.
- Merge commit release PR больше не достижим из выбранной Harness branch → publish блокируется.
- Exact merge SHA не прошёл свежую `Harness Release Qualification` → tag/Release не создаются.
- Последний обязательный Check Run на exact merge SHA отсутствует или ещё выполняется → ожидание до 15 минут, затем timeout без tag/Release; завершился неуспешно → немедленная блокировка.
- Check с ожидаемым именем создан не ожидаемым GitHub App → gate считает его недействительным.
- Metadata не соответствует requested version → publish блокируется до создания tag.
- Tag существует на другом commit → tag не перемещается.
- Release branch/tag/release уже существует при Prepare → overwrite не выполняется.
- Текущий release-state неконсистентен → Prepare блокируется и требует recovery PR.
- Последний stable не принят в canary через merged promotion PR или exact pin не совпадает → выпуск остаётся незавершённым, следующий Prepare блокируется.

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
.github/workflows/release-qualification.yml
.github/workflows/publish-release.yml
config/release.json
scripts/release.py
tests/test_release.py
tests/test_workflows.py
```

## Главное правило

Релиз Harness больше не начинается с **Draft a new release**.

Сначала:

```text
Prepare Harness Release
        ↓
Harness Release Qualification (exact candidate SHA)
```

после зелёного candidate qualification и review/merge:

```text
Publish Harness Release
```

Candidate qualification не является разрешением переиспользовать PASS для другого SHA. При запуске Publish exact release merge SHA автоматически квалифицируется повторно; tag создаётся только после успешной qualification и successful Harness Integrity checks на том же SHA.

После Publish обязательно заверши canary promotion и получи `COMPLETE` от release completion gate. Только после этого сообщай, что выпуск завершён, и готовь следующий релиз.
