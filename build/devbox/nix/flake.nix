{
  description = "Liuqin Linux Kit: kernel, ARM64 installer and rootfs build tools";

  # Immutable source URL avoids the GitHub API rate limit during installation.
  inputs.nixpkgs.url = "https://codeload.github.com/NixOS/nixpkgs/tar.gz/b6018f87da91d19d0ab4cf979885689b469cdd41";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forSystems = nixpkgs.lib.genAttrs systems;
    in {
      packages = forSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          # A static musl libc keeps the RAM helpers independent of the host's
          # Nix glibc loader and the target rootfs distribution.
          cross = pkgs.pkgsCross.aarch64-multiplatform-musl;
          python = pkgs.python311.withPackages (p: [ p.pyelftools ]);
          crossTools = pkgs.buildEnv {
            name = "liuqin-arm64-tools";
            paths = [ cross.buildPackages.gcc cross.buildPackages.binutils ];
            pathsToLink = [ "/bin" ];
          };
          busyboxBinary = pkgs.runCommand "liuqin-host-busybox" {} ''
            mkdir -p $out/bin
            ln -s ${pkgs.busybox}/bin/busybox $out/bin/busybox
          '';
        in {
          build-tools = pkgs.buildEnv {
            name = "liuqin-build-tools";
            paths = with pkgs; [
              bash coreutils findutils gnugrep gnused gawk diffutils which
              git curl cacert gnutar gzip pigz xz zstd bzip2 cpio rsync
              gnumake gcc binutils ccache bc bison flex perl pkg-config patch
              openssl.dev openssl.out elfutils.dev elfutils.out zlib.dev zlib.out
              ncurses.dev ncurses.out
              crossTools
              python dtc kmod e2fsprogs btrfs-progs libcap attr acl gptfdisk dpkg rpm gnupg policycoreutils
              squashfsTools p7zip util-linux busyboxBinary erofs-utils mtools patchelf
              pkgsStatic.qemu-user meson ninja file
            ];
            pathsToLink = [ "/bin" "/sbin" "/include" "/lib" "/share/pkgconfig" ];
            ignoreCollisions = false;
          };
          default = self.packages.${system}.build-tools;
        });
    };
}
