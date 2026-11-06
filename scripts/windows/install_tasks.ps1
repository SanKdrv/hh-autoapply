# Установка расписания в Windows Task Scheduler.
#
# Запуск из PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1
#
# Задачи запускаются только при активном входе пользователя — это важно,
# потому что используется настоящий браузер (Edge), а он требует сессии.

param(
    [string]$TaskPrefix = "hh-autoapply"
)

$ErrorActionPreference = "Stop"

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python     = Join-Path $ProjectDir ".venv\Scripts\python.exe"
# pythonw — запуск без окна консоли (логи всё равно пишутся в logs\agent.out.log)
$PythonW    = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) { $PythonW = $Python }
$ConfigPath = Join-Path $ProjectDir "config.json"

if (-not (Test-Path $Python)) {
    Write-Host "Не найден $Python" -ForegroundColor Red
    Write-Host "Создай окружение:  py -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$applyLimit = if ($cfg.schedule.apply_limit) { $cfg.schedule.apply_limit } else { 12 }
$everyHours = if ($cfg.schedule.every_hours) { $cfg.schedule.every_hours } else { 4 }
$digestHour = if ($cfg.diagnostics.hour) { $cfg.diagnostics.hour } else { 21 }

Write-Host "Проект:  $ProjectDir"
Write-Host "Python:  $Python"
Write-Host "Лимит откликов за цикл: $applyLimit"
Write-Host ""

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

function Register-HhTask {
    param([string]$Name, [string]$Arguments, $Triggers)

    # -X utf8: без этого кириллица в логах Windows падает на кодировке
    $action = New-ScheduledTaskAction -Execute $PythonW -Argument "-X utf8 $Arguments" -WorkingDirectory $ProjectDir
    Register-ScheduledTask -TaskName "$TaskPrefix-$Name" `
        -Action $action -Trigger $Triggers -Settings $settings -Principal $principal `
        -Force | Out-Null
    Write-Host "  установлена задача: $TaskPrefix-$Name"
}

# Циклы в рабочие часы: первый в start_hour (по умолчанию 10:00), далее
# каждые every_hours до 22:00. При every_hours=4 это 10:00, 14:00, 18:00.
$startHour = if ($null -ne $cfg.schedule.start_hour) { [int]$cfg.schedule.start_hour } else { 10 }
$hours = @()
$h = $startHour
while ($h -lt 22) { $hours += $h; $h += $everyHours }
if ($hours.Count -eq 0) { $hours = @($startHour) }

$cycleTriggers = @()
foreach ($hour in $hours) {
    $cycleTriggers += New-ScheduledTaskTrigger -Daily -At ("{0:d2}:00" -f $hour)
}
Register-HhTask -Name "cycle" -Arguments "main.py run --limit $applyLimit" -Triggers $cycleTriggers
Write-Host "    часы запуска: $($hours -join ', ')"

# Telegram-бот: в фоне (без окна консоли), при входе, с автоперезапуском
# и сторожевым таймером на случай падения.
$botAction = New-ScheduledTaskAction -Execute $PythonW -Argument "-X utf8 main.py bot" -WorkingDirectory $ProjectDir
$botLogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Сторож: каждые 10 минут проверяем, что бот жив. Если он уже запущен,
# повтор игнорируется (MultipleInstances=IgnoreNew); если упал — поднимаем.
# Вместе с триггером «при входе» это закрывает и перезагрузку: после неё
# бот поднимается сразу при входе пользователя.
$botWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$botTrigger = @($botLogonTrigger, $botWatchdog)
$botSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "$TaskPrefix-bot" -Action $botAction -Trigger $botTrigger `
    -Settings $botSettings -Principal $principal -Force | Out-Null
Write-Host "  установлена задача: $TaskPrefix-bot (фон без консоли, автозапуск при входе, сторож каждые 10 мин)"

# Анализ логов раз в день
$digestTrigger = New-ScheduledTaskTrigger -Daily -At ("{0:d2}:00" -f $digestHour)
Register-HhTask -Name "digest" -Arguments "main.py digest" -Triggers $digestTrigger
Write-Host "    час: $digestHour:00"

# Досылка недостающих сопроводительных (когда отклик ушёл без письма).
# Прикладываем письмо к отклику через «Add a cover letter»; уже отправленные не дублируем.
$followupHour = if ($null -ne $cfg.schedule.followup_hour) { [int]$cfg.schedule.followup_hour } else { 20 }
$followupTrigger = New-ScheduledTaskTrigger -Daily -At ("{0:d2}:00" -f $followupHour)
Register-HhTask -Name "followup" -Arguments "main.py followup" -Triggers $followupTrigger
Write-Host "    час: $followupHour:00"

# Проверка переписки каждый час: новые ответы работодателей → Telegram + черновики.
$inboxTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-HhTask -Name "inbox" -Arguments "main.py check" -Triggers $inboxTrigger
Write-Host "    переписка: каждые 1 ч"

# Поднятие резюме в поиске. hh разрешает раз в 4 часа, но окно отсчитывается
# от фактического поднятия, поэтому проверяем часто: пока окно закрыто команда
# выходит мгновенно (браузер не поднимает), а в первые минуты окна — поднимает.
# Триггер при входе важен отдельно: после выключения/сна пропущенные 15-минутные
# тики Windows не всегда догоняет, а при входе поднятие произойдёт сразу.
$boostLogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$boostWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-HhTask -Name "boost" -Arguments "main.py boost" -Triggers @($boostLogonTrigger, $boostWatchdog)
Write-Host "    резюме в поиске: при входе + проверка каждые 15 мин (поднятие — когда откроется окно hh)"

# Другие биржи (config.json → boards.enabled). Каждая идёт своей задачей,
# поэтому циклы работают параллельно: у бирж разные профили браузера и разные
# файлы-блокировки (run-<биржа>.lock), так что они не мешают друг другу.
foreach ($b in $cfg.boards.enabled) {
    if ($b -eq "hh") { continue }
    $bc = $cfg.boards.PSObject.Properties[$b].Value
    $bTitle = if ($bc -and $bc.title) { $bc.title } else { $b }
    $bStart = if ($bc -and $null -ne $bc.start_hour) { [int]$bc.start_hour } else { $startHour }
    $bLimit = if ($bc -and $bc.apply_limit) { $bc.apply_limit } else { $applyLimit }

    $bHours = @()
    $bh = $bStart
    while ($bh -lt 22) { $bHours += $bh; $bh += $everyHours }
    if ($bHours.Count -eq 0) { $bHours = @($bStart) }
    # :30 — чтобы циклы бирж не стартовали одновременно с hh (у каждой биржи
    # свой профиль браузера и своя блокировка, они работают параллельно)
    $bCycleTriggers = @()
    foreach ($hour in $bHours) {
        $bCycleTriggers += New-ScheduledTaskTrigger -Daily -At ("{0:d2}:30" -f $hour)
    }
    Register-HhTask -Name "cycle-$b" -Arguments "main.py run --limit $bLimit --board $b" -Triggers $bCycleTriggers

    # переписка — раз в час (со сдвигом на 20 минут, чтобы не совпадать с hh)
    $bInboxTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(20) `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-HhTask -Name "inbox-$b" -Arguments "main.py check --board $b" -Triggers $bInboxTrigger

    # поднятие резюме: при входе + проверка каждые 15 минут
    $bBoostLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $bBoostWatch = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-HhTask -Name "boost-$b" -Arguments "main.py boost --board $b" -Triggers @($bBoostLogon, $bBoostWatch)

    Write-Host "    биржа ${bTitle}: циклы $($bHours -join ', ') — задачи $TaskPrefix-cycle-$b, -inbox-$b, -boost-$b"
}

Write-Host ""
Write-Host "Готово. Проверить:  Get-ScheduledTask -TaskName '$TaskPrefix*'" -ForegroundColor Green
Write-Host "Запустить сейчас:   Start-ScheduledTask -TaskName '$TaskPrefix-cycle'"
Write-Host "Удалить:            powershell -File scripts\windows\uninstall_tasks.ps1"
