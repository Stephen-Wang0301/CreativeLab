$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$studyPython = 'D:\Development\Runtimes\Anaconda3\envs\skillcourse\python.exe'
if (-not (Test-Path -LiteralPath $studyPython)) { $studyPython = 'python' }
$tool = Join-Path $PSScriptRoot 'manage.py'
Write-Host 'CreativeLab'
Write-Host '1. 查看本地改动与已保存版本'
Write-Host '2. 更新测试网站（不创建 Git 版本）'
Write-Host '3. 确认保存一个阶段版本（仅本地）'
Write-Host '4. 上传已保存版本到 GitHub'
Write-Host '5. 下载服务器完整数据备份'
Write-Host '6. 下载服务器代码到私有对照目录'
Write-Host '7. 查看服务器状态'
Write-Host '0. 退出'
$choice = Read-Host '输入编号'
switch ($choice) {
    '1' { & $studyPython $tool status }
    '2' {
        if ((Read-Host '更新会短暂重启实验服务。输入 DEPLOY 继续') -ceq 'DEPLOY') {
            & $studyPython $tool deploy --working-tree
        }
    }
    '3' {
        $message = Read-Host '为这一阶段填写说明'
        if (-not [string]::IsNullOrWhiteSpace($message)) { & $studyPython $tool save -m $message }
    }
    '4' { & $studyPython $tool publish }
    '5' { & $studyPython $tool backup }
    '6' { & $studyPython $tool fetch-code }
    '7' { & $studyPython $tool server-status }
}
