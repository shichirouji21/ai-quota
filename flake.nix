{
  description = "ai-quota — unified AI quota CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        pyproject = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;

        ai-quota = python.pkgs.buildPythonApplication {
          pname = pyproject.name;
          version = pyproject.version;
          format = "pyproject";
          src = ./.;

          nativeBuildInputs = [ python.pkgs.setuptools python.pkgs.wheel ];
          propagatedBuildInputs = [ ];

          # Runtime binaries the adapters shell out to. `codex` and `claude`
          # are intentionally NOT propagated — they are user-managed and the
          # adapters degrade to status="unavailable" if missing.
          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (pkgs.lib.makeBinPath [ pkgs.gh ])
          ];

          nativeCheckInputs = [ python.pkgs.pytest ];
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            pytest -q
            runHook postCheck
          '';

          meta = {
            description = "Unified AI quota CLI for Codex, GitHub Copilot, and Claude";
            mainProgram = "ai-quota";
            license = pkgs.lib.licenses.mit;
          };
        };
      in
      {
        packages.default = ai-quota;
        apps.default = {
          type = "app";
          program = "${ai-quota}/bin/ai-quota";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            python
            python.pkgs.pip
            python.pkgs.pytest
            pkgs.ruff
            pkgs.gh
          ];

          shellHook = ''
            if [ ! -d .venv ]; then
              ${python}/bin/python -m venv .venv --system-site-packages
              .venv/bin/pip install --quiet -e . >/dev/null
            fi
            export PATH="$PWD/.venv/bin:$PATH"
          '';
        };
      });
}
