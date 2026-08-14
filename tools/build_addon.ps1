# build_addon.ps1 - 打包 6d-IME NVDA add-on
# 用法：在 PowerShell 執行此腳本，產生 6d-IME-<version>.nvda-addon

$addonRoot = Split-Path $PSScriptRoot -Parent
$manifestPath = Join-Path $addonRoot "manifest.ini"
$manifestRaw = Get-Content $manifestPath -Raw
$null = $manifestRaw -match 'version\s*=\s*"?([^"\r\n]+)"?'
$version = $Matches[1].Trim()
$outFile = Join-Path $addonRoot "6d-IME-$version.nvda-addon"

# 要打包進去的檔案（相對於 addonRoot）
$include = @(
    "manifest.ini",
    "README.md",
    "globalPlugins\sixdIME\__init__.py",
    "globalPlugins\sixdIME\Phn.tbl",
    "globalPlugins\sixdIME\nemethdefs.cti"
)

if (Test-Path $outFile) { Remove-Item $outFile -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($outFile, 'Create')

foreach ($rel in $include) {
    $full = Join-Path $addonRoot $rel
    if (Test-Path $full) {
        # ZipArchive 的 entry name 必須用正斜線
        $entryName = $rel -replace '\\', '/'
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zip, $full, $entryName,
            [System.IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
        Write-Host "  + $entryName"
    } else {
        Write-Warning "找不到: $full"
    }
}

$zip.Dispose()
Write-Host ""
Write-Host "完成：$outFile" -ForegroundColor Green
Write-Host "→ 在 NVDA 選單 > 工具 > 附加元件管理員 > 安裝，選擇此檔案"
