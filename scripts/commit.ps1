# 中文 commit 提交辅助脚本
# 用法: .\scripts\commit.ps1 "feat(模块): 中文标题" "可选的正文"
# 原理: 用 Python 以 UTF-8(无 BOM) 写 commit message 文件,避免 PowerShell 中文编码问题
param(
    [Parameter(Mandatory=$true)][string]$Subject,
    [string]$Body = ""
)

$repoRoot = Split-Path -Parent $PSScriptRoot
$msgFile = Join-Path $repoRoot ".git\COMMIT_MSG_TMP.txt"

$content = $Subject
if ($Body -ne "") { $content = "$Subject`n`n$Body" }

# 用 Python 写 UTF-8 无 BOM(经环境变量传递内容,避免命令行转义问题)
$env:GIT_MSG_SUBJECT = $Subject
$env:GIT_MSG_BODY = $Body
python -c "import os; from pathlib import Path; s=os.environ.get('GIT_MSG_SUBJECT',''); b=os.environ.get('GIT_MSG_BODY',''); c=s if not b else s+'\n\n'+b; Path(r'$msgFile').write_text(c+'\n', encoding='utf-8')"

if ($LASTEXITCODE -ne 0) { Write-Error "写 commit message 失败"; exit 1 }

Push-Location $repoRoot
try {
    git commit -F ".git\COMMIT_MSG_TMP.txt" @args
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item $msgFile -Force -ErrorAction SilentlyContinue
}
exit $code
