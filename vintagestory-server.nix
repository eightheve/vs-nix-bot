{
  lib,
  stdenv,
  fetchurl,
  makeWrapper,
  dotnet-runtime_10,
}:

stdenv.mkDerivation (finalAttrs: {
  pname = "vintagestory-server";
  version = "1.22.3";

  src = fetchurl {
    url = "https://cdn.vintagestory.at/gamefiles/stable/vs_server_linux-x64_${finalAttrs.version}.tar.gz";
    hash = "sha256-6uOin1gMqeQzTi+aoSy9vKSNoZ7SUMyNZ5NH9S6a53I=";
  };

  nativeBuildInputs = [ makeWrapper ];

  dontUnpack = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/vintagestory $out/bin
    tar xf $src -C $out/share/vintagestory
    rm -f $out/share/vintagestory/server.sh

    runHook postInstall
  '';

  preFixup = ''
    makeWrapper ${lib.getExe dotnet-runtime_10} $out/bin/vintagestory-server \
      --add-flags $out/share/vintagestory/VintagestoryServer.dll

    find "$out/share/vintagestory/assets/" -not -path "*/fonts/*" -regex ".*/.*[A-Z].*" | while read -r file; do
      local filename="$(basename -- "$file")"
      ln -sf "$filename" "''${file%/*}/''${filename,,}"
    done
  '';

  meta = {
    description = "Vintage Story dedicated server";
    homepage = "https://www.vintagestory.at/";
    license = lib.licenses.unfree;
    sourceProvenance = [ lib.sourceTypes.binaryBytecode ];
    platforms = [ "x86_64-linux" ];
    mainProgram = "vintagestory-server";
  };
})
