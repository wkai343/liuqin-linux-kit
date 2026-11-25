Name:           plasma-camera-liuqin-test
Version:        26.08.3
Release:        4%{?dist}
Summary:        Plasma Camera orientation test for the Xiaomi Pad 6 Pro
License:        BSD-3-Clause AND GPL-2.0-or-later AND CC0-1.0 AND GPL-3.0-or-later
URL:            https://invent.kde.org/plasma-mobile/plasma-camera
Source0:        https://download.kde.org/stable/release-service/%{version}/src/plasma-camera-%{version}.tar.xz
Source1:        org.kde.plasma.camera.liuqintest.desktop
Source2:        check-orientation.cpp
Source3:        liuqin-zh_CN.po
Patch0:         0001-liuqin-orientation-test.patch

BuildRequires:  gcc-c++, cmake, ninja-build, extra-cmake-modules, gettext, desktop-file-utils
BuildRequires:  cmake(Qt6Gui), cmake(Qt6QuickControls2), cmake(Qt6Test), cmake(Qt6Svg), cmake(Qt6Multimedia), cmake(Qt6Sensors)
BuildRequires:  cmake(KF6Kirigami), cmake(KF6CoreAddons), cmake(KF6I18n), cmake(KF6Config)
BuildRequires:  exiv2-devel, pkgconfig(libcamera)
# Reuse Fedora's translations and runtime QML dependencies.
Requires:       plasma-camera

%description
A separately installable Plasma Camera build for testing the orientation of
the two built-in liuqin cameras.

%prep
%autosetup -p1 -n plasma-camera-%{version}

%build
%cmake -G Ninja
%cmake_build --target plasma-camera

%install
install -Dm0755 %{_vpath_builddir}/bin/plasma-camera %{buildroot}%{_bindir}/%{name}
install -Dm0644 %{SOURCE1} %{buildroot}%{_datadir}/applications/org.kde.plasma.camera.liuqintest.desktop

install -d %{buildroot}%{_datadir}/locale/zh_CN/LC_MESSAGES
msgfmt --check %{SOURCE3} -o %{buildroot}%{_datadir}/locale/zh_CN/LC_MESSAGES/%{name}.mo

%check
%{__cxx} -std=c++20 %{SOURCE2} -Isrc/plasmacamera $(pkg-config --cflags --libs Qt6Gui) -o check-orientation
./check-orientation
desktop-file-validate %{buildroot}%{_datadir}/applications/org.kde.plasma.camera.liuqintest.desktop

%files
%license LICENSES
%{_bindir}/%{name}
%{_datadir}/applications/org.kde.plasma.camera.liuqintest.desktop
%lang(zh_CN) %{_datadir}/locale/zh_CN/LC_MESSAGES/%{name}.mo
