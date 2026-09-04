{ config, lib, pkgs, ... }:

let
  cfg = config.services.vintagestory;
  botEnv = pkgs.python3.withPackages (p: [ p.discordpy p.python-dotenv p.aiohttp ]);
  botScript = ./bot/bot.py;
in {
  options.services.vintagestory = {
    enable = lib.mkEnableOption "Vintage Story server with Discord bot frontend";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./vintagestory-server.nix { };
      description = "Vintage Story server package to use.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/vintagestory";
      description = ''
        State directory for the server and bot. The .env file and allowlist
        should live here. Created and owned by the vintagestory user.
      '';
    };

    envFile = lib.mkOption {
      type = lib.types.path;
      default = "${cfg.dataDir}/.env";
      description = ''
        Path to the .env file read by the bot. Must contain at least:
          DISCORD_TOKEN, CONSOLE_CHANNEL_ID, CHAT_CHANNEL_ID,
          ALLOWLIST_PATH, DATA_PATH, CHAT_REGEX (optional),
          MODS_PATH (optional, defaults to $DATA_PATH/mods).
        The module injects SERVER_BIN automatically; do not set it here.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 42420;
      description = "UDP+TCP port the server listens on (for firewall rules).";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open the UDP+TCP port in the firewall.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "vintagestory";
      description = "User to run the bot and server as.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
      home = cfg.dataDir;
      createHome = false;
      useDefaultShell = true;
    };
    users.groups.${cfg.user} = { };

    systemd.services.vintagestory-bot = {
      description = "Vintage Story Discord bot (owns the server subprocess)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        User = cfg.user;
        Group = cfg.user;
        StateDirectory = "vintagestory";
        WorkingDirectory = cfg.dataDir;
        Environment = [ "SERVER_BIN=${cfg.package}/bin/vintagestory-server" ];
        EnvironmentFile = cfg.envFile;
        ExecStart = "${botEnv}/bin/python ${botScript}";
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    networking.firewall.allowedUDPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
