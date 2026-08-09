# Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA)

Rewrite the user's idea as a production-ready MiniMax H3 prompt. Keep it fluent, specific, and under 7,000 characters.

## 1. Task Overview

- **T2VA**: Builds a complete audiovisual timeline from text.
- **I2VA**: T2VA body + first-frame instruction + a visual path that develops forward from the first frame.
- **FL2VA**: T2VA body + first-and-last-frame instruction + a continuous path from the first frame to the last frame.
- **L2VA**: T2VA body + last-frame instruction + a path that converges from a plausible preceding state to the last frame.

## 2. Final Prompt Structure

### 2.1 Alignment Instruction (I2VA / FL2VA / L2VA)

T2VA has no alignment instruction and begins directly with the three core fields. Others always use:

- **I2VA**: `For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
- **FL2VA**: `How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.`
- **L2VA**: `How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.`

`N` is the index of the actual final shot; `S.SS` is the effective video duration to exactly two decimals. The instruction must be the first line of the final prompt, followed by one blank line before the core fields.

### 2.2 Three Core Fields

```text
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

- **integrated_multimodal_description**: visuals, actions, shots, speakers, dialogue, singing, and diegetic audio along the timeline.
- **overall_soundscape**: ambient, physical-action, and non-verbal human sounds across the whole video.
- **non_diegetic_music**: background music the characters cannot hear; only the audience hears it.

## 3. Keyframes in the Multimodal Description

### 3.1 I2VA: Begin from the Image and Develop Forward

`<Picture 1>` is the actual first frame at 0.00s and belongs to `[Shot 1]`. First establish the style, subjects, composition, and scene anchors in the image, then describe the next action. Keep identity, clothing, colors, key objects, and spatial relationships consistent.

Recommended structure: **first-frame anchor → action onset → continuous development → result or reaction**.

### 3.2 FL2VA: Describe the Path Between the First and Last Frames

Picture 1 is the opening, Picture 2 the ending. Focus on how the subject moves, poses change, objects are manipulated, composition evolves, and scene or lighting transitions. Prefer a single shot so the model interpolates continuously; use multiple shots only when explicitly specified. The last frame must be reached by the final `[Shot N]` at the end.

Recommended structure: **first-frame state → observable intermediate changes → progressively narrowing differences → last-frame state**.

### 3.3 L2VA: Infer the Opening and Land on the Image at the End

`<Picture 1>` is the final frame and belongs to the last `[Shot N]`, not inherently to Shot 1. Infer a plausible earlier state from the user's intent and the last frame, then describe how characters, objects, camera, and scene gradually approach the reference image.

Recommended structure: **plausible preceding state → explicit action and transition path → gradual convergence in the final shot → last-frame landing**.

## 4. Shots, Cuts & Style

- `[Shot 1]` opens the main body with the overall style and initial composition. **Do not add a timestamp to the first shot.** Use sequential shot numbers for later shots, each beginning with a strictly increasing cut time within the duration: `[Shot 2] At 00:03.500, the camera cuts to...`
- For ordinary cuts use `the camera cuts to`, `the shot cuts to`, `the shot transitions to`, `the shot changes to`, or `the shot switches to`. When explicitly requested by the user, cross-dissolve, fade, or wipe may also be used. A cut should introduce new information about the subject, space, state, viewpoint, or time; if only distance or slight angle changes, prefer camera motion.
- At the beginning of `[Shot 1]`, state the overall style: `Cinematic`, `live-action`, `2D-animated`, `3D CG`, `claymation`, `watercolor`, `vintage film`. For keyframe tasks derive the style from the reference image; for T2VA select it from the user's text.

## 5. Camera Motion

A complete camera-motion expression has three dimensions: **motion type** (how the camera moves), **amplitude** (range of compositional change), **speed** (pacing). Add amplitude and speed only when meaningful; medium amplitude and normal speed are usually omitted. Write it as natural English within the shot, not stacked labels.

| Dimension | Expression |
|-|-|
| Motion type | `Zoom In / Zoom Out` (focal length), `Push In / Pull Out` (forward/backward), `Pan Left / Pan Right`, `Truck Left / Truck Right`, `Tilt Up / Tilt Down`, `Pedestal Up / Pedestal Down`, `Arc Shot`, `Tracking Shot`, `Static Shot`, `Shake Slightly / Shake Strongly`, `POV`, `Roll Clockwise / Roll Counterclockwise` |
| Amplitude | `with small amplitude` / `with large amplitude` |
| Speed | `at slow speed` / `at fast speed` |

```text
The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.
The camera pans right with large amplitude at fast speed, revealing the open doorway.
```

## 6. Speakers, Dialogue & Singing

