# SPDX-License-Identifier: MIT
# Configure only the Liuqin RAM installer's dedicated RNDIS adapter.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$ruleName = 'LiuqinLinuxKit-USB-Installer'
$ruleGroup = 'LiuqinLinuxKit'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '请从安装器的“设置 USB 网络”按钮启动，或以管理员身份运行此脚本。'
    }
    function Find-LiuqinAdapter {
        $devices = @(Get-CimInstance Win32_NetworkAdapter | Where-Object {
            $_.PNPDeviceID -match '^USB\\VID_1D6B&PID_1041(?:&MI_[0-9A-F]{2})?\\' -and
            $_.ConfigManagerErrorCode -eq 0 -and
            ($_.MACAddress -replace '[:-]', '') -eq '020000000701'
        })
        if ($devices.Count -gt 1) { throw '检测到多张平板安装网卡，请只连接一台平板。' }
        if ($devices.Count -eq 1) {
            $found = Get-NetAdapter -InterfaceIndex $devices[0].InterfaceIndex
            return $found
        }
        return $null
    }
    $rules = @(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)
    if (@($rules | Where-Object { $_.Group -ne $ruleGroup }).Count -gt 0) {
        throw '同名防火墙规则不属于本安装器，未修改。'
    }
    if ($Remove) {
        if ($rules.Count -gt 0) { $rules | Remove-NetFirewallRule }
        $adapter = Find-LiuqinAdapter
        if ($null -ne $adapter) {
            $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -eq '192.168.7.1' })
            foreach ($entry in $addresses) { Remove-NetIPAddress -InputObject $entry -Confirm:$false }
            Set-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AddressFamily IPv4 -Dhcp Enabled
        }
        Write-Host '已移除安装器防火墙规则，并恢复已连接专用网卡的 DHCP。'
        exit 0
    }
    Write-Host '请在安装器中创建备份或检查安装方案；平板进入临时安装环境后，将配置专用 USB 网卡。'
    $deadline = (Get-Date).AddSeconds(60)
    $adapter = $null
    do {
        $adapter = Find-LiuqinAdapter
        if ($null -ne $adapter) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($null -eq $adapter) {
        throw '未找到平板 RNDIS 网卡。请确认使用新版 installer.img，并在设备管理器检查 USB 网卡驱动。'
    }
    $index = $adapter.InterfaceIndex
    Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
    $oldAddresses = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne '192.168.7.1' })
    foreach ($entry in $oldAddresses) { Remove-NetIPAddress -InputObject $entry -Confirm:$false }
    $address = Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq '192.168.7.1' }
    if ($null -eq $address) {
        New-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress '192.168.7.1' -PrefixLength 24 | Out-Null
    } elseif ($address.PrefixLength -ne 24) {
        throw '专用网卡已有相同地址但前缀不同，请先移除旧地址后重试。'
    }
    Set-DnsClientServerAddress -InterfaceIndex $index -ResetServerAddresses
    if ($rules.Count -gt 0) { $rules | Remove-NetFirewallRule }
    New-NetFirewallRule -Name $ruleName -DisplayName 'Liuqin Linux Kit USB Installer' -Group $ruleGroup `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 37631 `
        -LocalAddress '192.168.7.1' -RemoteAddress '192.168.7.2' `
        -InterfaceAlias $adapter.Name -Profile Any -EdgeTraversalPolicy Block | Out-Null
    Write-Host ('已配置 USB 网卡：' + $adapter.Name + '，地址 192.168.7.1/24。')
    Write-Host '仅向平板开放 TCP 37631 安装端口，不配置互联网网关。可以回到安装器继续。'
    exit 0
} catch {
    Write-Host ('设置失败：' + $_.Exception.Message) -ForegroundColor Red
    Read-Host '按回车关闭'
    exit 1
}
