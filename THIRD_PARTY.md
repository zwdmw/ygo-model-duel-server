# Third-party sources and runtime assets

The repository combines application code with separately attributed dependencies.
Existing copyright and license notices remain applicable to their respective files.
Original application contributions by zwdmw use the root MIT `LICENSE`.

| Component | Source / notice |
|---|---|
| Model and environment foundation | `room/app-305m-admin-20260915/project/LICENSE` (Hastur's ygo-agent, MIT; envpool-derived material, Apache-2.0) |
| Vendored protocol utilities | `room/outbound/vendor/README.md` and `sources.json` record the snapshot and file identities |
| Evolution engine | <https://github.com/diangogav/EDOpro-server-ts>, package declares ISC; pinned commit and evolution-types submodule in `deploy/runtime-assets.lock.json` |
| Local Evolution changes | `deploy/evolution-patch/`; compiled JavaScript at the corresponding upstream paths, plus spectator/replay helpers |
| Node dependencies and native core | The runtime's `package.json` and `pnpm-lock.yaml` preserve the exact packages; their own licenses apply |
| Lucide icons | <https://github.com/lucide-icons/lucide>, ISC; bundled file preserves its notice |
| Card databases and Lua scripts | Runtime manifests retain upstream ProjectIgnis, Fluorohydride, Moecube and Evolution asset sources; these remain third-party game data |

The tournament Release contains a fixed engine/resource snapshot, with individual
SHA-256 values in `ygo-tournament-engine-files-20261114.json`. It omits credentials,
node_modules, generated user databases and player games. Source maps preserve the
upstream engine code alongside the compiled runtime. Python and Node dependencies
are installed independently from their package registries.

The 2026-09-19 model runtime Release contains the original CPU environment and card
assets. Model weights and private training/evaluation artifacts are not distributed.
Reproducing deployment does not reproduce the original trained policy or its score.

No blanket license is applied to third-party materials.
