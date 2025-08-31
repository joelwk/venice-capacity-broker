{ pkgs }: {
  # Target Linux nixpkgs 25.05 explicitly with Python 3.12
  # to avoid packages (e.g., sphinx >=8) excluding Python 3.10.
  deps = [
    pkgs.python312
    pkgs.python312Packages.pip
    pkgs.gcc
    pkgs.pkg-config
    pkgs.openssl
    pkgs.libffi
    pkgs.cacert
  ] ++ (if builtins.hasAttr "uv" pkgs then [ pkgs.uv ] else []);
}
