<#
uninstall.ps1 — remove COMPLETAMENTE o Syncronizer da maquina:
servico + binarios (Program Files) + dados/config/control.db/logs (ProgramData).

Use para comecar do zero (ex.: migrar de dev para prod).
Rode como ADMINISTRADOR e FECHE o services.msc antes.
#>
$ErrorActionPreference = "SilentlyContinue"

$svc  = "Syncronizer"
$nssm = "C:\Program Files\Syncronizer\runtime\nssm\nssm.exe"
$pf   = "C:\Program Files\Syncronizer"
$data = "C:\ProgramData\Syncronizer"

Write-Host "[1/4] Parando o servico..."
if (Test-Path $nssm) { & $nssm stop $svc } else { sc.exe stop $svc | Out-Null }
Start-Sleep -Seconds 5

Write-Host "[2/4] Encerrando processos presos do app..."
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.ExecutablePath -like "*\Syncronizer\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

Write-Host "[3/4] Removendo o servico..."
if (Test-Path $nssm) { & $nssm remove $svc confirm } else { sc.exe delete $svc | Out-Null }
Start-Sleep -Seconds 3

Write-Host "[4/4] Apagando arquivos (binarios + dados + config)..."
Remove-Item $data -Recurse -Force
Remove-Item $pf   -Recurse -Force

Write-Host ""
$svcLeft  = [bool](Get-Service $svc -ErrorAction SilentlyContinue)
$fileLeft = (Test-Path $data) -or (Test-Path $pf)
if ($svcLeft) {
    Write-Host "AVISO: o servico ainda existe (marcado para exclusao). Reinicie o Windows e rode este script de novo." -ForegroundColor Yellow
} elseif ($fileLeft) {
    Write-Host "AVISO: sobraram arquivos em uso. Feche o que estiver usando a pasta e rode de novo." -ForegroundColor Yellow
} else {
    Write-Host "OK: Syncronizer removido por completo. Pode reinstalar do zero." -ForegroundColor Green
}
