
## 运行命令
```bash
bash fetch.sh pull

echo "\n\n=======> arupa gen build"
bash build.sh arupa_desktop gen build --jobs 8

echo "\n\n=======> arupa package"
bash build.sh arupa_desktop package --zip

# echo "\n\n=======> arupa publish"
# bash build.sh arupa_desktop publish

echo "\n\n=======> nomad build"
bash build.sh nomad_desktop build

echo "\n\n=======> nomad package"
bash build.sh nomad_desktop package

# export NomadApiEnvironment=test

# echo "\n\n=======> nomad build"
# bash build.sh nomad_desktop build --variant debug

# echo "\n\n=======> nomad package"
# bash build.sh nomad_desktop package --variant debug

# unset NomadApiEnvironment
```

```powershell
.\fetch.ps1 pull

Write-Host '`n`n=======> arupa gen build'
.\build.ps1 arupa_desktop gen build --jobs 8

Write-Host '`n`n=======> arupa package'
.\build.ps1 arupa_desktop package --zip

# Write-Host '`n`n=======> arupa publish'
# .\build.ps1 arupa_desktop publish

Write-Host '`n`n=======> nomad build'
.\build.ps1 nomad_desktop build

Write-Host '`n`n=======> nomad package'
.\build.ps1 nomad_desktop package

# $env:NomadApiEnvironment='test'

# Write-Host '`n`n=======> nomad build'
# .\build.ps1 nomad_desktop build --variant debug

# Write-Host '`n`n=======> nomad package'
# .\build.ps1 nomad_desktop package --variant debug

# Remove-Item Env:\NomadApiEnvironment
```

## git命令
```shell
# 切换到 154.0.8037.21 分支
git checkout 154.0.8037.21
git branch -D flex/desktop/154.0.8037.21
git checkout -b flex/desktop/154.0.8037.21
git apply ../patches/src-desktop.diff

# 禁用自动合并
git config pull.rebase false

# 标记版本
git tag -a v1.2.0 -m "Release v1.2.0"
git push origin v1.2.0
git push --tags

# 只清理远端追踪引用
git fetch --prune
git remote prune origin
# 查看所有分支
git branch -a
```