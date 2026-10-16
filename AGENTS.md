# Сопровождение выпуска Harness

Процесс и команды: [README.md](README.md). Документация, комментарии и commit messages — на русском языке.

Запрос выпустить релиз включает полный цикл: Prepare, qualification, review/merge release PR, Publish, затем review/merge promotion PR в `release-canary` и проверку принятой базы. Не заканчивай работу на создании tag/Release или открытии promotion PR и не жди отдельного напоминания пользователя.

Перед merge promotion PR проверь diff и успешное завершение трёх независимых проверок: `Validate Harness`, `Validate Python 3.11 compatibility`, `Validate Windows boundaries`. После merge обязательно выполни `python3 scripts/release_completion.py --version vX.Y.Z`; только `COMPLETE` подтверждает завершение. При `BLOCKED` продолжай устранять причину в рамках разрешённого выпуска; опубликованный релиз не откатывай. Если продолжение невозможно, сообщи точный blocker и что promotion не завершён.

Перед Prepare следующего релиза тот же gate без `--version` обязан подтвердить принятие последнего stable. Локальный checkout, зелёный Publish и успешное создание promotion PR не заменяют проверку удалённого `release-canary/main`.
