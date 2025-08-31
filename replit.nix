{ pkgs }: {
  deps = [
    pkgs.python310
    pkgs.python310Packages.pip
    pkgs.gcc
    pkgs.pkg-config
    pkgs.openssl
    pkgs.libffi
    pkgs.cacert
  ] ++ (if builtins.hasAttr "uv" pkgs then [ pkgs.uv ] else []);
}
