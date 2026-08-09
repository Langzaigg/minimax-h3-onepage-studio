# Full-Reference Mode Rewrite Output Format Guide

Rewrite the user's idea as a production-ready full-reference MiniMax H3 prompt. Keep it fluent, specific, and under 7,000 characters. Write all six rewrite sections in English; preserve the original language only for dialogue and lyrics inside `<d>` and for text visibly present in the scene. Shots, camera movement, speakers, dialogue, and ordinary sound follow the Video Prompt Writing Guide (T2VA / I2VA / FL2VA / L2VA); this guide covers the reference labels and six-section format specific to full-reference mode.

**Description detail:** Make `detailed_description` as detailed and explicit as possible. For each shot, clearly establish composition, subject appearance and position, environment and lighting, actions and state changes, camera movement, current sound, and the points where referenced content appears or takes effect. Avoid reducing the description to a plot summary or a list of reference relationships.

## 1. Overall Structure

Output exactly these six sections in order, separated by blank lines:

| Section | Purpose |
| --- | --- |
| `subject_definitions` | Defines referenced content and its reference labels |
| `summary` | Task type, target video, and main reference relationships |
| `retention_analysis` | How each referenced item is preserved, transferred, copied, or reused |
| `detailed_description` | Visuals, actions, shots, sound, and dialogue in playback order |
| `overall_soundscape` | Ambient and physical sounds across the full video |
| `non_diegetic_music` | Background music audible only to the audience |

## 2. Reference Labels and Definitions (`subject_definitions`)

Four labels, numbered independently per category, keeping the same meaning across all sections:

| Label | Meaning |
| --- | --- |
| `<Subject N>` | Reusable visible content: people, animals, objects, scenes, clothing, props, styles, actions, expressions, poses |
| `<Picture N>` | A reference image used as a concrete frame, keyframe, or composition anchor |
| `<Video N>` | A video used as editing source, continuation start, or for camera/cuts/rhythm/temporal structure |
| `<Audio N>` | An audio signal that is copied or referenced (signal, music style, voice timbre, beat, continuity) |

Give every label one line stating its role and key features to follow. Give each asset a narrow, truthful role (identity / wardrobe / prop / scene / style / motion / voice / music); never use a vague "use the references".

**Subject from multiple assets:** Combine sources and state what each provides — `<Subject 1> is the woman whose appearance comes from <Picture 1> and whose walking motion comes from <Video 1>.`

**Picture as storyboard:** When an image plans shots, state which shots it maps to and what it defines — `<Picture 3> is a storyboard reference for [Shot 1] and [Shot 2], defining their viewpoint, subject placement, and shot order.`

**Video vs Subject:** If a person, object, scene, action, or effect from a reference video is reused as visible content, it belongs under `<Subject N>`; `<Video N>` identifies the asset or structural source only.

**Audio speaker ID:** When `<Audio N>` corresponds to a target speaker, reuse that speaker's global ID — write `<Subject N> (Sx)` if the speaker maps to a defined subject, or a stable voice description plus `(Sx)` otherwise. The ID comes from the target video's global speaker order and is never independently assigned in the audio definition.

**Independent numbering:** `<Video N>` and `<Audio N>` are numbered independently; indices do not encode a pairing. The same reference video may correspond to `<Video 1>` and `<Audio 2>`. An ordinary reference video does not create an `<Audio N>` merely because the file contains sound. State the shared source only when needed to remove provenance ambiguity.

## 3. `summary`

One short English paragraph beginning with a square-bracketed task-type prefix; combine types with ` + ` when several apply (do not repeat a type):

| Task type | When to use it |
| --- | --- |
| `keyframe completion` | An image serves as the target video's first frame, keyframe, last frame, or other concrete frame anchor |
| `reference generation` | An asset provides guidance (character, scene, style, action, camera, storyboard) without being a concrete frame or the source video |
| `video editing` | An existing source video is directly modified |
| `video continuation` | New content continues, extends, resumes, or transitions from an existing source video |
| `audio reuse` | The same audio signal is reused in full or in part |
| `audio reference` | Only music style, timbre, dialogue content, texture, beat, or continuity is referenced, not copied |

The mere presence of video or audio does not automatically create a corresponding task type — a reference video providing only camera movement, cuts, or rhythm normally belongs to `reference generation`. When editing a source video, also use `audio reuse` if its original audio remains audible; when continuing without copying the signal, use `audio reference` if the new audio only continues the original track's characteristics.

