# Удаление задач hh-autoapply из Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_tasks.ps1

param(
    [string]$TaskPrefix = "hh-autoapply"
)

foreach ($name in @("$TaskPrefix-cycle", "$TaskPrefix-bot", "$TaskPrefix-digest")) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "удалена задача: $name"
    }
}
Write-Host "Готово."
