# Установщик II+

Выпуск 2026.10.03 включает Python, ComfyUI, Wan 2.1 T2V 1.3B и все его веса.
Python и ComfyUI отдельно устанавливать не нужно. Требуются Windows 10/11 x64,
.NET Framework 4.8 и Microsoft Visual C++ Runtime; для CUDA — драйвер NVIDIA.

Выбирайте новую папку: существующая установка и её данные сохраняются.
Установщик скачивает 1–4 части одновременно, продолжает прерванную загрузку,
сверяет встроенные SHA-256, распаковывает ZIP прямо из частей и создаёт ярлык.
Нужно место для загрузки и приложения плюс 512 МиБ запаса; размер указан в окне.
После установки скачанные части можно автоматически удалить.
Для докачки выберите ту же папку назначения.

## Сборка

```powershell
.\scripts\build_portable.ps1 -OutputDir dist\II-portable-20261003 -IncludeWeights -IncludeWan
.\.venv\Scripts\python.exe scripts/build_installer_payload.py --source dist/II-portable-20261003 --output build/installer-20261003/release --version 2026.10.03 --base-url https://github.com/dydosux/II-downloads/releases/download/v2026.10.03
.\scripts\build_installer.ps1 -Manifest build/installer-20261003/release/manifest.json -Output build/installer-20261003/release/II-Setup.exe
.\.venv\Scripts\python.exe scripts/check_full_install.py --root build/installer-20261003
```

Каталоги сборки должны быть новыми. Личные журналы, входы и выходы ComfyUI,
кэши и .env не включаются. Непустой обучающий набор или подозрительный файл
останавливает упаковку. Веса включаются только с соответствующими параметрами.

Полная проверка использует настоящий установщик и части сборки, затем запускает
установленный Python и проверяет пути и наличие весов Wan. Для экономии места
кэш частей создаётся жёсткими ссылками на готовые файлы. Успех записывается
в full-install-result.txt только после всех проверок. Отдельные тесты
в tests/test_installer.py проверяют HTTP-докачку, обрывы, неверные хеши,
выход из каталога при распаковке и сохранность существующей установки.

## Публикация

EXE, части, manifest.json, SHA256SUMS.txt и лицензии моделей входят в один Release.
Для больших файлов scripts/transfer_blocks.py передаёт блоки по 32 МиБ.
Проверенные блоки пропускаются при повторном запуске. Параметры: prepare или
upload, --root, --repo, --tag, --release (числовой ID черновика).

GitHub Actions assemble-release.yml восстанавливает все части и проверяет размеры
и SHA-256 итоговых файлов. Затем из каталога репозитория выпуска запустите
scripts/assemble_release.py publish через Python с авторизованным GitHub CLI:
он повторно проверит файлы, удалит временные блоки и опубликует выпуск.
Разделение этапов сохраняет запросы в пределах квоты токена GitHub Actions.
Не запускайте несколько издателей одновременно. Готовность подтверждается
только после draft=false и публичной проверки ссылок.

Последний опубликованный установщик:
https://github.com/dydosux/II-downloads/releases/latest/download/II-Setup.exe

SHA-256 частей встроены в EXE: подмена удалённого манифеста не меняет доверенные
хеши. Установщик не подписан сертификатом издателя. Контрольные суммы
подтверждают целостность скачивания, но не заменяют подпись Windows.
