# FsTransfor RPM spec
# 构建: packaging/build-rpm.sh  (产物在 ~/rpmbuild/RPMS/noarch/)
Name:           fstransfor
Version:        0.2.0
Release:        8%{?dist}
Summary:        GTK4 双面板 SSH/SFTP 文件传输器
License:        MIT
URL:            https://github.com/Luyao-1024/fs_transfor
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-paramiko

%description
基于 GTK4 + libadwaita + paramiko 的多标签双面板 SSH/SFTP 文件传输器:
连接复用、拖拽/剪贴板传输、临时文件安全提交、连接暂停与恢复、
会话与路径记忆。

%prep
%autosetup

%build
# 纯 Python 应用, 无构建步骤

%install
mkdir -p %{buildroot}%{_libdir}/%{name}
cp -r fsapp %{buildroot}%{_libdir}/%{name}/fsapp
install -m 644 main.py %{buildroot}%{_libdir}/%{name}/main.py

mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/%{name} <<'EOF'
#!/bin/sh
exec /usr/bin/python3 %{_libdir}/fstransfor/main.py "$@"
EOF
chmod +x %{buildroot}%{_bindir}/%{name}

mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 644 data/icons/io.github.fstransfer.FsTransfor.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/

mkdir -p %{buildroot}%{_datadir}/applications
sed -e 's|^Exec=.*|Exec=%{_bindir}/%{name}|' \
    -e '/^Path=/d' \
    data/io.github.fstransfer.FsTransfor.desktop \
    > %{buildroot}%{_datadir}/applications/io.github.fstransfer.FsTransfor.desktop

%files
%license LICENSE
%doc README.md
%{_bindir}/fstransfor
%{_libdir}/fstransfor/
%{_datadir}/icons/hicolor/scalable/apps/io.github.fstransfer.FsTransfor.svg
%{_datadir}/applications/io.github.fstransfer.FsTransfor.desktop

%post
if [ -x /usr/bin/gtk4-update-icon-cache ]; then
    /usr/bin/gtk4-update-icon-cache -f %{_datadir}/icons/hicolor &>/dev/null || :
fi

%postun
if [ $1 -eq 0 ] && [ -x /usr/bin/gtk4-update-icon-cache ]; then
    /usr/bin/gtk4-update-icon-cache -f %{_datadir}/icons/hicolor &>/dev/null || :
fi

%changelog
* Thu Sep 10 2026 FsTransfor <local@localhost> - 0.2.0-1
- 初次打包: 数据完整性修复(临时文件安全提交/移动语义/链接安全删除)与
  连接暂停恢复 UX
