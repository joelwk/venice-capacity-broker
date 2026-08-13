{ pkgs }:
let
  # Prefer Python 3.12 when available (nixpkgs 25.05),
  # else fall back for legacy channels.
  python =
    if pkgs ? python312 then pkgs.python312
    else if pkgs ? python311 then pkgs.python311
    else if pkgs ? python310 then pkgs.python310
    else pkgs.python3;
  pythonPackages =
    if pkgs ? python312Packages then pkgs.python312Packages
    else if pkgs ? python311Packages then pkgs.python311Packages
    else if pkgs ? python310Packages then pkgs.python310Packages
    else pkgs.python3Packages;
in {
  deps = [
    python
    pythonPackages.pip
    pkgs.gcc
    pkgs.pkg-config
    pkgs.openssl
    pkgs.libffi
    pkgs.cacert
  ] ++ (if builtins.hasAttr "uv" pkgs then [ pkgs.uv ] else []);
}
