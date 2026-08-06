# Full-Reference Mode Rewrite Output Format Guide

Rewrite the user's idea as a production-ready full-reference MiniMax H3 prompt. Keep it fluent, specific, and under 7,000 characters. Shots, camera movement, speakers, dialogue, and ordinary sound follow the Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA); this guide covers the reference labels and six-section format specific to full-reference mode.

## 1. Output Structure

Output exactly these six sections in order, separated by blank lines:

| Section | Purpose |
| --- | --- |
| `subject_definitions` | Defines referenced content and its labels |
| `summary` | One short paragraph: task type + target video + main reference relationships |
| `retention_analysis` | Per label: how content is preserved, transferred, copied, or referenced |
| `detailed_description` | Shot-by-shot visuals, actions, sound, and dialogue in playback order |
| `overall_soundscape` | Ambient + physical-action + non-verbal human sounds |
| `non_diegetic_music` | Background music audible only to the audience (`N/A` if none) |

## 2. Labels (`subject_definitions`)

Four labels, numbered independently per category, keeping the same meaning across all sections:

- `<Subject N>` — reusable visible content (person, object, scene, clothing, style, action). One subject may come from several assets; one asset may provide several subjects.
- `<Picture N>` — an image used as a concrete frame/keyframe/composition anchor. If an image only defines a character or scene, cite it inside the relevant `<Subject N>` instead of creating a picture entry.
- `<Video N>` — a video used as editing source, continuation start, or for camera/cuts/rhythm/temporal structure.
- `<Audio N>` — audio copied or referenced (signal, music style, voice timbre, beat, continuity).

Give every label one line stating its role and the key features to follow. Give each reference asset a narrow, truthful role (identity / wardrobe / prop / scene / style / motion / voice / music). Never use a vague "use the references" in place of a proper role map.

```text
<Subject 1> is the young woman in <Picture 1>, with long dark hair, a blue cardigan, and a thin silver necklace.
<Picture 2> is the first frame of [Shot 1], showing a woman seated beside a café window.
<Video 1> is the source video for the target video edit.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).
```

## 3. `summary`

One short English paragraph. Begin with a square-bracketed task-type prefix; combine types with ` + ` when several apply (do not repeat a type):

`keyframe completion` (image is a concrete frame anchor) · `reference generation` (guidance only) · `video editing` (a source video is directly modified) · `video continuation` (new content continues a source video) · `audio reuse` (signal copied) · `audio reference` (style/timbre/beat referenced, not copied)

Use the defined labels to describe main subjects and shot flow. Do not introduce new labels here. For edits, start with `The target video is an edited version of <Video 1>.`

## 4. `retention_analysis`

One line per label, preserving meaning from `subject_definitions`. Visible content markers:

- `fully_preserved` · `partially_preserved` · `attribute_transfer` (transferred to another target subject) · `weak_reference` (broad style/category similarity)

```text
<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...
<Picture 2> ([Shot 1] first frame): fully_preserved - ...
<Video 1> (cut and pacing structure): weak_reference - ...
```

Audio markers: `fully_copy` (1:1 final track) · `partially_copy` (part of timeline or layers) · `reference` (timbre/rhythm/style followed, signal not copied) · `weak_reference`

```text
<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track.
<Audio 2>: reference - the target speaker follows <Audio 2>'s voice timbre and measured delivery without copying the original signal.
```

Choose markers only within the role already defined for that label. New actions/background added to the target video are not reference-fidelity losses.

## 5. `detailed_description`

Main body, shot by shot in playback order, inserting reference labels at first appearance and where their roles apply.

- Style opening: establish the overall style in 1–2 English sentences **before** `[Shot 1]`:
  `The target video is in a cinematic, literary music-video style with soft lighting and a slightly desaturated color palette.`
- `[Shot 1]` has no timestamp; later shots use `[Shot N] At MM:SS.mmm, ...` with strictly increasing cut times within the duration.
- Make it as detailed and explicit as possible: composition, subject appearance and position, environment and lighting, actions and state changes, camera movement, current sound, and the exact points where referenced content appears or takes effect. Avoid plot summaries.
- Cite `<Audio N>` in the relevant shot/phase and state whether the signal is copied or referenced.

## 6. Sound Sections

- `overall_soundscape`: 1–4 English sentences; ambient + physical + non-verbal sounds (see base guide). Do not repeat dialogue.
- `non_diegetic_music`: 1–3 sentences on instrumentation, tempo, rhythm, dynamics; `N/A` if none.

## 7. Negatives

Add a short `negatives:` list at the end when useful: no extra subjects · no identity/face/wardrobe drift · no voice or lip-sync mismatch · no broken eyelines · no subtitles or unmotivated cuts · no hard cuts/fades in keyframe tasks · no unwanted music.

## 8. Example (abridged)

```text
subject_definitions: <Subject 1> is the bartender in <Picture 1> — mid-forties, grey-streaked hair, white shirt, checked apron; <Subject 2> is the regular in <Picture 2> — younger, worn leather jacket, tired eyes; <Picture 1> and <Picture 2> define the two characters' faces, hair, and wardrobe only; ignore their backgrounds.

summary: [reference generation] A dimly lit jazz bar at closing time. The bartender wants to close up; the regular wants one more drink. The scene moves from polite tension to reluctant warmth.

retention_analysis: <Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - same identity, wardrobe, and posture. <Subject 2> (appears in [Shot 2], [Shot 3]): fully_preserved - same identity and wardrobe. <Picture 1>/<Picture 2> backgrounds: weak_reference - ignored.

detailed_description: The target video is in a cinematic, moody style with warm amber pendant lights and deep shadows. [Shot 1] Medium two-shot from behind the bar showing both characters; the camera holds static; <Subject 1> wipes a glass and delivers his line without looking up: <d>[English] We closed ten minutes ago, you know that.</d> [Shot 2] At 00:05.000, the camera cuts to a medium close-up of <Subject 2>, frame right; he leans in slightly and replies with a crooked smile: <d>[English] One more. For the road.</d> [Shot 3] At 00:10.000, the camera cuts back to the two-shot; <Subject 1> pauses, sighs, reaches for a bottle and pours; both share a beat of comfortable silence.
negatives: No extra people. No face drift. No wardrobe changes. No broken eyelines. No subtitles. No unmotivated cuts.
overall_soundscape: Quiet jazz-bar ambience: distant muffled traffic, ice settling in a glass, cloth on glassware; a bottle clink and liquid pour at the resolution.
non_diegetic_music: Sparse upright-bass notes at a slow tempo with soft brushwork, fading out at the end.
```
