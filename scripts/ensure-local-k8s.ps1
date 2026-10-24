# Bring up Docker Desktop Kubernetes without the cold-start cpuset failure.
# Kubelet 1.34 exits immediately when /sys/fs/cgroup/kubepods is created
# before the cpuset controller is delegated. kubectl then retries
# https://kubernetes.docker.internal:6443 until it prints EOF.
# Run this from the repo root instead of kubectl get nodes.

$ErrorActionPreference = "Stop"
# PowerShell 7 stops the script when kubectl exits non-zero. The cold-start
# EOF is expected; the checks below must keep running after it.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

function Wait-DockerDesktop {
    Write-Host "Starting Docker Desktop..."
    docker desktop start | Out-Null
    $deadline = (Get-Date).AddMinutes(3)
    do {
        docker desktop status 2>$null | Out-String | Write-Host
        $status = docker desktop status 2>$null | Out-String
        if ($status -match "Status\s+running") {
            return
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
    throw "Docker Desktop did not reach status running"
}

function Enable-Cpuset {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        wsl -d docker-desktop -- sh -c "echo +cpuset > /sys/fs/cgroup/cgroup.subtree_control; cat /sys/fs/cgroup/cgroup.subtree_control" 2>$null | Tee-Object -Variable subtree | Out-Host
        if ($subtree -match "cpuset") {
            Write-Host "cpuset controller is enabled"
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Could not enable the cpuset cgroup controller"
}

function Invoke-DockerApi {
    param([string]$Method, [string]$Path)
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream(".", "dockerBackendApiServer", [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(8000)
    $writer = New-Object System.IO.StreamWriter($pipe)
    $writer.AutoFlush = $true
    $writer.Write("$Method $Path HTTP/1.1`r`nHost: localhost`r`nContent-Length: 0`r`nConnection: close`r`n`r`n")
    $reader = New-Object System.IO.StreamReader($pipe)
    $text = $reader.ReadToEnd()
    $pipe.Dispose()
    return $text
}

function Get-K8sStatus {
    $text = Invoke-DockerApi -Method GET -Path "/kubernetes"
    if ($text -match '"status":"([^"]+)"') {
        return $Matches[1]
    }
    return "unknown"
}

function Test-NodeReady {
    # Windows PowerShell turns kubectl's stderr into a terminating error
    # while $ErrorActionPreference is Stop, even with 2>$null.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & kubectl --context docker-desktop --request-timeout=8s get nodes --no-headers *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-KubeletRunning {
    $procs = wsl -d docker-desktop -- ps w 2>$null | Out-String
    return $procs -match "kubelet"
}

Wait-DockerDesktop
Enable-Cpuset
kubectl config use-context docker-desktop | Out-Host

if (Test-NodeReady) {
    Write-Host "Kubernetes node is Ready"
    kubectl --context docker-desktop get nodes
    exit 0
}

Write-Host "API is not ready yet. kubectl EOF on a cold start is expected; continuing."
$status = Get-K8sStatus
Write-Host "Kubernetes status is $status"
$kubeletUp = Test-KubeletRunning

# A healthy boot can spend a short time in "starting" while kubelet is alive.
# Reset only when kubelet has already exited; that is the cpuset failure.
if ($status -eq "starting" -and $kubeletUp) {
    $deadline = (Get-Date).AddMinutes(3)
    do {
        if (Test-NodeReady) {
            Write-Host "Kubernetes node is Ready"
            kubectl --context docker-desktop get nodes
            exit 0
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
}

if ($status -eq "starting") {
    Write-Host "Kubelet is not running. Resetting the stuck Kubernetes start."
    Invoke-DockerApi -Method POST -Path "/kubernetes/reset" | Out-Null
    $deadline = (Get-Date).AddMinutes(2)
    do {
        $status = Get-K8sStatus
        Write-Host "Kubernetes status is $status"
        if ($status -ne "starting") { break }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)
}

Enable-Cpuset
Write-Host "Starting Kubernetes"
Invoke-DockerApi -Method POST -Path "/kubernetes/start" | Out-Null

$deadline = (Get-Date).AddMinutes(4)
do {
    if (Test-NodeReady) {
        Write-Host "Kubernetes node is Ready"
        kubectl --context docker-desktop get nodes
        exit 0
    }
    Start-Sleep -Seconds 5
} while ((Get-Date) -lt $deadline)

throw "Kubernetes did not become Ready. Check Docker Desktop Settings and confirm Kubernetes is enabled."
