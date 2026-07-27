# Image Observation and Multimodal Generation

**Status:** Draft proposal  
**Target:** Kimiya 1.x experimental language surface  
**Branch:** `feature/image-observation`

## Motivation

Kimiya currently distinguishes deterministic computation, model judgment,
world observation, and world effects. Files enter through `observe file(...)`;
display pixels enter through `observe screen(...)`. Programs that reason about
photographs, diagrams, scans, or other image files need the same explicit
provenance without pretending that an image is inherently a GUI screenshot or
reading it invisibly through a Python extension.

This proposal adds:

1. an `image` observation surface for content-addressed image evidence; and
2. an explicit `images` argument on `gen` for multimodal model calls.

The feature must preserve interpreter/compiled parity, announce remote pixel
egress before execution, and abstain rather than silently omit unreadable
images.

## Proposed syntax

Observe one image:

```kimiya
photo := observe image("DSC_1001.ARW")
check photo.exists
```

Send observed images to a generator:

```kimiya
schema Assessment:
    text: text
    confidence: num

a := gen<Assessment>("Assess focus, composition, expression, and distractions.", images=[photo]) by A
```

Compare a group:

```kimiya
choice := gen<Recommendation>("Choose exactly two photographs and explain the differences.", images=[first_photo, second_photo, third_photo]) by A
```

Text-only `gen` remains unchanged:

```kimiya
summary := gen<Summary>("Summarize these notes.") by A
```

## Image observation record

`observe image(path)` returns a closed record:

```text
{
    kind:          "image",
    exists:        bool,
    path:          text,
    sha:           text,
    width:         num,
    height:        num,
    format:        text,
    mime:          text,
    preview_path:  text
}
```

Semantics:

- `path` is the requested source path.
- `sha` addresses the original source bytes, not the generated preview.
- `width` and `height` describe the oriented full image when available.
- `format` is a short source-format label such as `JPEG`, `PNG`, `HEIC`,
  `ARW`, or `CR3`.
- `mime` describes the source when known.
- `preview_path` names a runtime-owned, immutable JPEG or PNG representation
  suitable for model transport.
- A failed observation returns `exists: false` and empty/zero metadata.
- An unreadable image never becomes an empty successful observation.

Programs should gate semantic use:

```kimiya
check photo.exists
```

Passing an image with `exists: false` to `gen` causes abstention.

## Source and preview provenance

The original image is evidence. The preview is a derived transport
representation.

The trace records:

- source path;
- source SHA-256;
- preview SHA-256;
- source and preview dimensions;
- source format;
- decoder identity and version;
- whether an embedded preview or decoded pixels were used.

The certificate records each image source SHA that reached a verdict-relevant
model call, plus its decoder provenance. It does not embed image bytes.

Preview generation must:

- apply orientation;
- preserve aspect ratio;
- avoid upscaling;
- enforce configured pixel and byte limits;
- use a deterministic encoder configuration where practical;
- write only inside the run artifact directory;
- never alter the source image.

## RAW decoding

RAW support is modular because camera formats and native dependencies evolve
independently from the language.

The initial decoder interface should provide:

```python
decode_image(path, artifact_directory, limits) -> ImageObservation
```

Decoder preference:

1. embedded JPEG preview when sufficiently large;
2. half-size RAW decode;
3. full decode only when within configured limits.

The default implementation may use LibRaw through `rawpy`. Basic JPEG and PNG
support should remain available through a lightweight decoder. Kimiya must
announce the selected decoder and include its implementation/version in the
trace.

Missing optional RAW support produces `exists: false` with a visible reason;
it must not reinterpret arbitrary bytes or silently fall back to a thumbnail
from an unrelated sidecar.

## Static typing

`observe image(...)` has the closed `ImageObservation` record type. Field
typos are errors:

```kimiya
check photo.exist       -- type error: field is `exists`
```

The `images` argument has type `list<ImageObservation>`.

Static errors include:

- passing text paths directly instead of observed images;
- passing screen observations as file images;
- passing arbitrary Python values as images without an observation wrapper;
- duplicate `images` arguments;
- supplying `images` to a construct that does not accept them.

An empty image list is valid and equivalent to a text-only call, though the
compiler may normalize it away.

## Discipline and trust rules

The image surface follows the observation discipline:

1. Image bytes may enter semantic computation only through an image
   observation or another explicitly declared visual observation surface.
2. A model call may receive only successful observed-image records.
3. Image paths supplied by a Python extension are not automatically trusted as
   observations.
4. Retry snapshots restore program state, not files or decoder caches.
5. Preview generation is a derived observation artifact, not a world effect.
6. Source images remain read-only.

The checker should reject a multimodal call whose image value is statically
known not to be an image observation. Runtime guards cover dynamically typed
Python-extension results.

## Agent capabilities

Every agent has an inferred or declared vision capability.

```kimiya
agent A:
    backend = "ollama"
    model   = "llava:13b"
    vision  = true
```

If `vision` is omitted, existing model-name inference applies. Explicit
`vision = true` remains the override for newly released models.

Sending images to an agent without vision capability causes abstention before
network access.

## Egress

Text prompt egress and pixel egress are related but distinct disclosures.

All-local example:

```text
network egress: none (all agents local)
image egress: none (all vision agents local)
```

Remote example:

```text
⚠ network egress: this program sends prompts to remote agents —
    A → google/gemini-2.5-flash @ openrouter.ai (openrouter)
  ⚠ image egress: observed image pixels may leave this machine —
    A receives up to 3 images per call
```

The announcement happens before the first model call. The certificate records
the actual remote hosts and source SHAs disclosed during execution.

A declared remote vision agent that never receives an image remains declared
prompt egress but does not become actual image egress in the certificate.

