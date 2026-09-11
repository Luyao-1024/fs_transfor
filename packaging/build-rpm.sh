#!/bin/sh
# 构建 FsTransfor RPM: ./packaging/build-rpm.sh
# 产物: ~/rpmbuild/RPMS/noarch/fstransfor-<版本>-<发行号>*.rpm
# 安装: sudo dnf install ~/rpmbuild/RPMS/noarch/fstransfor-*.rpm
set -e
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^Version:[[:space:]]*//p' packaging/fstransfor.spec)
mkdir -p ~/rpmbuild/SOURCES ~/rpmbuild/SPECS

# 源码包: 应用代码 + 图标 + 桌面文件(排除缓存与测试)
tar czf ~/rpmbuild/SOURCES/fstransfor-${VERSION}.tar.gz \
    --transform "s,^,fstransfor-${VERSION}/," \
    --exclude='__pycache__' --exclude='tests' --exclude='.venv' \
    fsapp main.py data README.md LICENSE

cp packaging/fstransfor.spec ~/rpmbuild/SPECS/
rpmbuild -bb ~/rpmbuild/SPECS/fstransfor.spec
echo
ls -1 ~/rpmbuild/RPMS/noarch/fstransfor-*.rpm
