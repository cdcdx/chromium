bash fetch.sh pull

bash build.sh arupa_desktop gen build package --zip
# bash build.sh arupa_desktop gen build 
# bash build.sh arupa_desktop package --zip

# bash build.sh arupa_desktop publish

bash build.sh nomad_desktop build package
# bash build.sh nomad_desktop build
# bash build.sh nomad_desktop package

# git checkout 154.0.8037.21
# git branch -D flex/desktop/154.0.8037.21
# git checkout -b flex/desktop/154.0.8037.21
# git apply ../patches/src-desktop.diff
