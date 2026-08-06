# v16 overnight render -- detached from any Claude session (2026-08-05 close).
# Safe to re-run: skips if the output already exists.
$rev = "C:\DropCat-Studio\review\assets"
$out = "$rev\chain60_v16.mp4"
if (Test-Path $out) { exit 0 }
Set-Location C:\DropCat-Studio\engine
$pA = "A single subject, centered, facing the camera, mouth opening and closing in precise sync with the singing vocals. Behind him, a man in denim overalls stands calmly at the crates, still and relaxed, slowly nodding along with the music. Stay in the original scene and setting, soft cinematic lighting, shallow depth of field, steady framing."
$pB = "A single subject, centered, facing the camera, mouth opening and closing in precise sync with the singing vocals, standing in a golden wheat field, wheat stalks in the foreground, soft afternoon light, steady framing."
$prompts = "$pA||$pB"
python chain.py --image "C:\DropCat-Studio\uploads\awm_alien_source.png" --song "$rev\adam60_dense.wav" --output $out --worker http://127.0.0.1:7899 --seeds-per-clip 4 --frames-per-clip 241 --crossfade 0.15 --min-clip-frames 169 --smart-seams --judge-select --images "C:\DropCat-Studio\uploads\awm_alien_source.png,$rev\sceneB_field.png" --scene-prompts $prompts *> "C:\DropCat-Studio\review\v16_render_log.txt"
