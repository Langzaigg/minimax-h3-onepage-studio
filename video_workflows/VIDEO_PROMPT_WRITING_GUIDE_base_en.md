# Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA)

Rewrite the user's idea as a production-ready MiniMax H3 prompt. Keep it fluent, specific, and under 7,000 characters.

## 1. Output Structure

Always output exactly these three fields, in order, separated by blank lines:

```text
integrated_multimodal_description: <main body>

overall_soundscape: <1-4 sentences>

non_diegetic_music: <1-3 sentences or N/A>
```

- **integrated_multimodal_description**: visual timeline — style, shots, subjects, actions, camera, dialogue, singing, and diegetic sound, in playback order.
- **overall_soundscape**: ambient + physical-action + non-verbal human sounds (wind, footsteps, impacts, breathing, panting). Do not repeat dialogue/singing.
- **non_diegetic_music**: background music only the audience hears. Focus on instrumentation, tempo, rhythm, dynamics; no abstract mood words. `N/A` if none.

## 2. Frame Alignment (I2VA / FL2VA / L2VA)

Begin with the alignment line, then one blank line before the fields.

- **I2VA**: `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
- **FL2VA**: `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.`
- **L2VA**: `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.`

`N` = final shot index; `S.SS` = effective duration to two decimals. T2VA has no alignment line.

## 3. Shots & Timeline

- `[Shot 1]` opens the main body with the overall style and initial composition (e.g. `[Shot 1] Live-action, cinematic, a medium-wide shot frames...`).
- Later shots start with strictly increasing cut times within the duration: `[Shot 2] At 00:03.500, the camera cuts to...`
- Keep frames aligned: I2VA starts from the image and develops forward; FL2VA describes one continuous causal path from Picture 1 to Picture 2 (prefer a single long take, no hard cuts); L2VA infers a plausible earlier state and lands on the image at the end.
- Identity, wardrobe, props, and spatial layout must stay consistent across shots.

## 4. Camera Language

Embed camera moves as natural actions with subjects — never stack tags like "Zoom in, 4k, cinematic".

`Motion type + amplitude + speed`, e.g.:

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
```

Approved verbs: Zoom In/Out (focal length), Push In/Pull Out (dolly), Pan L/R (pivot), Truck L/R (translate), Tilt Up/Down (pivot), Pedestal Up/Down (elevation), Arc Shot (orbital), Tracking Shot (follow), Static Shot, Shake Slightly/Strongly, POV, Roll. Omit amplitude/speed when neutral.

## 5. Speakers & Dialogue

- Stable IDs `(S1)`, `(S2)`; compound `(S1,S2)` for simultaneous speakers.
- First mention: identity, on/off-screen, pitch, rate, accent. Inside `<d>` keep only `[Language]` + exact user words, verbatim:
  `The young woman with a quiet, breathy voice (S1) says: <d>[English] I get off at the next station.</d>`
- Voiceover: use exactly `says in an off-screen voiceover` and immediately add `while his/her lips remain completely closed`.
- Preserve user dialogue and visible text verbatim; do not translate.

## 6. On-Screen Text

Wrap any visible text (signs, titles, UI) in English double quotes, verbatim: `A red neon sign reading "OPEN" glows in the dark.`

## 7. Negatives

Add a short `negatives:` list at the end of the prompt when useful. Keep it specific to likely failures:
- No extra subjects, identity drift, or face changes
- No wardrobe or prop swaps
- No broken eyelines, duplicated props, or teleportation
- No subtitles, on-screen text, or unmotivated cuts
- No hard cuts/fades in FL2VA/L2VA keyframe tasks
- No unwanted music unless requested

## 8. Examples

### Example 1 — T2VA (5s)

```text
integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise. The camera pushes in with small amplitude at slow speed as the middle-aged baker with a calm, slightly raspy voice (S1) places a fresh loaf on the wooden counter and says: <d>[English] First batch of the morning.</d> [Shot 2] At 00:05.000, the camera cuts to a close-up of steam rising from the sliced bread while the baker's final words carry over from the previous shot.
negatives: No extra people. No on-screen text. No sudden camera movements.
overall_soundscape: Wooden shutters scrape open over a quiet street; trays clink softly inside the bakery; the doorbell rings once, followed by light footsteps and the crisp sound of bread being sliced.
non_diegetic_music: A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end.
```

### Example 2 — FL2VA (8s single take)

```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a rain-soaked cyclist begins in the position and framing established by Picture 1, holding a closed black umbrella beside a silver bicycle. The camera pulls out with small amplitude at slow speed as she releases the handle, raises the umbrella above her shoulder, and presses the runner upward until the canopy opens. Water rolls from the expanding fabric while she steps beneath it, rotates the handle into the final angle, and settles into the pose, spacing, and composition established by Picture 2 at the end of the shot.
negatives: No hard cut. No fade or cross-dissolve. No teleportation. No identity or wardrobe change. No duplicate props.
overall_soundscape: Rain falls steadily on the pavement, followed by the metallic click of the umbrella runner and the soft snap of the canopy opening; water drips from the bicycle frame as distant traffic passes.
non_diegetic_music: N/A
```
