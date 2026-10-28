# Git 常用命令组合

## 重置分支
```bash
git checkout 154.0.8037.21
git branch -D flex/154.0.8037.21
git checkout -b flex/desktop/154.0.8037.21
git apply ../patches/src-desktop.diff
```

## 关闭 rebase / 拉取策略
```bash
git config pull.rebase false        # 拉取分叉时用 merge，不自动 rebase
git config pull.ff only             # 要求只快进，等价于 git pull --ff-only
git config --global --list --show-origin
```

## 查看状态与历史
```bash
git status -sb                                  # 精简状态，带分支和 upstream
git log --oneline --graph --decorate -20        # 图形化最近提交
git log --oneline --all --graph                 # 含所有分支
git log --stat -1 HEAD                          # 最近一次提交的文件统计
git log -p --follow -- path/to/file             # 单文件历史（跨重命名）
git log -S"keyword" --oneline -- path/          # 搜索哪次提交增删了该字符串
git show 154.0.8037.21:chrome/VERSION           # 查看某个版本里的文件内容
git diff --stat                                 # 未暂存改动概览
git diff --cached                               # 已暂存内容
git diff 154.0.8037.21 -- path/                 # 与基线版本的差异
git blame -L 10,20 path/to/file                 # 指定行区间的作者信息
git shortlog -sn HEAD                           # 按作者统计提交数
git describe --tags --always                    # 当前提交对应的最近 tag
```

## 暂存与提交
```bash
git add -A                                      # 全部改动（含删除）
git add -p                                      # 分块挑选，逐段确认
git commit -m "fix: ..."
git commit --amend --no-edit                    # 补进上一次提交，保留信息
git commit --fixup <commit>                     # 先生成 fixup，再 autosquash
git rebase -i --autosquash 154.0.8037.21
```

## 撤销与回滚
```bash
git restore --staged path/to/file               # 取消暂存，保留工作区
git restore path/to/file                        # 丢弃工作区改动
git reset --soft HEAD~1                         # 撤销提交，改动回到暂存区
git reset --mixed HEAD~1                        # 撤销提交，改动回到工作区
git reset --hard HEAD~1                         # 丢弃提交和改动（不可恢复，先看 reflog）
git revert --no-edit <commit>                   # 已推送的历史用反向提交
git clean -nd                                   # 预览将被删除的未跟踪文件
git clean -fd                                   # 删除未跟踪文件和目录
git clean -fdx                                  # 连同 .gitignore 忽略的产物一起删
git reflog                                      # 找回误删的提交/分支
git reset --hard HEAD@{2}
```

## 分支管理
```bash
git switch -c flex/desktop/154.0.8037.21        # 新建并切换
git switch -                                    # 回到上一个分支
git branch -vv                                  # 本地分支及其 upstream/领先落后
git branch -a                                   # 含远端分支
git branch --merged 154.0.8037.21               # 已合入的、可安全删除的分支
git branch -m flex/old flex/desktop/154.0.8037.21
git branch --set-upstream-to=origin/flex/desktop/154.0.8037.21
# 删除本地分支
git branch -d 154.0.8037.21
# 删除远程分支
git push origin --delete 154.0.8037.21
```

## 清理不存在的远程分支
```bash
git fetch --prune
git branch -a
git remote prune origin             # 只清理远端追踪引用
```

## 合并 / 变基 / 拣选
```bash
git merge --no-ff flex/154.0.8037.21      # 保留合并提交
git merge --abort                          # 放弃本次合并
git rebase 154.0.8037.21
git rebase --continue                      # 解决冲突后继续
git rebase --abort                         # 放弃本次变基
git rebase --skip                          # 跳过当前提交
git cherry-pick -x <commit>                # 记录来源提交
git cherry-pick A^..B                      # 连续拣选 A 到 B
git cherry-pick --abort
```

## 冲突处理
```bash
git diff --name-only --diff-filter=U       # 列出冲突文件
git checkout --ours path/to/file           # 取当前分支版本（rebase 时方向相反）
git checkout --theirs path/to/file         # 取被合入版本
git add path/to/file                       # 标记已解决
git diff --check                           # 检查冲突标记和行尾空白
```

## 远程同步
```bash
git fetch --prune
git pull --ff-only
git push -u origin flex/desktop/154.0.8037.21
git push --force-with-lease                # 变基后用，比 --force 安全
git remote -v
git remote set-url origin <url>
git ls-remote --tags origin | tail -20
git ls-remote --heads origin
```

## stash 临时改动
```bash
git stash push -u -m "wip: desktop"        # 含未跟踪文件
git stash list
git stash show -p stash@{0}
git stash pop                              # 应用并删除
git stash apply stash@{1}                  # 应用但保留
git stash drop stash@{0}
```

## tag
```bash
git tag -a v1.2.0 -m "Release v1.2.0"      # 附注 tag
git push origin v1.2.0
git push --tags
git tag -l "v1.*"
git tag --points-at HEAD
# 删除本地 tag
git tag -d v1.2.0
# 删除远程 tag
git push origin :refs/tags/v1.2.0
git push origin --delete tag v1.2.0
```

## 补丁（与 backup.sh / patches 流程配合）
```bash
git diff > ../patches/src-desktop.diff                    # 未暂存改动导出
git diff --binary 154.0.8037.21 > ../patches/src.diff     # 相对基线，支持二进制
git diff --binary --staged > ../patches/staged.diff
git apply --check ../patches/src-desktop.diff             # 只校验能否应用
git apply --binary ../patches/src-desktop.diff
git apply -3 ../patches/src-desktop.diff                  # 三方合并，冲突留标记
git apply --reverse ../patches/src-desktop.diff           # 反向撤销补丁
git format-patch -1 HEAD --stdout                          # 导出为邮件补丁
git am ../patches/0001-*.patch                             # 应用邮件补丁
```

## Chromium 大仓库
```bash
git -C src status -sb                     # 不切目录操作子仓库
git -C src fetch --depth=1 origin refs/tags/154.0.8037.21
git fetch --unshallow                     # 浅克隆补齐历史
git clone --filter=blob:none --no-checkout <url>
git sparse-checkout set --cone chrome third_party/blink
git count-objects -vH                     # 对象库体积
git gc --prune=now                        # 清理悬空对象
git tag -d 154.0.8037.21                  # 需要重新拉取同一版本时先删 tag
```

## worktree（同时保留多个版本检出）
```bash
git worktree add ../src-154 154.0.8037.21
git worktree list
git worktree remove ../src-154
git worktree prune
```

## 配置与排错
```bash
git config --global user.name "name"
git config --global user.email "name@example.com"
git config --global --add safe.directory /mnt/code/chromium   # 跨用户/挂载盘提示 dubious ownership
git config --global alias.lg "log --oneline --graph --decorate"
git show --stat <commit>
git fsck --lost-found                     # 找回丢失对象
```

注：Chromium 的依赖由 DEPS + gclient 管理，不用 `git submodule`；`src` 内的业务模块是挂载目录，
需要 `.git/info/exclude` 排除，不要放进 `git add -A`。