## Backend transport

The existing oracle `images` parameter is the common transport boundary.
Backends translate runtime-owned preview paths as follows:

- **Ollama:** request `images` using the backend-supported base64 payload.
- **OpenAI-compatible/vLLM:** content parts containing text and `image_url`
  data URLs, subject to endpoint capability.
- **OpenRouter:** OpenAI-compatible multimodal content parts.
- **Anthropic:** base64 image source blocks with supported MIME types.
- **Claude CLI:** allow the CLI to read runtime-owned preview paths using its
  existing read capability.

Backends must not accept arbitrary caller paths at this layer. They receive
validated observation records or normalized preview descriptors from the
runtime.

## Limits

Safe defaults are required:

```text
maximum images per model call: 8
maximum source files observed per run: 1,000
maximum preview long edge: 2,048 px
maximum encoded preview size: 8 MiB
maximum total preview bytes per call: 32 MiB
```

Limits should be configurable by environment or runtime policy, not untyped
program parameters. Resolved limits belong in the certificate.

Exceeding a limit causes visible abstention before network transmission.

## Freshness

An image observation is fresh for the source SHA recorded when observed.

Before a model call, the runtime verifies that:

- the source still exists;
- the source SHA still matches; and
- the derived preview still matches its recorded SHA.

A mismatch causes abstention and a trace record. The runtime must not silently
regenerate a changed image under the old observation identity.

Exact-SHA preview reuse is allowed and free. Cache provenance is traceable.

## Trace and certificate

Example trace records:

```json
{"kind":"observe_image","path":"DSC_1001.ARW","sha":"…","preview_sha":"…",
 "width":8192,"height":5464,"format":"ARW","decoder":"rawpy/LibRaw"}
{"kind":"gen","agent":"A=llava:13b@local","images":["…"],"image_count":1}
```

Certificate additions:

```json
{
  "image_observations": [
    {
      "path": "DSC_1001.ARW",
      "sha": "...",
      "preview_sha": "...",
      "decoder": "rawpy/LibRaw"
    }
  ],
  "image_egress": []
}
```

Remote image egress entries include agent, host, source SHA and preview SHA.
Paths may be represented relative to the working directory to reduce
unnecessary disclosure in shared certificates.

## Failure semantics

These conditions abstain visibly rather than throwing an uncaught exception:

- missing or unreadable source;
- unavailable RAW decoder;
- unsupported image format;
- failed preview generation;
- source/preview freshness mismatch;
- non-vision agent receiving images;
- image count or byte limit exceeded;
- backend rejects multimodal input;
- remote response is invalid.

Programmer errors detected statically remain checker/compiler errors.

Unexpected implementation defects remain runtime errors; they must not be
reported as certified abstentions.

## Compiler contract

The compiler must emit:

- image observation calls with the same record shape as the interpreter;
- freshness validation before multimodal calls;
- normalized image lists;
- the same egress announcements;
- the same trace and certificate fields;
- the same abstention reasons and effect behavior.

Compiled artifacts retain the Kimiya version compatibility check. Adding this
surface in a minor pre-2.0 release is permitted but must be recorded in the
changelog.

## Deterministic testing

`KIMIYA_MOCK=1` must support image observations without decoding private user
files. Tests use checked-in image fixtures.

The mock oracle should record received preview SHAs and produce deterministic
structured output. A fixture mechanism is desirable for constrained outputs:

```text
KIMIYA_MOCK_FIXTURE=tests/fixtures/multimodal_responses.json
```

Required test matrix:

1. static rejection of raw paths in `images`;
2. static rejection of non-image observations;
3. missing-image abstention;
4. unsupported-format abstention;
5. non-vision-agent abstention before egress;
6. local multimodal successful commit;
7. declared remote image-egress banner;
8. compiled/interpreted report equality;
9. source freshness mismatch;
10. preview freshness mismatch;
11. image-count and byte-limit enforcement;
12. irreversible effect remains gated after multimodal judgment.

Every successful example must pass:

```text
check
compile
generated Python syntax check
interpreted mock run
compiled mock run
output comparison
```

## Minimal example

```kimiya
agent A:
    backend = "ollama"
    model   = "llava:13b"
    vision  = true

agent B:
    backend = "ollama"
    model   = "gemma3:12b"
    vision  = true

agent C:
    backend = "openrouter"
    model   = "google/gemini-2.5-flash"
    key_env = "OPENROUTER_API_KEY"
    vision  = true

context k_photo:
    domain     = "whether a photographic assessment is supported by supplied images"
    preserve   = [image_identity, visible_evidence, uncertainty]
    allow_loss = [wording]

schema Assessment:
    text: text

p := observe image("photo.jpg")
check p.exists

a := gen<Assessment>("Describe only visible strengths and weaknesses of this photograph.", images=[p]) by A

if judge<5,4/5> (p ~ a.text) under k_photo panel [B, C]:
    commit(a)
else:
    abstain
```

The exact judgment relation for image-grounded claims needs a separate
decision. Reusing `shows(image, claim)` may be preferable to overloading
similarity (`~`) in the final example.

## Open questions

1. Should multimodal generation use `images=[...]` inside `gen`, or should
   images become an explicit prompt-part expression?
2. Should `shows(image, claim)` accept file-image observations in addition to
   screen observations?
3. Should image observation initially support only common bitmap formats,
   leaving RAW entirely to an optional decoder?
4. Should certificates record source paths by default, or only source SHAs and
   user-selected labels?
5. Is preview creation part of the core runtime or a formally registered
   observation provider?
6. Should agent capability failure be a static error when the agent declaration
   is known, rather than runtime abstention?

These questions should be resolved before parser implementation.