Use the defined labels to describe main subjects and shot flow; do not introduce new labels here. For edits, begin after the prefix with `The target video is an edited version of <Video 1>.`

## 4. `retention_analysis`

One line per label, preserving meaning from `subject_definitions`. Visible content markers:

| Marker | Meaning |
| --- | --- |
| `fully_preserved` | The defined role is fully preserved |
| `partially_preserved` | Still used, but some defined characteristics change or are only partially retained |
| `attribute_transfer` | Characteristics transfer to a different identifiable target subject |
| `weak_reference` | Only broad style/category/composition/atmosphere similarity remains |

```text
<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...
<Picture 2> ([Shot 1] first frame): fully_preserved - ...
<Video 1> (cut and pacing structure): weak_reference - ...
```

Audio markers: `fully_copy` (complete source audio as final track) · `partially_copy` (part of timeline or selected layers) · `reference` (timbre/rhythm/style followed, signal not copied) · `weak_reference`.

```text
<Audio 1>: fully_copy - <Audio 1> is reused 1:1 as the target video's complete final audio track.
<Audio 2>: reference - the target speaker follows <Audio 2>'s voice timbre and measured delivery without copying the original signal.
```

Choose markers only within the role already defined for that label. Newly added actions, backgrounds, or plot events are not losses of reference fidelity. Do not write `(Sx)` in `retention_analysis`.

## 5. `detailed_description`

Main body, shot by shot in target-video playback order, inserting reference labels at first appearance and where their roles apply.

- Style opening: establish the overall style in 1–2 English sentences **before** `[Shot 1]`: `The target video is in a cinematic, literary music-video style with soft lighting and a slightly desaturated color palette.`
- `[Shot 1]` has no timestamp; later shots use `[Shot N] At MM:SS.mmm, ...` with strictly increasing cut times within the duration.
- Length: normally **350–500 English words** for generation tasks. Dialogue-dense content prioritizes fitting the complete spoken timeline over word count. Video-editing descriptions scale with source complexity. A single shot does not automatically justify a shorter description.

### 5.1 Using Reference Labels in Shots

At the first clear appearance of an important `<Subject N>`, describe its referenced characteristics, position, and current action within what is visible. Continue the same label in later shots without redefining it. Use natural phrasing for concrete frame anchors:

```text
the shot begins from <Picture 1>
the shot's keyframe corresponds to <Picture 2>
the shot ends on <Picture 3>
```

Cite `<Video N>` naturally where its source state, structure, or continuation applies; cite `<Audio N>` in the shot or semantic phase where the audio relationship is active.

### 5.2 Speakers, Audio Sources, and Dialogue

When a referenced subject physically speaks, retain both labels: `<Subject 2> (S1) turns toward the woman and says, <d>[English] Last summer, I went to my grandfather's house. He talked about you.</d>` If the same subject speaks off-screen, keep the form and mark it as `off-screen`. When the speaker does not correspond to a defined subject, use a stable voice description followed by `(Sx)`.

When verbal content is only a cue within a directly reused BGM or complete soundtrack, and no person, character, narrator, or other independent vocal source physically produces it, use `<Audio N>` as the audible source and do not invent an additional `(Sx)`:

```text
When <Audio 1> reaches the phrase <d>[English] I'm lonely lonely lonely lonely lonely I'm lonely</d>, <Subject 1> performs the corresponding hand gesture without becoming a separate speaker source.
```

When dialogue, narration, or lyrics from reference audio are directly reused, or reperformance is explicitly requested, preserve the exact source words and original language inside `<d>`. Write `[unclear]` for unintelligible spans instead of guessing. Standardize punctuation to basic marks (`,`, `.`, `?`, `!`); remove repeated tildes, emoji, bullets, and decorative punctuation; end statements, questions, and exclamations with `.`, `?`, `!` before `</d>`. When only timbre, rhythm, emotion, or delivery is referenced, do not carry the original dialogue into the target video.

Assign `(Sx)` once according to the order of actual vocal events in the target video and reuse the ID at every vocal event. Voices physically produced by a concrete source use `(Sx)`; verbal cues existing only within a directly reused BGM or soundtrack use `<Audio N>`.

## 6. Sound Sections

