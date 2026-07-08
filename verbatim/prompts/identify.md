You are doing speaker attribution on an automatically-transcribed meeting.
Acoustic diarization was coarse — often everyone on the far end is lumped into a
single "Remote" label — so your job is to work out, from conversational context,
**who actually spoke each line**.

You are given the transcript as numbered lines:
`<n> [time] (currentLabel): text`

Use every cue: self-introductions, names people are addressed by ("Thanks,
Sarah"), roles, handoffs ("over to you, Mike"), topic ownership, and turn-taking.
"You" is the person recording (keep them as-is unless clearly named elsewhere).
Be consistent: the same person must get the exact same name on every line. If you
truly cannot tell, reuse the most likely current participant rather than inventing
a new one; use "Unknown Speaker" only as a last resort.

Speakers talk in runs of consecutive lines. Output the attribution as those runs
— one entry per maximal run of consecutive lines spoken by the same person. A run
boundary can fall anywhere the speaker changes, **including inside a single
"Remote" block** (that's the whole point — split merged speakers apart).

Output **ONLY** a single JSON object, no prose, no code fences:

{
  "roster": [
    {"name": "Full Name", "role": "short role/company if known", "confidence": "high|medium|low"}
  ],
  "segments": [
    {"from": 1, "to": 14, "speaker": "Full Name"},
    {"from": 15, "to": 15, "speaker": "Full Name"}
  ]
}

The segments must cover every line number from 1 to the last, in order, with no
gaps or overlaps. Every "speaker" must match a roster "name" exactly.

TRANSCRIPT:
{{NUMBERED}}
