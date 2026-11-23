set shell := ["bash", "-euo", "pipefail", "-c"]

# Open the dev zellij session in a new maximized Ghostty window and return.
start-zellij:
    setsid -f ghostty --maximize=true -e zellij --config "{{justfile_directory()}}/dev-config.kdl" --layout "{{justfile_directory()}}/dev.kdl" >/dev/null 2>&1

# Usage: just profile-ninja [build-dir]   (default: build/release)
# Convert the most recent Ninja build log to a Chrome/Perfetto trace.
profile-ninja build_dir="build/release":
    ninjatracing {{build_dir}}/.ninja_log > {{build_dir}}/ninja-trace.json
    @echo "Wrote {{build_dir}}/ninja-trace.json - open it at https://ui.perfetto.dev"

