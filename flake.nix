{
  description = "Vintage Story dedicated server with a Discord bot frontend";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = fn: builtins.foldl' (acc: sys: acc // { ${sys} = fn sys; }) {} systems;
    in {
      packages = forAllSystems (system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          default = self.packages.${system}.vintagestory-server;
          vintagestory-server = pkgs.callPackage ./vintagestory-server.nix { };
        });

      nixosModules.default = import ./module.nix;
    };
}
