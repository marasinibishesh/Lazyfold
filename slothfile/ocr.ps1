# SlothFile OCR helper: Windows' built-in OCR (Windows.Media.Ocr) from Windows PowerShell 5.1.
# Usage: powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ocr.ps1 <image> [<image> ...]
# Prints one compressed JSON object per image: {"path":..., "text":..., "error":...}.
# Exit code 2 when no OCR engine is available for the user's languages.
# With no arguments it only checks that an engine exists (used by ocr_available()).
# Paths arrive as literal arguments (-File never evaluates them), are read only,
# and nothing is written anywhere.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapTransform, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
} catch {
    $engine = $null
}
if ($null -eq $engine) { exit 2 }

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$maxDim = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension

function Read-Image([string]$path) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $w = [double]$decoder.OrientedPixelWidth
        $h = [double]$decoder.OrientedPixelHeight
        if ($w -le $maxDim -and $h -le $maxDim) {
            $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        } else {
            # Too big for the engine: scale down to fit instead of failing.
            $scale = [Math]::Min($maxDim / $w, $maxDim / $h)
            $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
            $transform.ScaledWidth = [uint32][Math]::Max(1, [Math]::Floor($decoder.PixelWidth * $scale))
            $transform.ScaledHeight = [uint32][Math]::Max(1, [Math]::Floor($decoder.PixelHeight * $scale))
            $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Fant
            $bitmap = Await ($decoder.GetSoftwareBitmapAsync(
                [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
                [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied,
                $transform,
                [Windows.Graphics.Imaging.ExifOrientationMode]::RespectExifOrientation,
                [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        }
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        # Join line by line: OcrResult.Text puts everything on one line.
        return (($result.Lines | ForEach-Object { $_.Text }) -join "`n")
    } finally {
        $stream.Dispose()
    }
}

foreach ($p in $args) {
    $path = [string]$p
    $obj = [ordered]@{ path = $path; text = ""; error = $null }
    try {
        $obj.text = Read-Image $path
    } catch {
        $obj.error = ($_.Exception.Message -replace '\s+', ' ').Trim()
    }
    [Console]::Out.WriteLine((New-Object PSObject -Property $obj | ConvertTo-Json -Compress))
    [Console]::Out.Flush()  # one complete line per image: a timeout keeps finished work
}
exit 0
