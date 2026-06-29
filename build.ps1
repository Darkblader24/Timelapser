# Timelapser – Build-Skript  (erstellt dist\Timelapser.exe)
# Aufruf:  .\build.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Relativ zum Skript-Verzeichnis, damit der Build von überall funktioniert.
Set-Location -Path $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

Add-Type -AssemblyName System.Windows.Forms

function Show-ErrorPopup($message) {
    [System.Windows.Forms.MessageBox]::Show(
        $message, "Build fehlgeschlagen",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
}

# Laufende App-Instanzen beenden, damit die EXE nicht gesperrt ist.
$running = Get-Process -Name Timelapser -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "==> Beende laufende Timelapser-Instanzen ($($running.Count)) ..." -ForegroundColor Cyan
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 500
}

# Alte EXE entfernen, bevor PyInstaller startet. Schlägt das fehl (Datei
# gesperrt, weil die App noch läuft), brechen wir mit klarer Meldung ab,
# statt PyInstaller mit einem kryptischen PermissionError abstürzen zu lassen.
$exePath = "dist\Timelapser.exe"
if (Test-Path $exePath) {
    try {
        Remove-Item $exePath -Force -ErrorAction Stop
    } catch {
        $msg = "Die vorhandene dist\Timelapser.exe konnte nicht gelöscht werden.`n`n" +
               "Wahrscheinlich läuft die App noch. Bitte schließe alle laufenden " +
               "Timelapser-Fenster und starte den Build erneut.`n`n" +
               "Details: $($_.Exception.Message)"
        Write-Host "Fehler: $msg" -ForegroundColor Red
        Show-ErrorPopup $msg
        exit 1
    }
}

Write-Host ""
Write-Host "==> Abhängigkeiten installieren …" -ForegroundColor Cyan
& $py -m pip install --upgrade customtkinter Pillow pyinstaller

Write-Host ""
Write-Host "==> EXE wird gebaut …" -ForegroundColor Cyan

# Pfad zu customtkinter-Daten ermitteln
$ctk_path = & $py -c "import customtkinter, os; print(os.path.dirname(customtkinter.__file__))"
$ctk_path = $ctk_path.Trim()

& $py -m PyInstaller `
    --onefile `
    --windowed `
    --name "Timelapser" `
    --add-data "$ctk_path;customtkinter" `
    app.py

Write-Host ""
if (Test-Path $exePath) {
    Write-Host "Fertig!  dist\Timelapser.exe wurde erstellt - starte die App ..." -ForegroundColor Green
    Start-Process -FilePath (Resolve-Path $exePath)
} else {
    $msg = "EXE nicht gefunden. Prüfe die PyInstaller-Ausgabe oben."
    Write-Host "Fehler: $msg" -ForegroundColor Red
    Show-ErrorPopup $msg
}
