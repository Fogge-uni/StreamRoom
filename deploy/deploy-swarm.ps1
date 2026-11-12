$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$savedVolumeSettings = @{}

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$DockerArgs)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Команда docker завершилась с ошибкой: docker $($DockerArgs -join ' ')"
    }
}

Push-Location $ProjectRoot
try {
    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
            $trimmedLine = $line.Trim()
            if (-not $trimmedLine -or $trimmedLine.StartsWith("#")) {
                continue
            }
            if ($trimmedLine -match '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                $key = $Matches[1]
                $value = $Matches[2].Trim().Trim('"').Trim("'")
                if (-not [Environment]::GetEnvironmentVariable($key, "Process")) {
                    [Environment]::SetEnvironmentVariable($key, $value, "Process")
                }
            }
        }
    }

    Write-Host "Проверка Docker..." -ForegroundColor Cyan
    $dockerState = & docker info --format '{{.Swarm.LocalNodeState}}'
    if ($LASTEXITCODE -ne 0) {
        throw "Docker недоступен. Запусти Docker Desktop и повтори команду."
    }

    if ($dockerState -ne "active") {
        Write-Host "Инициализация одноузлового Docker Swarm..." -ForegroundColor Cyan
        Invoke-Docker swarm init
    }
    else {
        Write-Host "Docker Swarm уже активен." -ForegroundColor Green
    }

    $stackServices = Invoke-Docker service ls --filter 'label=com.docker.stack.namespace=streamroom' --format '{{.Name}}'
    $volumeSettings = @(
        @{ Variable = "SWARM_POSTGRES_VOLUME"; Service = "streamroom_database"; Target = "/var/lib/postgresql/data" }
        @{ Variable = "SWARM_OWNCAST_VOLUME"; Service = "streamroom_owncast"; Target = "/app/data" }
    )
    foreach ($setting in $volumeSettings) {
        $variable = $setting.Variable
        $savedVolumeSettings[$variable] = [Environment]::GetEnvironmentVariable($variable, "Process")
        if (-not $savedVolumeSettings[$variable] -and $stackServices -contains $setting.Service) {
            $mounts = Invoke-Docker service inspect --format '{{json .Spec.TaskTemplate.ContainerSpec.Mounts}}' $setting.Service |
                ConvertFrom-Json
            $volume = $mounts | Where-Object { $_.Type -eq "volume" -and $_.Target -eq $setting.Target }
            if ($volume) {
                [Environment]::SetEnvironmentVariable($variable, $volume.Source, "Process")
            }
        }
    }

    $nodeName = & docker info --format '{{.Name}}'
    $nodeLabel = & docker node inspect $nodeName --format '{{ index .Spec.Labels "streamroom.storage" }}' 2>$null
    if ($nodeLabel -ne "true") {
        Write-Host "Пометка узла для хранения данных PostgreSQL и Owncast..." -ForegroundColor Cyan
        Invoke-Docker node update --label-add streamroom.storage=true $nodeName
    }

    $secretName = "streamroom_db_password"
    $existingSecrets = & docker secret ls --format '{{.Name}}'
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось проверить Docker Secrets. Убедись, что текущая нода является manager."
    }

    if ($existingSecrets -notcontains $secretName) {
        $secretDirectory = Join-Path $ProjectRoot ".secrets"
        New-Item -ItemType Directory -Force -Path $secretDirectory | Out-Null
        $secretFile = Join-Path $secretDirectory "database_password.txt"

        $randomBytes = New-Object byte[] 32
        $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $generator.GetBytes($randomBytes)
        }
        finally {
            $generator.Dispose()
        }
        $password = [Convert]::ToBase64String($randomBytes)
        [System.IO.File]::WriteAllText($secretFile, $password, [System.Text.Encoding]::ASCII)

        Write-Host "Создание Docker Secret для PostgreSQL..." -ForegroundColor Cyan
        try {
            Invoke-Docker secret create $secretName $secretFile
        }
        finally {
            Remove-Item -LiteralPath $secretFile -Force -ErrorAction SilentlyContinue
        }
    }
    else {
        Write-Host "Docker Secret $secretName уже существует." -ForegroundColor Green
    }

    Write-Host "Сборка образов приложения..." -ForegroundColor Cyan
    Invoke-Docker build -t streamroom-backend:local ./backend
    Invoke-Docker build -t streamroom-frontend:local ./frontend
    Invoke-Docker build -t streamroom-gateway:local ./gateway

    Write-Host "Развёртывание стека StreamRoom..." -ForegroundColor Cyan
    Invoke-Docker stack deploy --detach=false --with-registry-auth --compose-file docker-stack.yml streamroom

    foreach ($service in "streamroom_backend", "streamroom_frontend", "streamroom_gateway") {
        if ($stackServices -contains $service) {
            Invoke-Docker service update --force $service
        }
    }

    $health = Invoke-RestMethod -Uri "http://localhost:8080/api/health" -TimeoutSec 10
    if ($health.status -ne "ok" -or $health.database -ne "connected") {
        throw "Проверка /api/health не подтвердила связь с PostgreSQL."
    }

    Write-Host "Сервисы стека:" -ForegroundColor Green
    Invoke-Docker stack services streamroom
    Write-Host ""
    Write-Host "Портал:       http://localhost:8080" -ForegroundColor Green
    Write-Host "Видеосервер:  http://localhost:8081" -ForegroundColor Green
    Write-Host "RTMP для OBS: rtmp://localhost/live" -ForegroundColor Green
    Write-Host "Проверка задач: docker stack ps streamroom" -ForegroundColor Yellow
}
finally {
    foreach ($variable in $savedVolumeSettings.Keys) {
        [Environment]::SetEnvironmentVariable($variable, $savedVolumeSettings[$variable], "Process")
    }
    Pop-Location
}
