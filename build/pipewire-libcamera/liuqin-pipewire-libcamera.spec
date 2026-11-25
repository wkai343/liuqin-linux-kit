Name:           liuqin-pipewire-libcamera
Version:        1.6.11
Release:        4%{?dist}
Summary:        Preferred native camera size for the Liuqin WirePlumber monitor
License:        MIT
URL:            https://pipewire.org/
Source0:        https://codeload.github.com/PipeWire/pipewire/tar.gz/refs/tags/%{version}#/pipewire-%{version}.tar.gz
Source1:        60-liuqin-camera-format.conf
Source2:        liuqin-camera-focus
Patch0:         0001-libcamera-preferred-size.patch
BuildRequires:  gcc, gcc-c++, pkgconfig(libcamera) >= 0.6.0
Requires:       wireplumber >= 0.5
Requires:       pipewire-plugin-libcamera%{?_isa}
Requires:       pipewire-utils, python3

%description
An alternate build of the PipeWire SPA libcamera backend and a WirePlumber
rule for the Liuqin front camera. Clients can still select other sensor modes.
The liuqin-camera-focus command controls the lens on an active rear preview.

%prep
echo 'f3f70c4af173cc93d05070a9d412efe3ef5889bdf4ac32298e2c0d9ae1350559  %{SOURCE0}' | sha256sum -c -
%autosetup -p1 -n pipewire-%{version}

%build
%set_build_flags
plugin=spa/plugins/libcamera
%{__cc} $CFLAGS -fPIC -Ispa/include -c "$plugin/libcamera.c" -o libcamera.o
for unit in libcamera-device libcamera-manager libcamera-source; do
  %{__cxx} $CXXFLAGS -std=gnu++20 -fPIC -Ispa/include $(pkg-config --cflags libcamera) \
    -c "$plugin/$unit.cpp" -o "$unit.o"
done
%{__cxx} -shared $LDFLAGS -Wl,-z,defs -o libspa-liuqin-libcamera.so \
  libcamera.o libcamera-device.o libcamera-manager.o libcamera-source.o \
  $(pkg-config --libs libcamera) -pthread

%install
install -Dm0755 libspa-liuqin-libcamera.so %{buildroot}%{_libdir}/spa-0.2/libcamera/libspa-liuqin-libcamera.so
install -Dm0644 %{SOURCE1} %{buildroot}%{_datadir}/wireplumber/wireplumber.conf.d/60-liuqin-camera-format.conf
install -Dm0755 %{SOURCE2} %{buildroot}%{_bindir}/liuqin-camera-focus

%files
%license COPYING
%{_bindir}/liuqin-camera-focus
%{_libdir}/spa-0.2/libcamera/libspa-liuqin-libcamera.so
%{_datadir}/wireplumber/wireplumber.conf.d/60-liuqin-camera-format.conf

%changelog
* Wed Nov 25 2026 Liuqin Linux Kit contributors - 1.6.11-4
- Add tap-to-focus coordinates to the shared rear focus command.

* Thu Nov 19 2026 Liuqin Linux Kit contributors - 1.6.11-3
- Prefer the full rear still size when desktop clients leave dimensions open.

* Thu Nov 19 2026 Liuqin Linux Kit contributors - 1.6.11-2
- Provide a command to lock, set and release rear focus on a running PipeWire source.

* Thu Nov 19 2026 Liuqin Linux Kit contributors - 1.6.11-1
- Add per-node preferred capture size for the WirePlumber libcamera monitor.