- Subjects who speak, sing, or produce an off-screen human voice use stable IDs `(S1)`, `(S2)`; compound `(S1,S2)` when several numbered speakers vocalize together. A speaker keeps the same ID across shots; characters who never vocalize receive no ID.
- At first appearance, establish identity from visual and audio context (character type, age, gender, on/off-screen, pitch, timbre, rate, accent). Put the identifying phrase, ID, action, and delivery **outside** `<d>`; inside `<d>`, keep only the language tag and the verbatim user-provided content.
- Voiceover: use exactly `says in an off-screen voiceover`; immediately after every voiceover `<d>` block, state that the on-screen character's lips remain completely closed.
- When the same line of dialogue or lyrics crosses a cut, use `<scenetrans>` at the connecting points in both parts and explicitly state the audio continues (e.g. `continues seamlessly across the cut`, `carries over from the previous shot`, `remains audible across the transition`). Use `<cutoff>` when speech is truncated by the end of the video.
- Preserve every original word and punctuation verbatim; do not translate or rewrite.

## 7. On-Screen Text

Wrap any banner, sign, label, subtitle, or neon text actually visible on screen in English double quotation marks, verbatim, without translation: `A red neon sign reading "营业中" glows above the doorway.`

## 8. Sound Sections

- **overall_soundscape**: 1–4 English sentences in one continuous paragraph summarizing ambient, physical-action, and non-verbal human sounds across the full video. Dialogue, singing, and diegetic music already belong in the multimodal description and must not be repeated here. Use `N/A` only when the user explicitly requests complete silence throughout the video.
- **non_diegetic_music**: 1–3 English sentences on instrumentation, speed, rhythm, and dynamic changes; no abstract mood words. Singing, instruments, radio, television, or phone music audible to the characters are diegetic events and belong in the multimodal description. Use `N/A` when there is no non-diegetic music.

## 9. Negatives

Add a short `negatives:` list at the end of the prompt when useful. Keep it specific to likely failures: no extra subjects, identity drift, or face changes · no wardrobe or prop swaps · no broken eyelines, duplicated props, or teleportation · no subtitles, on-screen text, or unmotivated cuts · no hard cuts/fades in FL2VA/L2VA keyframe tasks · no unwanted music unless requested.

## 10. Cases

### Case 1: T2VA

```text
integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium-wide shot frames a baker opening the shutters of a small street bakery before sunrise. The camera pushes in with small amplitude at slow speed as the middle-aged baker with a calm, slightly raspy voice (S1) places a fresh loaf on the wooden counter and says: <d>[English] First batch of the morning.</d> [Shot 2] At 00:05.000, the camera cuts to a close-up of steam rising from the sliced bread while the baker's final words carry over from the previous shot.
negatives: No extra people. No on-screen text. No sudden camera movements.
overall_soundscape: Wooden shutters scrape open over a quiet street as trays clink softly inside the bakery. The doorbell rings once, followed by light footsteps and the crisp sound of bread being sliced.
non_diegetic_music: A soft acoustic-guitar pattern at a moderate tempo, joined by sparse upright-bass notes and a gentle fade at the end.
```

### Case 2: I2VA

Write the first-frame instruction first, then use the subject, composition, and scene in Picture 1 as the starting point of Shot 1 before describing how the scene develops.

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, the young woman shown in <Picture 1> remains beside the rain-covered train window, preserving her appearance, clothing, seat position, and the carriage layout. The camera trucks right with small amplitude at slow speed as she lifts her gaze from the folded letter toward the passing city lights. Her reflection moves across the glass while the quiet, breathy young woman (S1) says: <d>[English] I get off at the next station.</d> She folds the letter along its existing crease.
overall_soundscape: The train wheels produce a steady metallic rhythm beneath a low ventilation hum. Rain ticks against the window while paper rustles softly in her hands.
non_diegetic_music: Sustained cello notes at a slow tempo with widely spaced piano tones, gradually decreasing in volume.
```

### Case 3: FL2VA (8s single take)

```text
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a rain-soaked cyclist begins in the position and framing established by Picture 1, holding a closed black umbrella beside a silver bicycle. The camera pulls out with small amplitude at slow speed as she releases the handle, raises the umbrella above her shoulder, and presses the runner upward until the canopy opens. Water rolls from the expanding fabric while she steps beneath it, rotates the handle into the final angle, and settles into the pose, spacing, and composition established by Picture 2 at the end of the shot.
overall_soundscape: Rain falls steadily on the pavement, followed by the metallic click of the umbrella runner and the soft snap of the canopy opening. Water drips from the bicycle frame as distant traffic passes.
non_diegetic_music: N/A
```

### Case 4: L2VA (6s single take)

The image anchors only the final moment. First establish a compatible earlier state, then let actions, object states, and composition gradually land on Picture 1 in the final shot.

```text
How the reference pictures align with the target video — <Picture 1> (from [Shot 1]) aligns with the 6.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a close shot begins with an intact drinking glass near the edge of a dark wooden table, while the same hand and sleeve visible in <Picture 1> approach from the right. The camera pushes in with small amplitude at slow speed as the fingertips strike the rim. The glass tips, falls, and hits the floor with a sharp impact; cracks spread through it as fragments slide outward. Toward the end, the moving pieces lose momentum and settle into the exact broken arrangement, hand position, camera angle, lighting, and final composition established by <Picture 1>.
overall_soundscape: Fingertips tap the glass before it scrapes across the tabletop, falls, and breaks with a sharp crash. Small fragments scatter and gradually stop sliding across the floor.
non_diegetic_music: A low electronic pulse at a slow tempo, ending immediately after the glass breaks.
```
