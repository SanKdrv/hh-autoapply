# Первичная настройка hh-autoapply на Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
#
# Создаёт окружение, ставит зависимости и готовит .env.

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $ProjectDir

Write-Host "Проект: $ProjectDir" -ForegroundColor Cyan
Write-Host ""

# 1. Python
$py = $null
foreach ($candidate in @("py", "python")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0) { $py = $candidate; Write-Host "Python найден: $ver ($candidate)"; break }
    } catch { }
}
if (-not $py) {
    Write-Host "Python не найден. Установи с python.org и включи «Add Python to PATH»." -ForegroundColor Red
    exit 1
}

# 2. Виртуальное окружение
$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Создаю окружение .venv ..."
    & $py -m venv .venv
}
if (-not (Test-Path $venvPython)) {
    Write-Host "Не удалось создать окружение." -ForegroundColor Red
    exit 1
}

# 3. Зависимости
Write-Host "Ставлю зависимости ..."
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt
Write-Host "  selenium: $(& $venvPython -c "import selenium; print(selenium.__version__)")"

# 4. .env
$envFile = Join-Path $ProjectDir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $ProjectDir ".env.example") $envFile
    Write-Host ""
    Write-Host "Создан .env — заполни ключи:" -ForegroundColor Yellow
    Write-Host "  DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"
    Write-Host "Открой:  notepad `"$envFile`""
    Write-Host "Затем:   .venv\Scripts\python main.py notify-test"
} else {
    Write-Host ".env уже есть — не трогаю."
}

# 5. Браузер
$edge = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($edge) {
    Write-Host "Edge найден: $edge"
} else {
    Write-Host "Microsoft Edge не найден — он нужен для автоотклика." -ForegroundColor Yellow
    Write-Host "Обычно уже установлен в Windows; если нет — поставь вручную."
}

Write-Host ""
Write-Host "Дальше по порядку:" -ForegroundColor Green
Write-Host "  1) notepad .env                        # ключи"
Write-Host "  2) .venv\Scripts\python main.py login  # один раз войти в hh"
Write-Host "  3) .venv\Scripts\python main.py selftest"
Write-Host "  4) powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1"
