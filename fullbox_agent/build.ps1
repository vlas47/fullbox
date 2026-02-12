$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$serviceProj = Join-Path $root "src\\Fullbox.Agent.Service\\Fullbox.Agent.Service.csproj"
$trayProj = Join-Path $root "src\\Fullbox.Agent.Tray\\Fullbox.Agent.Tray.csproj"
$setupProj = Join-Path $root "src\\Fullbox.Agent.Setup\\Fullbox.Agent.Setup.csproj"
$outDir = Join-Path $root "out"
$serviceOut = Join-Path $outDir "service"
$trayOut = Join-Path $outDir "tray"
$setupOut = Join-Path $outDir "setup"
$distDir = Join-Path $root "dist"
$bundlePath = Join-Path $root "fullbox_agent_bundle.zip"

New-Item -ItemType Directory -Path $serviceOut -Force | Out-Null
New-Item -ItemType Directory -Path $trayOut -Force | Out-Null
New-Item -ItemType Directory -Path $setupOut -Force | Out-Null
New-Item -ItemType Directory -Path $distDir -Force | Out-Null

dotnet publish $serviceProj -c Release -r win-x64 -p:PublishSingleFile=true -p:SelfContained=true -o $serviceOut
dotnet publish $trayProj -c Release -r win-x64 -p:PublishSingleFile=true -p:SelfContained=true -o $trayOut

Copy-Item -LiteralPath (Join-Path $serviceOut "Fullbox.Agent.Service.exe") -Destination (Join-Path $distDir "Fullbox.Agent.Service.exe") -Force
Copy-Item -LiteralPath (Join-Path $trayOut "Fullbox.Agent.Tray.exe") -Destination (Join-Path $distDir "Fullbox.Agent.Tray.exe") -Force

$configPath = Join-Path $distDir "config.json"
if (-not (Test-Path $configPath)) {
  $configPath = Join-Path $distDir "config.sample.json"
}

$bundleSources = @(
  $configPath,
  (Join-Path $distDir "Fullbox.Agent.Service.exe"),
  (Join-Path $distDir "Fullbox.Agent.Tray.exe"),
  (Join-Path $distDir "install_agent.cmd"),
  (Join-Path $distDir "README.txt")
)

if (Test-Path $bundlePath) {
  Remove-Item $bundlePath -Force
}
Compress-Archive -Path $bundleSources -DestinationPath $bundlePath

dotnet publish $setupProj -c Release -r win-x64 -p:PublishSingleFile=true -p:SelfContained=true -o $setupOut

Write-Host "Build complete:"
Write-Host "  Service -> $serviceOut"
Write-Host "  Tray    -> $trayOut"
Write-Host "  Setup   -> $setupOut"
Write-Host "  Bundle  -> $bundlePath"
