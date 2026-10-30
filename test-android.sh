#!/usr/bin/env bash

# 默认环境
export LANG=C.UTF-8
export LC_ALL=C.UTF-8


echo "\n\n\n ====> build arupa kernel"
bash build.sh arupa_android gen build --os android --jobs 12
# bash build.sh arupa_android gen build package --zip --os android


# echo "\n\n\n ====> package arupa kernel"
bash build.sh arupa_android package --zip

# echo "\n\n\n ====> publish arupa kernel"
# bash build.sh arupa_android publish

echo "\n\n\n ====> migrate"
bash scripts/migrate.sh


echo "\n\n\n ====> build nomad apk (--variant debug / release)"
# bash build.sh nomad_android build package --os android --arch x64 --variant debug
bash build.sh nomad_android build package --os android --arch arm64 --variant debug


# echo "\n\n\n ====> kvm"
# echo "jifang6688" | sudo -S setfacl -m u:$USER:rw /dev/kvm


# echo "\n\n\n ====> emulator"
# bash scripts/emulator.sh --emulator

# git checkout 154.0.8037.21
# git branch -D flex/android/154.0.8037.21
# git checkout -b flex/android/154.0.8037.21
# git apply ../patches/src-android.diff

# git config pull.rebase false
