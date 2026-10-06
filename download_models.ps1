#!/usr/bin/env powershell
# Download all WinBrow models

$models = @(
    @{
        Name = "Qwen 3.5 0.8B (LLM)"
        Url = "https://huggingface.co/Qwen/Qwen3.5-0.8B-GGUF/resolve/main/qwen3.5-0.8b-q4_k_m.gguf"
        Path = "models\qwen\qwen3.5-0.8b-q4_k_m.gguf"
        Size = "~550 MB"
    },
    @{
        Name = "Whisper base.en (STT)"
        Url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
        Path = "models\whisper\ggml-base.en.bin"
        Size = "~142 MB"
    }
)

$llamaServer = @{
    Name = "llama.cpp server"
    Url = "https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-server-win64.zip"
    Path = "llama_cpp\llama-server.zip"
    Size = "~15 MB"
}

Write-Host "=== WinBrow Model Downloader ===" -ForegroundColor Cyan
Write-Host "Total: ~700 MB`n"

# Create directories
New-Item -ItemType Directory -Force -Path "models\qwen" | Out-Null
New-Item -ItemType Directory -Force -Path "models\whisper" | Out-Null
New-Item -ItemType Directory -Force -Path "llama_cpp" | Out-Null

function Download-File($url, $path, $name) {
    Write-Host "Downloading $name ($url)..." -ForegroundColor Yellow
    try {
        $wc = New-Object System.Net.WebClient
        if ($env:HF_TOKEN) {
            $wc.Headers.Add("Authorization", "Bearer $env:HF_TOKEN")
        }
        $wc.DownloadFile($url, $path)
        Write-Host "  ✓ Saved to $path" -ForegroundColor Green
    } catch {
        Write-Host "  ✗ Failed: $($_.Exception.Message)" -ForegroundColor Red
        if ($_.Exception.Message -like "*401*") {
            Write-Host "  Note: Hugging Face requires a token for some models." -ForegroundColor Yellow
            Write-Host "  Set HF_TOKEN env var or download manually from the URL." -ForegroundColor Yellow
        }
    }
}

# Download models
foreach ($m in $models) {
    Download-File $m.Url $m.Path $m.Name
}

# Download llama.cpp server
Write-Host "`nDownloading llama.cpp server..."
try {
    $wc = New-Object System.Net.WebClient
    $wc.DownloadFile($llamaServer.Url, $llamaServer.Path)
    Write-Host "  Extracting..."
    Expand-Archive -Force -Path $llamaServer.Path -DestinationPath "llama_cpp"
    Remove-Item $llamaServer.Path
    Write-Host "  ✓ llama-server.exe ready" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Failed: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n=== Done ===" -ForegroundColor Cyan