- `overall_soundscape`: 1–4 English sentences; ambient + physical + non-verbal sounds (see base guide). Dialogue, singing, and shot-synchronized events remain in `detailed_description`.
- `non_diegetic_music`: 1–3 sentences on instrumentation, tempo, and dynamic development; `N/A` if none.
- When reference audio is used, state its copy or reference relationship only in the section matching the audible layer: ambience and sound effects in `overall_soundscape`, audience-only score in `non_diegetic_music`. If the same audio provides both, describe the relationship in each section.
- Write complete dialogue and lyrics only inside `<d>` in `detailed_description`; do not repeat them here.

## 7. Negatives

Add a short `negatives:` list at the end when useful: no extra subjects · no identity/face/wardrobe drift · no voice or lip-sync mismatch · no broken eyelines · no subtitles or unmotivated cuts · no hard cuts/fades in keyframe tasks · no unwanted music.

## 8. Complete Example

```text
subject_definitions:
<Subject 1> is the coffee-shop environment in <Picture 1>, featuring an exposed brick wall, an orange tufted sofa with patterned pillows, a neon sign, and a wooden coffee table.
<Subject 2> is the fluffy white Samoyed in <Picture 2>, <Picture 3>, and <Picture 4>, with thick white fur, pointed ears, a dark nose, and a curved tail.
<Subject 3> is the young blonde woman in <Video 1>, with long blonde hair and a light-pink button-down shirt with rolled-up sleeves.
<Subject 4> is the young man in <Video 2>, with short wavy brown hair and a dark-grey hoodie with drawstrings.
<Audio 1> is the voice-timbre reference for <Subject 3> (S1), containing a spoken English vocal layer.

summary:
[reference generation + audio reference] The target video shows <Subject 3> eating a cookie in <Subject 1>. <Subject 4> enters with <Subject 2>, which lunges toward the cookie. The three-shot exchange uses <Audio 1> as the voice-timbre reference for <Subject 3> and ends with a canned audience laugh.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2], [Shot 3]): fully_preserved - the exposed brick wall, orange tufted sofa, patterned pillows, neon sign, and wooden coffee table are retained.
<Subject 2> (appears in [Shot 1], [Shot 2]): fully_preserved - the Samoyed's thick white fur, pointed ears, dark nose, and curved tail are retained.
<Subject 3> (appears in [Shot 1], [Shot 2], [Shot 3]): fully_preserved - the blonde woman's identity, long hair, and light-pink shirt are retained.
<Subject 4> (appears in [Shot 1], [Shot 2]): fully_preserved - the young man's short wavy brown hair and dark-grey hoodie are retained.
<Audio 1>: reference - its vocal timbre guides the dialogue delivery of <Subject 3> without copying the original signal.

detailed_description:
The target video uses a realistic multi-camera sitcom style with warm indoor lighting.
[Shot 1] A medium shot establishes <Subject 1>, the coffee shop with its exposed brick wall, orange tufted sofa, patterned pillows, neon sign, and wooden coffee table. <Subject 3> (S1), the young woman with long blonde hair and a light-pink button-down shirt with rolled-up sleeves, sits on the sofa holding a chocolate-chip cookie. From the left, <Subject 4>, the young man with short wavy brown hair and a dark-grey hoodie with drawstrings, enters holding the leash of <Subject 2>, the thick-furred white Samoyed with pointed ears, a dark nose, and a curved tail. The dog lunges toward the cookie and pulls the leash taut. <Subject 3> (S1) jerks her hand back and, using the clear youthful voice timbre referenced from <Audio 1>, exclaims with light annoyance, <d>[English] Hey! Watch your dog!</d> She closes her lips and guards the cookie while <Subject 4> pulls the dog back.
[Shot 2] At 00:03.000, the shot cuts to a close-up of <Subject 4> (S2), the young man in the dark-grey hoodie from Shot 1, sitting beside <Subject 3> on the sofa and holding <Subject 2> securely in his arms. <Subject 4> (S2) says in a casual young male voice with a playful tone and an easy conversational pace, <d>[English] He just likes cookies more than me.</d> He closes his mouth into an apologetic smile and strokes the dog's thick white fur.
[Shot 3] At 00:05.000, the shot cuts to a close-up of <Subject 3> (S1), the blonde woman in the light-pink shirt from Shot 1. Her annoyance softens as she looks toward the Samoyed. <Subject 3> (S1) replies in the same clear youthful voice referenced from <Audio 1> with an amused cadence, <d>[English] Well, he has good taste at least.</d> She smiles and raises the cookie in a small toast-like gesture. A classic canned audience laugh begins immediately after the line and continues through the final frame.

overall_soundscape:
Soft indoor coffee-shop room tone continues throughout the scene.

non_diegetic_music:
N/A
```
